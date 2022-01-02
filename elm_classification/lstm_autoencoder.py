import os
import time
import argparse
import logging
import pickle
from typing import Tuple, Union
import warnings

warnings.filterwarnings(action="ignore")

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.transforms as transforms
import seaborn as sns
import torch
import torch.nn as nn
from sklearn import metrics

from options.train_arguments import TrainArguments
from src import utils

sns.set_theme(style="whitegrid", palette="muted", font_scale=1.25)
palette = list(sns.color_palette("muted").as_hex())
LABELS = ["no ELM", "ELM"]


def get_all_data(
    args: argparse.Namespace, logger: logging.Logger
) -> Tuple[tuple, tuple, tuple]:
    data_cls = utils.create_data(args.data_preproc)
    data_obj = data_cls(args, logger)
    train_data, valid_data, test_data = data_obj.get_data()
    return train_data, valid_data, test_data


def print_data_info(args: argparse.Namespace, data: tuple, verbose=0) -> None:
    signals = data[0]
    labels = data[1]
    allowed_indices = data[2]
    window_start = data[3]

    print(f"Signals shape: {signals.shape}")
    print(f"Labels shape: {labels.shape}")
    print(f"Allowed indices shape: {allowed_indices.shape}")
    print(f"Window start shape: {window_start.shape}")

    if verbose > 0:
        num_elms = len(window_start)
        for i_elm in range(num_elms):
            i_start = window_start[i_elm]
            if i_elm < num_elms - 1:
                i_stop = window_start[i_elm + 1] - args.signal_window_size
            else:
                i_stop = labels.size - args.signal_window_size
            print(i_start, i_stop)
            print(signals[i_start:i_stop].shape)
            print(
                labels[
                    i_start
                    + args.signal_window_size
                    - 1 : i_stop
                    + args.signal_window_size
                    - 1
                ].shape
            )


def print_arrays_shape(X: np.ndarray, y: np.ndarray, mode: str) -> None:
    print(f"X_{mode} shape: {X.shape}")
    print(f"y_{mode} shape: {y.shape}")


def temporalize(
    args: argparse.Namespace,
    signals: np.ndarray,
    labels: np.ndarray,
    allowed_indices: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    X = []
    y = []
    count = 1
    repeats = []
    for i_current in range(len(allowed_indices)):
        if i_current == 0:
            i_prev = 0
        else:
            i_prev = i_current - 1
        prev_time_idx = allowed_indices[i_prev]
        current_time_idx = allowed_indices[i_current]
        diff = current_time_idx - prev_time_idx
        if diff == 1 or diff == 0:
            repeats.append(count)
        else:
            repeats.append(count)
            count += 1
        signal_window = signals[
            current_time_idx : current_time_idx + args.signal_window_size
        ]
        label = labels[
            current_time_idx
            + args.signal_window_size
            + args.label_look_ahead
            - 1
        ]
        X.append(signal_window)
        y.append(label)
    repeats = np.array(repeats)
    X = np.array(X).reshape(-1, args.signal_window_size, 64)
    X = X.astype(np.float32)
    y = np.array(y).astype(np.uint8)
    print(f"X shape: {X.shape}")
    print(f"y shape: {y.shape}")
    print(f"Repeats shape: {repeats.shape}")
    return X, y, repeats


def make_tensors(
    X: Union[np.ndarray, torch.Tensor], y: Union[np.ndarray, torch.Tensor]
) -> Tuple[torch.Tensor, torch.Tensor]:
    X = torch.as_tensor(X, dtype=torch.float32)
    y = torch.as_tensor(y, dtype=torch.long)

    return X, y


def create_tensor_dataset(
    X: Union[np.ndarray, torch.Tensor], y: Union[np.ndarray, torch.Tensor]
) -> torch.utils.data.Dataset:
    X, y = make_tensors(X, y)
    dataset = torch.utils.data.TensorDataset(X, y)
    return dataset


class FCAutoencoder(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
        input_features: int = 1024,
        num_nodes: list = [128, 32],
        dropout: float = 0.3,
    ) -> None:
        super(FCAutoencoder, self).__init__()
        self.args = args
        self.num_nodes = num_nodes
        self.fc1 = nn.Linear(
            in_features=input_features, out_features=num_nodes[0]
        )
        self.fc2 = nn.Linear(
            in_features=num_nodes[0], out_features=num_nodes[1]
        )
        self.fc3 = nn.Linear(
            in_features=num_nodes[1], out_features=num_nodes[0]
        )
        self.fc4 = nn.Linear(
            in_features=num_nodes[0], out_features=input_features
        )
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, x):
        x = torch.flatten(x, 1)
        x = self.relu(self.dropout(self.fc1(x)))
        x = self.relu(self.dropout(self.fc2(x)))
        x = self.relu(self.dropout(self.fc3(x)))
        x = self.fc4(x)

        return x.view(-1, self.args.signal_window_size, 64)


