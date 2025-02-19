import argparse
import glob
import json
import multiprocessing
import os
import random
import re
from importlib import import_module
from pathlib import Path
from sklearn.metrics import f1_score
from utils import *

import torchvision.models

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.optim import *
from torch.optim.lr_scheduler import StepLR
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.optim.lr_scheduler  import CosineAnnealingLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import wandb

from dataset import MaskBaseDataset
from loss import create_criterion
import copy
import pickle

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']

def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"


def train(data_dir, model_dir, args):
    if args.name != 'test':
        wandb.init(project= 'mask-project', reinit=True,
                    config={"batch_size": args.batch_size,
                    "lr"        : args.lr,
                    "epochs"    : args.epochs,
                    "name"      : args.name,
                    "criterion_name" : args.criterion
                        })

        wandb.run.name = args.name

    seed_everything(args.seed)

    save_dir = increment_path(os.path.join(model_dir, args.log_name))

    # -- settings
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- dataset
    dataset_module = getattr(import_module("dataset"), args.dataset)  # default: BaseAugmentation
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(import_module("dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )
    dataset.set_transform(transform)

    # -- data_loader
    train_set, val_set = dataset.split_dataset()


    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        pin_memory=use_cuda,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=False,
        pin_memory=use_cuda,
        drop_last=True,
    )

    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: resnet18
    model = model_module(
        num_classes=num_classes
    ).to(device)
    # model = torch.nn.DataParallel(model)
  
    # -- loss & metric
    criterion = create_criterion(args.criterion)  # default: focal
    opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: adam
    optimizer = opt_module(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.lr,
        weight_decay=5e-4
    )

    # scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)
    # scheduler= ReduceLROnPlateau(optimizer, 'min', patience=10)
    scheduler= CosineAnnealingLR(optimizer, T_max=50, eta_min=0)

    # -- logging
    logger = SummaryWriter(log_dir=save_dir)
    with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)
    model_save= copy.copy(model.state_dict())
    if args.name != 'test':
        wandb.watch(model,log='gradients',log_freq= args.log_interval)
    
    best_val_acc = 0
    best_val_f1 = 0
    best_val_loss = np.inf
    for epoch in range(args.epochs):
        # train loop
        model.train()
        loss_value = 0
        matches = 0
        pred_lst= []
        label_lst=[]
        for idx, train_batch in enumerate(train_loader):
            inputs, labels = train_batch
            inputs = inputs.to(device)
            labels = labels.to(device)
            optimizer.zero_grad()

            outs = model(inputs)
            preds = torch.argmax(outs, dim=-1)
            pred_lst.extend(preds.cpu().numpy())
            label_lst.extend(labels.cpu().numpy())
            loss = criterion(outs, labels)

            loss.backward()
            optimizer.step()

            loss_value += loss.item()
            matches += (preds == labels).sum().item()
            if (idx + 1) % args.log_interval == 0:
                train_loss = loss_value / args.log_interval
                train_acc = matches / args.batch_size / args.log_interval
                train_f1= f1_score(pred_lst, label_lst, average='macro')
                current_lr = get_lr(optimizer)
                print(
                    f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                    f"training loss {train_loss:4.4} || training f1 {train_f1:4.2%} || training accuracy {train_acc:4.2%} || lr {current_lr}"
                )
                logger.add_scalar("Train/loss", train_loss, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/accuracy", train_acc, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/f1-score", train_f1, epoch * len(train_loader) + idx)
                loss_value = 0
                matches = 0
                if args.name != 'test':    
                    wandb.log({'train_loss':train_loss,
                'train_f1':train_f1})

        # scheduler.step(loss)
        scheduler.step()
        # val loop
        with torch.no_grad():
            print("Calculating validation results...")
            model.eval()
            val_loss_items = []
            val_acc_items = []
            pred_lst=[]
            label_lst=[]
            figure = None
            for val_batch in val_loader:
                inputs, labels = val_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                outs = model(inputs)
                preds = torch.argmax(outs, dim=-1)

                loss_item = criterion(outs, labels).item()
                acc_item = (labels == preds).sum().item()
                val_loss_items.append(loss_item)
                val_acc_items.append(acc_item)

                pred_lst.extend(preds.cpu().numpy())
                label_lst.extend(labels.cpu().numpy())

            val_loss = np.sum(val_loss_items) / len(val_loader)
            val_acc = np.sum(val_acc_items) / len(val_set)
            val_f1= f1_score(pred_lst, label_lst, average='macro')
            best_val_loss = min(best_val_loss, val_loss)
            # if val_acc > best_val_acc:
            #     print(f"New best model for val accuracy : {val_acc:4.2%}! saving the best model..")
            #     torch.save(model, os.path.join(model_dir, 'best_model.pt'))
            #     # torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
            #     best_val_acc = val_acc
            # model.load_state_dict(model_save)
            
            if val_f1 > best_val_f1:   
                print(f"New best model for val f1 score : {val_f1:4.2%}! saving the best model..")
                torch.save(model, os.path.join(model_dir, '{}.pt'.format(args.name)))
    
                best_val_f1 = val_f1
            torch.save(model, os.path.join(model_dir, '{}_last.pt'.format(args.name)))
          
            print(
                f"[Val] acc : {val_acc:4.2%}, f1: {val_f1:4.2%} , loss: {val_loss:4.2}|| "
                f"best f1 : {best_val_f1:4.2%}, best loss: {best_val_loss:4.2}")
            # f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2} || "
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            logger.add_scalar("Val/f1-score", val_f1, epoch)
            # logger.add_figure("results", figure, epoch)
            print()
            if args.name != 'test':
                wandb.log({
                    "Valid loss": val_loss,
                    "Valid acc" : val_acc,
                    "Valid f1" : val_f1
                    })
                
        if args.name != 'test':
            wandb.log({
                    "confusion_mat": wandb.plot.confusion_matrix(preds=np.array(pred_lst),y_true=np.array(label_lst))
                    })
            wandb.log({
                "confusion_matrix": wandb.sklearn.plot_confusion_matrix(np.array(label_lst),np.array(pred_lst))
            })
        # early stopping
        if current_lr < 1e-8:
            print("too small learning rate! early stop!!!")
            break

    record_expr(args.name, train_loss, val_loss, val_f1, best_val_f1, args)
    print('record experiments!!!')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    import os

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=32, help='random seed (default: 42)') # 시드 바꾸면 dataset에서seed도 바꿔주기
    parser.add_argument('--epochs', type=int, default=10, help='number of epochs to train (default: 1)')
    parser.add_argument('--dataset', type=str, default='MaskSplitByProfileDataset', help='dataset augmentation type (default: MaskBaseDataset)')
    parser.add_argument('--augmentation', type=str, default='BaseAugmentation', help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument("--resize", nargs="+", type=list, default=[512,384], help='resize size for image when training') #128,96 원래는 512,384
    parser.add_argument('--batch_size', type=int, default=64, help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=64, help='input batch size for validing (default: 1000)')
    parser.add_argument('--model', type=str, default='resnet18', help='model type (default: resnet18)')
    parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer type (default: Adam)')
    parser.add_argument('--lr', type=float, default=1e-3, help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='focal', help='criterion type (default: focal)')
    parser.add_argument('--lr_decay_step', type=int, default=20, help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--log_interval', type=int, default=20, help='how many batches to wait before logging training status')
    parser.add_argument('--log_name', default='exp', help='log save at {SM_MODEL_DIR}/{log_name}')
    parser.add_argument('--name', default='best_model', help='model save at {SM_MODEL_DIR}/{name}')

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images2'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './model'))


    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)