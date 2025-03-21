#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import os
import random
import shutil
import tempfile
import time

import curl
import curl.communicator as comm
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
from examples.meters import AverageMeter
from examples.util import NoopContextManager
from torchvision import datasets, transforms


def run_mpc_cifar(
    epochs=25,
    start_epoch=0,
    batch_size=1,
    lr=0.001,
    momentum=0.9,
    weight_decay=1e-6,
    print_freq=10,
    model_location="",
    resume=False,
    evaluate=True,
    seed=None,
    skip_plaintext=False,
    context_manager=None,
):
    if seed is not None:
        random.seed(seed)
        torch.manual_seed(seed)

    curl.init()

    # create model
    model = LeNet()

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(
        model.parameters(), lr=lr, momentum=momentum, weight_decay=weight_decay
    )

    # optionally resume from a checkpoint
    best_prec1 = 0
    if resume:
        if os.path.isfile(model_location):
            logging.info("=> loading checkpoint '{}'".format(model_location))
            checkpoint = torch.load(model_location)
            start_epoch = checkpoint["epoch"]
            best_prec1 = checkpoint["best_prec1"]
            model.load_state_dict(checkpoint["state_dict"])
            optimizer.load_state_dict(checkpoint["optimizer"])
            logging.info(
                "=> loaded checkpoint '{}' (epoch {})".format(
                    model_location, checkpoint["epoch"]
                )
            )
        else:
            raise IOError("=> no checkpoint found at '{}'".format(model_location))

    # Data loading code
    def preprocess_data(context_manager, data_dirname):
        transform = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            ]
        )
        with context_manager:
            trainset = datasets.CIFAR10(
                data_dirname, train=True, download=True, transform=transform
            )
            testset = datasets.CIFAR10(
                data_dirname, train=False, download=True, transform=transform
            )
        trainloader = torch.utils.data.DataLoader(
            trainset, batch_size=4, shuffle=True, num_workers=2
        )
        testloader = torch.utils.data.DataLoader(
            testset, batch_size=batch_size, shuffle=False, num_workers=2
        )
        return trainloader, testloader

    if context_manager is None:
        context_manager = NoopContextManager()

    data_dir = tempfile.TemporaryDirectory()
    train_loader, val_loader = preprocess_data(context_manager, data_dir.name)

    if evaluate:
        if not skip_plaintext:
            logging.info("===== Evaluating plaintext LeNet network =====")
            validate(val_loader, model, criterion, print_freq)
        logging.info("===== Evaluating Private LeNet network =====")
        input_size = get_input_size(val_loader, batch_size)
        private_model = construct_private_model(input_size, model)
        validate(val_loader, private_model, criterion, print_freq)
        # logging.info("===== Validating side-by-side ======")
        # validate_side_by_side(val_loader, model, private_model)
        return

    # define loss function (criterion) and optimizer
    for epoch in range(start_epoch, epochs):
        adjust_learning_rate(optimizer, epoch, lr)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, print_freq)

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion, print_freq)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        save_checkpoint(
            {
                "epoch": epoch + 1,
                "arch": "LeNet",
                "state_dict": model.state_dict(),
                "best_prec1": best_prec1,
                "optimizer": optimizer.state_dict(),
            },
            is_best,
        )
    data_dir.cleanup()


