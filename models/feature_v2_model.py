import argparse
from typing import Tuple, Union

import torch
import torch.nn as nn


class FeatureV2Model(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
        fc_units: Union[int, Tuple[int, int]] = (128, 64),
        dropout_rate: float = 0.4,
        negative_slope: float = 0.02,
        filter_size: tuple = (3, 3, 3),
        maxpool_size: int = 2,
        num_filters: tuple = (8, 16),
    ):
        """
        8x8 + time feature blocks followed by fully-connected layers. This function
        takes in a 4-dimensional tensor of size: `(1, signal_window_size, 8, 8)`
        performs maxpooling to downsample the spatial dimension by half, perform a
        3-d convolution with a filter size identical to the spatial dimensions of the
        input to avoid the sliding of the kernel over the input. Finally, it adds a
        couple of fully connected layers on the output of the 3-d convolution.

        Args:
        -----
            fc_units (Union[int, Tuple[int, int]], optional): Number of hidden
                units in each layer. Defaults to (40, 20).
            dropout_rate (float, optional): Fraction of total hidden units that will
                be turned off for drop out. Defaults to 0.2.
            negative_slope (float, optional): Slope of LeakyReLU activation for negative
                `x`. Defaults to 0.02.
            maxpool_size (int, optional): Size of the kernel used for maxpooling. Use
                0 to skip maxpooling. Defaults to 2.
            num_filters (int, optional): Dimensionality of the output space.
                Essentially, it gives the number of output kernels after convolution.
                Defaults to 10.
        """
        super(FeatureV2Model, self).__init__()
        pool_size = [1, maxpool_size, maxpool_size]
        self.args = args
        self.conv1 = nn.Conv3d(
            in_channels=1, out_channels=num_filters[0], kernel_size=filter_size
        )
        self.conv2 = nn.Conv3d(
            in_channels=num_filters[0],
            out_channels=num_filters[1],
            kernel_size=filter_size,
        )
        self.maxpool = nn.MaxPool3d(kernel_size=pool_size)
        self.relu = nn.LeakyReLU(negative_slope=negative_slope)
        self.dropout3d = nn.Dropout3d(p=dropout_rate)
        input_features = 64 if self.args.signal_window_size == 8 else 192
        self.fc1 = nn.Linear(
            in_features=input_features, out_features=fc_units[0]
        )
        self.fc2 = nn.Linear(in_features=fc_units[0], out_features=fc_units[1])
        self.fc3 = nn.Linear(in_features=fc_units[1], out_features=1)
        self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.dropout3d(self.conv1(x))
        x = self.relu(x)
        x = self.maxpool(x)
        x = self.dropout3d(self.conv2(x))
        x = self.relu(x)
        x = torch.flatten(x, 1)
        x = self.relu(self.dropout(self.fc1(x)))
        x = self.relu(self.dropout(self.fc2(x)))
        x = self.fc3(x)

        return x