class Encoder(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
        seq_len: int,
        n_features: int,
        n_layers: int,
        dropout: float,
    ):
        super(Encoder, self).__init__()
        self.args = args
        self.seq_len = seq_len
        self.n_features = n_features
        self.hidden_dim = self.args.hidden_size
        self.n_layers = n_layers
        self.dropout = dropout
        self.rnn = nn.LSTM(
            input_size=self.n_features,
            hidden_size=self.hidden_dim,
            batch_first=True,
            num_layers=self.n_layers,
            dropout=self.dropout,
        )

    def forward(self, x):
        _, (hidden, _) = self.rnn(x)
        # hidden size: (num_layers, batch_size, hidden_size)
        # hidden = hidden.reshape(
        #     batch_size, -1
        # )  # (batch_size, num_layers*hidden_size)
        hidden = (
            hidden.transpose(0, 1)
            .contiguous()
            .view(-1, self.n_layers * self.hidden_dim)
        )
        return hidden


class Decoder(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
        seq_len: int,
        n_features: int,
        n_layers: int,
        dropout: float,
    ):
        super(Decoder, self).__init__()
        self.args = args
        self.seq_len = seq_len
        self.n_features = n_features
        self.hidden_dim = self.args.hidden_size
        self.n_layers = n_layers
        self.dropout = dropout
        self.rnn = nn.LSTM(
            input_size=self.n_layers * self.hidden_dim,
            hidden_size=self.hidden_dim,
            batch_first=True,
            num_layers=self.n_layers,
            dropout=self.dropout,
        )
        self.fc = nn.Linear(self.hidden_dim, self.n_features)

    def forward(self, x):
        # x: (batch_size, num_layers*hidden_size)
        x = x.repeat(1, self.seq_len, 1)
        x = x.reshape(-1, self.seq_len, self.n_layers * self.hidden_dim)
        x, _ = self.rnn(x)  # x: (batch_size, seq_len, hidden_dim)
        x = self.fc(x)  # x: (batch_size, seq_len, n_features)

        return x


