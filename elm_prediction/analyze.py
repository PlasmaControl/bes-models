"""
Main inference and error analysis script to run inference on the test data 
using the trained model. It calculates micro and macro predictions for each ELM 
event in the test data and create metrics like confusion metrics, classification
report. It also creates (and saves) various plots such as time series plots for 
the ELM events with the ground truth and model predictions as well as the confusion
matrices for both macro and micro predictions. Using the  command line argument 
`--dry_run` will just show the plots, it will not save them.
"""
print(__doc__)
import os
import pickle
from typing import Tuple, List, Union
import argparse
import logging
from pathlib import Path

import torch
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import seaborn as sns
from sklearn import metrics
from tqdm import tqdm

try:
    from .data_preprocessing import *
    from .src import utils, dataset
    from .options.test_arguments import TestArguments
except ImportError:
    from elm_prediction.data_preprocessing import *
    from elm_prediction.src import utils, dataset
    from elm_prediction.options.test_arguments import TestArguments


sns.set_theme(style="whitegrid", palette="muted", font_scale=1.25)
palette = list(sns.color_palette("muted").as_hex())
LABELS = ["no ELM", "ELM"]


def predict(
    args: argparse.Namespace,
    model: object,
    device: torch.device,
    test_data: tuple,
) -> dict:
    """Function to create micro and macro predictions for each ELM event in the
    test data. Micro predictions are basically the model predictions calculated
    for each time step for each ELM event. Macro predictions, on the other hand,
    are calculated after dividing the micro predictions into two regions -
    `micro_predictions_pre_active_elms` and `micro_predictions_active_elms` using
    the buffer limits (75 us before and after the first time step of ELM onset).
    For both regions, a macro prediction will predict high (i.e. prediction=1)
    if atleast one micro prediction in that region predicted high. This custom
    prediction metric will put a strong restriction for a model prediction and
    will result in lot more number of false positives than false negatives for
    macro predictions.

    Args:
    -----
        args (argparse.Namespace): Argparse namespace object containing all the
            base and test arguments.
        model (object): Instance of the model used for inference.
        device (torch.device): Device where the predictions are being made.
        test_data (tuple): Tuple containing the test signals, labels, valid_indices
            and window_start_indices.

    Returns:
    --------
        Python dictionary containing the signals and true labels alongwith micro
        and macro predictions.
    """
    signals = test_data[0]
    print(f"Signals shape: {signals.shape}")
    labels = test_data[1]
    _ = test_data[2]  # sample_indices
    window_start = test_data[3]
    elm_indices = test_data[4]
    num_elms = len(window_start)
    elm_predictions = dict()
    # iterate through each ELM event
    for i_elm in range(num_elms):
        print(f"Processing elm event with start index: {window_start[i_elm]}")
        i_start = window_start[i_elm]
        if i_elm < num_elms - 1:
            i_stop = window_start[i_elm + 1] - 1
        else:
            i_stop = labels.size
        # gathering the indices for active elm events
        elm_signals = signals[i_start:i_stop, ...]
        elm_labels = labels[i_start:i_stop]
        active_elm = np.where(elm_labels > 0.0)[0]
        active_elm_start = active_elm[0]
        active_elm_lower_buffer = active_elm_start - args.truncate_buffer
        active_elm_upper_buffer = active_elm_start + args.truncate_buffer
        predictions = []
        effective_len = (
            elm_labels.size
            - args.signal_window_size
            - args.label_look_ahead
            + 1
        )
        # iterate through the each allowed time step
        for j in range(effective_len):
            # reshape the data accroding to the data preprocessing technique
            if args.data_preproc == "gradient":
                input_signals = np.array(
                    elm_signals[j : j + args.signal_window_size, :, :].reshape(
                        [1, args.signal_window_size, 8, 8, 6]
                    ),
                    dtype=np.float32,
                )
                input_signals = np.transpose(
                    input_signals, axes=(0, 4, 1, 2, 3)
                )
            else:
                input_signals = np.array(
                    elm_signals[j : j + args.signal_window_size, :, :].reshape(
                        [1, 1, args.signal_window_size, 8, 8]
                    ),
                    dtype=np.float32,
                )
            input_signals = torch.as_tensor(input_signals, dtype=torch.float32)
            input_signals = input_signals.to(device)
            outputs = model(input_signals)
            predictions.append(outputs.item())
        predictions = np.array(predictions)
        elm_time = np.arange(elm_labels.size)
        # convert logits to probability
        # calculate micro predictions for each time step
        micro_predictions = (
            torch.sigmoid(torch.as_tensor(predictions, dtype=torch.float32))
            .cpu()
            .numpy()
        )
        micro_predictions = np.pad(
            micro_predictions,
            pad_width=(
                args.signal_window_size + args.label_look_ahead - 1,
                0,
            ),
            mode="constant",
            constant_values=0,
        )
        # filter labels and micro-predictions for active elm regions
        elm_labels_active_elms = elm_labels[
            active_elm_lower_buffer:active_elm_upper_buffer
        ]
        micro_predictions_active_elms = micro_predictions[
            active_elm_lower_buffer:active_elm_upper_buffer
        ]
        # filter labels and micro-predictions for non-active elm regions
        micro_predictions_pre_active_elms = micro_predictions[
            :active_elm_lower_buffer
        ]
        # calculate macro predictions for each region
        macro_predictions_active_elms = np.array(
            [np.any(micro_predictions_active_elms > 0.5).astype(int)]
        )
        macro_predictions_pre_active_elms = np.array(
            [np.any(micro_predictions_pre_active_elms > 0.5).astype(int)]
        )

        macro_labels = np.array([0, 1], dtype="int")
        macro_predictions = np.concatenate(
            [
                macro_predictions_pre_active_elms,
                macro_predictions_active_elms,
            ]
        )
        elm_time = np.arange(elm_labels.size)
        print(f"Signals shape: {elm_signals.shape}")
        print(f"Labels shape: {elm_labels.shape}")
        print(f"Time shape: {elm_time.shape}")
        elm_predictions[window_start[i_elm]] = {
            "signals": elm_signals,
            "labels": elm_labels,
            "micro_predictions": micro_predictions,
            "macro_labels": macro_labels,
            "macro_predictions": macro_predictions,
            "elm_time": elm_time,
            "elm_index": elm_indices[i_elm],
        }
    return elm_predictions


