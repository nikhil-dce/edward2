# coding=utf-8
# Copyright 2020 The Edward2 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Variational inference for Wide ResNet 28-10 on CIFAR-10 and CIFAR-100.

This script performs variational inference with a few notable techniques:

1. Normal prior whose mean is tied at the variational posterior's. This makes
   the KL penalty only penalize the weight posterior's standard deviation and
   not its mean. The prior's standard deviation is a fixed hyperparameter.
2. Fully factorized normal variational distribution (Blundell et al., 2015).
3. Flipout for lower-variance gradients in convolutional layers and the final
   dense layer (Wen et al., 2018).
4. KL annealing (Bowman et al., 2015).
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import os
import time
from absl import app
from absl import flags
from absl import logging

import edward2 as ed
import utils  # local file import
import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds

flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('per_core_batch_size', 64, 'Batch size per TPU core/GPU.')
flags.DEFINE_float('base_learning_rate', 0.1,
                   'Base learning rate when total batch size is 128. It is '
                   'scaled by the ratio of the total batch size to 128.')
flags.DEFINE_integer('lr_warmup_epochs', 1,
                     'Number of epochs for a linear warmup to the initial '
                     'learning rate. Use 0 to do no warmup.')
flags.DEFINE_float('lr_decay_ratio', 0.2, 'Amount to decay learning rate.')
flags.DEFINE_list('lr_decay_epochs', [60, 120, 160],
                  'Epochs to decay learning rate by.')
flags.DEFINE_integer('kl_annealing_epochs', 200,
                     'Number of epoch over which to anneal the KL term to 1.')
flags.DEFINE_float('l2', 4e-4, 'L2 regularization coefficient.')
flags.DEFINE_float('prior_stddev', 0.1, 'Fixed stddev for weight prior.')
flags.DEFINE_float('stddev_init', 1e-3,
                   'Initialization of posterior standard deviation parameters.')
flags.DEFINE_enum('dataset', 'cifar10',
                  enum_values=['cifar10', 'cifar100'],
                  help='Dataset.')
# TODO(ghassen): consider adding CIFAR-100-C to TFDS.
flags.DEFINE_string('cifar100_c_path', None,
                    'Path to the TFRecords files for CIFAR-100-C. Only valid '
                    '(and required) if dataset is cifar100 and corruptions.')
flags.DEFINE_integer('corruptions_interval', 250,
                     'Number of epochs between evaluating on the corrupted '
                     'test data. Use -1 to never evaluate.')
flags.DEFINE_integer('checkpoint_interval', 25,
                     'Number of epochs between saving checkpoints. Use -1 to '
                     'never save checkpoints.')
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')
flags.DEFINE_integer('num_eval_samples', 5,
                     'Number of samples per example during evaluation. Only '
                     'used for corrupted predicitons.')
flags.DEFINE_string('output_dir', '/tmp/cifar', 'Output directory.')
flags.DEFINE_integer('train_epochs', 250, 'Number of training epochs.')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', False, 'Whether to run on GPU or otherwise TPU.')
flags.DEFINE_bool('use_bfloat16', False, 'Whether to use mixed precision.')
flags.DEFINE_integer('num_cores', 8, 'Number of TPU cores or number of GPUs.')
flags.DEFINE_string('tpu', None,
                    'Name of the TPU. Only used if use_gpu is False.')
FLAGS = flags.FLAGS

BatchNormalization = functools.partial(  # pylint: disable=invalid-name
    tf.keras.layers.BatchNormalization,
    epsilon=1e-5,  # using epsilon and momentum defaults from Torch
    momentum=0.9)
Conv2DFlipout = functools.partial(  # pylint: disable=invalid-name
    ed.layers.Conv2DFlipout,
    kernel_size=3,
    padding='same',
    use_bias=False)


class NormalKLDivergenceWithTiedMean(tf.keras.regularizers.Regularizer):
  """KL with normal prior whose mean is fixed at the variational posterior's."""

  def __init__(self, stddev=1., scale_factor=1.):
    """Constructs regularizer."""
    self.stddev = stddev
    self.scale_factor = scale_factor

  def __call__(self, x):
    """Computes regularization given an ed.Normal random variable as input."""
    if not isinstance(x, ed.RandomVariable):
      raise ValueError('Input must be an ed.RandomVariable.')
    prior = ed.Independent(
        ed.Normal(loc=x.distribution.mean(), scale=self.stddev).distribution,
        reinterpreted_batch_ndims=len(x.distribution.event_shape))
    regularization = x.distribution.kl_divergence(prior.distribution)
    return self.scale_factor * regularization

  def get_config(self):
    return {
        'stddev': self.stddev,
        'scale_factor': self.scale_factor,
    }


