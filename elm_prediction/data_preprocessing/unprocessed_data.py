"""
Data class to package BES data for training using PyTorch without any 
modifications and transformations.
"""
from typing import Tuple

import numpy as np
import h5py

try:
    from .base_data import BaseData
except ImportError:
    from base_data import BaseData


class UnprocessedData(BaseData):
    def _preprocess_data(
        self,
        elm_indices: np.ndarray = None,
        shuffle_sample_indices: bool = False,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Helper function to preprocess the data: reshape the input signal, use
        allowed indices to upsample the class minority labels [active ELM events].

        Args:
        -----
            elm_indices (np.ndarray, optional): ELM event indices for the corresponding
                mode. Defaults to None.
            shuffle_sample_indices (bool, optional): Whether to shuffle the sample
                indices. Defaults to False.

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
        valid_t0 = []
        labels = []

        # get ELM indices from the data file if not provided
        if elm_indices is None:
            elm_indices = self.elm_indices

        # iterate through all the ELM indices
        with h5py.File(self.datafile, 'r') as hf:
            for elm_index in elm_indices:
                elm_key = f"{elm_index:05d}"
                elm_event = hf[elm_key]
                _signals = np.array(elm_event["signals"], dtype=np.float32)
                # transposing so that the time dimension comes forward
                _signals = np.transpose(_signals, (1, 0)).reshape(-1, 8, 8)
                if self.args.automatic_labels:
                    _labels = np.array(elm_event["automatic_labels"], dtype=np.float32)
                else:
                    try:
                        _labels = np.array(elm_event["labels"], dtype=np.float32)
                    except KeyError:
                        _labels = np.array(elm_event["manual_labels"], dtype=np.float32)

                if self.args.normalize_data:
                    _signals = _signals.reshape(-1, 64)
                    _signals[:, :32] = _signals[:, :32] / np.max(_signals[:, :32])
                    _signals[:, 32:] = _signals[:, 32:] / np.max(_signals[:, 32:])
                    _signals = _signals.reshape(-1, 8, 8)

                if self.args.truncate_inputs:
                    active_elm_indices = np.where(_labels > 0)[0]
                    elm_end_index = active_elm_indices[-1] + self.args.truncate_buffer
                    _signals = _signals[:elm_end_index, ...]
                    _labels = _labels[:elm_end_index]

                if len(_labels) < 2000:
                    continue
                else:
                    # get all the allowed indices till current time step
                    (
                        signals,
                        labels,
                        valid_t0,
                        window_start,
                        elm_start,
                        elm_stop,
                    ) = self._get_valid_indices(
                        _signals=_signals,
                        _labels=_labels,
                        window_start_indices=window_start,
                        elm_start_indices=elm_start,
                        elm_stop_indices=elm_stop,
                        valid_t0=valid_t0,
                        labels=labels,
                        signals=signals,
                    )

        # valid indices for data sampling
        sample_indices = np.arange(valid_t0.size, dtype="int")
        sample_indices = sample_indices[valid_t0 == 1]

        if shuffle_sample_indices:
            np.random.shuffle(sample_indices)

        self.logger.info(
            "Data tensors -> signals, labels, sample_indices, window_start_indices:"
        )
        for tensor in [
            signals,
            labels,
            sample_indices,
            window_start,
        ]:
            tmp = f" shape {tensor.shape}, dtype {tensor.dtype},"
            tmp += f" min {np.min(tensor):.3f}, max {np.max(tensor):.3f}"
            if hasattr(tensor, "device"):
                tmp += f" device {tensor.device[-5:]}"
            self.logger.info(tmp)
        return signals, labels, sample_indices, window_start, elm_indices
