# -*- coding: utf-8 -*-
"""
Created on Wed Jul  6 13:15:26 2022

@author: jsl6
"""
ROOT_INT = 2
DOWNWEIGHT_P = False
EMBED = False

import numpy as np
import sys
import os
from time import time
import collections
import torch
from models import FlipyFlopy
import matplotlib.pyplot as plt
plt.close('all')
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

###############################################################################
############################## Dictionaries ###################################
###############################################################################

from utils import DicObj
criteria = open("input_data/ion_stats/criteria.txt","r").read().split("\n")
D = DicObj(criteria=criteria)
pfilter = D.create_filter('"p" in ion')

###############################################################################
################################ Dataset ######################################
###############################################################################

from utils import LoadObj
L = LoadObj(D, embed=EMBED)

fpostr = np.loadtxt('input_data/txt_pos/fpostrain.txt')
ftr = open("input_data/datasets/train.txt", "r")

# intorch = L.input_from_file(fpostr, ftr)
trlab = np.array([line.strip() for line in 
                  open('input_data/labels/training_labels.txt','r')])

# validation
fposval = np.loadtxt('input_data/txt_pos/fposval.txt')
g = open("input_data/datasets/val.txt", "r")
intorch_val = L.input_from_file(fposval, 'input_data/datasets/val.txt')[0]
targ_val,mz_val = L.target(fposval, g)
g.close()
# testing
fposte = np.loadtxt('input_data/txt_pos/fpostest.txt')
g = open("input_data/datasets/test.txt", "r")
intorch_test = L.input_from_file(fposte,'input_data/datasets/test.txt')[0]
targ_test,mz_test = L.target(fposte, g)
# find long sequence for mirrorplot
Lens = []
for pos in fposte:
    g.seek(pos);Lens.append(len(g.readline().split()[1].split('|')[0]))
MPIND = np.argmax(Lens)
g.close()

###############################################################################
################################## Model ######################################
###############################################################################

arrdims=21
Blocks=9
model = FlipyFlopy(
    in_ch=D.channels,
    seq_len=D.seq_len,
    out_dim=len(D.dictionary),
    embedsz=256,
    blocks=Blocks,
    head=(16,16,64),
    units=None,
    filtlast=512,
    mask=False,
    CEembed=EMBED,
    device=device
    )
sys.stdout.write("Total model parameters: ")
model.total_params()

# Load weights
# model.load_state_dict(torch.load("./saved_models/ckpt_epoch0_0.8911"))

# TRANSFER LEARNING
# model.final = torch.nn.Sequential(torch.nn.Linear(512,D.dicsz), torch.nn.Sigmoid())
# for parm in model.parameters(): parm.requires_grad=False
# for parm in model.final.parameters(): parm.requires_grad=True

# loading optimizer state requires it to be initialized with model GPU parms
# model.to(device)
opt = torch.optim.Adam(model.parameters(), 3e-4)
# opt.load_state_dict(torch.load('./saved_models/opt.sd', map_location=device))

###############################################################################
########################### Loss function #####################################
###############################################################################

CS = torch.nn.CosineSimilarity(dim=-1)
def LossFunc(targ, pred, pwt=0.5, root=ROOT_INT):
    targ = L.root_intensity(targ, root=root) if root!=False else targ
    pred = L.root_intensity(pred, root=root) if root!=False else pred
    # targ[:,pfilter] = pwt*targ[:,pfilter]
    # pred[:,pfilter] = pwt*pred[:,pfilter]
    cs = CS(targ, pred)
    return -cs

def AdvAtt(samples, targ):
    model.train()
    model.zero_grad()
    samplesgpu = samples.to(device)
    samplesgpu.requires_grad = True
    targgpu = targ.to(device)
    model.to(device)
    out,_,_ = model(samplesgpu)
    loss,(cs,mae) = LossFunc(targgpu, out);loss = loss.mean()
    loss.backward()
    grads = samplesgpu.grad
    samplesgpu2 = samplesgpu + 10*grads
    return samplesgpu2, targgpu

###############################################################################
########################## Training and testing ###############################
###############################################################################

