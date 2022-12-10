import torch
import torch.nn as nn

from functools import partial
from collections import defaultdict

import layers

class ModSequential(nn.Sequential):
    def __init__(self, trackacts: bool = False, *args):
        super(ModSequential, self).__init__(*args)
        self.activations = defaultdict(torch.Tensor)
        for name, module in self.named_modules():
            if isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d):
                module.register_forward_hook(partial(self._hook, name))

    def _hook(self, name, module, input, output):
        self.activations[name] = torch.cat((self.activations[name], output.detach()), dim=0)
        if self.activations[name].shape[0] > 2*self.activations[name].shape[1]:
            self.activations[name] = self.activations[name][-2*self.activations[name].shape[1]:]
        
    def forward(self, x):
        for module in self._modules.values():
            x = module(x)
        return x

    def mask(self, layerindex: int, neurons: list = [], clearacts: bool = False):
        for i, module in enumerate(self._modules.values()):
            if i == layerindex and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.mask(neurons, [])
            elif i == layerindex+1 and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.mask([], neurons)
        if clearacts:
            for index in (layerindex+1, layerindex):
                self.activations[str(index)] = torch.Tensor()

    
    def unmask(self, layerindex: int, neurons: list = [], clearacts: bool = False):
        for i, module in enumerate(self._modules.values()):
            if i == layerindex and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.mask(neurons, [])
            elif i == layerindex+1 and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.mask([], neurons)
        if clearacts:
            for index in (layerindex+1, layerindex):
                self.activations[str(index)] = torch.Tensor()
            

    def prune(self, layerindex: int, neurons: list = [], optimizer=None, clearacts: bool = False):
        for i, module in enumerate(self._modules.values()):
            if i == layerindex and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.prune(neurons, [], optimizer=optimizer)
            elif i == layerindex+1 and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.prune([], neurons, optimizer=optimizer)
        for index in (layerindex+1, layerindex):
            if clearacts:
                self.activations[str(index)] = torch.Tensor()
            elif index == layerindex:
                neuronstokeep = range(self.activations[str(index)].shape[1])
                neuronstokeep = [
                    ntk for ntk in neurons if ntk not in neurons]
                self.activations[str(index)] = self.activations[str(index)][:, neuronstokeep]


    def grow(self, layerindex: int, newneurons=0, faninweights=None, fanoutweights=None, optimizer=None, clearacts: bool = False):
        for i, module in enumerate(self._modules.values()):
            if i == layerindex and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.grow(newneurons, 0, faninweights, optimizer=optimizer)
            elif i == layerindex+1 and (isinstance(module, layers.ModLinear) or isinstance(module, layers.ModConv2d)):
                module.grow(0, newneurons, fanoutweights, optimizer=optimizer)
        for index in (layerindex+1, layerindex):
            if clearacts:
                self.activations[str(index)] = torch.Tensor()
            elif index == layerindex:
                self.activations[str(index)] = torch.cat(
                    (self.activations[str(index)], torch.zeros(
                        self.activations[str(index)].shape[0], newneurons)), dim=1)