class LSTMAutoencoder(nn.Module):
    def __init__(self, encoder: Encoder, decoder: Decoder):
        super(LSTMAutoencoder, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

        assert (
            encoder.hidden_dim == decoder.hidden_dim
        ), "Hidden dimensions for both encoder and decoder must be equal"

        assert (
            encoder.n_layers == decoder.n_layers
        ), "Encoder and decoder should have same number of layers"

    def forward(self, input):
        # input = torch.unsqueeze(input, 0)
        # encode
        hidden = self.encoder(input)
        # decode
        y = self.decoder(hidden)

        return y.squeeze(0)


def train_model(
    args: argparse.Namespace,
    train_dataloader: torch.utils.data.DataLoader,
    valid_dataloader: torch.utils.data.DataLoader,
):
    model = None
    if args.model_name == "lstm_ae":
        seq_len = args.signal_window_size
        n_features = 64
        n_layers = 2
        pct = 0.3
        encoder = Encoder(
            args,
            seq_len=seq_len,
            n_features=n_features,
            n_layers=n_layers,
            dropout=pct,
        )
        decoder = Decoder(
            args,
            seq_len=seq_len,
            n_features=n_features,
            n_layers=n_layers,
            dropout=pct,
        )
        model = LSTMAutoencoder(encoder, decoder)
    elif args.model_name == "fc_ae":
        model = FCAutoencoder(args, input_features=1024)
    else:
        raise NameError("Model name is not understood.")
    model = model.to(args.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=0.5, patience=4, verbose=True
    )
    criterion = nn.L1Loss(reduction="mean")
    history = dict(train=[], valid=[])

    for epoch in range(args.n_epochs):
        model = model.train()
        ts = time.time()
        train_losses = []

        for data in train_dataloader:
            seq_in = data[0]
            seq_in = seq_in.to(args.device)

            optimizer.zero_grad()

            seq_out = model(seq_in)

            loss = criterion(seq_out, seq_in)

            loss.backward()
            optimizer.step()

            train_losses.append(loss.item())

        valid_losses = []
        model.eval()
        with torch.no_grad():
            for data in valid_dataloader:
                seq_in = data[0]
                seq_in = seq_in.to(args.device)

                seq_out = model(seq_in)

                loss = criterion(seq_out, seq_in)

                valid_losses.append(loss.item())
        te = time.time()
        train_epoch_loss = np.mean(train_losses)
        valid_epoch_loss = np.mean(valid_losses)
        scheduler.step(valid_epoch_loss)

        history["train"].append(train_epoch_loss)
        history["valid"].append(valid_epoch_loss)

        print(f"Epoch: {epoch+1}, time taken: {(te-ts):.2f} seconds")
        print(
            f"\ttrain loss: {train_epoch_loss:.5f}, valid loss: {valid_epoch_loss:.5f}"
        )

    return model, history


def plot_loss(
    args: argparse.Namespace,
    history: dict,
    base_path: str,
    show_plots: bool = True,
) -> None:
    plt.figure(figsize=(8, 6), dpi=120)
    plt.plot(history["train"], label="train", lw=2.5)
    plt.plot(history["valid"], label="valid", lw=2.5)
    plt.ylabel("Loss")
    plt.xlabel("epoch")
    plt.title("Loss over training epochs")
    plt.legend(frameon=False)
    if not args.dry_run:
        fname = f"train_valid_loss_{args.model_name}{args.filename_suffix}.png"
        fpath = os.path.join(
            base_path,
            f"signal_window_{args.signal_window_size}",
            f"label_look_ahead_{args.label_look_ahead}",
            fname,
        )
        plt.savefig(
            fpath,
            dpi=200,
        )
    if show_plots:
        plt.show()


def precision_recall_curve(
    args: argparse.Namespace,
    error_df: pd.DataFrame,
    base_path: str,
    show_plots: bool = True,
) -> None:
    # plot precision-recall curve
    precision, recall, threshold = metrics.precision_recall_curve(
        error_df.ground_truth.values, error_df.reconstruction_error.values
    )
    print(
        f"Precision, recall, thresh shape: {precision.shape}, {recall.shape}, {threshold.shape}"
    )
    plt.figure(figsize=(8, 6), dpi=120)
    plt.plot(threshold, precision[1:], label="Precision", lw=2.5)
    plt.plot(threshold, recall[1:], label="Recall", lw=2.5)
    plt.title("Precision and recall for different thresholds")
    plt.xlabel("Threshold")
    plt.ylabel("Precision/Recall")
    plt.legend(frameon=False)
    plt.tight_layout()
    if not args.dry_run:
        fname = f"{args.model_name}_precision_recall_curve{args.filename_suffix}.png"
        fpath = os.path.join(
            base_path,
            f"signal_window_{args.signal_window_size}",
            f"label_look_ahead_{args.label_look_ahead}",
            fname,
        )
        plt.savefig(
            fpath,
            dpi=200,
        )
    if show_plots:
        plt.show()


def plot_recons_loss_dist(
    args: argparse.Namespace,
    error_df: pd.DataFrame,
    threshold_val: float,
    base_path: str,
    show_plots: bool = True,
):
    # plot reconstruction error distribution for no ELMs
    fig = plt.figure(figsize=(14, 6), dpi=120)

    no_elms = error_df[error_df["ground_truth"] == 0].loc[
        :, "reconstruction_error"
    ]
    ax = fig.add_subplot(121)
    sns.distplot(no_elms, bins=50, kde=True, label="no ELMS", ax=ax)
    ax.axvline(
        threshold_val,
        zorder=10,
        ls="--",
        lw=1.25,
        c="crimson",
        label="Threshold",
    )
    trans = transforms.blended_transform_factory(
        ax.transData, ax.get_xticklabels()[0].get_transform()
    )
    ax.text(
        threshold_val,
        0,
        f"{threshold_val:.3f}",
        color="crimson",
        transform=trans,
        ha="center",
        va="center",
        fontsize=7,
    )
    ax.legend(frameon=False)

    # plot reconstruction error for ELMS
    elms = error_df[error_df["ground_truth"] == 1].loc[
        :, "reconstruction_error"
    ]
    ax = fig.add_subplot(122)
    sns.distplot(elms, bins=50, kde=True, label="ELMS", ax=ax)
    ax.axvline(
        threshold_val,
        zorder=10,
        ls="--",
        lw=1.25,
        c="crimson",
        label="Threshold",
    )
    trans = transforms.blended_transform_factory(
        ax.transData, ax.get_xticklabels()[0].get_transform()
    )
    ax.text(
        threshold_val,
        0,
        f"{threshold_val:.3f}",
        color="crimson",
        transform=trans,
        ha="left",
        va="center",
        fontsize=7,
    )
    ax.legend(frameon=False)
    plt.suptitle("Comparison of reconstruction Error")
    plt.tight_layout()
    if not args.dry_run:
        fname = (
            f"{args.model_name}_reconstruction_error{args.filename_suffix}.png"
        )
        fpath = os.path.join(
            base_path,
            f"signal_window_{args.signal_window_size}",
            f"label_look_ahead_{args.label_look_ahead}",
            fname,
        )
        plt.savefig(
            fpath,
            dpi=200,
        )
    if show_plots:
        plt.show()


def plot_recons_loss_with_signals(
    args: argparse.Namespace,
    error_df: pd.DataFrame,
    threshold_val: float,
    plot_thresh: bool,
    base_path: str,
    show_plots: bool = True,
) -> None:
    # plot reconstruction loss with signals
    if plot_thresh:
        # groups = error_df.groupby("ground_truth")
        # fig = plt.figure(figsize=(12, 6), dpi=200)
        # ax = fig.add_subplot()
        # for (name, group), alpha in zip(groups, [1, 0.8]):
        #     ax.plot(
        #         group.index,
        #         group.reconstruction_error,
        #         marker="o",
        #         ms=3,
        #         linestyle="",
        #         label=LABELS[1] if name == 1 else LABELS[0],
        #         alpha=alpha,
        #     )
        # ax.axhline(
        #     y=threshold_val,
        #     zorder=10,
        #     ls="--",
        #     lw=1.0,
        #     c="crimson",
        #     label="Threshold",
        # )
        # ax.set_ylabel("Reconstruction Loss")
        # ax.set_xlabel("Data point index")
        # ax.set_title("Reconstruction error for different classes")
        # ax.legend(frameon=False)
        fig = plt.figure(figsize=(14, 12), dpi=120)
        classes = ["no ELM", "ELM", "Threshold"]
        class_colors = [palette[0], palette[1], "crimson"]
        for i, id in enumerate(error_df["id"].unique().tolist()):
            print(f"ID: {id}")
            df = error_df[error_df["id"] == id]
            ax = plt.subplot(4, 3, i + 1)
            df = df.reset_index(drop=True)
            indices = df.index.tolist()
            ax.scatter(
                indices,
                df.reconstruction_error,
                c=df["ground_truth"].map({0: palette[0], 1: palette[1]}),
                s=2,
                marker="o",
            )
            ax.axhline(
                y=threshold_val,
                zorder=10,
                ls="--",
                lw=1.0,
                c="crimson",
                label="Threshold",
            )
            handles = [
                plt.plot(
                    [],
                    [],
                    marker="o" if j <= 1 else "_",
                    ms=3,
                    ls="",
                    color=class_colors[j],
                    label="{:s}".format(classes[j]),
                )[0]
                for j in range(len(classes))
            ]
            legend1 = ax.legend(
                handles=handles,
                # classes,
                loc="upper left",
                fontsize=8,
                frameon=False,
            )
            ax.add_artist(legend1)
            if i in [0, 3, 6, 9]:
                ax.set_ylabel("Reconstruction Loss", fontsize=9)
            if i in [9, 10, 11]:
                ax.set_xlabel("Data point index", fontsize=9)
            ax.tick_params(axis="x", labelsize=7)
            ax.tick_params(axis="y", labelsize=7)
            ax.xaxis.grid(False)
            ax.yaxis.grid(True, lw=0.5)
            if i == 11:
                break
        plt.suptitle("Reconstruction error")
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])
        if not args.dry_run:
            fname = f"{args.model_name}_recon_error_with_threshold{args.filename_suffix}.png"
            fpath = os.path.join(
                base_path,
                f"signal_window_{args.signal_window_size}",
                f"label_look_ahead_{args.label_look_ahead}",
                fname,
            )
            plt.savefig(
                fpath,
                dpi=200,
            )
        if show_plots:
            plt.show()
    else:
        fig = plt.figure(figsize=(14, 12), dpi=120)
        classes = ["no ELM", "ELM"]
        class_colors = [palette[0], palette[1]]
        for i, id in enumerate(error_df["id"].unique().tolist()):
            print(f"ID: {id}")
            df = error_df[error_df["id"] == id]
            ax = plt.subplot(4, 3, i + 1)
            df = df.reset_index(drop=True)
            indices = df.index.tolist()
            ax.scatter(
                indices,
                df.reconstruction_error_scaled,
                c=df["ground_truth"].map({0: palette[0], 1: palette[1]}),
                s=2,
                marker="o",
            )
            handles = [
                plt.plot(
                    [],
                    [],
                    marker="o",
                    ms=3,
                    ls="",
                    color=class_colors[i],
                    label="{:s}".format(classes[i]),
                )[0]
                for i in range(len(classes))
            ]
            legend1 = ax.legend(
                handles=handles,
                # classes,
                loc="upper left",
                fontsize=8,
                frameon=False,
            )
            ax.add_artist(legend1)
            (line1,) = ax.plot(
                indices, df.ground_truth, label="ground truth", c=palette[-3]
            )
            (line2,) = ax.plot(
                indices,
                df.ch_22,
                zorder=-1,
                label="Ch:22",
                c=palette[2],
            )
            if i in [0, 3, 6, 9]:
                ax.set_ylabel("Reconstruction Loss", fontsize=9)
            if i in [9, 10, 11]:
                ax.set_xlabel("Data point index", fontsize=9)
            ax.tick_params(axis="x", labelsize=7)
            ax.tick_params(axis="y", labelsize=7)
            ax.legend(
                handles=[line1, line2],
                loc="center left",
                fontsize=8,
                frameon=False,
            )
            ax.xaxis.grid(False)
            ax.yaxis.grid(True, lw=0.5)
            if i == 11:
                break
        plt.suptitle("Reconstruction error")
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])
        if not args.dry_run:
            fname = f"{args.model_name}_recon_error_with_signals{args.filename_suffix}.png"
            fpath = os.path.join(
                base_path,
                f"signal_window_{args.signal_window_size}",
                f"label_look_ahead_{args.label_look_ahead}",
                fname,
            )
            plt.savefig(
                fpath,
                dpi=200,
            )
        if show_plots:
            plt.show()


