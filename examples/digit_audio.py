# ===========================================================================
# Should reach: > 0.9861111% on test set with default configuration
# One titan X:
# Benchmark TRAIN-batch: 0.11614914765 (s)
# Benchmark TRAIN-epoch: 5.98400415693 (s)
# Benchmark PRED-batch: 0.183033730263 (s)
# Benchmark PRED-epoch: 3.5595933524 (s)
# ===========================================================================
from __future__ import print_function, absolute_import, division

from odin.utils import ArgController

# ====== parse arguments ====== #
args = ArgController(
).add('-bk', 'backend: tensorflow or theano', 'tensorflow'
).add('-dev', 'gpu or cpu', 'gpu'
).add('-dt', 'dtype: float32 or float16', 'float32'
).add('-feat', 'feature type: mfcc, mspec, or spec', 'mspec'
# for trainign
).add('-lr', 'learning rate', 0.0001
).add('-epoch', 'number of epoch', 8
).add('-bs', 'batch size', 8
).parse()

# ====== import ====== #
import os
os.environ['ODIN'] = 'float32,%s,%s' % (args['dev'], args['bk'])

import numpy as np
np.random.seed(1208)

from odin import nnet as N, backend as K, fuel as F, stats
from odin.utils import get_modelpath, stdio, get_logpath, uuid
from odin.basic import has_roles, BIAS, WEIGHT
from odin import training

# set log path
NAME = uuid()
print("Auto generated identification:", NAME)
stdio(path=get_logpath('digit_audio_%s.log' % NAME, override=True))

# ===========================================================================
# Load dataset and some consts
# ===========================================================================
ds = F.load_digit_audio(dtype=args['dt'])
print(ds)
nb_classes = 10 # 10 digits (0-9)

# ===========================================================================
# Create feeder
# ===========================================================================
indices = np.genfromtxt(ds['indices.csv'], dtype='str', delimiter=' ')
longest_utterances = max(int(end) - int(start) - 1 for i, start, end in indices)
np.random.shuffle(indices)
n = indices.shape[0]
train = indices[:int(0.6 * n)]
valid = indices[int(0.6 * n):int(0.8 * n)]
test = indices[int(0.8 * n):]
print('Nb train:', train.shape, stats.freqcount([int(i[0][0]) for i in train]))
print('Nb valid:', valid.shape, stats.freqcount([int(i[0][0]) for i in valid]))
print('Nb test:', test.shape, stats.freqcount([int(i[0][0]) for i in test]))

# One titan X:
# Benchmark TRAIN-batch: 0.11614914765
# Benchmark TRAIN-epoch: 5.98400415693
# Benchmark PRED-batch: 0.183033730263
# Benchmark PRED-epoch: 3.5595933524
# we need a deterministic results, hence ncpu=1
train_feeder = F.Feeder(ds[args['feat']], train, ncpu=1)
test_feeder = F.Feeder(ds[args['feat']], test, ncpu=2)
valid_feeder = F.Feeder(ds[args['feat']], valid, ncpu=2)

recipes = [
    F.recipes.Name2Trans(converter_func=lambda x: int(x[0])),
    F.recipes.Normalization(
        mean=ds[args['feat'] + '_mean'],
        std=ds[args['feat'] + '_std'],
        local_normalize=False
    ),
    F.recipes.Sequencing(frame_length=longest_utterances, hop_length=1,
                         end='pad', endvalue=0,
                         transcription_transform=lambda x: x[-1]),
    F.CreateBatch()
]
train_feeder.set_recipes(recipes)
test_feeder.set_recipes(recipes)
valid_feeder.set_recipes(recipes)
print('Feature shape:', train_feeder.shape)
feat_shape = (None,) + train_feeder.shape[1:]
X = K.placeholder(shape=feat_shape, name='X')
y = K.placeholder(shape=(None,), dtype='int32', name='y')

# ===========================================================================
# Create network
# ===========================================================================
f = N.Sequence([
    # ====== CNN ====== #
    N.Dimshuffle(pattern=(0, 1, 2, 'x')),
    N.Conv(num_filters=32, filter_size=3, pad='same', strides=1,
           activation=K.linear),
    N.BatchNorm(activation=K.relu),
    N.Conv(num_filters=64, filter_size=3, pad='same', strides=1,
           activation=K.linear),
    N.BatchNorm(activation=K.relu),
    N.Pool(pool_size=2, strides=None, pad='valid', mode='max'),
    N.Flatten(outdim=3),

    # ====== RNN ====== #
    N.CudnnRNN(128, rnn_mode='lstm', num_layers=3,
               direction_mode='bidirectional'),

    # ====== Dense ====== #
    N.Flatten(outdim=2),
    # N.Dropout(level=0.2), # adding dropout does not help
    N.Dense(num_units=1024, activation=K.relu),
    N.Dense(num_units=512, activation=K.relu),
    N.Dense(num_units=nb_classes, activation=K.softmax)
], debug=True)

K.set_training(True); y_train = f(X)
K.set_training(False); y_score = f(X)

# ====== create cost ====== #
cost_train = K.mean(K.categorical_crossentropy(y_train, y))
cost_test_1 = K.mean(K.categorical_crossentropy(y_score, y))
cost_test_2 = K.mean(K.categorical_accuracy(y_score, y))
cost_test_3 = K.confusion_matrix(y_score, y, labels=range(10))

# ====== create optimizer ====== #
parameters = [p for p in f.parameters if has_roles(p, [WEIGHT, BIAS])]
optimizer = K.optimizers.RMSProp(lr=args['lr'])
updates = optimizer.get_updates(cost_train, parameters)

# ====== create function ====== #
print('Building training functions ...')
f_train = K.function([X, y], [cost_train, optimizer.norm],
                     updates=updates)
print('Building testing functions ...')
f_test = K.function([X, y], [cost_test_1, cost_test_2, cost_test_3])
print('Building predicting functions ...')
f_pred = K.function(X, y_score)

# ===========================================================================
# Build trainer
# ===========================================================================
print('Start training ...')
task = training.MainLoop(batch_size=args['bs'], seed=1208, shuffle_level=2)
task.set_save(get_modelpath(name='digit_audio_%s.ai' % NAME, override=True), f)
task.set_task(f_train, train_feeder, epoch=args['epoch'], name='train')
task.set_subtask(f_test, valid_feeder, freq=0.6, name='valid')
task.set_subtask(f_test, test_feeder, when=-1, name='test')
task.set_callback([
    training.ProgressMonitor(name='train', format='Results: {:.4f}-{:.4f}'),
    training.ProgressMonitor(name='valid', format='Results: {:.4f}-{:.4f}',
                             tracking={2: lambda x: sum(x)}),
    training.ProgressMonitor(name='test', format='Results: {:.4f}-{:.4f}'),
    training.History(),
    training.EarlyStopGeneralizationLoss('valid', threshold=5, patience=3),
    training.NaNDetector(('train', 'valid'), patience=3, rollback=True)
])
task.run()

# ====== plot the training process ====== #
task['History'].print_info()
task['History'].print_batch('train')
task['History'].print_batch('valid')
task['History'].print_epoch('test')
print('Benchmark TRAIN-batch:', task['History'].benchmark('train', 'batch_end').mean)
print('Benchmark TRAIN-epoch:', task['History'].benchmark('train', 'epoch_end').mean)
print('Benchmark PRED-batch:', task['History'].benchmark('valid', 'batch_end').mean)
print('Benchmark PRED-epoch:', task['History'].benchmark('valid', 'epoch_end').mean)