def plot(
    args: argparse.Namespace,
    elm_predictions: dict,
    plot_dir: str,
    elms: List[int],
    elm_range: str,
    n_rows: Union[int, None] = None,
    n_cols: Union[int, None] = None,
    figsize: tuple = (12, 14),
) -> None:
    """Function to plot the time series plot of the ELM events with the
    ground truth and prediction. Apart from the basic plotting arguments, it takes
    in the dictionary containing the signals, labels, and their corresponding micro
    and macro predictions.
    """
    flag = False
    fig = plt.figure(figsize=figsize)
    for i, i_elm in enumerate(elms):
        signals = elm_predictions[i_elm]["signals"]
        labels = elm_predictions[i_elm]["labels"]
        # `elm_start` and `elm_end` indices
        try:
            elm_start = np.where(labels > 0)[0][0]
            elm_end = np.where(labels > 0)[0][-1]
        # edge case when the ELM event time series is too small because of larger
        # signal window and label lookahead that it skips the `elm_start` completely
        except IndexError:
            elm_start = len(labels) - 80
            elm_end = len(labels)
            flag = True
        predictions = elm_predictions[i_elm]["micro_predictions"]
        elm_time = elm_predictions[i_elm]["elm_time"]
        print(f"ELM {i + 1} of {len(elms)} with {len(elm_time)} time points")
        if (n_rows is not None) and (n_cols is not None):
            plt.subplot(n_rows, n_cols, i + 1)
        else:
            plt.subplot(args.num_rows, args.num_cols, i + 1)
        # grab the channel 22 for different data preprocessing techniques
        if args.data_preproc == "gradient":
            plt.plot(
                elm_time,
                signals[:, 2, 6, 0] / np.max(signals),
                label="BES ch. 22",
                lw=1.25,
            )
        else:
            plt.plot(
                elm_time,
                signals[:, 2, 6] / np.max(signals),
                label="Ch. 22",
                lw=1.25,
            )
        plt.plot(
            elm_time,
            labels + 0.02,
            label="Ground truth",
            ls="-",
            lw=1.25,
        )
        plt.plot(
            elm_time,
            predictions,
            label="Prediction",
            ls="-",
            lw=1,
        )
        # edge case when the ELM event is too small, plot it in red
        if flag:
            plt.axvline(
                elm_start - args.truncate_buffer,
                ymin=0,
                ymax=0.9,
                c="r",
                ls="--",
                alpha=0.65,
                lw=1.5,
                label="Buffer limits",
            )
            plt.axvline(
                elm_start + args.truncate_buffer,
                ymin=0,
                ymax=0.9,
                c="r",
                ls="--",
                alpha=0.65,
                lw=1.5,
            )
        else:
            plt.axvline(
                elm_start - args.truncate_buffer,
                ymin=0,
                ymax=0.9,
                c="k",
                ls="--",
                alpha=0.65,
                lw=1.5,
                label="Buffer limits",
            )
            plt.axvline(
                elm_end,
                ymin=0,
                ymax=0.9,
                c="k",
                ls="--",
                alpha=0.65,
                lw=1.5,
            )
        plt.xlabel("Time (micro-s)", fontsize=10)
        plt.ylabel("Signal | label", fontsize=10)
        plt.tick_params(axis="x", labelsize=8)
        plt.tick_params(axis="y", labelsize=8)
        plt.ylim([None, 1.1])
        sns.despine(offset=10, trim=False)
        plt.legend(fontsize=7, ncol=2, frameon=False)
        plt.gca().spines["left"].set_color("lightgrey")
        plt.gca().spines["bottom"].set_color("lightgrey")
        plt.grid(axis="y")
        flag = False
    plt.suptitle(
        f"Model output, ELM index: {elm_range}",
        fontsize=20,
    )
    plt.tight_layout(rect=[0, 0.03, 1, 0.95])
    if not args.dry_run:
        fig.savefig(
            os.path.join(
                plot_dir,
                f"{args.model_name}_lookahead_{args.label_look_ahead}_{args.data_preproc}_time_series_{elm_range}{args.filename_suffix}.png",
            ),
            dpi=200,
        )
    plt.show()