def train_step(samples, targ, attack=False):
    if attack:
        samplesgpu, targgpu = AdvAtt(samples, targ)
    else:
        samplesgpu = [m.to(device) for m in samples]
        targgpu = targ.to(device)
        model.to(device)
    
    model.train()
    model.zero_grad()
    out,_,_ = model(samplesgpu, test=False)
    
    loss = LossFunc(targgpu, out, root=ROOT_INT)
    loss = loss.mean()
    loss.backward()
    opt.step()
    return loss

def Testing(batch_size, Ids, Tds):
    with torch.no_grad():
        model.eval()
        tot = Ids[0].shape[0]
        steps = (tot//batch_size) if tot%batch_size==0 else (tot//batch_size)+1
        model.to(device)
        Loss = 0
        arr = torch.zeros(Blocks, arrdims)
        for m in range(steps):
            begin = m*batch_size
            end = (m+1)*batch_size
            # Test set
            samplesgpu = [n[begin:end].to(device) for n in Ids]
            out,out2,FMs = model(samplesgpu)
            loss = LossFunc(Tds[begin:end].to(device), out)
            Loss += loss.sum()
            arr += torch.tensor([[n for n in m] for m in out2])
    model.to('cpu')
    Loss = (Loss/tot).to('cpu').detach().numpy()
    return Loss, arr.detach().numpy() / steps

testintime = []
validintime = []
def train(epochs,
          batch_size=100,
          lr_decay_start = 1e10,
          lr_decay_rate = 0.9,
          shuffle=True, 
          svwts=False):
    
    print("Starting training for %d epochs"%epochs)
    tot = len(trlab)
    steps = tot//batch_size if tot%batch_size==0 else tot//batch_size + 1
    
    # Testing before training begins
    test,_ = Testing(batch_size, intorch_test, targ_test)
    val,_ = Testing(batch_size, intorch_val, targ_val)
    mirrorplot(MPIND)
    if svwts: torch.save(model.state_dict(), 'saved_models/ckpt_%.4f'%(-val))
    print("Val/Test: %6.3f / %6.3f"%(-val,-test))
    
    # Training loop
    for i in range(epochs):
        start_epoch = time()
        P = np.random.permutation(tot)
        if i>=lr_decay_start:
            opt.param_groups[0]['lr'] *= lr_decay_rate
        
        # trainintime=[]
        runav = collections.deque(maxlen=50)
        train_loss = 0
        # Train an epoch
        for j in range(steps):
            start_step = time()
            
            begin = j*batch_size
            end = (j+1)*batch_size
            
            # samples = intorch[P[begin:end]]
            samples,info = L.input_from_str(trlab[P[begin:end]])
            targ,_ = L.target(fpostr[P[begin:end]], fp=ftr, return_mz=False)
            Loss = train_step(samples, targ, False)
            train_loss += Loss
            
            runav.append(float(Loss.to('cpu').detach().numpy()))
            # trainintime.append(runav[-1])
            if j%50==0: sys.stdout.write("\rStep %d/%d; Loss: %.3f (%.2f s)"%(
                    j+1, steps, np.mean(runav), time()-start_step)
            )
        
        # Testing after training epoch
        train_loss = train_loss.to('cpu').detach().numpy() / steps
        sys.stdout.write("\rTesting...%50s"%(""))
        test_loss, tarr = Testing(batch_size, intorch_test, targ_test)
        val_loss, varr = Testing(batch_size, intorch_val, targ_val)
        testintime.append(float(test_loss))
        validintime.append(float(val_loss))
        
        # Saving progress to file after training epoch
        # with open("C:/Users/jsl6/Desktop/lossintime.txt", "a") as f:
        #     f.write(" ".join([str(q) for q in trainintime]))
        #     f.write(" ")
        with open('C:/Users/jsl6/Desktop/actarr.txt','a') as f:
            f.write("".join(['%9d'%m for m in np.arange(arrdims)])+'\n')
            for m in range(Blocks): 
                f.write("".join(['%9.5f'%a for a in tarr[m]])+'\n')    
        mirrorplot(MPIND, epoch=i, maxnorm=True)
        
        # Save checkpoint
        if svwts=='top':
            currbest = np.maximum(np.max([float(m.split('_')[-1]) 
                                  for m in os.listdir('./saved_models/') 
                                  if m.split('_')[0]=='ckpt']), 0
            )
            if -val_loss>currbest:
                for file in os.listdir('./saved_models/'):
                    if file.split('_')[0]=='ckpt': 
                        os.remove('./saved_models/%s'%file)
                torch.save(model.state_dict(), 
                           "saved_models/ckpt_epoch%d_%.4f"%(i,-val_loss)
                )
        elif (svwts=='all') | (svwts==True):
            torch.save(model.state_dict(), 
                       "saved_models/ckpt_epoch%d_%.4f"%(i,-val_loss)
            )
            torch.save(opt.state_dict(), "saved_models/opt.sd")
        
        # Print out results
        sys.stdout.write(
  "\rEpoch %d; Train loss: %.3f; Val loss: %6.3f; Test loss: %6.3f; %.1f s\n"%(
            i, train_loss, -val_loss, -test_loss, time()-start_epoch)
        )
    model.to("cpu")

def mirrorplot(iloc=0, epoch=0, maxnorm=True, save=True):
    plt.close('all')
    model.eval()
    model.to("cpu")
    
    with open("input_data/datasets/test.txt") as g:
        g.seek(fposte[iloc])
        [seq,mod,charge,ev,nmpks] = g.readline().split()[1].split('|')
        sample = [m[iloc:iloc+1] for m in intorch_test]
        [targ,mz] = [m.squeeze().detach().numpy() for m in 
                     L.target(fposte[iloc:iloc+1], g)]
    with torch.no_grad():
        pred = model(sample)[0].squeeze().detach().numpy()
    if maxnorm: pred /= pred.max()
    
    # Calculate masses for each dictionary key-string, ignoring the doubled up p/p^1
    mzpred = np.array([D.calcmass(seq,charge,mod,key) 
                       for key,value in 
                       D.dictionary.items()]
    )
    sort = mzpred.argsort() # ion dictionary index to m/z ascending order
    pred = pred[sort]
    targ = targ[sort]
    mz = mz[sort]
    mzpred = mzpred[sort]
    
    plt.close('all')
    fig,ax = plt.subplots()
    fig.set_figwidth(15)
    ax.set_xlabel("m/z")
    ax.set_ylabel("Intensity")
    
    # for x,y,x2,y2 in zip(mz,targ,mzpred,pred):
    #     # ymin, ymax between 0-1
    #     ax.axvline(x, 0.5, 0.5+(1/1.1)*y/2, linewidth=1, color='red')
    #     ax.axvline(x2, 0.5-(1/1.1)*y2/2, 0.5, linewidth=1, color='blue')
    ax.vlines(mz, ymin=0, ymax=targ, linewidth=1, color='red')
    ax.vlines(mzpred, ymin=-pred, ymax=0, linewidth=1, color='blue')
    
    ax.set_xlim([0, ax.get_xlim()[1]])
    ax.set_ylim([-1.1,1.1])
    ax.set_xlim([0,2000])
    ax.set_xticks(np.arange(0,ax.get_xlim()[1],500))
    ax.set_xticks(np.arange(0,ax.get_xlim()[1],100), minor=True)
    
    
    sim = (pred*targ).sum() / np.linalg.norm(pred) / np.linalg.norm(targ)
    mae  = abs(pred[pred>0.05]-targ[pred>0.05]).mean()
    seq = seq
    charge = int(charge)
    ev = float(ev)
    ax.set_title(
        "Seq: %s(%d); Charge: +%d; eV: %.2f; Mod: %s; Sim=%.3f; MAE: %.4f"%(
        seq, len(seq), charge, ev, mod, sim, mae)
    )
    
    if save:
        fig.savefig("C:/Users/jsl6/Desktop/mirroplot%d_%d.jpg"%(iloc,epoch))
        plt.close()

# train(25, 100, lr_decay_start=13, lr_decay_rate=0.9, svwts='top')