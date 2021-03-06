'''Active Learning Procedure in PyTorch.

Reference:
[Yoo et al. 2019] Learning Loss for Active Learning (https://arxiv.org/abs/1905.03677)
'''

# Python
import os
import random

# Torch
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data.sampler import SubsetRandomSampler

# Torchvison
import torchvision.transforms as T
import torchvision.models as models
from torchvision.datasets import CIFAR100, CIFAR10

# Utils
import visdom
from tqdm import tqdm
import sys

# Custom
import models.resnet as resnet
import models.lossnet as lossnet
from debug_config import *
from data.sampler import SubsetSequentialSampler
from pytorch_metric_learning import losses


##
# Data
train_transform = T.Compose([
    T.RandomHorizontalFlip(),
    T.RandomCrop(size=32, padding=4),
    T.ToTensor(),
    T.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010]) # T.Normalize((0.5071, 0.4867, 0.4408), (0.2675, 0.2565, 0.2761)) # CIFAR-100
])

test_transform = T.Compose([
    T.ToTensor(),
    T.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010]) # T.Normalize((0.5071, 0.4867, 0.4408), (0.2675, 0.2565, 0.2761)) # CIFAR-100
])

cifar10_train = CIFAR10('./cifar10', train=True, download=True, transform=train_transform)
cifar10_unlabeled   = CIFAR10('./cifar10', train=True, download=True, transform=test_transform)
cifar10_test  = CIFAR10('./cifar10', train=False, download=True, transform=test_transform)