def basic_block(inputs, filters, strides, prior_stddev, dataset_size,
                stddev_init):
  """Basic residual block of two 3x3 convs.

  Args:
    inputs: tf.Tensor.
    filters: Number of filters for Conv2D.
    strides: Stride dimensions for Conv2D.
    prior_stddev: Fixed standard deviation for weight prior.
    dataset_size: Dataset size to properly scale the KL.
    stddev_init: float to initialize variational posterior stddev parameters.

  Returns:
    tf.Tensor.
  """
  x = inputs
  y = inputs
  y = BatchNormalization()(y)
  y = tf.keras.layers.Activation('relu')(y)
  y = Conv2DFlipout(
      filters,
      strides=strides,
      kernel_initializer=ed.initializers.TrainableHeNormal(
          stddev_initializer=tf.keras.initializers.TruncatedNormal(
              mean=np.log(np.expm1(stddev_init)), stddev=0.1)),
      kernel_regularizer=NormalKLDivergenceWithTiedMean(
          stddev=prior_stddev, scale_factor=1./dataset_size))(y)
  y = BatchNormalization()(y)
  y = tf.keras.layers.Activation('relu')(y)
  y = Conv2DFlipout(
      filters,
      strides=1,
      kernel_initializer=ed.initializers.TrainableHeNormal(
          stddev_initializer=tf.keras.initializers.TruncatedNormal(
              mean=np.log(np.expm1(stddev_init)), stddev=0.1)),
      kernel_regularizer=NormalKLDivergenceWithTiedMean(
          stddev=prior_stddev, scale_factor=1./dataset_size))(y)
  if not x.shape.is_compatible_with(y.shape):
    x = Conv2DFlipout(
        filters,
        kernel_size=1,
        strides=strides,
        kernel_initializer=ed.initializers.TrainableHeNormal(
            stddev_initializer=tf.keras.initializers.TruncatedNormal(
                mean=np.log(np.expm1(stddev_init)), stddev=0.1)),
        kernel_regularizer=NormalKLDivergenceWithTiedMean(
            stddev=prior_stddev, scale_factor=1./dataset_size))(x)
  x = tf.keras.layers.add([x, y])
  return x


def group(inputs, filters, strides, num_blocks, **kwargs):
  """Group of residual blocks."""
  x = basic_block(inputs, filters=filters, strides=strides, **kwargs)
  for _ in range(num_blocks - 1):
    x = basic_block(x, filters=filters, strides=1, **kwargs)
  return x


def wide_resnet(input_shape, depth, width_multiplier, num_classes,
                prior_stddev, dataset_size,
                stddev_init):
  """Builds Wide ResNet.

  Following Zagoruyko and Komodakis (2016), it accepts a width multiplier on the
  number of filters. Using three groups of residual blocks, the network maps
  spatial features of size 32x32 -> 16x16 -> 8x8.

  Args:
    input_shape: tf.Tensor.
    depth: Total number of convolutional layers. "n" in WRN-n-k. It differs from
      He et al. (2015)'s notation which uses the maximum depth of the network
      counting non-conv layers like dense.
    width_multiplier: Integer to multiply the number of typical filters by. "k"
      in WRN-n-k.
    num_classes: Number of output classes.
    prior_stddev: Fixed standard deviation for weight prior.
    dataset_size: Dataset size to properly scale the KL.
    stddev_init: float to initialize variational posterior stddev parameters.

  Returns:
    tf.keras.Model.
  """
  if (depth - 4) % 6 != 0:
    raise ValueError('depth should be 6n+4 (e.g., 16, 22, 28, 40).')
  num_blocks = (depth - 4) // 6
  inputs = tf.keras.layers.Input(shape=input_shape)
  x = Conv2DFlipout(
      16,
      strides=1,
      kernel_initializer=ed.initializers.TrainableHeNormal(
          stddev_initializer=tf.keras.initializers.TruncatedNormal(
              mean=np.log(np.expm1(stddev_init)), stddev=0.1)),
      kernel_regularizer=NormalKLDivergenceWithTiedMean(
          stddev=prior_stddev, scale_factor=1./dataset_size))(inputs)
  for strides, filters in zip([1, 2, 2], [16, 32, 64]):
    x = group(x,
              filters=filters * width_multiplier,
              strides=strides,
              num_blocks=num_blocks,
              prior_stddev=prior_stddev,
              dataset_size=dataset_size,
              stddev_init=stddev_init)

  x = BatchNormalization()(x)
  x = tf.keras.layers.Activation('relu')(x)
  x = tf.keras.layers.AveragePooling2D(pool_size=8)(x)
  x = tf.keras.layers.Flatten()(x)
  x = ed.layers.DenseFlipout(
      num_classes,
      kernel_initializer=ed.initializers.TrainableHeNormal(
          stddev_initializer=tf.keras.initializers.TruncatedNormal(
              mean=np.log(np.expm1(stddev_init)), stddev=0.1)),
      kernel_regularizer=NormalKLDivergenceWithTiedMean(
          stddev=prior_stddev, scale_factor=1./dataset_size))(x)
  return tf.keras.Model(inputs=inputs, outputs=x)