def plot_all(
    args: argparse.Namespace,
    elm_predictions: dict,
    plot_dir: str,
) -> None:
    """Helper function to plot the time series plots for all the ELM events in
    the test set on multiple pages.
    """
    elm_ids = list(elm_predictions.keys())
    print('elm_ids:', elm_ids)
    n_elms = len(elm_ids)
    num_pages = n_elms // 12 + 1 if n_elms%12 > 0 else n_elms // 12

    nrows = 3
    ncols = 4
    fig, axes = plt.subplots(ncols=ncols, nrows=nrows, figsize=(ncols*4, nrows*3))

    for i_page in range(num_pages):
        elms = elm_ids[i_page * 12 : (i_page + 1) * 12]
        for i_elm, elm in enumerate(elms):
            plt.sca(axes.flat[i_elm])
            plt.cla()
            signals = elm_predictions[elm]["signals"]
            labels = elm_predictions[elm]["labels"]
            predictions = elm_predictions[elm]["micro_predictions"]
            elm_time = elm_predictions[elm]["elm_time"]
            elm_index = elm_predictions[elm]["elm_index"]
            if i_page==0 and i_elm==0:
                print('First ELM event')
                print(f'signals.shape: {signals.shape}')
                print(f'labels.shape: {labels.shape}')
                print(f'predictions.shape: {predictions.shape}')
                print(f'elm_time.shape: {elm_time.shape}')
            active_elm = np.where(labels > 0)[0]
            active_elm_start = active_elm[0]
            active_elm_end = active_elm[-1]
            # plot signal, labels, and prediction
            plt.plot(
                elm_time,
                signals[:, 2, 6] / np.max(signals[:, 2, 6]),
                label="BES ch 22",
            )
            plt.plot(
                elm_time,
                labels + 0.02,
                label="Ground truth",
            )
            plt.plot(
                elm_time,
                predictions,
                label="Prediction",
                lw=1.5,
            )
            plt.axvline(
                active_elm_start - args.truncate_buffer,
                ymin=0,
                ymax=0.9,
                c="k",
                ls="--",
                alpha=0.65,
                label="Buffer limits",
            )
            plt.axvline(
                active_elm_end,
                ymin=0,
                ymax=0.9,
                c="k",
                ls="--",
                alpha=0.65,
            )
            plt.xlabel("Time (micro-s)", fontsize=11)
            plt.ylabel("Signal | label", fontsize=11)
            plt.tick_params(axis="x", labelsize=11)
            plt.tick_params(axis="y", labelsize=11)
            plt.ylim([None, 1.1])
            plt.legend(fontsize=9)
            plt.title(f'ELM index {elm_index}', fontsize=12)
        plt.tight_layout()
        plt.show()