##
# Loss Prediction Loss
def LossPredLoss(input, target, margin=1.0, reduction='mean'):
    assert len(input) % 2 == 0, 'the batch size is not even.'
    assert input.shape == input.flip(0).shape
    
    input = (input - input.flip(0))[:len(input)//2] # [l_1 - l_2B, l_2 - l_2B-1, ... , l_B - l_B+1], where batch_size = 2B
    target = (target - target.flip(0))[:len(target)//2]
    target = target.detach()

    one = 2 * torch.sign(torch.clamp(target, min=0)) - 1 # 1 operation which is defined by the authors
    
    if reduction == 'mean':
        loss = torch.sum(torch.clamp(margin - one * input, min=0))
        loss = loss / input.size(0) # Note that the size of input is already halved
    elif reduction == 'none':
        loss = torch.clamp(margin - one * input, min=0)
    else:
        NotImplementedError()
    
    return loss


##
# Train Utils
iters = 0

#
def train_epoch(models, criterion, optimizers, dataloaders, epoch, epoch_loss, vis=None, plot_data=None):
    models['backbone'].train()
    models['module'].train()
    global iters, WEIGHT

    for data in tqdm(dataloaders['train'], leave=False, total=len(dataloaders['train'])): 
        inputs = data[0].cuda()
        labels = data[1].cuda()
        iters += 1

        optimizers['backbone'].zero_grad()
        optimizers['module'].zero_grad()
        scores, features = models['backbone'](inputs)
        if args.aux3 == 'LogRatioLoss':
            representations = models['backbone'].representations
        target_loss = criterion(scores, labels)

        if epoch > epoch_loss:
            # After 120 epochs, stop the gradient from the loss prediction module propagated to the target model.
            features[0] = features[0].detach()
            features[1] = features[1].detach()
            features[2] = features[2].detach()
            features[3] = features[3].detach()
        pred_loss, embeddings = models['module'](features)
        pred_loss = pred_loss.view(pred_loss.size(0))

        m_backbone_loss = torch.sum(target_loss) / target_loss.size(0)
        if args.aux1 == 'None':
            m_module_loss   = 0
        elif args.aux1 == 'MarginRankingLoss':
            m_module_loss   = LossPredLoss(pred_loss, target_loss, margin=MARGIN)
        elif args.aux1 == 'MSE':
            target_loss = target_loss.detach()
            m_module_loss   = WEIGHT_MSE * nn.MSELoss()(pred_loss, target_loss)
        elif args.aux1 == 'L1':
            target_loss = target_loss.detach()
            m_module_loss   = WEIGHT_MSE * nn.L1Loss()(pred_loss, target_loss)
        elif args.aux1 == 'SmoothL1':
            target_loss = target_loss.detach()
            m_module_loss   = WEIGHT_MSE * nn.SmoothL1Loss()(pred_loss, target_loss)
        elif args.aux1 == 'Triplet':
            loss_fuc_4loss  = losses.TripletMarginLoss(margin=0.1)
            pred_loss = pred_loss.view(pred_loss.size(0),1)
            m_module_loss   =loss_fuc_4loss(pred_loss, labels)


        if args.aux2 == 'TripletMarginLoss':
            loss_fuc = losses.TripletMarginLoss(margin=0.1)
        elif args.aux2 == 'NPairsLoss':
            loss_fuc = losses.NPairsLoss()
        elif args.aux2 == 'NCALoss':
            loss_fuc = losses.NCALoss()
        elif args.aux2 == 'GeneralizedLiftedStructureLoss':
            loss_fuc = losses.GeneralizedLiftedStructureLoss(neg_margin = 0.1)
        elif args.aux2 == 'NTXentLoss':
            loss_fuc = losses.NTXentLoss(temperature=0.1)
        elif args.aux2 == 'ContrastiveLoss':
            loss_fuc = losses.ContrastiveLoss()
        
        if args.aux2 == 'None':
            m_module_tloss = 0
        else:
            m_module_tloss  = loss_fuc(embeddings, labels)


        if args.aux3 == 'LogRatioLoss':
            from auxiliary.logratio import LossToDist, LogRatioLoss
            pred_loss = pred_loss.view(pred_loss.size(0))
            gt_dist = LossToDist()(pred_loss)
            representations = representations.detach()
            # representations = torch.nn.functional.normalize(representations, p=2, dim=1)
            m_module_lloss   = LogRatioLoss()(representations, gt_dist)
        else:
            m_module_lloss = 0

        
        loss            = m_backbone_loss + WEIGHT * m_module_loss + WEIGHT2 * m_module_tloss + 0.1 * m_module_lloss

        loss.backward()
        if args.gc:
            nn.utils.clip_grad_norm_(models['module'].parameters(),1)
        optimizers['backbone'].step()
        optimizers['module'].step()

        # Visualize
        if (iters % 10 == 0) and (vis != None) and (plot_data != None):
            plot_data['X'].append(iters)
            try:
                m_module_loss = m_module_loss.item()
            except:
                pass
            try:
                m_module_tloss = m_module_tloss.item()
            except:
                pass
            plot_data['Y'].append([
                m_backbone_loss.item(),
                m_module_loss,
                m_module_tloss,
                loss.item()
            ])
            vis.line(
                X=np.stack([np.array(plot_data['X'])] * len(plot_data['legend']), 1),
                Y=np.array(plot_data['Y']),
                opts={
                    'title': 'Loss over Time',
                    'legend': plot_data['legend'],
                    'xlabel': 'Iterations',
                    'ylabel': 'Loss',
                    'width': 1200,
                    'height': 390,
                },
                win=1
            )

#
def test(models, dataloaders, mode='val'):
    assert mode == 'val' or mode == 'test'
    models['backbone'].eval()
    models['module'].eval()

    total = 0
    correct = 0
    with torch.no_grad():
        for (inputs, labels) in dataloaders[mode]:
            inputs = inputs.cuda()
            labels = labels.cuda()

            scores, _ = models['backbone'](inputs)
            _, preds = torch.max(scores.data, 1)
            total += labels.size(0)
            correct += (preds == labels).sum().item()
    
    return 100 * correct / total

#
def train(models, criterion, optimizers, schedulers, dataloaders, start_num_epoch, num_epochs, epoch_loss, vis, plot_data):
    print('>> Train a Model.')
    best_acc = 0.
    checkpoint_dir = os.path.join('./cifar10', 'train', 'weights')
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    
    for epoch in range(start_num_epoch, num_epochs):
        train_epoch(models, criterion, optimizers, dataloaders, epoch, epoch_loss, vis, plot_data)

        schedulers['backbone'].step()
        schedulers['module'].step()

        # Save a checkpoint
        if False and epoch % 5 == 4:
            acc = test(models, dataloaders, 'test')
            if best_acc < acc:
                best_acc = acc
                torch.save({
                    'epoch': epoch + 1,
                    'state_dict_backbone': models['backbone'].state_dict(),
                    'state_dict_module': models['module'].state_dict()
                },
                '%s/active_resnet18_cifar10.pth' % (checkpoint_dir))
            print('Val Acc: {:.3f} \t Best Acc: {:.3f}'.format(acc, best_acc))
    print('>> Finished.')


##
# Main
if __name__ == '__main__':
    vis = visdom.Visdom(server='http://localhost', port=9000)
    plot_data = {'X': [], 'Y': [], 'legend': ['Backbone Loss', 'Auxiliary Loss', 'Metric Loss', 'Total Loss']}

#    random.seed("Inyoung Cho")
#    torch.manual_seed(0)
#    torch.backends.cudnn.deterministic = True
#    torch.backends.cudnn.benchmark = False
#    np.random.seed(0)


    for trial in range(TRIALS):
        # Random seed
        random.seed(trial)
        torch.manual_seed(trial)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        np.random.seed(trial)
        
        # Initialize a labeled dataset by randomly sampling K=ADDENDUM=1,000 data points from the entire dataset.
        indices = list(range(NUM_TRAIN))
        random.shuffle(indices)
        labeled_set = indices[:INITIALQUERY]
        unlabeled_set = indices[INITIALQUERY:]
        
        train_loader = DataLoader(cifar10_train, batch_size=BATCH, 
                                  sampler=SubsetRandomSampler(labeled_set), 
                                  pin_memory=True)
        test_loader  = DataLoader(cifar10_test, batch_size=BATCH)
        dataloaders  = {'train': train_loader, 'test': test_loader}
        
        # Active learning cycles
        for cycle in range(CYCLES):
            
            # Model
            resnet18    = resnet.ResNet18(num_classes=10).cuda()
            loss_module = lossnet.LossNet().cuda()
            models      = {'backbone': resnet18, 'module': loss_module}
            torch.backends.cudnn.benchmark = True

            # Loss, criterion and scheduler (re)initialization
            criterion      = nn.CrossEntropyLoss(reduction='none')
            optim_backbone = optim.SGD(models['backbone'].parameters(), lr=LR, 
                                    momentum=MOMENTUM, weight_decay=WDECAY)
            optim_module   = optim.SGD(models['module'].parameters(), lr=LR, 
                                    momentum=MOMENTUM, weight_decay=LWDECAY)
            sched_backbone = lr_scheduler.MultiStepLR(optim_backbone, milestones=MILESTONES)
            sched_module   = lr_scheduler.MultiStepLR(optim_module, milestones=MILESTONES)

            optimizers = {'backbone': optim_backbone, 'module': optim_module}
            schedulers = {'backbone': sched_backbone, 'module': sched_module}

            # Training and test
            if args.middle_pick:
                train(models, criterion, optimizers, schedulers, dataloaders, 0, MILESTONES[0], EPOCHL, vis, plot_data)
                from strategy.sampler import Sampler
                uncertainty, real_loss, subset = Sampler(args.rule, models, cifar10_unlabeled, unlabeled_set)
                train(models, criterion, optimizers, schedulers, dataloaders, MILESTONES[0], EPOCH, EPOCHL, vis, plot_data)
            else:
                train(models, criterion, optimizers, schedulers, dataloaders, 0, EPOCH, EPOCHL, vis, plot_data)
                from strategy.sampler import Sampler
                uncertainty, real_loss, subset = Sampler(args.rule, models, cifar10_unlabeled, unlabeled_set)

            acc = test(models, dataloaders, mode='test')
            print('Trial {}/{} || Cycle {}/{} || Label set size {}: Test acc {}'.format(trial+1, TRIALS, cycle+1, CYCLES, len(labeled_set), acc))

            # Index in ascending order
            arg = np.argsort(uncertainty)

            # Plot
            if args.picked_plot:
                import plot.plotting as pt
                pt.dot_plot(np.sort(uncertainty)[-(SUBSET//10):],real_loss[arg][-(SUBSET//10):].tolist(), loc = '.', name = 'picked.png')
                pt.dot_plot(np.sort(uncertainty)[:-(SUBSET//10)], real_loss[arg][:-(SUBSET//10)].tolist(), loc = '.', name = 'unpicked.png')
                import sys; sys.exit()

            # Update the labeled dataset and the unlabeled dataset, respectively
            labeled_set += list(torch.tensor(subset)[arg][-ADDENDUM:].numpy())
            temp_loader = DataLoader(cifar10_unlabeled, batch_size=BATCH,
                                    sampler = SubsetSequentialSampler(list(torch.tensor(subset)[arg][-ADDENDUM:].numpy())),
                                    pin_memory=True)
            unlabeled_set = list(torch.tensor(subset)[arg][:-ADDENDUM].numpy()) + unlabeled_set[SUBSET:]

            # Create a new dataloader for the updated labeled dataset
            dataloaders['train'] = DataLoader(cifar10_train, batch_size=BATCH, 
                                              sampler=SubsetRandomSampler(labeled_set), 
                                              pin_memory=True)
        
        # Save a checkpoint
        torch.save({
                    'trial': trial + 1,
                    'state_dict_backbone': models['backbone'].state_dict(),
                    'state_dict_module': models['module'].state_dict()
                },
                './cifar10/train/weights/active_resnet18_cifar10_trial{}.pth'.format(trial))
