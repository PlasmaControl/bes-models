import os
import sys
import time
import pickle
import argparse
from typing import Union, Tuple, List, Dict, Any, Set
import logging
import matplotlib.pyplot as plt
from pathlib import Path

import torch
import torch.nn as nn

import numpy as np
from sklearn.metrics import roc_auc_score, f1_score
from torch.utils.tensorboard import SummaryWriter

from options.train_arguments import TrainArguments
from src import utils, trainer, dataset
from visualization import Visualizations, PCA
from src.train_VAE import ELBOLoss
from models import multi_features_model

LOGGER = logging.getLogger('__main__')
sys.excepthook = utils.log_exceptions(LOGGER)


def train_loop(args: argparse.Namespace, data_obj: object, test_datafile_name: str, fold: Union[int, None] = None,
        desc: bool = False, ) -> None:
    """Actual function to put the model to training. Use command line arg
    `--dry_run` to not create test data file and model checkpoint.

    Args:
    -----
        args (argparse.Namespace): Namespace object that stores all the command
            line arguments.
        data_obj (object): Data object that creates train, validation and test data.
        test_datafile_name (str): Name of the pickle file that stores the test data.
        fold (Union[int, None]): Integer index for the fold if using k-fold cross
        validation. Defaults to None.
        desc (bool): If true, prints the model architecture and details.
    """
    # containers to hold train and validation losses
    train_loss = []
    valid_loss = []
    roc_scores = []
    f1_scores = []
    test_data_path, model_ckpt_path = utils.create_output_paths(args, infer_mode=False)
    test_data_file = os.path.join(test_data_path, test_datafile_name)

    # add loss values to tensorboard
    if args.add_tensorboard:
        writer = SummaryWriter(
                log_dir=os.path.join(args.log_dir, "tensorboard", f"{args.model_name}{args.filename_suffix}", ))

    LOGGER.info("-" * 30)

    if not args.dry_run:
        LOGGER.info(f"Test data will be saved to: {test_data_file}")
    LOGGER.info("-" * 30)
    LOGGER.info(f"       Training fold: {fold}       ")
    LOGGER.info("-" * 30)

    # turn off model details for subsequent folds/epochs
    if fold is not None:
        if fold >= 1:
            desc = False

    # create train, valid and test data
    train_data, valid_data, _ = data_obj.get_data(shuffle_sample_indices=args.shuffle_sample_indices, fold=fold)

    # dump test data into to a file
    if not args.dry_run:
        with open(test_data_file, "w+b") as f:
            pickle.dump({"signals": valid_data[0], "labels": valid_data[1], "sample_indices": valid_data[2],
                         "window_start": valid_data[3], }, f, )

    # create image transforms
    if (("feature" in args.model_name) or (args.model_name.startswith("cnn")) or (
            args.model_name.startswith("rnn")) or (args.model_name.upper().startswith("VAE"))):
        transforms = None
    else:
        transforms = dataset.get_transforms(args)

    # create datasets
    train_dataset = dataset.ELMDataset(args, *train_data, logger=LOGGER, phase="training", )

    valid_dataset = dataset.ELMDataset(args, *valid_data, logger=LOGGER, phase="validation", )

    # training and validation dataloaders
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
            num_workers=args.num_workers, pin_memory=True, drop_last=True, )

    valid_loader = torch.utils.data.DataLoader(valid_dataset, batch_size=args.batch_size, shuffle=False,
            num_workers=args.num_workers, pin_memory=True, drop_last=True, )

    # model
    raw_model = (multi_features_model.RawFeatureModel(args) if args.raw_num_filters > 0 else None)
    fft_model = (multi_features_model.FFTFeatureModel(args) if args.fft_num_filters > 0 else None)
    cwt_model = (multi_features_model.CWTFeatureModel(args) if args.wt_num_filters > 0 else None)
    features = [type(f).__name__ for f in [raw_model, fft_model, cwt_model] if f]

    model_cls = utils.create_model(args.model_name)
    if 'MULTI' in args.model_name.upper():
        model = model_cls(args, raw_model, fft_model, cwt_model)
    else:
        model = model_cls(args)

    device = torch.device(args.device)  # "cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    LOGGER.info("-" * 100)
    LOGGER.info(f"Training with model `{args.model_name}` with features from {features}")
    LOGGER.info("-" * 100)

    # display model details
    if desc:
        if args.model_name == "rnn":
            input_size = (args.batch_size, args.signal_window_size, 64)
        else:
            if args.data_preproc == "interpolate":
                input_size = (
                        args.batch_size, 1, args.signal_window_size, args.interpolate_size, args.interpolate_size,)
            elif args.data_preproc == "gradient":
                input_size = (args.batch_size, 6, args.signal_window_size, 8, 8,)
            elif args.data_preproc == "rnn":
                input_size = (args.batch_size, args.signal_window_size, 64)
            else:
                input_size = (args.batch_size, 1, args.signal_window_size, 8, 8,)
        x = torch.rand(*input_size)
        x = x.to(device)
        utils.model_details(model, x, input_size)
        # make torchviz visualisation of model.
        if args.viz == "show_autograd":
            utils.model_viz(model, x, show_autograd=True)
        elif args.viz:
            utils.model_viz(model, x, show_autograd=False)
        if args.add_tensorboard:
            writer.add_graph(model, x)

    # optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # get the lr scheduler
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=2,
            verbose=True, )

    # loss function
    is_vae = args.model_name.lower().startswith('vae')
    criterion = ELBOLoss(reduction='none', beta=args.vae_beta) if is_vae else nn.BCEWithLogitsLoss(reduction="none")

    # define variables for ROC and loss
    best_score = 0
    best_loss = np.inf

    # instantiate training object
    use_rnn = True if args.data_preproc == "rnn" else False
    engine = trainer.Run(model, device=device, criterion=criterion, optimizer=optimizer, use_focal_loss=args.focal_loss,
            use_rnn=use_rnn, )

    valid_loss = []
    valid_mse_loss = []
    valid_likelihood_loss = []
    valid_kl_loss = []

    train_loss = []
    train_mse_loss = []
    train_kl_loss = []
    train_likelihood_loss = []

    # iterate through all the epochs
    for epoch in range(args.n_epochs):
        start_time = time.time()
        # train
        if is_vae:
            train_avg_loss, train_kl, train_likelihood, train_mse = engine.train(train_loader, epoch,
                                                                                 print_every=args.train_print_every)
            train_kl_loss.append(train_kl)
            train_likelihood_loss.append(train_likelihood)
            train_mse_loss.append(train_mse)
        else:
            train_avg_loss = engine.train(train_loader, epoch, print_every=args.train_print_every)

        train_loss.append(train_avg_loss)

        # evaluate
        losses = engine.evaluate(valid_loader, print_every=args.valid_print_every)
        if is_vae:
            val_avg_loss, val_kl, val_likelihood, val_mse, preds, valid_labels = losses
            valid_mse_loss.append(val_mse)
            valid_likelihood_loss.append(val_likelihood)
            valid_kl_loss.append(val_kl)
        else:
            val_avg_loss, preds, valid_labels = losses

        valid_loss.append(val_avg_loss)

        # step the scheduler
        scheduler.step(val_avg_loss)
        # print(f"Train losses: {train_loss}")
        # print(f"Valid losses: {valid_loss}")
        if args.add_tensorboard:
            writer.add_scalars(
                    f"{args.model_name}_signal_window_{args.signal_window_size}_lookahead_{args.label_look_ahead}",
                    {"train_loss": train_avg_loss, "valid_loss": val_avg_loss, }, epoch + 1, )
            writer.close()
        # scoring
        if is_vae:
            f1 = -1
        else:
            roc_score = roc_auc_score(valid_labels, preds)
            roc_scores.append(roc_score)
            thresh = 0.35
            f1 = f1_score(valid_labels, (preds > thresh).astype(int))
            f1_scores.append(f1)
        elapsed = time.time() - start_time

        LOGGER.info(
                f"Epoch: {epoch + 1}, \tavg train loss: {train_avg_loss:.4f}, \tavg validation loss: {val_avg_loss:.4f}")
        LOGGER.info(f"Epoch: {epoch + 1}, \tROC-AUC score: {roc_score:.4f}, \ttime elapsed: {elapsed}")

        if f1 > best_score:
            best_score = f1
            LOGGER.info(f"Epoch: {epoch + 1}, \tSave Best Score: {best_score:.4f} Model")
            if not args.dry_run:
                # save the model if best ROC is found
                model_save_path = os.path.join(model_ckpt_path, f"{args.model_name}_lookahead_{args.label_look_ahead}_"
                                                                f"{args.data_preproc}"
                                                                f"{'_' + args.balance_data if args.balance_data else ''}"
                                                                f"{'_' + args.filename_suffix if args.filename_suffix else ''}.pth", )
                torch.save({"model": model.state_dict(), "preds": preds}, model_save_path, )
                LOGGER.info(f"Model saved to: {model_save_path}")

        if val_avg_loss < best_loss:
            best_loss = val_avg_loss
            LOGGER.info(f"Epoch: {epoch + 1}, \tSave Best Loss: {best_loss:.4f} Model")

    train_loss = np.array(train_loss)
    valid_loss = np.array(valid_loss)
    roc_scores = np.array(roc_scores)
    f1_scores = np.array(f1_scores)

    outputs_file = (
                Path("outputs") / f"signal_window_{args.signal_window_size}" / f"label_look_ahead_{args.label_look_ahead}" / "training_metrics" / f"{args.model_name}{args.filename_suffix}.pkl")
    outputs_file.parent.mkdir(parents=True, exist_ok=True)  # make dir. for output file

    with open(outputs_file.as_posix(), "wb") as f:
        pickle.dump({"train_loss": train_loss, "valid_loss": valid_loss, "roc_scores": roc_scores,
                     "f1_scores": f1_scores, }, f, )

    return dict(training={'loss': train_loss, 'kl_loss': train_kl_loss, 'log_likelihood_loss': train_likelihood_loss,
                          'mse_loss': train_mse_loss},
                validation={'loss': valid_loss, 'kl_loss': valid_kl_loss, 'log_likelihood_loss': valid_likelihood_loss,
                            'mse_loss': valid_mse_loss})


