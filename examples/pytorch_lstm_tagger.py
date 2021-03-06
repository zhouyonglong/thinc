from __future__ import print_function, division
import plac
import numpy
import time
from timeit import default_timer as timer
import dill as pickle

import spacy
from spacy.attrs import ORTH, LOWER, PREFIX, SUFFIX, SHAPE
from spacy.tokens.doc import Doc

from thinc import prefer_gpu
from thinc.i2v import Embed, HashEmbed
from thinc.api import with_flatten, wrap
from thinc.extra.wrappers import PyTorchWrapperRNN
from thinc.v2v import Model, Maxout, Softmax

from thinc.api import layerize, chain, concatenate, clone, add
from thinc.api import with_getitem, flatten_add_lengths, with_square_sequences
from thinc.neural.util import flatten_sequences, remap_ids, to_categorical
from thinc.neural.optimizers import SGD
from thinc.neural.util import get_array_module
from thinc.neural.ops import CupyOps

import torch
import torch.nn
import torch.autograd

from thinc.extra.datasets import ancora_pos_tags


def PyTorchBiLSTM(nO, nI, depth):
    model = torch.nn.LSTM(nI, nO//2, depth, bidirectional=True)
    return with_square_sequences(PyTorchWrapperRNN(model))


def FeatureExtracter(lang, attrs=[LOWER, SHAPE, PREFIX, SUFFIX], tokenized=True):
    nlp = spacy.blank(lang)
    nlp.vocab.lex_attr_getters[PREFIX] = lambda string: string[:3]
    nlp.vocab.lex_attr_getters[SUFFIX] = lambda string: string[-3:]
    def forward(texts, drop=0.):
        if tokenized:
            docs = [Doc(nlp.vocab, words) for words in texts]
        else:
            docs = [nlp(text) for text in texts]
        features = [doc.to_array(attrs) for doc in docs]
        def backward(d_features, sgd=None):
            return d_features
        return features, backward
    return layerize(forward)

epoch_train_acc = 0.
def track_progress(**context):
    model = context['model']
    dev_X = context['dev_X']
    dev_y = model.ops.flatten(context['dev_y'])
    n_train = context['n_train']
    trainer = context['trainer']
    n_dev = len(dev_y)
    epoch_times = [timer()]
    def each_epoch():
        global epoch_train_acc
        epoch_start = epoch_times[-1]
        epoch_end = timer()
        wps_train = n_train / (epoch_end-epoch_start)
        dev_start = timer()
        acc = model.evaluate(dev_X, dev_y)
        dev_end = timer()
        wps_run = n_dev / (dev_end-dev_start)
        stats = (acc, wps_train, wps_run)
        print("%.3f dev acc, %d wps train, %d wps run" % stats)
        epoch_train_acc = 0.
        epoch_times.append(timer())
    return each_epoch


def preprocess(ops, get_feats, data, nr_tag, npad=4):
    Xs, ys = zip(*data)
    Xs = [ops.asarray(x) for x in get_feats(Xs)]
    ys = [ops.asarray(to_categorical(y, nb_classes=nr_tag)) for y in ys]
    return Xs, ys


_i = 0
def debug(X, drop=0.):
    global _i
    if _i % 1000 == 0:
        print(X.mean(), X.var())
    _i += 1
    return X, lambda d, sgd: d


@plac.annotations(
    width=("Width of the hidden layers", "option", "w", int),
    vector_length=("Width of the word vectors", "option", "V", int),
    depth=("Depth of the hidden layers", "option", "d", int),
    min_batch_size=("Minimum minibatch size during training", "option", "b", int),
    max_batch_size=("Maximum minibatch size during training", "option", "B", int),
    learn_rate=("Learning rate", "option", "e", float),
    momentum=("Momentum", "option", "m", float),
    dropout=("Dropout rate", "option", "D", float),
    dropout_decay=("Dropout decay", "option", "C", float),
    nb_epoch=("Maximum passes over the training data", "option", "i", int),
    L2=("L2 regularization penalty", "option", "L", float),
)
def main(width=128, depth=1, vector_length=128,
         min_batch_size=16, max_batch_size=16, learn_rate=0.001,
         momentum=0.9, dropout=0.5, dropout_decay=1e-4,
         nb_epoch=20, L2=1e-6):
    using_gpu = prefer_gpu()
    if using_gpu:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    cfg = dict(locals())
    print(cfg)
    train_data, check_data, nr_tag = ancora_pos_tags()
    train_data = list(train_data)
    check_data = list(check_data)

    extracter = FeatureExtracter('es', attrs=[LOWER, SHAPE, PREFIX, SUFFIX])
    with Model.define_operators({'**': clone, '>>': chain, '+': add,
                                 '|': concatenate}):
        lower_case = HashEmbed(width, 100, column=0)
        shape      = HashEmbed(width//2, 200, column=1)
        prefix     = HashEmbed(width//2, 100, column=2)
        suffix     = HashEmbed(width//2, 100, column=3)

        model = (
            with_flatten(
                (lower_case | shape | prefix | suffix)
                >> Maxout(width, pieces=3)
            )
            >> PyTorchBiLSTM(width, width, depth)
            >> with_flatten(Softmax(nr_tag))
        )

    train_X, train_y = preprocess(model.ops, extracter, train_data, nr_tag)
    dev_X, dev_y = preprocess(model.ops, extracter, check_data, nr_tag)

    n_train = float(sum(len(x) for x in train_X))
    global epoch_train_acc
    with model.begin_training(train_X[:10], train_y[:10], **cfg) as (trainer, optimizer):
        trainer.each_epoch.append(track_progress(**locals()))
        trainer.batch_size = min_batch_size
        batch_size = float(min_batch_size)
        for X, y in trainer.iterate(train_X, train_y):
            yh, backprop = model.begin_update(X, drop=trainer.dropout)

            gradient = [yh[i]-y[i] for i in range(len(yh))]

            backprop(gradient, optimizer)

            trainer.batch_size = min(int(batch_size), max_batch_size)
            batch_size *= 1.001
    print(model.evaluate(dev_X, model.ops.flatten(dev_y)))
    with open('/tmp/model.pickle', 'wb') as file_:
        pickle.dump(model, file_)


if __name__ == '__main__':
    if 1:
        plac.call(main)
    else:
        import cProfile
        import pstats
        cProfile.runctx("plac.call(main)", globals(), locals(), "Profile.prof")
        s = pstats.Stats("Profile.prof")
        s.strip_dirs().sort_stats("time").print_stats()