def show_metrics(
    args: argparse.Namespace,
    y_true: np.ndarray,
    y_probas: np.ndarray,
    report_dir: str,
    roc_dir: str,
    plot_dir: str,
    pred_mode: str,
) -> None:
    """Show metrics like confusion matrix and classification report for both
    micro and macro predictions.

    Args:
    -----
        args (argparse.Namespace): Argparse namespace object.
        y_true (np.ndarray): True labels.
        y_probas (np.ndarray): Prediction probabilities (output of sigmoid).
        report_dir (str): Output directory path to save classification reports.
        roc_dir (str): Output directory path to save TPR, FPR and threshold arrays
            to calculate ROC curves.
        plot_dir (str): Output directory path to save confusion matrix plots.
        pred_mode (str): Whether to calculate metrics for micro or macro predictions.
    """
    if pred_mode == "micro":
        if np.array_equal(y_probas, y_probas.astype(bool)):
            raise ValueError(
                "Metrics for micro mode require micro_predictions but macro_predictions are passed."
            )
        # calculate predictions from the probabilities
        y_preds = (y_probas > args.threshold).astype(int)

        # creating a classification report
        cm = metrics.confusion_matrix(y_true, y_preds)

        # calculate the log of the confusion matrix scaled by the
        # total error (false positives + false negatives)
        # cm_log = np.log(cm)
        x, y = np.where(~np.eye(cm.shape[0], dtype=bool))
        coords = tuple(zip(x, y))
        total_error = np.sum(cm[coords])
        cm_scaled = cm / total_error

        # classification report
        cr = metrics.classification_report(y_true, y_preds, output_dict=True)
        df = pd.DataFrame(cr).transpose()
        print(f"Classification report:\n{df}")

        # ROC details
        fpr, tpr, thresh = metrics.roc_curve(y_true, y_probas)
        roc_details = pd.DataFrame()
        roc_details["fpr"] = fpr
        roc_details["tpr"] = tpr
        roc_details["threshold"] = thresh

        # plots
        fig = plt.figure(figsize=(8, 6), dpi=100)
        ax = fig.add_subplot()
        sns.heatmap(
            cm,
            xticklabels=LABELS,
            yticklabels=LABELS,
            annot=True,
            ax=ax,
            annot_kws={"size": 14},
            # fmt=".3f",
            fmt="d",
            norm=LogNorm(),
        )
        plt.setp(ax.get_yticklabels(), rotation=0)
        ax.set_xlabel("Predicted Label", fontsize=14)
        ax.set_ylabel("True Label", fontsize=14)
        ax.text(
            x=0.5,
            y=1.05,
            s="Micro predictions",
            fontsize=18,
            ha="center",
            va="bottom",
            transform=ax.transAxes,
        )
        ax.text(
            x=0.5,
            y=1.01,
            s=f"Signal window: {args.signal_window_size}, Label look ahead: {args.label_look_ahead}",
            fontsize=12,
            alpha=0.75,
            ha="center",
            va="bottom",
            transform=ax.transAxes,
        )
        plt.tight_layout()
        if not args.dry_run:
            df.to_csv(
                os.path.join(
                    report_dir,
                    f"{args.model_name}_classification_report_micro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.csv",
                ),
                index=True,
            )
            roc_details.to_csv(
                os.path.join(
                    roc_dir,
                    f"{args.model_name}_roc_details_micro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.csv",
                ),
                index=False,
            )
            fig.savefig(
                os.path.join(
                    plot_dir,
                    f"{args.model_name}_confusion_matrix_micro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.png",
                ),
                dpi=100,
            )
        plt.show()
    elif pred_mode == "macro":
        if not np.array_equal(y_probas, y_probas.astype(bool)):
            raise ValueError(
                "Metrics for macro mode require macro_predictions but micro_predictions are passed."
            )

        # creating a classification report
        cm = metrics.confusion_matrix(y_true, y_probas)
        cr = metrics.classification_report(y_true, y_probas, output_dict=True)
        df = pd.DataFrame(cr).transpose()
        print(f"Classification report:\n{df}")

        # ROC details
        fpr, tpr, thresh = metrics.roc_curve(y_true, y_probas)
        roc_details = pd.DataFrame()
        roc_details["fpr"] = fpr
        roc_details["tpr"] = tpr
        roc_details["threshold"] = thresh

        # plot
        fig = plt.figure(figsize=(8, 6))
        ax = fig.add_subplot()
        sns.heatmap(
            cm,
            annot=True,
            xticklabels=LABELS,
            yticklabels=LABELS,
            ax=ax,
            annot_kws={"size": 14},
            fmt="d",
        )
        plt.setp(ax.get_yticklabels(), rotation=0)
        ax.set_xlabel("Predicted Label", fontsize=14)
        ax.set_ylabel("True Label", fontsize=14)
        ax.text(
            x=0.5,
            y=1.05,
            s="Macro predictions",
            fontsize=18,
            ha="center",
            va="bottom",
            transform=ax.transAxes,
        )
        ax.text(
            x=0.5,
            y=1.01,
            s=f"Signal window: {args.signal_window_size}, Label look ahead: {args.label_look_ahead}",
            fontsize=12,
            alpha=0.75,
            ha="center",
            va="bottom",
            transform=ax.transAxes,
        )
        plt.tight_layout()
        if not args.dry_run:
            df.to_csv(
                os.path.join(
                    report_dir,
                    f"{args.model_name}_classification_report_macro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.csv",
                ),
                index=True,
            )
            roc_details.to_csv(
                os.path.join(
                    roc_dir,
                    f"{args.model_name}_roc_details_macro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.csv",
                ),
                index=False,
            )
            fig.savefig(
                os.path.join(
                    plot_dir,
                    f"{args.model_name}_confusion_matrix_macro_lookahead_{args.label_look_ahead}_{args.data_preproc}{args.filename_suffix}.png",
                ),
                dpi=100,
            )
        plt.show()
    else:
        raise ValueError(
            f"Expected pred_mode to be either `micro` or`macro` but {pred_mode} is passed."
        )