if __name__ == "__main__":
    args, parser = TrainArguments().parse(verbose=True)
    LOGGER = utils.make_logger(script_name=__name__, log_file=os.path.join(args.log_dir,
                                                                           f"output_logs_{args.model_name}{args.filename_suffix}.log", ), )
    args.output_dir = 'outputs'
    args.test_data_info = False
    lookaheads = np.arange(0, 1001, 100)
    for j, x in enumerate(lookaheads):
        args.label_look_ahead = x
        data_cls = utils.create_data(args.data_preproc)
        data_obj = data_cls(args, LOGGER)
        if not os.path.isfile(os.path.join(args.test_data_dir, f"test_data_lookahead_{args.label_look_ahead}_"
                                                               f"{args.data_preproc}.pkl")):
            train_loop(args, data_obj,
                       test_datafile_name=f"test_data_lookahead_{args.label_look_ahead}_{args.data_preproc}.pkl")

        viz = Visualizations(args, LOGGER)
        layers = list(viz.model.layers.keys())[:-1]
        if j == 0:
            evrs = np.empty((len(layers), len(lookaheads)))
        for i, layer in enumerate(layers):
            pca = PCA(viz, layer=layer, elm_index=[0])
            pca.perform_PCA()
            evrs[i][j] = pca.pca_dict.get('evr')[1]

    import pandas as pd

    df = pd.DataFrame(evrs.T, columns=layers, index=lookaheads)
    df.index.name = 'Look Ahead'
    df.plot(title='EVR of PC2 vs Label Lookaheads')
    plt.show()
