"""
Data class to package BES data for training using PyTorch
"""
import os
import sys
import logging
from typing import Tuple, Callable, List

import h5py
import numpy as np
import pandas as pd
from sklearn import model_selection
import torch

# run the code from top level directory
sys.path.append("../model_tools")
from model_tools import utilities, config

# log the model and data preprocessing outputs
def get_logger(stream_handler=True):
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    # create handlers
    f_handler = logging.FileHandler(
        os.path.join(config.output_dir, "output_logs.log")
    )

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


# create the logger object
LOGGER = get_logger()


class Data:
    def __init__(
        self,
        datafile: str = None,
        fraction_validate: float = 0.2,
        fraction_test: float = 0.1,
        signal_dtype: str = "float32",
        kfold: bool = False,
        smoothen_transition: bool = False,
    ):
        """Helper class that takes care of all the data preparation steps: reading
        the HDF5 file, split all the ELM events into training, validation and test
        sets, upsample the data to reduce class imbalance and create a sample signal
        window.

        Args:
        -----
            datafile (str, optional): Path to the input datafile. Defaults to None.
            fraction_validate (float, optional): Fraction of the total data to
                be used as a validation set. Ignored when using K-fold cross-
                validation. Defaults to 0.2.
            fraction_test (float, optional): Fraction of the total data to be
                used as test set. Defaults to 0.1.
            signal_dtype (str, optional): Datatype of the signals. Defaults to "float32".
            kfold (bool, optional): Boolean showing whether to use K-fold cross-
                validation or not. Defaults to False.
            smoothen_transition (bool, optional): Boolean showing whether to smooth
                the labels so that there is a gradual transition of the labels from
                0 to 1 with respect to the input time series. Defaults to False.
        """
        self.datafile = datafile
        if self.datafile is None:
            self.datafile = os.path.join(utilities.data_dir, config.file_name)
        self.fraction_validate = fraction_validate
        self.fraction_test = fraction_test
        self.signal_dtype = signal_dtype
        self.kfold = kfold
        self.smoothen_transition = smoothen_transition
        self.max_elms = config.max_elms

        self.transition = np.linspace(0, 1, 2 * config.transition_halfwidth + 3)

    def get_data(
        self, elm_indices: np.ndarray = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Helper function to preprocess the data: reshape the input signal, use
        allowed indices to upsample the class minority labels [active ELM events].

        Args:
        -----
            elm_indices (np.ndarray, optional): ELM event indices for the corresponding
                mode. Defaults to None.

        Returns:
        --------
            Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]: Tuple containing
                original signals, correponding labels, sample indices obtained
                after upsampling and start index for each ELM event.
        """
        signals = None
        window_start = None
        elm_start = None
        elm_stop = None
        hf = None
        valid_t0 = []
        labels = []

        # get ELM indices from the data file if not provided
        if elm_indices is None:
            elm_indices, hf = self._read_file()
        else:
            _, hf = self._read_file()

        # iterate through all the ELM indices
        for elm_index in elm_indices:
            elm_key = f"{elm_index:05d}"
            elm_event = hf[elm_key]
            _signals = np.array(elm_event["signals"], dtype=self.signal_dtype)
            # transposing so that the time dimension comes forward
            _signals = np.transpose(_signals, (1, 0)).reshape(-1, 8, 8)
            _labels = np.array(elm_event["labels"], dtype=self.signal_dtype)

            # TODO: add label smoothening

            # get all the allowed indices till current time step
            indices_data = self._get_valid_indices(
                _signals=_signals,
                _labels=_labels,
                window_start_indices=window_start,
                elm_start_indices=elm_start,
                elm_stop_indices=elm_stop,
                valid_t0=valid_t0,
                labels=labels,
                signals=signals,
            )
            (
                signals,
                labels,
                valid_t0,
                window_start,
                elm_start,
                elm_stop,
            ) = indices_data

        _labels = np.array(labels)

        # valid indices for data sampling
        valid_indices = np.arange(valid_t0.size, dtype="int")
        valid_indices = valid_indices[valid_t0 == 1]

        sample_indices = self._oversample_data(
            _labels, valid_indices, elm_start, elm_stop
        )

        LOGGER.info(
            "Data tensors: signals, labels, valid_indices, sample_indices, window_start_indices"
        )
        for tensor in [
            signals,
            labels,
            valid_indices,
            sample_indices,
            window_start,
        ]:
            tmp = f"  shape {tensor.shape} dtype {tensor.dtype}"
            tmp += f" min {np.min(tensor):.3f} max {np.max(tensor):.3f}"
            if hasattr(tensor, "device"):
                tmp += f" device {tensor.device[-5:]}"
            LOGGER.info(tmp)

        hf.close()
        if hf:
            LOGGER.info("File is open")
        else:
            LOGGER.info("File is closed")
        return signals, labels, sample_indices, window_start

    def _partition_elms(
        self, max_elms: int = None, fold: int = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Partition all the ELM events into training, validation and test indices.
        Training and validation sets are created based on simple splitting with
        validation set being `fraction_validate` of the training set or by K-fold
        cross-validation.

        Args:
        -----
            max_elms (int, optional): Maximum number of ELM events to be used.
                Defaults to None (Take the entire data).
            fold (int, optional): Fold index for K-fold cross-validation. Defaults
                to None.

        Raises:
        -------
            Exception:  Throws error when `kfold` is set to True but fold index
                is not passed.

        Returns:
        --------
            Tuple[np.ndarray, np.ndarray, np.ndarray]: Tuple containing training,
                validation and test ELM indices.
        """
        # get ELM indices from datafile
        elm_index, _ = self._read_file()

        # limit the data according to the max number of events passed
        if max_elms is not None and max_elms != -1:
            LOGGER.info(f"Limiting data read to {max_elms} events.")
            n_elms = max_elms
        else:
            n_elms = len(elm_index)

        # split the data into train, validation and test sets
        training_elms, test_elms = model_selection.train_test_split(
            elm_index[:n_elms],
            test_size=self.fraction_test,
            shuffle=True,
            random_state=config.seed,
        )

        # kfold cross validation
        if self.kfold and fold is None:
            raise Exception(
                f"K-fold cross validation is passed but fold index in range [0, {config.folds}) is not specified."
            )

        if self.kfold:
            LOGGER.info("Using K-fold cross validation")
            self._kfold_cross_val(training_elms)
            training_elms = self.df[self.df["fold"] != fold]["elm_events"]
            validation_elms = self.df[self.df["fold"] == fold]["elm_events"]
        else:
            LOGGER.info(
                "Creating training and validation datasets by simple splitting"
            )
            training_elms, validation_elms = model_selection.train_test_split(
                training_elms, test_size=self.fraction_validate
            )
        LOGGER.info(f"Number of training ELM events: {training_elms.size}")
        LOGGER.info(f"Number of validation ELM events: {validation_elms.size}")
        LOGGER.info(f"Number of test ELM events: {test_elms.size}")

        return training_elms, validation_elms, test_elms

    def _kfold_cross_val(self, training_elms: np.ndarray) -> None:
        """Helper function to perform K-fold cross-validation.

        Args:
        -----
            training_elms (np.ndarray): Indices for training ELM events.
        """
        self.df = pd.DataFrame()
        kf = model_selection.KFold(
            n_splits=config.folds, shuffle=True, random_state=config.seed
        )
        self.df["elm_events"] = training_elms
        self.df["fold"] = -1
        for f_, (_, valid_idx) in enumerate(kf.split(X=training_elms)):
            self.df.loc[valid_idx, "fold"] = f_

    def _read_file(self) -> Tuple[np.ndarray, h5py.File]:
        """Helper function to read a HDF5 file.

        Returns:
        --------
            Tuple[np.ndarray, h5py.File]: Tuple containing ELM indices and file object.
        """
        assert os.path.exists(self.datafile)
        LOGGER.info(f"Found datafile: {self.datafile}")

        # get ELM indices from datafile
        hf = h5py.File(self.datafile, "r")
        LOGGER.info(f"Number of ELM events in the datafile: {len(hf)}")
        elm_index = np.array([int(key) for key in hf], dtype=np.int)
        return elm_index, hf

    def _get_valid_indices(
        self,
        _signals: np.ndarray,
        _labels: np.ndarray,
        window_start_indices: np.ndarray = None,
        elm_start_indices: np.ndarray = None,
        elm_stop_indices: np.ndarray = None,
        valid_t0: np.ndarray = None,
        labels: np.ndarray = None,
        signals: np.ndarray = None,
    ) -> Tuple[
        np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray
    ]:
        """Helper function to concatenate the signals and labels for the ELM events
        for a given mode. It also creates allowed indices to sample from with respect
        to signal window size and label look ahead. See the README to know more
        about it.

        Args:
        -----
            _signals (np.ndarray): NumPy array containing the inputs.
            _labels (np.ndarray): NumPy array containing the labels.
            window_start_indices (np.ndarray, optional): Array containing the
                start indices of the ELM events till (t-1)th time data point. Defaults
                to None.
            elm_start_indices (np.ndarray, optional): Array containing the start
                indices of the active ELM events till (t-1)th time data point.
                Defaults to None.
            elm_stop_indices (np.ndarray, optional): Array containing the end
                vertices of the active ELM events till (t-1)th time data point.
                Defaults to None.
            valid_t0 (np.ndarray, optional): Array containing all the allowed
                vertices of the ELM events till (t-1)th time data point. Defaults
                to None.
            labels (tf.Tensor, optional): Tensor containing the labels of the ELM
                events till (t-1)th time data point. Defaults to None.
            signals (tf.Tensor, optional): Tensor containing the input signals of
                the ELM events till (t-1)th time data point. Defaults to None.

        Returns:
        --------
            Tuple[ tf.Tensor, tf.Tensor, np.ndarray, np.ndarray, np.ndarray, np.ndarray ]: Tuple containing
                signals, labels, valid_t0, start and stop indices appended with current
                time data point.
        """
        # allowed indices; time data points which can be used for creating the data chunks
        _valid_t0 = np.ones(_labels.shape, dtype=np.int8)
        _valid_t0[
            -(config.signal_window_size + config.label_look_ahead) + 1 :
        ] = 0

        # indices for active elm events in each elm event
        active_elm_events = np.nonzero(_labels >= 0.5)[0]

        if signals is None:
            # initialize arrays
            window_start_indices = np.array([0])
            elm_start_indices = active_elm_events[0]
            elm_start_indices = active_elm_events[-1]
            valid_t0 = _valid_t0
            signals = _signals
            labels = _labels
        else:
            # concat on axis 0 (time dimension)
            last_index = len(labels) - 1
            window_start_indices = np.append(
                window_start_indices, last_index + 1
            )
            elm_start_indices = np.append(
                elm_start_indices, active_elm_events[0] + last_index + 1
            )
            elm_stop_indices = np.append(
                elm_stop_indices, active_elm_events[-1] + last_index + 1
            )
            valid_t0 = np.concatenate([valid_t0, _valid_t0])
            signals = np.concatenate([signals, _signals], axis=0)
            labels = np.concatenate([labels, _labels], axis=0)

        return (
            signals,
            labels,
            valid_t0,
            window_start_indices,
            elm_start_indices,
            elm_stop_indices,
        )

    def _oversample_data(
        self,
        _labels: np.ndarray,
        valid_indices: np.ndarray,
        elm_start: np.ndarray,
        elm_stop: np.ndarray,
        index_buffer: int = 20,
    ) -> np.ndarray:
        """Helper function to reduce the class imbalance by upsampling the data
        points with active ELMS.

        Args:
        -----
            labels_np (np.ndarray): NumPy array containing the labels.
            valid_indices (np.ndarray): Array containing the allowed indices.
            elm_start (np.ndarray): Array containing the indices for the start of
                active ELM events.
            elm_stop (np.ndarray): Array containing the indices for the end of
                active ELM events.
            index_buffer (int, optional): Number of buffer indices to use when
                doing upsampling. Defaults to 20.

        Returns:
        --------
            np.ndarray: Array containing all the indices which can be used to
                create the data chunk.
        """
        # indices for sampling data
        sample_indices = valid_indices

        # oversample active ELM periods to reduce class imbalance
        fraction_elm = np.count_nonzero(_labels >= 0.5) / _labels.shape[0]
        LOGGER.info(f"Active ELM fraction (raw data): {fraction_elm:.3f}")
        oversample_count = int((1 - fraction_elm) / fraction_elm) - 1
        LOGGER.info(
            f"Active ELM oversampling for balanced data: {oversample_count}"
        )
        for i_start, i_stop in zip(elm_start, elm_stop):
            assert np.all(_labels[i_start : i_stop + 1] >= 0.5)
            active_elm_window = np.arange(
                i_start - index_buffer, i_stop + index_buffer, dtype="int"
            )
            active_elm_window = np.tile(active_elm_window, [oversample_count])
            sample_indices = np.concatenate([sample_indices, active_elm_window])
        fraction_elm = (
            np.count_nonzero(_labels[sample_indices] >= 0.5)
            / sample_indices.size
        )
        LOGGER.info(f"Active ELM fraction (balanced data): {fraction_elm:.3f}")
        return sample_indices
