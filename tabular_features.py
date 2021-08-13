import os
import argparse
from typing import Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from options.test_arguments import TestArguments
from src import utils


def get_elm_df(
    args: argparse.Namespace,
    data: tuple,
) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    signals, labels, sample_indices, window_start = data
    print(f"Signals shape: {signals.shape}")
    print(f"Labels shape: {labels.shape}")
    print(f"Sample indices shape: {sample_indices.shape}")
    print(f"Window start: {window_start}")
    df = pd.DataFrame()
    df["sample_indices"] = sample_indices
    df["elm_event"] = None

    # calculate adjusted differences to incorporate `signal_window_size`
    # and `label_look_ahead`
    adjusted_diffs = (
        np.diff(window_start)
        - args.signal_window_size
        - args.label_look_ahead
        + 1
    )
    print(f"Adjusted diffs: {adjusted_diffs}")

    # label the corresponding row with the ELM event number
    elm_serial_num = np.repeat(
        np.arange(1, len(window_start)),
        repeats=adjusted_diffs,
    )
    # print(elm_serial_num.shape)
    # print(np.max(elm_serial_num))
    # print(f"Window start shape: {window_start.shape}")

    # fill the remaining dataframe with the last `window_start` index
    remaining_rows_serial_num = np.repeat(
        np.max(elm_serial_num) + 1, repeats=len(df) - len(elm_serial_num)
    )
    elm_serial_num = np.append(elm_serial_num, remaining_rows_serial_num)
    df["elm_event"] = elm_serial_num

    return df, signals, labels


def pool_features(
    args: argparse.Namespace,
    df: pd.DataFrame,
    signals: np.ndarray,
    labels: np.ndarray,
) -> pd.DataFrame:
    features_max = []
    features_avg = []
    look_ahead_labels = []
    for idx in range(len(df)):
        elm_idx = df["sample_indices"].iloc[idx]
        # if elm_idx <= start:
        signal_window = signals[elm_idx : elm_idx + args.signal_window_size]
        label = labels[
            elm_idx + args.signal_window_size + args.label_look_ahead - 1
        ].astype("int")
        maxpool = nn.MaxPool3d(
            kernel_size=(args.signal_window_size, 5, 5),
            stride=(1, 1, 1),
        )
        avgpool = nn.AvgPool3d(
            kernel_size=(args.signal_window_size, 5, 5),
            stride=(1, 1, 1),
        )
        y_max = maxpool(
            torch.tensor(signal_window).view(
                1, 1, args.signal_window_size, 8, 8
            )
        )
        y_max = y_max.view(-1)
        y_avg = avgpool(
            torch.tensor(signal_window).view(
                1, 1, args.signal_window_size, 8, 8
            )
        )
        y_avg = y_avg.view(-1)
        features_max.append(y_max.numpy().tolist())
        features_avg.append(y_avg.numpy().tolist())
        look_ahead_labels.append(label)
    df["max_pool_features"] = features_max
    df["avg_pool_features"] = features_avg
    df["label"] = look_ahead_labels
    expanded_max_df = pd.DataFrame(
        df["max_pool_features"].to_list(),
        columns=[f"max_pool_{i+1}" for i in range(len(features_max[0]))],
    )
    expanded_avg_df = pd.DataFrame(
        df["avg_pool_features"].to_list(),
        columns=[f"avg_pool_{i+1}" for i in range(len(features_avg[0]))],
    )
    df = pd.concat([df, expanded_max_df, expanded_avg_df], axis=1)
    df.drop(["max_pool_features", "avg_pool_features"], axis=1, inplace=True)
    return df


if __name__ == "__main__":
    args, parser = TestArguments().parse(verbose=True)
    utils.test_args_compat(args, parser)
    LOGGER = utils.get_logger(script_name=__name__)
    data_cls = utils.create_data(args.data_preproc)
    data_obj = data_cls(args, LOGGER)
    train_data, valid_data, test_data = data_obj.get_data()

    train_df, train_signals, train_labels = get_elm_df(args, train_data)
    train_df = pool_features(args, train_df, train_signals, train_labels)

    valid_df, valid_signals, valid_labels = get_elm_df(args, valid_data)
    valid_df = pool_features(args, valid_df, valid_signals, valid_labels)

    # test_df, test_signals, test_labels = get_elm_df(args, test_data)
    # test_df = pool_features(args, test_df, test_signals, test_labels)

    _, _, _, _, roc_dir = utils.create_output_paths(args, infer_mode=True)

    if not args.dry_run:
        train_df.to_csv(
            os.path.join(
                roc_dir, f"train_features_df_{args.label_look_ahead}.csv"
            ),
            index=False,
        )
        valid_df.to_csv(
            os.path.join(
                roc_dir, f"valid_features_df_{args.label_look_ahead}.csv"
            ),
            index=False,
        )
        # test_df.to_csv(
        #     os.path.join(
        #         roc_dir, f"test_features_df_{args.label_look_ahead}.csv"
        #     ),
        #     index=False,
        # )
    print("-" * 20)
    print(f"  Train dataframe")
    print("-" * 20)
    print(train_df.head())
    print(train_df.info())
    del train_df

    print("-" * 20)
    print(f"Validation dataframe")
    print("-" * 20)
    print(valid_df.head())
    print(valid_df.info())
    del valid_df

    # print("-" * 20)
    # print(f" Test dataframe:")
    # print("-" * 20)
    # print(test_df.head())
    # print(test_df.info())
    # del test_df