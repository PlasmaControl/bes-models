import pickle
import sys
import io
import time

import numpy as np
import h5py
import torch
import torchinfo
from sklearn import metrics

try:
    from ..source.train_base import _Model_Trainer
    from ..source.models import Multi_Features
except ImportError:
    from bes_models_2.source.train_base import _Model_Trainer
    from bes_models_2.source.models import Multi_Features


class ELM_Dataset(torch.utils.data.Dataset):

    def __init__(
        self,
        signals: np.ndarray, 
        labels: np.ndarray, 
        sample_indices: np.ndarray, 
        window_start: np.ndarray,
        signal_window_size: int,
        label_look_ahead: int,
    ) -> None:
        self.signals = signals
        self.labels = labels
        self.sample_indices = sample_indices
        self.window_start = window_start
        self.signal_window_size = signal_window_size
        self.label_look_ahead = label_look_ahead

    def __len__(self):
        return self.sample_indices.size

    def __getitem__(self, idx: int):
        time_idx = self.sample_indices[idx]
        # BES signal window data
        signal_window = self.signals[
            time_idx : time_idx + self.signal_window_size
        ]
        signal_window = signal_window[np.newaxis, ...]
        signal_window = torch.as_tensor(signal_window, dtype=torch.float32)
        # label for signal window
        label = self.labels[
            time_idx
            + self.signal_window_size
            + self.label_look_ahead
            - 1
        ]
        label = torch.as_tensor(label)

        return signal_window, label



