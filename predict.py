# -*- coding: utf-8 -*-
"""
Created on Thu Oct 20 08:18:08 2022

@author: jsl6
"""
import torch
from models import FlipyFlopy
from utils import DicObj, EvalObj
import matplotlib.pyplot as plt
plt.close('all')
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
PATH = "C:/Users/jsl6/Documents/Python Scripts/Pytorch/SpecPred/prosit/AIData8/"

# Instantiate DicObj
criteria = [line.strip() for line in 
            open(PATH + "input_data/ion_stats/criteria.txt")]
D = DicObj(criteria=criteria)

# Instantiate model
config = {
    line.split("\t")[0]:eval(line.split("\t")[1]) 
    for line in open(PATH+"saved_models/230203/config.tsv","r")
}
model1 = FlipyFlopy(**config, device=device)
model1.load_state_dict(
    torch.load(PATH+'saved_models/230203/ckpt_0.9274')
)

# Instantiate EvalObj
mlist = [model1]
E = EvalObj(mlist, D, enable_gpu=True)
