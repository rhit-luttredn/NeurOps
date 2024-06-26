import torch
import torch.nn as nn
from torch.nn.parameter import Parameter
from .initializations import *


"""
    A modifiable version of Linear that can increase or decrease neuron count and/or be masked
"""
class ModLinear(nn.Linear):
    def __init__(self, in_features: int, out_features: int, bias: bool = True, masked: bool = False,
                 learnable_mask: bool = False, nonlinearity = 'relu', prebatchnorm: bool = False, 
                 preflatten: bool = False, predropout: bool = False):

        super().__init__(in_features, out_features, bias)

        self.masked = masked

        if masked:
            self.mask_vector = Parameter(torch.ones(
                self.out_features), requires_grad=learnable_mask)

        if nonlinearity == 'relu':
            self.nonlinearity = nn.ReLU()
        elif nonlinearity == '':
            self.nonlinearity = nn.Identity()
        else:
            self.nonlinearity = nonlinearity

        if prebatchnorm:
            self.batchnorm = nn.BatchNorm1d(self.in_features)
        else:
            self.batchnorm = nn.Identity()
        
        if preflatten:
            self.preflatten = nn.Flatten(start_dim=1)
        else:
            self.preflatten = nn.Identity()

        if predropout:
            self.predropout = nn.Dropout(p=0.5)
        else:
            self.predropout = nn.Identity()
    
    def weight_parameters(self):
        if self.bias is not None:
            return [self.weight, self.bias]
        else:
            return [self.weight]

    def get_weights(self):
        return self.mask_vector.unsqueeze(1)*self.weight if self.masked else self.weight

    def get_biases(self):
        return self.mask_vector * self.bias if self.masked else self.bias

    def width(self, masked = True):
        if masked and self.masked:
            return torch.sum(self.mask_vector != 0).item()
        return self.out_features

    def parameter_count(self, masked: bool = False, previous_mask = None):
        count = 0
        if masked and self.masked:
            weights = self.get_weights() 
            if previous_mask is not None:
                weights = weights * previous_mask
            count += torch.sum(weights != 0).item()
            if self.bias is not None:
                count += torch.sum(self.get_biases() != 0).item()
            if not isinstance(self.batchnorm, nn.Identity) and previous_mask is not None:
                count += torch.sum(self.batchnorm.weight * previous_mask != 0).item()
                count += torch.sum(self.batchnorm.bias * previous_mask != 0).item()
        else:
            count += torch.sum(self.weight != 0).item()
            if self.bias is not None:
                count += torch.sum(self.bias != 0).item()
            if not isinstance(self.batchnorm, nn.Identity):
                count += torch.sum(self.batchnorm.weight != 0).item()
                count += torch.sum(self.batchnorm.bias != 0).item()
        return count
    
    def FLOPs_count(self, input, masked: bool = False, previous_mask = None):
        if input is None:
            return 0, None
        input = self.preflatten(input)
        input = self.predropout(input)
        FLOPs = 0
        if not isinstance(self.batchnorm, nn.Identity):
            if previous_mask is not None:
                FLOPs += torch.sum(self.batchnorm.weight * previous_mask != 0).item() * input.shape[1]
                FLOPs += torch.sum(self.batchnorm.bias * previous_mask != 0).item()
            else:
                FLOPs += torch.sum(self.batchnorm.weight != 0).item() * input.shape[1]
                FLOPs += torch.sum(self.batchnorm.bias != 0).item()
        if masked and self.masked:
            weights = self.get_weights()
            if self.bias is not None:
                biases = self.get_biases()
            else:
                biases = 0
            if previous_mask is not None:
                weights = weights * previous_mask
            FLOPs += torch.sum(weights != 0).item() * input.shape[1] + torch.sum(biases != 0).item()
        else:
            FLOPs += self.weight.shape[0] * input.shape[1] + self.bias.shape[0]
        if not isinstance(self.nonlinearity, nn.Identity):
            FLOPs += input.shape[0] * self.weight.shape[0]
        input = self(input)
        return FLOPs, input


    def forward(self, x: torch.Tensor, aux: torch.Tensor = None, old_x: torch.Tensor = None, 
                previous: nn.Module = None):
        out = nn.functional.linear(self.batchnorm(self.predropout(self.preflatten(x))), self.get_weights(),
                                                      self.get_biases())
        if aux is None:
            return self.nonlinearity(out)
        if isinstance(previous, nn.Linear):
            if len(old_x.shape) == 4:
                old_x = nn.Flatten(start_dim=1)(old_x)
            auxout =  nn.functional.linear(previous.batchnorm(old_x), aux)
        elif isinstance(previous, nn.Conv2d):
            auxout = nn.Flatten(start_dim=1)(previous.postpool(nn.functional.conv2d(previous.batchnorm(old_x), aux, 
                                              padding=previous.padding)))
        if self.masked:
            auxout = auxout * self.mask_vector.view(1, -1)
        return self.nonlinearity(out + auxout)


    """
        Mask fanin weights of neurons of this layer that have indices in fanin and fanout weights 
        of neurons of the previous layer that have indices in fanout.

        fanin: list of indices of neurons of this layer
        fanout: list of indices of neurons of the previous layer
    """

    def mask(self, fanin=[]):
        self.mask_vector.data[fanin] = 0

    """
        Unmask fanin weights of neurons of this layer that have indices in fanin and fanout weights 
        of neurons of the previous layer that have indices in fanout. If optimizer is provided, ensure
        that the optimizer state is also refreshed for a new neuron.

        fanin: list of indices of neurons of this layer
        fanout: list of indices of neurons of the previous layer
        optimizer: optimizer to reinitialize state of newly unmasked neurons
    """

    def unmask(self, fanin=[], fanout=[], optimizer=None):
        self.mask_vector.data[fanin] = 1

        if not isinstance(self.batchnorm, nn.Identity):
            if self.batchnorm.running_mean is not None:
                self.batchnorm.running_mean[fanin] = 0
                self.batchnorm.running_var[fanin] = 1
            if self.batchnorm.weight is not None:
                self.batchnorm.weight.data[fanin] = 1
                self.batchnorm.bias.data[fanin] = 0  

        if optimizer is not None:
            for group in optimizer.param_groups:
                for (_, param) in enumerate(group['params']):
                    if param is self.weight:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                opt_state_param.data[fanin] = 0
                                opt_state_param.data[:, fanout] = 0
                    if self.bias is not None and param is self.bias:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                opt_state_param.data[fanin] = 0
                    if not isinstance(self.batchnorm, nn.Identity):
                        if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                    opt_state_param.data[fanin] = 0
                        if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                    opt_state_param.data[fanin] = 0

    """
        Remove fanin weights of neurons (of this layer) in list fanin_to_prune from the layer, and 
        fanout weights of neurons (of previous layer) in list fanout_to_prune.

        fanin_to_prune: list of neurons to remove from this layer
        fanout_to_prune: list of neurons to remove from previous layer
        optimizer: optimizer to update to new shape of the layer
    """
    def prune(self, fanin_to_prune=[], fanout_to_prune=[], optimizer=None):
        fanin_to_keep = range(self.out_features)
        fanin_to_keep = [
            fitk for fitk in fanin_to_keep if fitk not in fanin_to_prune]

        fanout_to_keep = range(self.in_features)
        fanout_to_keep = [
            fotk for fotk in fanout_to_keep if fotk not in fanout_to_prune]

        with torch.no_grad():
            new_weight = Parameter(self.weight[fanin_to_keep, :][:, fanout_to_keep])
            if self.bias is not None:
                new_bias = Parameter(self.bias[fanin_to_keep])
            if self.masked:
                new_mask_vector = Parameter(self.mask_vector[fanin_to_keep], requires_grad=self.mask_vector.requires_grad)
            if not isinstance(self.batchnorm, nn.Identity) and self.batchnorm.weight is not None:
                new_batchnorm_weight = Parameter(self.batchnorm.weight[fanout_to_keep])
                new_batchnorm_bias = Parameter(self.batchnorm.bias[fanout_to_keep])

        if optimizer is not None:
            for group in optimizer.param_groups:
                for (index, param) in enumerate(group['params']):
                    if param is self.weight:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep,
                                                :][:, fanout_to_keep]
                        optimizer.state[new_weight] = optimizer.state[param]
                        group['params'][index] = new_weight
                    if self.bias is not None and param is self.bias:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep]
                        optimizer.state[new_bias] = optimizer.state[param]
                        group['params'][index] = new_bias
                    if self.masked and param is self.mask_vector:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.mask_vector.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep]
                        optimizer.state[new_mask_vector] = optimizer.state[param]
                        group['params'][index] = new_mask_vector
                    if not isinstance(self.batchnorm, nn.Identity):
                        if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                    opt_state_param.data = opt_state_param.data[fanout_to_keep]
                            optimizer.state[new_batchnorm_weight] = optimizer.state[param]
                            group['params'][index] = new_batchnorm_weight
                        if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                    opt_state_param.data = opt_state_param.data[fanout_to_keep]
                            optimizer.state[new_batchnorm_bias] = optimizer.state[param]
                            group['params'][index] = new_batchnorm_bias

        self.weight = new_weight
        if self.bias is not None:
            self.bias = new_bias
        if self.masked:
            self.mask_vector = new_mask_vector

        self.out_features = len(fanin_to_keep)
        self.in_features = len(fanout_to_keep)

        if not isinstance(self.batchnorm, nn.Identity):
            self.batchnorm.num_features = self.in_features
            if self.batchnorm.running_mean is not None:
                self.batchnorm.running_mean = self.batchnorm.running_mean[fanout_to_keep]
                self.batchnorm.running_var = self.batchnorm.running_var[fanout_to_keep]
            if self.batchnorm.weight is not None:
                self.batchnorm.weight = new_batchnorm_weight
                self.batchnorm.bias = new_batchnorm_bias

    """
        Add new_out_features new neurons to the layer (and new_in_features new inputs to the layer), with 
        weights fanin_weights and fanout_weights respectively.

        If fanin_weights and/or fanout_weights are None, they are initialized with zeros. If fanin_weights and/or fanout_weights are 1D 
        tensors, they are expanded to 2D tensors with the appropriate number of neurons/inputs. If fanin_weights and/or fanout_weights is 
        "kaiming", they are initialized with the Kaiming initialization. If fanin_weights and/or fanout_weights is 
        "iterative_orthogonalization", they are initialized via iterative orthogonalization using the input. 

        new_out_features: number of neurons to add to this layer
        new_in_features: number of inputs to add to this layer
        fanin_weights: weights of the new neurons in this layer
        fanout_weights: weights of the new inputs (adding neurons to the previous layer, so initializing their fanout weights)
        optimizer: optimizer to update to new shape of the layer
    """

    def grow(self, new_out_features=0, new_in_features=0, fanin_weights=None, fanout_weights=None, optimizer=None, activations=None):
        if new_in_features > 0:
            if fanout_weights == "kaiming":
                fanout_weights = kaiming_uniform(torch.zeros(self.out_features,self.in_features+new_in_features))[:, :new_in_features]
            elif fanout_weights == "iterative_orthogonalization":
                fanout_weights = iterative_orthogonalization(torch.zeros(self.out_features,
                                                                        self.in_features+new_in_features), 
                                                            input=activations)[:, :new_in_features]
            elif fanout_weights == "autoinit":
                fanout_weights = autoinit(torch.zeros(self.out_features,
                                                      new_in_features), 
                                          input=activations)
            elif not isinstance(fanout_weights, torch.Tensor):
                fanout_weights = torch.zeros(self.out_features, new_in_features)
            elif isinstance(fanin_weights, torch.Tensor) and len(fanout_weights.shape) == 1:
                fanout_weights = fanout_weights.unsqueeze(0)

            with torch.no_grad():
                new_weight = Parameter(
                    torch.cat((self.weight.data, fanout_weights.to(self.weight.device)), dim=1))
                if not isinstance(self.batchnorm, nn.Identity) and self.batchnorm.weight is not None:
                    new_batchnorm_weight = Parameter(
                        torch.cat((self.batchnorm.weight.data, torch.ones(new_in_features, device=self.batchnorm.weight.device)), dim=0))
                    new_batchnorm_bias = Parameter(
                        torch.cat((self.batchnorm.bias.data, torch.zeros(new_in_features, device=self.batchnorm.bias.device)), dim=0))                    

            if optimizer is not None:
                for group in optimizer.param_groups:
                    for (index, param) in enumerate(group['params']):
                        if param is self.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros_like(fanout_weights, device=opt_state_param.device)), dim=1)
                            optimizer.state[new_weight] = optimizer.state[param]
                            del optimizer.state[param]
                            group['params'][index] = new_weight
                        if not isinstance(self.batchnorm, nn.Identity):
                            if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                                for (_, opt_state_param) in optimizer.state[param].items():
                                    if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                        opt_state_param.data = torch.cat(
                                            (opt_state_param.data, torch.ones(new_in_features, device=opt_state_param.device)), dim=1)
                                optimizer.state[new_batchnorm_weight] = optimizer.state[param]
                                del optimizer.state[param]
                                group['params'][index] = new_batchnorm_weight
                            if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                                for (_, opt_state_param) in optimizer.state[param].items():
                                    if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                        opt_state_param.data = torch.cat(
                                            (opt_state_param.data, torch.ones(new_in_features, device=opt_state_param.device)), dim=1)
                                optimizer.state[new_batchnorm_bias] = optimizer.state[param]
                                del optimizer.state[param]
                                group['params'][index] = new_batchnorm_bias

            self.weight = new_weight

            self.in_features = self.in_features + new_in_features

            if not isinstance(self.batchnorm, nn.Identity):
                self.batchnorm.num_features = self.in_features
                if self.batchnorm.running_mean is not None:
                    self.batchnorm.running_mean = torch.cat(
                        (self.batchnorm.running_mean, torch.zeros(new_in_features, device=self.batchnorm.running_mean.device)))
                    self.batchnorm.running_var = torch.cat(
                        (self.batchnorm.running_var, torch.ones(new_in_features, device=self.batchnorm.running_var.device)))
                if self.batchnorm.weight is not None:
                    self.batchnorm.weight = new_batchnorm_weight
                    self.batchnorm.bias = new_batchnorm_bias

        if new_out_features > 0:
            if fanin_weights == "kaiming":
                fanin_weights = kaiming_uniform(torch.zeros(new_out_features+self.out_features, self.in_features))[:new_out_features, :]
            elif fanin_weights == "iterative_orthogonalization":
                fanin_weights = iterative_orthogonalization(torch.zeros(new_out_features+self.out_features, 
                                                                       self.in_features), 
                                                           input=activations)[:new_out_features, :]
            elif fanin_weights == "autoinit":
                fanin_weights = autoinit(torch.zeros(new_out_features, self.in_features),
                                         input=activations)
            elif fanin_weights == "north_select":
                fanin_weights = north_select(torch.zeros(new_out_features,self.in_features), self.weight.data, 
                                              input=activations)
            elif not isinstance(fanin_weights, torch.Tensor):
                fanin_weights = torch.zeros(new_out_features, self.in_features)
            elif isinstance(fanin_weights, torch.Tensor) and len(fanin_weights.shape) == 1:
                fanin_weights = fanin_weights.unsqueeze(1)

            with torch.no_grad():
                new_weight = Parameter(
                    torch.cat((self.weight.data, fanin_weights.to(self.weight.device)), dim=0))
                if self.bias is not None:
                    new_bias = Parameter(
                        torch.cat((self.bias.data, torch.zeros(new_out_features, device=self.bias.device)), dim=0))
                if self.masked:
                    new_mask_vector = Parameter(torch.cat(
                        (self.mask_vector.data, torch.ones(new_out_features, device=self.mask_vector.device))), requires_grad=self.mask_vector.requires_grad)

            if optimizer is not None:
                for group in optimizer.param_groups:
                    for (index, param) in enumerate(group['params']):
                        if param is self.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros_like(fanin_weights, device=opt_state_param.device)), dim=0)
                            optimizer.state[new_weight] = optimizer.state[param]
                            del optimizer.state[param]
                            group['params'][index] = new_weight
                        if self.bias is not None and param is self.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros(new_out_features, device=opt_state_param.device)))
                            optimizer.state[new_bias] = optimizer.state[param]
                            del optimizer.state[param]
                            group['params'][index] = new_bias
                        if self.masked and param is self.mask_vector:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.mask_vector.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.ones(new_out_features,  device=opt_state_param.device)))
                            optimizer.state[new_mask_vector] = optimizer.state[param]
                            del optimizer.state[param]
                            group['params'][index] = new_mask_vector

            self.weight = new_weight
            if self.bias is not None:
                self.bias = new_bias
            if self.masked:
                self.mask_vector = new_mask_vector

            self.out_features = self.out_features + new_out_features


