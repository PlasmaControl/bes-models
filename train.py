import os
import time
import pickle
import argparse
from typing import Union

import torch
import torch.nn as nn
import numpy as np
from sklearn.metrics import roc_auc_score

from options.train_arguments import TrainArguments
from src import config, data, utils, run
import models


def train_loop(
    args,
    data_obj: data.Data,
    test_datafile_name: str,
    fold: Union[int, None] = None,
    desc: bool = True,
):
    # TODO: Implement K-fold cross-validation
    if (not args.kfold) and (fold is not None):
        LOGGER.info(
            f"K-fold is set to {args.kfold} but fold index is passed!"
            " Proceeding without using K-fold."
        )
        fold = None

    # test data file path
    test_data_file = os.path.join(test_datafile_name)

    LOGGER.info("-" * 60)
    if args.data_mode == "balanced":
        LOGGER.info("Training with balanced classes.")
    else:
        LOGGER.info("Training using unbalanced (original) classes.")

    LOGGER.info(f"Test data will be saved to: {test_data_file}")
    LOGGER.info("-" * 30)
    LOGGER.info(f"       Training fold: {fold}       ")
    LOGGER.info("-" * 30)

    # turn off model details for subsequent folds/epochs
    if fold is not None:
        if fold >= 1:
            desc = False

    # create train, valid and test data
    train_data, valid_data, test_data = data_obj.get_data(
        shuffle_sample_indices=args.shuffle_sample_indices, fold=fold
    )

    # dump test data into to a file
    with open(test_data_file, "wb") as f:
        pickle.dump(
            {
                "signals": test_data[0],
                "labels": test_data[1],
                "sample_indices": test_data[2],
                "window_start": test_data[3],
            },
            f,
        )

    # create image transforms
    if args.model_name in ["FeatureModel", "CNNModel"]:
        transforms = None
    else:
        transforms = data.get_transforms(args)

    # create datasets
    train_dataset = data.ELMDataset(
        args,
        *train_data,
        logger=LOGGER,
        transform=transforms,
    )

    valid_dataset = data.ELMDataset(
        args,
        *valid_data,
        logger=LOGGER,
        transform=transforms,
    )

    # training and validation dataloaders
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True,
    )

    valid_loader = torch.utils.data.DataLoader(
        valid_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True,
    )

    # model
    model_cls = utils.find_model_using_name(args.model_name)
    model = model_cls(args)
    print(model)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    LOGGER.info("-" * 50)
    LOGGER.info(f"       Training with model: {args.model_name}       ")
    LOGGER.info("-" * 50)

    # display model details
    if desc:
        if args.stack_elm_events and args.model_name == "stacked_elm_model":
            input_size = (args.batch_size, 1, args.size, args.size)
        else:
            input_size = (
                args.batch_size,
                1,
                args.signal_window_size,
                8,
                8,
            )
        x = torch.rand(*input_size)
        x = x.to(device)
        utils.model_details(model, x, input_size)

    # optimizer
    optimizer = utils.get_optimizer(args, model)

    # get the lr scheduler
    scheduler = utils.get_lr_scheduler(args, optimizer, dataloader=train_loader)

    # loss function
    criterion = nn.BCEWithLogitsLoss(reduction="none")

    # define variables for ROC and loss
    best_score = 0
    best_loss = np.inf

    # instantiate training object
    engine = run.Run(
        model,
        device=device,
        criterion=criterion,
        optimizer=optimizer,
        use_focal_loss=args.focal_loss,
    )

    # iterate through all the epochs
    for epoch in range(args.epochs):
        start_time = time.time()

        if args.scheduler in [
            "CosineAnnealingLR",
            "CyclicLR",
            "OneCycleLR",
        ]:
            # train
            avg_loss = engine.train(
                train_loader,
                epoch,
                scheduler=scheduler,
                print_every=args.train_print_every,
            )

            # evaluate
            avg_val_loss, preds, valid_labels = engine.evaluate(
                valid_loader, print_every=args.valid_print_every
            )
            scheduler = utils.get_lr_scheduler(
                args,
                optimizer,
                dataloader=train_loader,
            )
        else:
            # train
            avg_loss = engine.train(
                train_loader, epoch, print_every=args.train_print_every
            )

            # evaluate
            avg_val_loss, preds, valid_labels = engine.evaluate(
                valid_loader, print_every=args.valid_print_every
            )

            # step the scheduler
            if args.scheduler == "ReduceLROnPlateau":
                scheduler.step(avg_val_loss)
            else:
                scheduler.step()

        # scoring
        roc_score = roc_auc_score(valid_labels, preds)
        elapsed = time.time() - start_time

        LOGGER.info(
            f"Epoch: {epoch + 1}, \tavg train loss: {avg_loss:.4f}, \tavg validation loss: {avg_val_loss:.4f}"
        )
        LOGGER.info(
            f"Epoch: {epoch +1}, \tROC-AUC score: {roc_score:.4f}, \ttime elapsed: {elapsed}"
        )

        # save the model if best ROC is found
        model_save_path = os.path.join(
            args.model_dir,
            f"{args.model_name}_fold{fold}_best_roc_{args.data_mode}_lookahead_{args.label_look_ahead}_noise_{args.sigma}.pth",
        )
        if roc_score > best_score:
            best_score = roc_score
            LOGGER.info(
                f"Epoch: {epoch+1}, \tSave Best Score: {best_score:.4f} Model"
            )
            torch.save(
                {"model": model.state_dict(), "preds": preds},
                model_save_path,
            )

        if avg_val_loss < best_loss:
            best_loss = avg_val_loss
            LOGGER.info(
                f"Epoch: {epoch+1}, \tSave Best Loss: {best_loss:.4f} Model"
            )
        LOGGER.info(f"Model saved to: {model_save_path}")
    # # # save the predictions in the valid dataframe
    # # valid_folds["preds"] = torch.load(
    # #     os.path.join(
    # #         args.model_dir, f"{args.model_name}_fold{fold}_best_roc.pth"
    # #     ),
    # #     map_location=torch.device("cpu"),
    # # )["preds"]

    # # return valid_folds


if __name__ == "__main__":
    args, parser = TrainArguments().parse(verbose=True)
    utils.test_args_compat(args, parser)
    LOGGER = utils.get_logger(
        script_name=__name__,
        log_file=f"output_logs_{args.data_mode}_noise_{args.sigma}.log",
    )
    data_obj = data.Data(args, LOGGER)
    train_loop(
        args,
        data_obj,
        test_datafile_name=f"test_data_{args.data_mode}_lookahead_{args.label_look_ahead}.pkl",
    )