def main(argv):
  del argv  # unused arg
  tf.enable_v2_behavior()
  tf.io.gfile.makedirs(FLAGS.output_dir)
  logging.info('Saving checkpoints at %s', FLAGS.output_dir)
  tf.random.set_seed(FLAGS.seed)

  if FLAGS.use_gpu:
    logging.info('Use GPU')
    strategy = tf.distribute.MirroredStrategy()
  else:
    logging.info('Use TPU at %s',
                 FLAGS.tpu if FLAGS.tpu is not None else 'local')
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=FLAGS.tpu)
    tf.config.experimental_connect_to_cluster(resolver)
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.experimental.TPUStrategy(resolver)

  train_input_fn = utils.load_input_fn(
      split=tfds.Split.TRAIN,
      name=FLAGS.dataset,
      batch_size=FLAGS.per_core_batch_size,
      use_bfloat16=FLAGS.use_bfloat16)
  clean_test_input_fn = utils.load_input_fn(
      split=tfds.Split.TEST,
      name=FLAGS.dataset,
      batch_size=FLAGS.per_core_batch_size,
      use_bfloat16=FLAGS.use_bfloat16)
  train_dataset = strategy.experimental_distribute_datasets_from_function(
      train_input_fn)

  test_datasets = {
      'clean': strategy.experimental_distribute_datasets_from_function(
          clean_test_input_fn),
  }
  if FLAGS.corruptions_interval > 0:
    if FLAGS.dataset == 'cifar10':
      load_c_input_fn = utils.load_cifar10_c_input_fn
    else:
      load_c_input_fn = functools.partial(utils.load_cifar100_c_input_fn,
                                          path=FLAGS.cifar100_c_path)
    corruption_types, max_intensity = utils.load_corrupted_test_info(
        FLAGS.dataset)
    for corruption in corruption_types:
      for intensity in range(1, max_intensity + 1):
        input_fn = load_c_input_fn(
            corruption_name=corruption,
            corruption_intensity=intensity,
            batch_size=FLAGS.per_core_batch_size,
            use_bfloat16=FLAGS.use_bfloat16)
        test_datasets['{0}_{1}'.format(corruption, intensity)] = (
            strategy.experimental_distribute_datasets_from_function(input_fn))

  ds_info = tfds.builder(FLAGS.dataset).info
  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  train_dataset_size = ds_info.splits['train'].num_examples
  steps_per_epoch = train_dataset_size // batch_size
  steps_per_eval = ds_info.splits['test'].num_examples // batch_size
  num_classes = ds_info.features['label'].num_classes

  if FLAGS.use_bfloat16:
    policy = tf.keras.mixed_precision.experimental.Policy('mixed_bfloat16')
    tf.keras.mixed_precision.experimental.set_policy(policy)

  summary_writer = tf.summary.create_file_writer(
      os.path.join(FLAGS.output_dir, 'summaries'))

  with strategy.scope():
    logging.info('Building ResNet model')
    model = wide_resnet(input_shape=ds_info.features['image'].shape,
                        depth=28,
                        width_multiplier=10,
                        num_classes=num_classes,
                        prior_stddev=FLAGS.prior_stddev,
                        dataset_size=train_dataset_size,
                        stddev_init=FLAGS.stddev_init)
    logging.info('Model input shape: %s', model.input_shape)
    logging.info('Model output shape: %s', model.output_shape)
    logging.info('Model number of weights: %s', model.count_params())
    # Linearly scale learning rate and the decay epochs by vanilla settings.
    base_lr = FLAGS.base_learning_rate * batch_size / 128
    lr_decay_epochs = [(start_epoch * FLAGS.train_epochs) // 200
                       for start_epoch in FLAGS.lr_decay_epochs]
    lr_schedule = utils.LearningRateSchedule(
        steps_per_epoch,
        base_lr,
        decay_ratio=FLAGS.lr_decay_ratio,
        decay_epochs=lr_decay_epochs,
        warmup_epochs=FLAGS.lr_warmup_epochs)
    optimizer = tf.keras.optimizers.SGD(lr_schedule,
                                        momentum=0.9,
                                        nesterov=True)
    metrics = {
        'train/negative_log_likelihood': tf.keras.metrics.Mean(),
        'train/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
        'train/loss': tf.keras.metrics.Mean(),
        'train/ece': ed.metrics.ExpectedCalibrationError(
            num_classes=num_classes, num_bins=FLAGS.num_bins),
        'train/kl': tf.keras.metrics.Mean(),
        'train/kl_scale': tf.keras.metrics.Mean(),
        'test/negative_log_likelihood': tf.keras.metrics.Mean(),
        'test/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
        'test/ece': ed.metrics.ExpectedCalibrationError(
            num_classes=num_classes, num_bins=FLAGS.num_bins),
    }
    if FLAGS.corruptions_interval > 0:
      corrupt_metrics = {}
      for intensity in range(1, max_intensity + 1):
        for corruption in corruption_types:
          dataset_name = '{0}_{1}'.format(corruption, intensity)
          corrupt_metrics['test/nll_{}'.format(dataset_name)] = (
              tf.keras.metrics.Mean())
          corrupt_metrics['test/accuracy_{}'.format(dataset_name)] = (
              tf.keras.metrics.SparseCategoricalAccuracy())
          corrupt_metrics['test/ece_{}'.format(dataset_name)] = (
              ed.metrics.ExpectedCalibrationError(
                  num_classes=num_classes, num_bins=FLAGS.num_bins))

    global_step = tf.Variable(
        0,
        trainable=False,
        name='global_step',
        dtype=tf.int64,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA)
    checkpoint = tf.train.Checkpoint(model=model,
                                     optimizer=optimizer,
                                     global_step=global_step)
    latest_checkpoint = tf.train.latest_checkpoint(FLAGS.output_dir)
    initial_epoch = 0
    if latest_checkpoint:
      # checkpoint.restore must be within a strategy.scope() so that optimizer
      # slot variables are mirrored.
      checkpoint.restore(latest_checkpoint)
      logging.info('Loaded checkpoint %s', latest_checkpoint)
      initial_epoch = optimizer.iterations.numpy() // steps_per_epoch

  @tf.function
  def train_step(iterator):
    """Training StepFn."""
    def step_fn(inputs):
      """Per-Replica StepFn."""
      images, labels = inputs
      with tf.GradientTape() as tape:
        logits = model(images, training=True)
        if FLAGS.use_bfloat16:
          logits = tf.cast(logits, tf.float32)
        negative_log_likelihood = tf.reduce_mean(
            tf.keras.losses.sparse_categorical_crossentropy(labels,
                                                            logits,
                                                            from_logits=True))

        filtered_variables = []
        for var in model.trainable_variables:
          # Apply l2 on the BN parameters and bias terms. This
          # excludes only fast weight approximate posterior/prior parameters,
          # but pay caution to their naming scheme.
          if 'batch_norm' in var.name or 'bias' in var.name:
            filtered_variables.append(tf.reshape(var, (-1,)))

        l2_loss = FLAGS.l2 * 2 * tf.nn.l2_loss(
            tf.concat(filtered_variables, axis=0))
        kl = sum(model.losses)
        kl_scale = tf.cast(global_step + 1, tf.float32)
        kl_scale /= steps_per_epoch * FLAGS.kl_annealing_epochs
        kl_scale = tf.minimum(1., kl_scale)
        kl_loss = kl_scale * kl

        # Scale the loss given the TPUStrategy will reduce sum all gradients.
        loss = negative_log_likelihood + l2_loss + kl_loss
        scaled_loss = loss / strategy.num_replicas_in_sync

      grads = tape.gradient(scaled_loss, model.trainable_variables)
      optimizer.apply_gradients(zip(grads, model.trainable_variables))

      probs = tf.nn.softmax(logits)
      metrics['train/ece'].update_state(labels, probs)
      metrics['train/loss'].update_state(loss)
      metrics['train/negative_log_likelihood'].update_state(
          negative_log_likelihood)
      metrics['train/kl'].update_state(kl)
      metrics['train/kl_scale'].update_state(kl_scale)
      metrics['train/accuracy'].update_state(labels, logits)

      global_step.assign_add(1)

    strategy.experimental_run_v2(step_fn, args=(next(iterator),))

  @tf.function
  def test_step(iterator, dataset_name):
    """Evaluation StepFn."""
    def step_fn(inputs):
      """Per-Replica StepFn."""
      images, labels = inputs
      # TODO(trandustin): Use more eval samples only on corrupted predictions;
      # it's expensive but a one-time compute if scheduled post-training.
      if FLAGS.num_eval_samples > 1 and dataset_name != 'clean':
        logits = tf.stack([model(images, training=False)
                           for _ in range(FLAGS.num_eval_samples)], axis=0)
      else:
        logits = model(images, training=False)
      if FLAGS.use_bfloat16:
        logits = tf.cast(logits, tf.float32)
      probs = tf.nn.softmax(logits)
      if FLAGS.num_eval_samples > 1 and dataset_name != 'clean':
        probs = tf.reduce_mean(probs, axis=0)
      negative_log_likelihood = tf.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(labels, probs))

      if dataset_name == 'clean':
        metrics['test/negative_log_likelihood'].update_state(
            negative_log_likelihood)
        metrics['test/accuracy'].update_state(labels, probs)
        metrics['test/ece'].update_state(labels, probs)
      else:
        corrupt_metrics['test/nll_{}'.format(dataset_name)].update_state(
            negative_log_likelihood)
        corrupt_metrics['test/accuracy_{}'.format(dataset_name)].update_state(
            labels, probs)
        corrupt_metrics['test/ece_{}'.format(dataset_name)].update_state(
            labels, probs)

    strategy.experimental_run_v2(step_fn, args=(next(iterator),))

  train_iterator = iter(train_dataset)
  start_time = time.time()
  for epoch in range(initial_epoch, FLAGS.train_epochs):
    logging.info('Starting to run epoch: %s', epoch)
    for step in range(steps_per_epoch):
      train_step(train_iterator)

      current_step = epoch * steps_per_epoch + (step + 1)
      max_steps = steps_per_epoch * FLAGS.train_epochs
      time_elapsed = time.time() - start_time
      steps_per_sec = float(current_step) / time_elapsed
      eta_seconds = (max_steps - current_step) / steps_per_sec
      message = ('{:.1%} completion: epoch {:d}/{:d}. {:.1f} steps/s. '
                 'ETA: {:.0f} min. Time elapsed: {:.0f} min'.format(
                     current_step / max_steps,
                     epoch + 1,
                     FLAGS.train_epochs,
                     steps_per_sec,
                     eta_seconds / 60,
                     time_elapsed / 60))
      if step % 20 == 0:
        logging.info(message)

    datasets_to_evaluate = {'clean': test_datasets['clean']}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      datasets_to_evaluate = test_datasets
    for dataset_name, test_dataset in datasets_to_evaluate.items():
      test_iterator = iter(test_dataset)
      logging.info('Testing on dataset %s', dataset_name)
      for step in range(steps_per_eval):
        if step % 20 == 0:
          logging.info('Starting to run eval step %s of epoch: %s', step,
                       epoch)
        test_step(test_iterator, dataset_name)
      logging.info('Done with testing on %s', dataset_name)

    corrupt_results = {}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      corrupt_results = utils.aggregate_corrupt_metrics(corrupt_metrics,
                                                        corruption_types,
                                                        max_intensity)

    logging.info('Train Loss: %.4f, Accuracy: %.2f%%',
                 metrics['train/loss'].result(),
                 metrics['train/accuracy'].result() * 100)
    logging.info('Test NLL: %.4f, Accuracy: %.2f%%',
                 metrics['test/negative_log_likelihood'].result(),
                 metrics['test/accuracy'].result() * 100)
    total_results = {name: metric.result() for name, metric in metrics.items()}
    total_results.update(corrupt_results)
    with summary_writer.as_default():
      for name, result in total_results.items():
        tf.summary.scalar(name, result, step=epoch + 1)

    for metric in metrics.values():
      metric.reset_states()

    if (FLAGS.checkpoint_interval > 0 and
        (epoch + 1) % FLAGS.checkpoint_interval == 0):
      checkpoint_name = checkpoint.save(
          os.path.join(FLAGS.output_dir, 'checkpoint'))
      logging.info('Saved checkpoint to %s', checkpoint_name)

if __name__ == '__main__':
  app.run(main)
