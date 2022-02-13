import os
import logging
import time
import math
import argparse
import importlib
from typing import Union, Tuple
from pathlib import Path

import torch
from torchinfo import summary


class MetricMonitor:
    """Calculates and stores the average value of the metrics/loss"""

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset all the parameters to zero."""
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n: int = 1):
        """Update the value of the metrics and calculate their
        average value over the whole dataset.
        Args:
        -----
            val (float): Computed metric (per batch)
            n (int, optional): Batch size. Defaults to 1.
        """
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


# log the model and data preprocessing outputs
def get_logger(
    script_name: str,
    log_file: Union[str, None] = None,
    stream_handler: bool = True,
) -> logging.getLogger:
    """Initiate the logger to log the progress into a file.

    Args:
    -----
        script_name (str): Name of the scripts outputting the logs.
        log_file (str): Name of the log file.
        stream_handler (bool, optional): Whether or not to show logs in the
            console. Defaults to True.

    Returns:
    --------
        logging.getLogger: Logger object.
    """
    logger = logging.getLogger(name=script_name)
    logger.setLevel(logging.INFO)

    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)  # make dir. for log file
        # create handlers
        f_handler = logging.FileHandler(log_path.as_posix(), mode="w")
        # create formatters and add it to the handlers
        f_format = logging.Formatter(
            "%(asctime)s:%(name)s: %(levelname)s:%(message)s"
        )
        f_handler.setFormatter(f_format)
        # add handlers to the logger
        logger.addHandler(f_handler)

    # display the logs in console
    if stream_handler:
        s_handler = logging.StreamHandler()
        s_format = logging.Formatter("%(name)s: %(levelname)s:%(message)s")
        s_handler.setFormatter(s_format)
        logger.addHandler(s_handler)

    return logger


def as_minutes_seconds(s: int) -> str:
    m = math.floor(s / 60)
    s -= m * 60
    m, s = int(m), int(s)
    return f"{m:2d}m {s:2d}s"


def time_since(since: int, percent: float) -> str:
    now = time.time()
    elapsed = now - since
    total_estimated = elapsed / percent
    remaining = total_estimated - elapsed
    return f"{as_minutes_seconds(elapsed)} (remain {as_minutes_seconds(remaining)})"


def create_data(data_name: str):
    data_filename = data_name + "_data"
    data_class_path = "data_preprocessing." + data_filename
    data_lib = importlib.import_module(data_class_path)
    data_class = None
    _data_name = data_name.replace("_", "") + "data"
    for name, cls in data_lib.__dict__.items():
        if name.lower() == _data_name.lower():
            data_class = cls

    return data_class


def create_output_paths(
    args: argparse.Namespace, infer_mode: bool = False
) -> Tuple[str]:
    test_data_path = os.path.join(
        args.test_data_dir, f"signal_window_{args.signal_window_size}"
    )
    model_ckpt_path = os.path.join(
        args.model_ckpts, f"signal_window_{args.signal_window_size}"
    )
    if not os.path.exists(test_data_path):  # moved outside of previous `else` clause
        os.makedirs(test_data_path, exist_ok=True)
    if not os.path.exists(model_ckpt_path):  # moved outside of previous `else` clause
        os.makedirs(model_ckpt_path, exist_ok=True)
    if infer_mode:
        base_path = os.path.join(
            args.output_dir,
            f"signal_window_{args.signal_window_size}",
        )
        look_ahead_path = os.path.join(
            base_path, f"label_look_ahead_{args.label_look_ahead}"
        )
        clf_report_path = os.path.join(
            look_ahead_path, "classification_reports"
        )
        plot_path = os.path.join(look_ahead_path, "plots")
        roc_path = os.path.join(look_ahead_path, "roc")
        paths = [clf_report_path, plot_path, roc_path]
        for p in paths:
            if not os.path.exists(p):
                os.makedirs(p, exist_ok=True)
        return (
            test_data_path,
            model_ckpt_path,
            clf_report_path,
            plot_path,
            roc_path,
        )
    return test_data_path, model_ckpt_path


def get_params(model: object) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def model_details(model: object, x: torch.Tensor, input_size: tuple) -> None:
    print("\t\t\t\tMODEL SUMMARY")
    summary(model, input_size=input_size)
    print(f"Output size: {model(x).shape}")
    print(f"Model contains {get_params(model)} trainable parameters!")


def create_model(model_name: str):
    model_filename = model_name + "_model"
    model_path = "models." + model_filename
    model_lib = importlib.import_module(model_path)
    model = None
    _model_name = model_name.replace("_", "") + "model"
    for name, cls in model_lib.__dict__.items():
        if name.lower() == _model_name.lower():
            model = cls

    return model