"""
    A modifiable version of Conv2D that can increase or decrease channel count and/or be masked
"""
class ModConv2d(nn.Conv2d):
    def __init__(self, masked: bool = False, bias: bool = True, learnable_mask: bool = False, nonlinearity = 'relu',
                 prebatchnorm: bool = False, postpool = None, *args, **kwargs):

        super().__init__(bias=bias, *args, **kwargs)

        self.masked = masked
        self.learnable_mask = learnable_mask

        if masked:
            self.mask_vector = Parameter(torch.ones(
                self.out_channels), requires_grad=learnable_mask)

        if nonlinearity == 'relu':
            self.nonlinearity = nn.ReLU()
        elif nonlinearity == '' or nonlinearity is None:
            self.nonlinearity = nn.Identity()
        else:
            self.nonlinearity = nonlinearity

        if prebatchnorm:
            self.batchnorm = nn.BatchNorm2d(self.in_channels)
        else:
            self.batchnorm = nn.Identity()

        if postpool is not None:
            self.postpool = postpool
        else:
            self.postpool = nn.Identity()

    def weightparameters(self):
        if self.bias is not None:
            return [self.weight, self.bias]
        else:
            return [self.weight]

    def get_weights(self):
        return self.weight * self.mask_vector.view(-1,1,1,1) if self.masked else self.weight

    def get_biases(self):
        if self.bias is None:
            return None
        return self.mask_vector * self.bias if self.masked else self.bias

    def width(self, masked=True):
        if masked and self.masked:
            return torch.sum(self.mask_vector != 0).item()
        return self.out_channels
    
    def parameter_count(self, masked: bool = False, previous_mask = None):
        count = 0
        if masked and self.masked:
            count += torch.sum(self.get_weights() != 0).item()
            if self.bias is not None:
                count += torch.sum(self.get_biases() != 0).item()
            if not isinstance(self.batchnorm, nn.Identity) and previous_mask is not None:
                count += torch.sum(self.batchnorm.weight * previous_mask != 0).item()
                count += torch.sum(self.batchnorm.bias * previous_mask != 0).item()
        else:
            count += torch.sum(self.weight != 0).item()
            if self.bias is not None:
                count += torch.sum(self.bias != 0).item()
            if not isinstance(self.batchnorm, nn.Identity):
                count += torch.sum(self.batchnorm.weight != 0).item()
                count += torch.sum(self.batchnorm.bias != 0).item()
        return count

    def FLOPs_count(self, input, masked: bool = False, previous_mask = None):
        FLOPs = 0
        if not isinstance(self.batchnorm, nn.Identity):
            if previous_mask is not None:
                FLOPs += torch.sum(self.batchnorm.weight * previous_mask != 0).item() * input.shape[1]
                FLOPs += torch.sum(self.batchnorm.bias * previous_mask != 0).item()
            else:
                FLOPs += torch.sum(self.batchnorm.weight != 0).item() * input.shape[1]
                FLOPs += torch.sum(self.batchnorm.bias != 0).item()
        if masked and self.masked:
            weights = self.get_weights()
            if self.bias is not None:
                biases = self.get_biases()
            else:
                biases = 0
            if previous_mask is not None:
                weights = weights * previous_mask
            FLOPs += (2*torch.sum(weights != 0).item()  + torch.sum(biases != 0).item()) * input.shape[0] * input.shape[2] * input.shape[3] 
        else:
            if self.bias is not None:
                biases =  self.bias.shape[0]
            else:
                biases = 0
            FLOPs += (2*self.weight.shape[0] + biases)* input.shape[0] * input.shape[2] * input.shape[3] 
        input = self(input)
        if not isinstance(self.nonlinearity, nn.Identity):
            FLOPs += torch.numel(input)
        return FLOPs, input

    def forward(self, x: torch.Tensor, aux: torch.Tensor = None, old_x: torch.Tensor = None, previous: nn.Module = None):
        out = nn.functional.conv2d(self.batchnorm(x), self.get_weights(), self.get_biases(), self.stride, 
                                   self.padding, self.dilation, self.groups)
        if aux is None:
            return self.postpool(self.nonlinearity(out))
        stride = [(s1 + s2) if ((s1 > 1) and (s2 > 1)) else (s1 + s2 -1)
                 for s1, s2 in zip(previous.stride, self.stride)]
        padding = [p1 + p2 for p1, p2 in zip(previous.padding, self.padding)]
        return self.postpool(self.nonlinearity(out + previous.postpool(nn.functional.conv2d(previous.batchnorm(old_x), aux,  
                                                                                            padding=padding, stride=stride))))


    """
        Mask fanin weights of neurons of this layer that have indices in fanin and fanout weights 
        of neurons of the previous layer that have indices in fanout.

        fanin: list of indices of neurons of this layer
        fanout: list of indices of neurons of the previous layer
    """

    def mask(self, fanin=[]):
        self.mask_vector.data[fanin] = 0

    """
        Unmask fanin weights of channels of this layer that have indices in fanin and fanout weights 
        of channels of the previous layer that have indices in fanout. If batchnorm is used, also
        unmask batchnorm parameters. If optimizer is provided, also reinitialize optimizer state.

        fanin: list of indices of channels of this layer
        fanout: list of indices of channels of the previous layer
        optimizer: optimizer used to train the model
    """

    def unmask(self, fanin=[], fanout=[], optimizer=None):
        self.mask_vector.data[fanin] = 1

        if not isinstance(self.batchnorm, nn.Identity):
            if self.batchnorm.running_mean is not None:
                self.batchnorm.running_mean[fanout] = 0
                self.batchnorm.running_var[fanout] = 1
            if self.batchnorm.weight is not None:
                self.batchnorm.weight.data[fanout] = 1
                self.batchnorm.bias.data[fanout] = 0

        if optimizer is not None:
            for group in optimizer.param_groups:
                for (_, param) in enumerate(group['params']):
                    if param is self.weight:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                opt_state_param.data[fanout] = 0
                                opt_state_param.data[:, fanin] = 0
                    if self.bias is not None and param is self.bias:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                opt_state_param.data[fanout] = 0
                    if not isinstance(self.batchnorm, nn.Identity):
                        if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                    opt_state_param.data[fanout] = 0
                        if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                    opt_state_param.data[fanout] = 0


    """
        Remove fanin weights of channels (of this layer) in list fanin_to_prune from the layer, and 
        fanout weights of channels (of previous layer) in list fanout_to_prune.

        fanin_to_prune: list of channels to remove from this layer
        fanout_to_prune: list of channels to remove from previous layer
    """

    def prune(self, fanin_to_prune=[], fanout_to_prune=[], optimizer=None):
        fanin_to_keep = range(self.out_channels)
        fanin_to_keep = [
            fitk for fitk in fanin_to_keep if fitk not in fanin_to_prune]

        fanout_to_keep = range(self.in_channels)
        fanout_to_keep = [
            fotk for fotk in fanout_to_keep if fotk not in fanout_to_prune]

        with torch.no_grad():
            if self.masked:
                new_mask_vector = Parameter(self.mask_vector[fanin_to_keep], requires_grad=self.mask_vector.requires_grad)
            new_weight = Parameter(self.weight[fanin_to_keep, :][:, fanout_to_keep])
            if self.bias is not None:
                new_bias = Parameter(self.bias[fanin_to_keep])
            if not isinstance(self.batchnorm, nn.Identity) and self.batchnorm.weight is not None:
                new_batchnorm_weight = Parameter(self.batchnorm.weight[fanout_to_keep])
                new_batchnorm_bias = Parameter(self.batchnorm.bias[fanout_to_keep])

        if optimizer is not None:
            for group in optimizer.param_groups:
                for (index, param) in enumerate(group['params']):
                    if param is self.weight:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep,
                                                :][:, fanout_to_keep]
                        optimizer.state[new_weight] = optimizer.state[param]
                        group['params'][index] = new_weight
                    if self.bias is not None and param is self.bias:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep]
                        optimizer.state[new_bias] = optimizer.state[param]
                        group['params'][index] = new_bias
                    if self.masked and param is self.mask_vector:
                        for (_, opt_state_param) in optimizer.state[param].items():
                            if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.mask_vector.shape:
                                opt_state_param.data = opt_state_param.data[fanin_to_keep]
                        optimizer.state[new_mask_vector] = optimizer.state[param]
                        group['params'][index] = new_mask_vector
                    if not isinstance(self.batchnorm, nn.Identity):
                        if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                    opt_state_param.data = opt_state_param.data[fanout_to_keep]
                            optimizer.state[new_batchnorm_weight] = optimizer.state[param]
                            group['params'][index] = new_batchnorm_weight
                        if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                    opt_state_param.data = opt_state_param.data[fanout_to_keep]
                            optimizer.state[new_batchnorm_bias] = optimizer.state[param]
                            group['params'][index] = new_batchnorm_bias

        self.weight = new_weight
        if self.bias is not None:
            self.bias = new_bias
        if self.masked:
            self.mask_vector = new_mask_vector

        self.out_channels = len(fanin_to_keep)
        self.in_channels = len(fanout_to_keep)

        if not isinstance(self.batchnorm, nn.Identity):
            self.batchnorm.num_features = self.in_channels
            if self.batchnorm.running_mean is not None:
                self.batchnorm.running_mean = self.batchnorm.running_mean[fanout_to_keep]
                self.batchnorm.running_var = self.batchnorm.running_var[fanout_to_keep]
            if self.batchnorm.weight is not None:
                self.batchnorm.weight = new_batchnorm_weight
                self.batchnorm.bias = new_batchnorm_bias

    """
        Add new_out_channels new channels to the layer (and new_in_channels new inputs to the layer), with 
        weights fanin_weights and fanout_weights respectively.

        If fanin_weights and/or fanout_weights are None, they are initialized with zeros. If fanin_weights and/or fanout_weights are 1D 
        tensors, they are expanded to 2D tensors with the appropriate number of neurons/inputs. If fanin_weights and/or fanout_weights is 
        "kaiming", they are initialized with the Kaiming initialization. If fanin_weights and/or fanout_weights is 
        "iterative_orthogonalization", they are initialized via iterative orthogonalization using the input. 


        new_out_channels: number of channels to add to this layer
        new_in_channels: number of inputs to add to this layer
        fanin_weights: weights of the new channels
        fanout_weights: weights of the new inputs (channels of previous layer)
    """

    def grow(self, new_out_channels=0, new_in_channels=0, fanin_weights=None, fanout_weights=None, optimizer=None, 
             activations: torch.Tensor = None):
        if new_in_channels > 0:
            if fanout_weights == "kaiming":
                fanout_weights = kaiming_uniform(torch.zeros(self.out_channels,self.in_channels+new_in_channels, 
                                                            *self.kernel_size))[:, :new_in_channels]
            elif fanout_weights == "iterative_orthogonalization":
                fanout_weights = iterative_orthogonalization(torch.zeros(self.out_channels,self.in_channels+new_in_channels, 
                                                                        *self.kernel_size), 
                                                            input=activations, stride=self.stride)[:, :new_in_channels]
            elif fanout_weights == "autoinit":
                fanout_weights = autoinit(torch.zeros(self.out_channels, new_in_channels, *self.kernel_size),
                                            input=activations)
            elif not isinstance(fanout_weights, torch.Tensor):
                fanout_weights = torch.zeros(self.out_channels, new_in_channels, *self.kernel_size)
            elif isinstance(fanout_weights, torch.Tensor) and len(fanout_weights.shape) <= 2:
                fanout_weights = torch.reshape(
                    fanout_weights, (self.out_channels, new_in_channels, *self.kernel_size))
            elif isinstance(fanout_weights, torch.Tensor) and len(fanout_weights.shape) == 3:
                fanout_weights = fanout_weights.unsqueeze(0)

            with torch.no_grad():
                new_weight = Parameter(
                    torch.cat((self.weight.data, fanout_weights.to(self.weight.device)), dim=1))
                if not isinstance(self.batchnorm, nn.Identity) and self.batchnorm.weight is not None:
                    new_batchnorm_weight = Parameter(
                        torch.cat((self.batchnorm.weight.data, torch.ones(new_in_channels, device=self.batchnorm.weight.device)), dim=0))
                    new_batchnorm_bias = Parameter(
                        torch.cat((self.batchnorm.bias.data, torch.zeros(new_in_channels, device=self.batchnorm.bias.device)), dim=0))


            if optimizer is not None:
                for group in optimizer.param_groups:
                    for (index, param) in enumerate(group['params']):
                        if param is self.weight:  # note: p will automatically be updated in optimizer.param_groups
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros_like(fanout_weights, device=opt_state_param.device)), dim=1)
                            optimizer.state[new_weight] = optimizer.state[param]
                            group['params'][index] = new_weight
                        if not isinstance(self.batchnorm, nn.Identity):
                            if self.batchnorm.weight is not None and param is self.batchnorm.weight:
                                for (_, opt_state_param) in optimizer.state[param].items():
                                    if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.weight.shape:
                                        opt_state_param.data = torch.cat(
                                            (opt_state_param.data, torch.ones(new_in_channels, device=opt_state_param.device)), dim=1)
                                optimizer.state[new_batchnorm_weight] = optimizer.state[param]
                                group['params'][index] = new_batchnorm_weight
                            if self.batchnorm.bias is not None and param is self.batchnorm.bias:
                                for (_, opt_state_param) in optimizer.state[param].items():
                                    if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.batchnorm.bias.shape:
                                        opt_state_param.data = torch.cat(
                                            (opt_state_param.data, torch.ones(new_in_channels, device=opt_state_param.device)), dim=1)
                                optimizer.state[new_batchnorm_bias] = optimizer.state[param]
                                group['params'][index] = new_batchnorm_bias


            self.weight = new_weight

            self.in_channels = self.in_channels + new_in_channels

            if not isinstance(self.batchnorm, nn.Identity):
                self.batchnorm.num_features = self.in_channels
                if self.batchnorm.running_mean is not None:
                    self.batchnorm.running_mean = torch.cat(
                        (self.batchnorm.running_mean, torch.zeros(new_in_channels, device=self.batchnorm.running_mean.device)))
                    self.batchnorm.running_var = torch.cat(
                        (self.batchnorm.running_var, torch.ones(new_in_channels, device=self.batchnorm.running_var.device)))
                if self.batchnorm.weight is not None:
                    self.batchnorm.weight = new_batchnorm_weight
                    self.batchnorm.bias = new_batchnorm_bias


        if new_out_channels > 0:
            if fanin_weights == "kaiming":
                fanin_weights = kaiming_uniform(torch.zeros(new_out_channels+self.out_channels, self.in_channels, 
                                                           *self.kernel_size))[:new_out_channels]
            elif fanin_weights == "iterative_orthogonalization":
                fanin_weights = iterative_orthogonalization(torch.zeros(new_out_channels+self.out_channels,self.in_channels, 
                                                                       *self.kernel_size), 
                                                           input=activations, stride=self.stride)[:new_out_channels, :]
            elif fanin_weights == "autoinit":
                fanin_weights = autoinit(torch.zeros(new_out_channels, self.in_channels, *self.kernel_size),
                                        input=activations)
            elif fanin_weights == "north_select":
                fanin_weights = north_select(torch.zeros(new_out_channels,self.in_channels, *self.kernel_size), self.weight.data, 
                                              input=activations, stride=self.stride)
            elif not isinstance(fanin_weights, torch.Tensor):
                fanin_weights = torch.zeros(new_out_channels, self.in_channels, *self.kernel_size)
            elif isinstance(fanin_weights, torch.Tensor) and len(fanin_weights.shape) <= 2:
                fanin_weights = torch.reshape(
                    fanin_weights, (new_out_channels, self.in_channels, *self.kernel_size))
            elif isinstance(fanin_weights, torch.Tensor) and len(fanin_weights.shape) == 3:
                fanin_weights = fanin_weights.unsqueeze(1)

            new_weight = Parameter(
                torch.cat((self.weight.data, fanin_weights.to(self.weight.device)), dim=0))
            if self.bias is not None:
                new_bias = Parameter(
                    torch.cat((self.bias.data, torch.zeros(new_out_channels, device=self.bias.device)), dim=0))
            if self.masked:
                new_mask_vector = Parameter(torch.cat(
                    (self.mask_vector, torch.ones(new_out_channels, device=self.mask_vector.device))), requires_grad=self.mask_vector.requires_grad)

            if optimizer is not None:
                for group in optimizer.param_groups:
                    for (index, param) in enumerate(group['params']):
                        if param is self.weight:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.weight.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros_like(fanin_weights, device=opt_state_param.device)), dim=0)
                            optimizer.state[new_weight] = optimizer.state[param]
                            group['params'][index] = new_weight
                        if self.bias is not None and param is self.bias:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.bias.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.zeros(new_out_channels, device=opt_state_param.device)))
                            optimizer.state[new_bias] = optimizer.state[param]
                            group['params'][index] = new_bias
                        if self.masked and param is self.mask_vector:
                            for (_, opt_state_param) in optimizer.state[param].items():
                                if isinstance(opt_state_param, torch.Tensor) and opt_state_param.shape == self.mask_vector.shape:
                                    opt_state_param.data = torch.cat(
                                        (opt_state_param.data, torch.ones(new_out_channels, device=opt_state_param.device)))
                            optimizer.state[new_mask_vector] = optimizer.state[param]
                            group['params'][index] = new_mask_vector

            self.weight = new_weight
            if self.bias is not None:
                self.bias = new_bias
            if self.masked:
                self.mask_vector = new_mask_vector

            self.out_channels = self.out_channels + new_out_channels