def model_predict(
    args: argparse.Namespace,
    logger: logging.Logger,
    model: object,
    device: torch.device,
    data: tuple,
) -> None:
    """Make predictions on the validation set to assess the model's performance
    on the test/validation set using metrics like ROC or F1-scores.
    """
    # put the model to eval mode
    model.eval()
    predictions = []
    targets = []
    # create pytorch dataset for test set
    test_dataset = dataset.ELMDataset(
        args, *data[0:4], logger=logger, phase="testing"
    )
    # dataloader
    data_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        drop_last=True,
    )
    inputs, _ = next(iter(data_loader))
    logger.info(f"Input size: {inputs.shape}")
    # iterate through the dataloader
    for images, labels in tqdm(data_loader):
        images = images.to(device)

        with torch.no_grad():
            preds = model(images)
        preds = preds.view(-1)
        predictions.append(torch.sigmoid(preds).cpu().numpy())
        targets.append(labels.cpu().numpy())
    predictions = np.concatenate(predictions)
    targets = np.concatenate(targets)
    f1_thresh = 0.35  # threshold for F1-score
    f1 = metrics.f1_score(targets, (predictions > f1_thresh).astype(int))
    # display ROC and F1-score
    logger.info(f"ROC score on test data: {metrics.roc_auc_score(targets, predictions):.4f}")
    logger.info(f"F1 score on test data: {f1:.4f}")


