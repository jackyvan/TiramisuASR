# Copyright 2020 Huy Le Nguyen (@usimarit)
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
import abc
import functools
import glob
import multiprocessing
import os
import random

import numpy as np
import tensorflow as tf

from .base_dataset import BaseDataset
from ..featurizers.speech_featurizers import read_raw_audio, SpeechFeaturizer
from ..featurizers.text_featurizers import TextFeaturizer
from ..utils.utils import bytestring_feature, print_one_line, read_bytes
from ..augmentations.augments import SeganAugment

AUTOTUNE = tf.data.experimental.AUTOTUNE
TFRECORD_SHARDS = 16


def to_tfrecord(path, audio, transcript):
    feature = {
        "path": bytestring_feature([path]),
        "audio": bytestring_feature([audio]),
        "transcript": bytestring_feature([transcript])
    }
    return tf.train.Example(features=tf.train.Features(feature=feature))


def write_tfrecord_file(splitted_entries):
    shard_path, entries = splitted_entries
    with tf.io.TFRecordWriter(shard_path, options='ZLIB') as out:
        for audio_file, _, transcript in entries:
            with open(audio_file, "rb") as f:
                audio = f.read()
            example = to_tfrecord(bytes(audio_file, "utf-8"), audio, bytes(transcript, "utf-8"))
            out.write(example.SerializeToString())
            print_one_line("Processed:", audio_file)
    print(f"\nCreated {shard_path}")


class ASRDataset(BaseDataset):
    def __init__(self,
                 stage: str,
                 speech_featurizer: SpeechFeaturizer,
                 text_featurizer: TextFeaturizer,
                 data_paths: list,
                 augmentations: dict = None,
                 shuffle: bool = False):
        super(ASRDataset, self).__init__(data_paths, augmentations, shuffle, stage)
        self.speech_featurizer = speech_featurizer
        self.text_featurizer = text_featurizer
        self.segan_augment = False
        # Check segan augmentation
        if any([isinstance(augment, SeganAugment) for augment in self.augmentations["before"]]):
            self.segan_augment = True

    def read_entries(self):
        lines = []
        for file_path in self.data_paths:
            print(f"Reading {file_path} ...")
            with tf.io.gfile.GFile(file_path, "r") as f:
                temp_lines = f.read().splitlines()
                # Skip the header of tsv file
                lines += temp_lines[1:]
        # The files is "\t" seperated
        lines = [line.split("\t", 2) for line in lines]
        lines = np.array(lines)
        if self.shuffle:
            np.random.shuffle(lines)  # Mix transcripts.tsv
        return lines

    def preprocess(self, audio, transcript, with_augment=False):
        signal = read_raw_audio(audio.numpy(), self.speech_featurizer.sample_rate)

        if with_augment:
            augment = random.choice([None] + self.augmentations["before"])
            if augment is not None:
                signal = augment(signal=signal, sample_rate=self.speech_featurizer.sample_rate)

        features = self.speech_featurizer.extract(signal)

        if with_augment:
            augment = random.choice([None] + self.augmentations["after"])
            if augment is not None:
                features = augment(features)

        label = self.text_featurizer.extract(transcript.numpy().decode("utf-8"))
        label_length = tf.cast(tf.shape(label)[0], tf.int32)
        pred_inp = self.text_featurizer.prepand_blank(label)
        features = tf.convert_to_tensor(features, tf.float32)
        input_length = tf.cast(tf.shape(features)[0], tf.int32)
        return features, input_length, label, label_length, pred_inp

    def process(self, dataset, batch_size):
        if self.augmentations["include_original"]:
            augmented_dataset = dataset.map(
                functools.partial(self.parse, augment=True),
                num_parallel_calls=1 if self.segan_augment else AUTOTUNE
            )
            dataset = dataset.map(functools.partial(
                self.parse, augment=False), num_parallel_calls=AUTOTUNE)
            dataset = dataset.concatenate(augmented_dataset)
        else:
            dataset = dataset.map(functools.partial(self.parse, augment=True),
                                  num_parallel_calls=1 if self.segan_augment else AUTOTUNE)
        if self.shuffle:
            dataset = dataset.shuffle(TFRECORD_SHARDS, reshuffle_each_iteration=True)

        # PADDED BATCH the dataset
        feature_dim, channel_dim = self.speech_featurizer.compute_feature_dim()

        dataset = dataset.padded_batch(
            batch_size=batch_size,
            padded_shapes=(
                tf.TensorShape([]),
                tf.TensorShape([None, feature_dim, channel_dim]),
                tf.TensorShape([]),
                tf.TensorShape([None]),
                tf.TensorShape([]),
                tf.TensorShape([None])
            ),
            padding_values=("", 0., 0, self.text_featurizer.blank,
                            0, self.text_featurizer.blank)
        )

        # PREFETCH to improve speed of input length
        dataset = dataset.prefetch(AUTOTUNE)
        return dataset

    @abc.abstractmethod
    def parse(self, *args, **kwargs):
        """
        Returns:
            path, features, input_lengths, labels, label_lengths, pred_inp
        """
        pass

    @abc.abstractmethod
    def create(self, batch_size):
        pass


