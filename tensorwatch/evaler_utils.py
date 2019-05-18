# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import torch
import math
import random
import heapq
from . import utils
from .lv_types import ImagePlotItem
from collections import OrderedDict
from itertools import groupby, islice
import operator
from typing import Callable, List, Iterable, Any, Sized, Tuple
from . import utils

def skip_k(l:Iterable[Any], k:int)->Iterable[Any]:
    """For given iterable, return only k-th items, strating from 0
    """
    for index, item in enumerate(g):
        if index % k == 0:
            yield item

def to_tuples(l:Iterable[Any], key_f=lambda x:x, val_f=lambda x:x)->Iterable[tuple]:
    """Apply functions on each item to generate tuples of key and value pairs
    """
    return ((key_f(i), val_f(i)) for i in l)

def reduce(l:Iterable[Any], key_f=lambda x:x, val_f=lambda x:x, 
           reducer:Callable[[List[Any]],Any]=None)->Iterable[tuple]:
    """Group values by key and then apply reducer on each group
    """

    # extract key, value pairs
    tuples = to_tuples(l, key_f, val_f)

    # we must sort the keys for groupby
    tuples = sorted(tuples, key=operator.itemgetter(0))
    tuples = list(tuples)
    # group by key
    groups = groupby(tuples, key=operator.itemgetter(0))
    # item paert of groups includes whole tuple, separate only the values
    groups = ((key, (t1 for t0, t1 in group)) for key, group in groups)
    # run reducer on items in each group
    if reducer:
        groups = ((k, reducer(items)) for k, items in groups)
    return groups


def combine_groups(existing_groups:dict, new_groups:Iterable[tuple], 
                   sort_key, reverse=False, k=1)->None:
    """concate items in new groups with existing groups, sort items and take k items in each group
    """
    for new_group in new_groups:
        exisiting_items = existing_groups.get(new_group[0], None)
        if exisiting_items is None:
            merged = new_group[1]
        else:
            exisiting_items = list(exisiting_items)
            new_group = list(new_group)
            merged = heapq.merge(exisiting_items, new_group[1], key=sort_key, reverse=reverse)
        merged = list(merged)
        existing_groups[new_group[0]] = list(islice(merged, k))

def topk(labels:Sized, metric:Sized=None, items:Sized=None, k:int=1, 
                 order='rnd', sort_groups=False, out_f:callable=None)->Iterable[Any]:
    """Returns groups of k items for each label sorted by metric

    This function accepts batch values, for example, for image classification with batch of 100,
    we may have 100 rows and columns for input, net_output, label, loss. We want to group by label
    then for each group sort by loss and take first two value from each group. This would allow us 
    to display best two predictions for each class in a batch. If we sort by loss in reverse then
    we can display worse two predictions in a batch. The parameter of this function is columns for the batch
    i.e. in this example labels would be list of 100 values, metric would be list of 100 floats for loss per item
    and items parameter could be list of 100 tuples of (input, output)
    """

    if labels is None: # for non-classification problems
        if metric is not None:
            labels = [0] * len(metric)
        else:
            raise ValueError('Both labels and metric parameters cannot be None')
    # if target is one dimentional tensor then extract values from it
    if len(labels) > 0 and utils.has_method(labels[0], 'item') and len(labels[0].shape)==0:
        labels = [label.item() for label in labels]

    # if metric column in not supplied assume some constant for each row
    if metric is None or len(metric)==0:
        metric = [0] * len(labels)
    elif utils.has_method(metric[0], 'mean'): # if each loss is per class Pytorch torch.Tensor
        metric = [i.mean() for i in metric]


    # if items is not supplied then create list of same size as labels
    if items is None or len(items)==0:
        items = [None] * len(labels)

    # convert columns to rows
    batch = list((*i[:2], i[2:]) for i in zip(labels, metric, *items))

    # group by label, sort item in each group by metric, take k items in each group
    reverse = True if order=='dsc' else False
    key_f = (lambda i: (i[1])) if order != 'rnd' else lambda i: random.random()
    groups = reduce(batch, key_f=lambda b: b[0], # key is label
        # sort by metric and take k items
        reducer=lambda bi: islice(sorted(bi, key=key_f, reverse=reverse), k))
    
    # sort groups by key so output is consistent each time (order of digits, for example, in MNIST)
    if sort_groups:
        groups = sorted(groups.items(), key=lambda g: g[0])

    # if output extractor function is supplied then run it on each group
    if out_f:
        return (out_val for group in groups for out_val in out_f(group))
    else:
        return groups


def topk_all(batches:Iterable[Any], batch_vals:Callable[[Any], Tuple[Sized, Sized, Sized]], 
        out_f:callable, k:int=1, order='rnd', sort_groups=True)->Iterable[Any]:
    """Same as k but here we maintain top items across entire run
    """

    # this dictionary will keep the best/worst per label we have so far
    merged_groups = {}

    # this iterator will keep going on for each batch in each epoch
    for batch in batches:
        unpacker = lambda a0,a1,a2=None:(a0,a1,a2)
        metric, items, labels = unpacker(*batch_vals(batch))

        # first run top in class for batch
        groups = topk(labels, metric, items, k=k, order=order,
                              sort_groups=False)

        # update best/worst we have seen so far by sorting on metric
        reverse = True if order=='dsc' else False
        sort_key = (lambda g: (g[1])) if order != 'rnd' else lambda g: random.random()
        combine_groups(merged_groups, groups, sort_key=sort_key, reverse=reverse, k=k)

        # sort best/worst by label
        sorted_groups = sorted(merged_groups.items(), key=lambda g: g[0]) if sort_groups else merged_groups
        sorted_groups = list(sorted_groups)
        # output best/worst after each batch
        if out_f:
            yield (out_f(*val) for key, vals in sorted_groups \
                           for val in vals) # out_f takes label, metric, item
        else:
            yield sorted_groups


def reduce_params(model, param_reducer:callable, include_weights=True, include_bias=False):
    """aggregate weights or biases, use param_reducer to transform tensor to scaler
    """

    # TODO: handle Pytorch and TF models separately
    # get parameters for each submodule
    for i, (param_group_name, param_group) in enumerate(model.named_parameters()):
        if param_group.requires_grad:
            is_bias = 'bias' in param_group_name
            if (include_weights and not is_bias) or (include_bias and is_bias):
                yield i, param_reducer(param_group), param_group_name


def image_class_outf(label, metric, item): 
    """item is assumed to be (input_image, logits, ....)
    """
    net_input = item[0].data.cpu().numpy()
    # turn log-probabilities in to (max log-probability, class ID)
    net_output = torch.max(item[1],0)
    # return image, text
    return ImagePlotItem((net_input,), title="Label:{},Prob:{:.2f},Pred:{:.2f},Loss:{:.2f}".\
        format(label, math.exp(net_output[0].item()), net_output[1].item(), metric))


def image_image_outf(label, metric, item): 
    """item is assumed to be (Image1, Image2, ....)
    """
    return ImagePlotItem(tuple(i.data.cpu().numpy() if isinstance(i, torch.Tensor) else i for i in item), 
                         title="loss:{:.2f}".format(metric))



def grads_abs_mean(model):
    return reduce_params(model, lambda p: p.grad.abs().mean().item())
def grads_abs_sum(model):
    return reduce_params(model, lambda p: p.grad.abs().sum().item())
def weights_abs_mean(model):
    return reduce_params(model, lambda p: p.abs().mean().item())
def weights_abs_sum(model):
    return reduce_params(model, lambda p: p.abs().sum().item())