def plot_confusion_matrix(
    args: argparse.Namespace,
    error_df: pd.DataFrame,
    base_path: str,
    show_plots: bool = True,
) -> None:
    # confusion matrix
    conf_matrix = metrics.confusion_matrix(
        error_df.ground_truth.values, error_df.predictions
    )
    plt.figure(figsize=(8, 6), dpi=100)
    sns.heatmap(
        conf_matrix, xticklabels=LABELS, yticklabels=LABELS, annot=True, fmt="d"
    )
    plt.title("Confusion Matrix")
    plt.xlabel("Predicted Label")
    plt.ylabel("True Label")
    plt.tight_layout()
    if not args.dry_run:
        png_fname = (
            f"{args.model_name}_confusion_matrix{args.filename_suffix}.png"
        )
        npy_fname = (
            f"{args.model_name}_confusion_matrix{args.filename_suffix}.npy"
        )
        specific_path = os.path.join(
            base_path,
            f"signal_window_{args.signal_window_size}",
            f"label_look_ahead_{args.label_look_ahead}",
        )
        png_fpath = os.path.join(specific_path, png_fname)
        npy_fpath = os.path.join(specific_path, npy_fname)
        plt.savefig(
            png_fpath,
            dpi=200,
        )
        with open(npy_fpath, "wb") as f:
            np.save(f, conf_matrix)
    if show_plots:
        plt.show()