class ELM_Classification_Model(_Model_Trainer):

    def __init__(
        self,
        label_look_ahead: int = 200,  # prediction horizon in samples
        threshold: float = 0.5,  # threshold for binary classification
        max_elms: int = None,  # limit ELMs
        minibatch_interval: int = 2000,  # print minibatch info
        **kwargs,
    ) -> None:

        # subclass attributes
        self.label_look_ahead = label_look_ahead
        self.threshold = threshold
        self.max_elms = max_elms
        self.minibatch_interval = minibatch_interval

        # init parent class
        super().__init__(**kwargs)

        self.train_data = None
        self.validation_data = None
        self.test_data = None
        self._get_data()

        if self.test_data_file:
            self._save_test_data()
        
        self.train_dataset = None
        self.validation_dataset = None
        self._make_datasets()

        self.train_data_loader = None
        self.validation_data_loader = None
        self._make_data_loaders()

        self.model = None
        self._make_features()

        self.optimizer = None
        self.scheduler = None
        self.loss_function = None
        self._make_optimizer_scheduler_loss()

        self.results = None

    def train(self):
        best_score = -np.inf  # best F1 score
        self.results = {
            'train_loss': np.empty(0),
            'valid_loss': np.empty(0),
            'scores': np.empty(0),  # F1 scores
            'roc_scores': np.empty(0),  # ROC-AUC
        }

        # send model to device
        self.model = self.model.to(self.device)

        # batches_per_epoch = len(self.train_data_loader) // self.batch_size
        # print(len(self.train_data_loader), self.batch_size)
        self.logger.info(f"Batches per epoch {len(self.train_data_loader)}")

        t_start_training = time.time()
        self.logger.info(f"\nBegin training loop with {self.n_epochs} epochs")

        # loop over epochs
        for epoch in range(self.n_epochs):
            t_start_epoch = time.time()
            
            train_loss = self._train_epoch()

            self.results['train_loss'] = np.append(
                self.results['train_loss'],
                train_loss,
            )

            valid_loss, predictions, true_labels = self.evaluate()

            self.results['valid_loss'] = np.append(
                self.results['valid_loss'],
                valid_loss,
            )

            # apply learning rate scheduler
            self.scheduler.step(valid_loss)

            # F1 score
            f1_score = metrics.f1_score(
                true_labels,
                (predictions > self.threshold).astype(int),
            )
            self.results['scores'] = np.append(
                self.results['scores'],
                f1_score,
            )

            # ROC-AUC score
            roc_score = metrics.roc_auc_score(
                true_labels,
                predictions,
            )
            self.results['roc_scores'] = np.append(
                self.results['roc_scores'],
                roc_score,
            )

            t_end_epoch = time.time()

            tmp =  f"Ep {epoch+1:03d}  "
            tmp += f"train loss {train_loss:.3f}  "
            tmp += f"val loss {valid_loss:.3f}  "
            tmp += f"f1 {f1_score:.3f}  "
            tmp += f"roc {roc_score:.3f}  "
            tmp += f"ep time {t_end_epoch-t_start_epoch:.1f} s "
            tmp += f"(total time {t_end_epoch-t_start_training:.1f} s)"
            self.logger.info(tmp)

            # best F1 score and save model
            if f1_score > best_score:
                best_score = f1_score
                self.logger.info(f"  Best F1 {best_score:.3f}, saving model...")
                # save pytorch checkpoint ...
                # save onnx format ...

        t_end_training = time.time()
        self.logger.info(f"\nEnd training loop, elapsed time {t_end_training-t_start_training:.1f} s")

    def _train_epoch(self):
        # train mode
        self.model.train()
        # loop over batches
        t_start = time.time()
        # accumulate batch-wise losses
        losses = np.array(0)
        for i_batch, (signal_windows, labels) in enumerate(self.train_data_loader):
            # reset gradients
            self.optimizer.zero_grad()
            # send data to device
            signal_windows = signal_windows.to(self.device)
            labels = labels.to(self.device)
            # calc predictions
            predictions = self.model(signal_windows)
            # calc loss
            loss = self.loss_function(
                predictions.squeeze(),
                labels.type_as(predictions),
            )
            # reduce losses
            loss = loss.mean()
            losses = np.append(losses, loss.detach().numpy())
            # backpropagate
            loss.backward()
            # update model with optimization step
            self.optimizer.step()
            if (i_batch+1)%self.minibatch_interval == 0:
                t_minibatch = time.time()
                tmp =  f"  Train batch {i_batch+1:06d}/{len(self.train_data_loader)}  "
                tmp += f"loss {loss:.3f} (avg loss {losses.mean():.3f})  "
                tmp += f"epoch time {t_minibatch-t_start:.1f} s"
                self.logger.info(tmp)
        return losses.mean()  # return avg. batch loss

    def evaluate(self):
        # evaluate mode
        self.model.eval()
        t_start = time.time()
        losses = np.array(0)
        all_predictions = []
        all_labels = []
        for i_batch, (signal_windows, labels) in enumerate(self.validation_data_loader):
            signal_windows = signal_windows.to(self.device)
            labels = labels.to(self.device)
            with torch.no_grad():
                predictions = self.model(signal_windows)
            loss = self.loss_function(
                predictions.squeeze(),
                labels.type_as(predictions),
            )
            loss = loss.mean()
            losses = np.append(losses, loss.detach().numpy())
            all_labels.append(labels.cpu().numpy())
            all_predictions.append(predictions.sigmoid().cpu().numpy())
            if (i_batch+1)%self.minibatch_interval==0:
                t_minibatch = time.time()
                tmp =  f"  Valid. batch {i_batch+1:06d}/{len(self.validation_data_loader)}  "
                tmp += f"loss {loss:.3f} (avg loss {losses.mean():.3f})  "
                tmp += f"epoch time {t_minibatch-t_start:.1f} s"
                self.logger.info(tmp)
        all_labels = np.concatenate(all_labels)
        all_predictions = np.concatenate(all_predictions)
        return losses.mean(), all_predictions, all_labels

    def _make_optimizer_scheduler_loss(self):
        if self.optimizer_type.lower() == 'adam':
            self.optimizer = torch.optim.Adam(
                self.model.parameters(), 
                lr=self.learning_rate, 
                weight_decay=self.weight_decay,
            )
        elif self.optimizer_type.lower() == 'sgd':
            self.optimizer = torch.optim.SGD(
                self.model.parameters(), 
                lr=self.learning_rate, 
                weight_decay=self.weight_decay,
                momentum=self.sgd_momentum,
                dampening=self.sgd_dampening,
            )

        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer,
            mode="min",
            factor=0.5,
            patience=2,
            verbose=True,
        )
        self.loss_function = torch.nn.BCEWithLogitsLoss(reduction="none")

    def _make_features(self):
        self.model = Multi_Features(logger=self.logger)
        self.model = self.model.to(self.device)

        self.logger.info("MODEL SUMMARY")

        n_params = sum(p.numel() for p in self.model.parameters() if p.requires_grad)

        input_size = (
            self.batch_size,
            1,
            self.signal_window_size,
            8,
            8,
        )
        x = torch.rand(*input_size)
        x = x.to(self.device)
        tmp_io = io.StringIO()
        sys.stdout = tmp_io
        print()
        torchinfo.summary(self.model, input_size=input_size, device=self.device)
        sys.stdout = sys.__stdout__
        self.logger.info(tmp_io.getvalue())
        self.logger.info(f"Model contains {n_params} trainable parameters")
        self.logger.info(f'Batched input size: {x.shape}')
        self.logger.info(f"Batched output size: {self.model(x).shape}")


    def _make_data_loaders(self):
        self.train_data_loader = torch.utils.data.DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
        )

        self.validation_data_loader = torch.utils.data.DataLoader(
            self.validation_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
    )

    def _make_datasets(self):
        self.train_dataset = ELM_Dataset(
            *self.train_data[0:4], 
            self.signal_window_size,
            self.label_look_ahead,
        )
        self.validation_dataset = ELM_Dataset(
            *self.validation_data[0:4], 
            self.signal_window_size,
            self.label_look_ahead,
        )

    def _save_test_data(self):
        test_data_file = self.output_dir / self.test_data_file
        self.logger.info(f"Test data file: {test_data_file}")
        with test_data_file.open('wb') as f:
            pickle.dump(
                {
                    "signals": self.test_data[0],
                    "labels": self.test_data[1],
                    "sample_indices": self.test_data[2],
                    "window_start": self.test_data[3],
                    "elm_indices": self.test_data[4],
                },
                f,
            )
        self.logger.info(f"  File size: {test_data_file.stat().st_size/1e6:.1f} MB")

    def _get_data(self):

        self.input_data_file = self.input_data_file.resolve()
        assert self.input_data_file.exists(), f"{self.input_data_file} does not exist"
        self.logger.info(f"Data file: {self.input_data_file}")

        with h5py.File(self.input_data_file, "r") as data_file:
            elm_indices = np.array(
                [int(key) for key in data_file], 
                dtype=np.int32,
            )
            time_frames = sum([data_file[key]['time'].shape[0] for key in data_file])
        self.logger.info(f"Events in data file: {elm_indices.size}")
        self.logger.info(f"Total time frames: {time_frames}")

        np.random.shuffle(elm_indices)
        if self.max_elms:
            elm_indices = elm_indices[:self.max_elms]
            self.logger.info(f"Limiting data to {self.max_elms} ELM events")
        n_test_elms = int(self.fraction_test * elm_indices.size)
        n_validation_elms = int(self.fraction_validation * elm_indices.size)
        test_elms, validation_elms, training_elms = np.split(
            elm_indices,
            [n_test_elms, n_test_elms+n_validation_elms]
        )

        self.logger.info(f"Training ELM events: {training_elms.size}")
        self.train_data = self._preprocess_data(
            training_elms,
            shuffle_indices=True,
            oversample_active_elm=True,
        )

        self.logger.info(f"Validation ELM events: {validation_elms.size}")
        self.validation_data = self._preprocess_data(
            validation_elms,
            shuffle_indices=False,
            oversample_active_elm=False,
        )

        self.logger.info(f"Test ELM events: {test_elms.size}")
        self.test_data = self._preprocess_data(
            test_elms,
            shuffle_indices=False,
            oversample_active_elm=False,
        )

    def _preprocess_data(
        self,
        elm_indices,
        oversample_active_elm: bool = False,
        shuffle_indices: bool = False,
    ) -> None:
        packaged_signals = None
        packaged_window_start = None
        packaged_valid_t0 = []
        packaged_labels = []
        with h5py.File(self.input_data_file, 'r') as h5_file:
            for elm_index in elm_indices:
                elm_key = f"{elm_index:05d}"
                elm_event = h5_file[elm_key]
                signals = np.array(elm_event["signals"], dtype=np.float32)
                # transpose so time dim. first
                signals = np.transpose(signals, (1, 0)).reshape(-1, 8, 8)
                try:
                    labels = np.array(elm_event["labels"], dtype=np.float32)
                except KeyError:
                    labels = np.array(elm_event["manual_labels"], dtype=np.float32)
                # indices for active elm times in each elm event
                active_elm_indices = np.nonzero(labels == 1)[0]
                active_elm_start_index = active_elm_indices[0]
                # `t0` is first index (or earliest time, or trailing time point) for signal window
                # `valid_t0` denotes valid `t0` time points for signal window
                # initialize to zeros
                valid_t0 = np.zeros(labels.shape, dtype=np.int32)
                # largest `t0` index with signal window in pre-ELM period
                largest_t0_index_for_pre_elm_period = active_elm_start_index - self.signal_window_size
                if largest_t0_index_for_pre_elm_period < 0:
                    # insufficient pre-elm period for signal window size
                    return None
                assert labels[largest_t0_index_for_pre_elm_period + (self.signal_window_size-1)    ] == 0
                assert labels[largest_t0_index_for_pre_elm_period + (self.signal_window_size-1) + 1] == 1
                # `t0` time points up to `largest_t0` are valid
                valid_t0[0:largest_t0_index_for_pre_elm_period+1] = 1
                assert valid_t0[largest_t0_index_for_pre_elm_period    ] == 1
                assert valid_t0[largest_t0_index_for_pre_elm_period + 1] == 0
                # labels after ELM onset should be active ELM, even if in post-ELM period
                last_label_for_active_elm_in_pre_elm_signal = (
                    largest_t0_index_for_pre_elm_period
                    + (self.signal_window_size - 1)
                    + self.label_look_ahead
                )
                labels[ active_elm_start_index : last_label_for_active_elm_in_pre_elm_signal+1 ] = 1
                assert labels[last_label_for_active_elm_in_pre_elm_signal] == 1
                if packaged_signals is None:
                    packaged_window_start = np.array([0])
                    packaged_valid_t0 = valid_t0
                    packaged_signals = signals
                    packaged_labels = labels
                else:
                    last_index = packaged_labels.size - 1
                    packaged_window_start = np.append(
                        packaged_window_start, 
                        last_index + 1
                    )
                    packaged_valid_t0 = np.concatenate([packaged_valid_t0, valid_t0])
                    packaged_signals = np.concatenate([packaged_signals, signals], axis=0)
                    packaged_labels = np.concatenate([packaged_labels, labels], axis=0)                

        # valid indices for data sampling
        packaged_valid_t0_indices = np.arange(packaged_valid_t0.size, dtype="int")
        packaged_valid_t0_indices = packaged_valid_t0_indices[packaged_valid_t0 == 1]

        packaged_label_indices_for_valid_t0 = (
            packaged_valid_t0_indices 
            + (self.signal_window_size-1)
            + self.label_look_ahead
            )
        packaged_labels_for_valid_t0 = packaged_labels[packaged_label_indices_for_valid_t0]
        n_active_elm = np.count_nonzero(packaged_labels_for_valid_t0)
        n_inactive_elm = np.count_nonzero(packaged_labels_for_valid_t0-1)
        active_elm_fraction = n_active_elm/(n_active_elm+n_inactive_elm)

        self.logger.info(f"  Count of inactive ELM labels: {n_inactive_elm}")
        self.logger.info(f"  Count of active ELM labels: {n_active_elm}")
        self.logger.info(f"  % active: {active_elm_fraction*1e2:.1f} %")
        min_active_elm_fraction = 0.2
        if oversample_active_elm and active_elm_fraction < min_active_elm_fraction:
            oversample_factor = int(min_active_elm_fraction * n_inactive_elm / (n_active_elm*(1-min_active_elm_fraction)))+1
            self.logger.info(f"  Oversample active ELM factor: {oversample_factor}")
            assert oversample_factor >= 1
            packaged_active_elm_label_indices_for_valid_t0 = packaged_label_indices_for_valid_t0[
                packaged_labels[packaged_label_indices_for_valid_t0] == 1
            ]
            packaged_active_elm_valid_t0_indices = (
                packaged_active_elm_label_indices_for_valid_t0
                - (self.signal_window_size-1)
                - self.label_look_ahead
            )
            for i in np.arange(oversample_factor-1):
                packaged_valid_t0_indices = np.append(
                    packaged_valid_t0_indices,
                    packaged_active_elm_valid_t0_indices,
                )
            packaged_label_indices_for_valid_t0 = (
                packaged_valid_t0_indices
                + (self.signal_window_size-1)
                + self.label_look_ahead
                )
            packaged_labels_for_valid_t0 = packaged_labels[ packaged_label_indices_for_valid_t0 ]
            n_active_elm = np.count_nonzero(packaged_labels_for_valid_t0)
            n_inactive_elm = np.count_nonzero(packaged_labels_for_valid_t0-1)
            active_elm_fraction = n_active_elm/(n_active_elm+n_inactive_elm)
            self.logger.info(f"  New count of inactive ELM labels: {n_inactive_elm}")
            self.logger.info(f"  New count of active ELM labels: {n_active_elm}")
            self.logger.info(f"  New % active: {active_elm_fraction*1e2:.1f} %")

        if shuffle_indices:
            np.random.shuffle(packaged_valid_t0_indices)

        self.logger.info( "  Data tensors -> signals, labels, sample_indices, window_start_indices:")
        for tensor in [
            packaged_signals,
            packaged_labels,
            packaged_valid_t0_indices,
            packaged_window_start,
        ]:
            tmp = f"  shape {tensor.shape}, dtype {tensor.dtype},"
            tmp += f" min {np.min(tensor):.3f}, max {np.max(tensor):.3f}"
            if hasattr(tensor, "device"):
                tmp += f" device {tensor.device[-5:]}"
            self.logger.info(tmp)

        return (
            packaged_signals, 
            packaged_labels, 
            packaged_valid_t0_indices, 
            packaged_window_start, 
            elm_indices,
        )



if __name__=='__main__':
    m = ELM_Classification_Model(n_epochs=5, minibatch_interval=25)
    m.train()