class ASRTFRecordDataset(ASRDataset):
    """ Dataset for ASR using TFRecords """

    def __init__(self,
                 data_paths: list,
                 tfrecords_dir: str,
                 speech_featurizer: SpeechFeaturizer,
                 text_featurizer: TextFeaturizer,
                 stage: str,
                 augmentations: dict = None,
                 shuffle: bool = False):
        super(ASRTFRecordDataset, self).__init__(stage, speech_featurizer, text_featurizer,
                                                 data_paths, augmentations, shuffle)
        self.tfrecords_dir = tfrecords_dir
        if not os.path.exists(self.tfrecords_dir):
            os.makedirs(self.tfrecords_dir)

    def create_tfrecords(self):
        if not os.path.exists(self.tfrecords_dir):
            os.makedirs(self.tfrecords_dir)

        if glob.glob(os.path.join(self.tfrecords_dir, f"{self.stage}*.tfrecord")):
            print(f"TFRecords're already existed: {self.stage}")
            return True

        entries = self.read_entries()
        if len(entries) <= 0:
            return False

        print(f"Creating {self.stage}.tfrecord ...")

        def get_shard_path(shard_id):
            return os.path.join(self.tfrecords_dir, f"{self.stage}_{shard_id}.tfrecord")

        shards = [get_shard_path(idx) for idx in range(1, TFRECORD_SHARDS + 1)]

        splitted_entries = np.array_split(entries, TFRECORD_SHARDS)
        with multiprocessing.Pool(TFRECORD_SHARDS) as pool:
            pool.map(write_tfrecord_file, zip(shards, splitted_entries))

        return True

    def parse(self, record, augment=False):
        feature_description = {
            "path": tf.io.FixedLenFeature([], tf.string),
            "audio": tf.io.FixedLenFeature([], tf.string),
            "transcript": tf.io.FixedLenFeature([], tf.string)
        }
        example = tf.io.parse_single_example(record, feature_description)

        features, input_length, label, label_length, pred_inp = tf.py_function(
            functools.partial(self.preprocess, with_augment=augment),
            inp=[example["audio"], example["transcript"]],
            Tout=(tf.float32, tf.int32, tf.int32, tf.int32, tf.int32)
        )
        return example["path"], features, input_length, label, label_length, pred_inp

    def create(self, batch_size):
        # Create TFRecords dataset
        have_data = self.create_tfrecords()
        if not have_data:
            return None

        pattern = os.path.join(self.tfrecords_dir, f"{self.stage}*.tfrecord")
        files_ds = tf.data.Dataset.list_files(pattern)
        ignore_order = tf.data.Options()
        ignore_order.experimental_deterministic = False
        files_ds = files_ds.with_options(ignore_order)
        dataset = tf.data.TFRecordDataset(
            files_ds, compression_type='ZLIB', num_parallel_reads=AUTOTUNE)

        return self.process(dataset, batch_size)


class ASRSliceDataset(ASRDataset):
    """ Dataset for ASR using Slice """

    def parse(self, record, augment=False):
        path = record[0]
        transcript = record[-1]
        audio = tf.numpy_function(read_bytes, [path], tf.string)
        features, input_length, label, label_length, pred_inp = tf.py_function(
            functools.partial(self.preprocess, with_augment=augment),
            inp=[audio, transcript],
            Tout=(tf.float32, tf.int32, tf.int32, tf.int32, tf.int32)
        )
        return path, features, input_length, label, label_length, pred_inp

    def create(self, batch_size):
        # Create Generator dataset

        entries = self.read_entries()
        if len(entries) == 0:
            return None
        entries = np.delete(entries, 1, 1)  # Remove unused duration

        dataset = tf.data.Dataset.from_tensor_slices(entries)

        return self.process(dataset, batch_size)