def get_dict_values(pred_dict: dict, mode: str):
    """Helper function to extract values from the prediction dictionary."""
    targets = []
    predictions = []
    if mode == "micro":
        for vals in pred_dict.values():
            for k, v in vals.items():
                if k == "labels":
                    targets.append(v)
                if k == "micro_predictions":
                    predictions.append(v)
        return np.concatenate(targets), np.concatenate(predictions)
    elif mode == "macro":
        for vals in pred_dict.values():
            for k, v in vals.items():
                if k == "macro_labels":
                    targets.append(v)
                if k == "macro_predictions":
                    predictions.append(v)
        return np.concatenate(targets), np.concatenate(predictions)


def main(
    args: argparse.Namespace,
) -> None:
    """Actual function encapsulating all analysis function and making inference."""

    output_dir = Path(args.output_dir)
    assert(output_dir.exists())

    LOGGER = utils.get_logger(
        script_name=__name__,
        stream_handler=True,
        log_file=(output_dir / 'analysis.log').as_posix(),
    )

    model_cls = utils.create_model_class(args.model_name)
    model = model_cls(args)

    if args.device.startswith('cuda'):
        args.device = 'cuda'
    device = torch.device(args.device)

    model = model.to(device)

    # restore paths
    test_data_file, checkpoint_file, clf_report_dir, plot_dir, roc_dir = \
        utils.create_output_paths(args, infer_mode=True)

    # load the model checkpoint
    LOGGER.info(f"  Model checkpoint: {checkpoint_file}")
    model.load_state_dict(
        torch.load(
            checkpoint_file,
            map_location=device,
        )["model"]
    )

    # restore test data
    LOGGER.info(f"  Test data file: {test_data_file}")
    with open(test_data_file, "rb") as f:
        test_data = pickle.load(f)

    signals = test_data["signals"]
    labels = test_data["labels"]
    sample_indices = test_data["sample_indices"]
    window_start = test_data["window_start"]
    elm_indices = test_data["elm_indices"]

    LOGGER.info("-------->  Test data information")
    LOGGER.info(f"  Signals shape: {signals.shape}")
    LOGGER.info(f"  Labels shape: {labels.shape}")
    LOGGER.info(f"  Sample indices shape: {sample_indices.shape}")
    LOGGER.info(f"  Window start indices: {window_start.shape}")
    LOGGER.info(f"  ELM indices: {elm_indices.shape}")

    test_data = (signals, labels, sample_indices, window_start, elm_indices)
    model_predict(args, LOGGER, model, device, test_data)

    # get prediction dictionary containing truncated signals, labels,
    # micro-/macro-predictions and elm_time
    pred_dict = predict(args, model, device, test_data)

    plot_all(args, pred_dict, plot_dir)

    # # show metrics for micro/macro predictions
    # for mode in ['micro', 'macro']:
    #     targets, predictions = get_dict_values(pred_dict, mode=mode)
    #     show_metrics(
    #         args,
    #         targets,
    #         predictions,
    #         clf_report_dir,
    #         roc_dir,
    #         plot_dir,
    #         pred_mode=mode,
    #     )


if __name__ == "__main__":
    args_file = Path('run_dir/args.pkl')
    with args_file.open('rb') as f:
        args = pickle.load(f)
    args = TestArguments().parse(verbose=True, existing_namespace=args)
    main(args)