def plot_metrics(
    args: argparse.Namespace,
    error_df: pd.DataFrame,
    threshold_val: float,
    show_plots: bool = True,
    base_path: str = "outputs/ts_anomaly_detection_plots",
):
    precision_recall_curve(
        args, error_df, base_path=base_path, show_plots=show_plots
    )
    plot_recons_loss_dist(
        args, error_df, threshold_val, base_path, show_plots=show_plots
    )
    # plot_recons_loss_dist(args, name, error_df, plot_log=True)
    plot_recons_loss_with_signals(
        args,
        error_df,
        threshold_val,
        plot_thresh=False,
        base_path=base_path,
        show_plots=show_plots,
    )
    plot_recons_loss_with_signals(
        args,
        error_df,
        threshold_val,
        plot_thresh=True,
        base_path=base_path,
        show_plots=show_plots,
    )
    plot_confusion_matrix(args, error_df, base_path, show_plots=show_plots)


def main(
    args: argparse.Namespace,
    logger: logging.Logger,
    show_plots: bool = True,
    base_path="outputs/ts_anomaly_detection_plots",
):
    # get model checkpoint and test data path
    test_data_path, model_ckpt_path = utils.create_output_paths(
        args, infer_mode=False
    )

    # get train and valid data
    train_data, valid_data, _ = get_all_data(args, logger)

    # reshape the train signals and print info
    (
        train_signals,
        train_labels,
        train_allowed_indices,
        _,
    ) = train_data
    train_signals = train_signals.reshape(-1, 64)
    print_data_info(args, train_data)

    # reshape the valid signals and print info
    (
        valid_signals,
        valid_labels,
        valid_allowed_indices,
        _,
    ) = valid_data
    valid_signals = valid_signals.reshape(-1, 64)
    print_data_info(args, valid_data)
    del valid_data, train_data

    # create train signals and labels suited for an RNN
    X_train, y_train, _ = temporalize(
        args, train_signals, train_labels, train_allowed_indices
    )
    print_arrays_shape(X_train, y_train, mode="train")

    # create valid signals and labels suited for an RNN
    X_valid, y_valid, repeats_valid = temporalize(
        args, valid_signals, valid_labels, valid_allowed_indices
    )
    print_arrays_shape(X_valid, y_valid, mode="valid")

    # autoencoders will only be trained on the negative classes
    X_train_y0 = X_train[y_train == 0]
    X_train_y1 = X_train[y_train == 1]
    del X_train
    y_train_y0_idx = np.where(y_train == 0)[0]
    y_train_y1_idx = np.where(y_train == 1)[0]
    y_train_y0 = y_train[y_train_y0_idx]
    y_train_y1 = y_train[y_train_y1_idx]
    del y_train

    print_arrays_shape(X_train_y0, y_train_y0, mode="train_y0")
    print_arrays_shape(X_train_y1, y_train_y1, mode="train_y1")

    X_valid_y0 = X_valid[y_valid == 0]
    X_valid_y1 = X_valid[y_valid == 1]
    y_valid_y0_idx = np.where(y_valid == 0)[0]
    y_valid_y1_idx = np.where(y_valid == 1)[0]
    y_valid_y0 = y_valid[y_valid_y0_idx]
    y_valid_y1 = y_valid[y_valid_y1_idx]

    if not args.dry_run:
        fname = os.path.join(
            test_data_path,
            f"validation_data_sws_{args.signal_window_size}_la_{args.label_look_ahead}{args.filename_suffix}.pkl",
        )
        with open(fname, "wb") as f:
            pickle.dump(
                {
                    "signals": X_valid,
                    "labels": y_valid,
                },
                f,
            )

    print_arrays_shape(X_valid_y0, y_valid_y0, mode="valid_y0")
    print_arrays_shape(X_valid_y1, y_valid_y1, mode="valid_y1")

    train_dataset = create_tensor_dataset(X_train_y0, y_train_y0)
    valid_dataset = create_tensor_dataset(X_valid_y0, y_valid_y0)
    validation_dataset = create_tensor_dataset(X_valid, y_valid)

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True,
        shuffle=False,
    )
    valid_loader = torch.utils.data.DataLoader(
        validation_dataset,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True,
        shuffle=False,
    )
    validation_loader = torch.utils.data.DataLoader(
        validation_dataset,
        batch_size=1,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=False,
        shuffle=False,
    )
    model, history = train_model(args, train_loader, valid_loader)
    threshold = np.mean(history["train"]) + 3 * np.std(history["train"])
    print(f"Threshold value: {threshold}")

    # save the model
    if not args.dry_run:
        if args.model_name in ["lstm_ae", "fc_ae"]:
            model_path = os.path.join(
                model_ckpt_path,
                f"{args.model_name}_sws_{args.signal_window_size}_la_{args.label_look_ahead}{args.filename_suffix}.pth",
            )
        else:
            raise NameError("Model name is not understood.")
        torch.save(model, model_path)
    plot_loss(args, history, base_path=base_path, show_plots=show_plots)

    # # classification
    with torch.no_grad():
        mae = []
        sequences = []
        for data in validation_loader:
            seq = data[0]
            seq = seq.to(args.device)
            sequences.append(seq[0, 0, 21].cpu().numpy().tolist())
            pred_seq = model(seq)
            loss = torch.mean(torch.abs(torch.squeeze(seq, 0) - pred_seq))
            mae.append(loss.cpu().numpy())
        mae = np.array(mae)
    error_df = pd.DataFrame(
        {
            "reconstruction_error": mae,
            "reconstruction_error_scaled": mae / np.max(mae),
            "ground_truth": y_valid.tolist(),
            "id": repeats_valid.tolist(),
            "ch_22": sequences,
        }
    )
    predictions = (error_df.reconstruction_error.values > threshold).astype(int)
    error_df["predictions"] = predictions
    print(error_df)
    plot_metrics(
        args,
        error_df,
        threshold_val=threshold,
        show_plots=show_plots,
        base_path=base_path,
    )


if __name__ == "__main__":
    # initialize the argparse and the logger
    args, parser = TrainArguments().parse(verbose=True)
    logger = utils.get_logger(script_name=__name__)
    main(args, logger, show_plots=False)