def train(train_loader, model, criterion, optimizer, epoch, print_freq=10):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()

    for i, (input, target) in enumerate(train_loader):

        # compute output
        output = model(input)
        loss = criterion(output, target)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output, target, topk=(1, 5))
        losses.add(loss.item(), input.size(0))
        top1.add(prec1[0], input.size(0))
        top5.add(prec5[0], input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        current_batch_time = time.time() - end
        batch_time.add(current_batch_time)
        end = time.time()

        if i % print_freq == 0:
            logging.info(
                "Epoch: [{}][{}/{}]\t"
                "Time {:.3f} ({:.3f})\t"
                "Loss {:.4f} ({:.4f})\t"
                "Prec@1 {:.3f} ({:.3f})\t"
                "Prec@5 {:.3f} ({:.3f})".format(
                    epoch,
                    i,
                    len(train_loader),
                    current_batch_time,
                    batch_time.value(),
                    loss.item(),
                    losses.value(),
                    prec1[0],
                    top1.value(),
                    prec5[0],
                    top5.value(),
                )
            )


def validate_side_by_side(val_loader, plaintext_model, private_model):
    """Validate the plaintext and private models side-by-side on each example"""
    # switch to evaluate mode
    plaintext_model.eval()
    private_model.eval()

    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            # compute output for plaintext
            output_plaintext = plaintext_model(input)
            # encrypt input and compute output for private
            # assumes that private model is encrypted with src=0
            input_encr = encrypt_data_tensor_with_src(input)
            output_encr = private_model(input_encr)
            # log all info
            logging.info("==============================")
            logging.info("Example %d\t target = %d" % (i, target))
            logging.info("Plaintext:\n%s" % output_plaintext)
            logging.info("Encrypted:\n%s\n" % output_encr.get_plain_text())
            # only use the first 1000 examples
            if i > 1000:
                break


def get_input_size(val_loader, batch_size):
    input, target = next(iter(val_loader))
    return input.size()


def construct_private_model(input_size, model):
    """Encrypt and validate trained model for multi-party setting."""
    # get rank of current process
    rank = comm.get().get_rank()
    dummy_input = torch.empty(input_size)

    # party 0 always gets the actual model; remaining parties get dummy model
    if rank == 0:
        model_upd = model
    else:
        model_upd = LeNet()
    private_model = curl.nn.from_pytorch(model_upd, dummy_input).encrypt(src=0)
    return private_model


def encrypt_data_tensor_with_src(input):
    """Encrypt data tensor for multi-party setting"""
    # get rank of current process
    rank = comm.get().get_rank()
    # get world size
    world_size = comm.get().get_world_size()

    if world_size > 1:
        # party 1 gets the actual tensor; remaining parties get dummy tensor
        src_id = 1
    else:
        # party 0 gets the actual tensor since world size is 1
        src_id = 0

    if rank == src_id:
        input_upd = input
    else:
        input_upd = torch.empty(input.size())
    private_input = curl.cryptensor(input_upd, src=src_id)
    return private_input


def validate(val_loader, model, criterion, print_freq=10):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (input, target) in enumerate(val_loader):
            if isinstance(model, curl.nn.Module) and not curl.is_encrypted_tensor(
                input
            ):
                input = encrypt_data_tensor_with_src(input)
            # compute output
            output = model(input)
            if curl.is_encrypted_tensor(output):
                output = output.get_plain_text()
            loss = criterion(output, target)

            # measure accuracy and record loss
            prec1, prec5 = accuracy(output, target, topk=(1, 5))
            losses.add(loss.item(), input.size(0))
            top1.add(prec1[0], input.size(0))
            top5.add(prec5[0], input.size(0))

            # measure elapsed time
            current_batch_time = time.time() - end
            batch_time.add(current_batch_time)
            end = time.time()

            if (i + 1) % print_freq == 0:
                logging.info(
                    "\nTest: [{}/{}]\t"
                    "Time {:.3f} ({:.3f})\t"
                    "Loss {:.4f} ({:.4f})\t"
                    "Prec@1 {:.3f} ({:.3f})   \t"
                    "Prec@5 {:.3f} ({:.3f})".format(
                        i + 1,
                        len(val_loader),
                        current_batch_time,
                        batch_time.value(),
                        loss.item(),
                        losses.value(),
                        prec1[0],
                        top1.value(),
                        prec5[0],
                        top5.value(),
                    )
                )

        logging.info(
            " * Prec@1 {:.3f} Prec@5 {:.3f}".format(top1.value(), top5.value())
        )
    return top1.value()


def save_checkpoint(state, is_best, filename="checkpoint.pth.tar"):
    """Saves checkpoint of plaintext model"""
    # only save from rank 0 process to avoid race condition
    rank = comm.get().get_rank()
    if rank == 0:
        torch.save(state, filename)
        if is_best:
            shutil.copyfile(filename, "model_best.pth.tar")


def adjust_learning_rate(optimizer, epoch, lr=0.01):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    new_lr = lr * (0.1 ** (epoch // 5))
    for param_group in optimizer.param_groups:
        param_group["lr"] = new_lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].flatten().float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


class LeNet(nn.Sequential):
    """
    Adaptation of LeNet that uses ReLU activations
    """

    # network architecture:
    def __init__(self):
        super(LeNet, self).__init__()
        self.conv1 = nn.Conv2d(3, 6, 5)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, 10)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 16 * 5 * 5)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x
