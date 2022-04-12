import argparse

import numpy as np
import torch
import torch.nn as nn

import pywt
from pytorch_wavelets.dwt.transform1d import DWT1DForward

from elm_prediction.src import torch_dct as dct

class _FeatureBase(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
    ):
        super(_FeatureBase, self).__init__()

        self.args = args

        # spatial maxpool
        self.maxpool_size = self.args.mf_maxpool_size
        assert self.maxpool_size in [1, 2, 4]
        if self.maxpool_size > 1:
            self.maxpool = nn.MaxPool3d(
                kernel_size=[1, self.maxpool_size, self.maxpool_size],
            )
        else:
            self.maxpool = None

        # signal window
        self.signal_window_size = self.args.signal_window_size
        assert np.log2(self.signal_window_size) % 1 == 0  # ensure power of 2

        # time slice interval (i.e. data[::interval])
        self.time_slice_interval = self.args.mf_time_slice_interval
        assert self.time_slice_interval >= 1
        assert np.log2(self.time_slice_interval) % 1 == 0  # ensure power of 2
        self.time_points = self.signal_window_size // self.time_slice_interval

        # subwindows
        self.subwindow_size = self.args.subwindow_size
        if self.subwindow_size == -1:
            self.subwindow_size = self.time_points
        assert np.log2(self.subwindow_size) % 1 == 0  # ensure power of 2
        assert self.subwindow_size <= self.time_points
        self.subwindow_nbins = self.time_points // self.subwindow_size
        assert self.subwindow_nbins >= 1
        
        self.relu = nn.LeakyReLU(negative_slope=self.args.mf_negative_slope)
        self.dropout3d = nn.Dropout3d(p=self.args.mf_dropout_rate)

        self.num_filters = None  # set in subclass
        self.conv = None  # set in subclass

    def _time_interval_and_maxpool(self, x: torch.Tensor) -> torch.Tensor:
        if self.time_slice_interval > 1:
            x = x[:, :, ::self.time_slice_interval, :, :]
        if self.maxpool:
            x = self.maxpool(x)
        return x

    def _dropout_relu_flatten(self, x: torch.Tensor) -> torch.Tensor:
        return torch.flatten(self.relu(self.dropout3d(x)), 1)


class RawFeatureModel(_FeatureBase):
    def __init__(self, *args, **kwargs):
        """
        Use the raw BES channels values as features. This function takes in a 5-dimensional
        tensor of size: `(N, 1, signal_window_size, 8, 8)`, N=batch_size and
        performs maxpooling to downsample the spatial dimension by half, perform a
        3-d convolution with a filter size identical to the spatial dimensions of the
        input to avoid the sliding of the kernel over the input. Finally, a feature map
        is generated that can be concatenated with other features.

        Args:
        -----
            args (argparse.Namespace): Command line arguments containing the information
                about signal_window.
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
        super(RawFeatureModel, self).__init__(*args, **kwargs)

        # filters per subwindow
        self.num_filters = self.args.raw_num_filters

        filter_size = (
            self.subwindow_size,
            8 // self.maxpool_size,
            8 // self.maxpool_size,
        )

        # list of conv. filter banks (each with self.num_filters) with size self.subwindow_bins
        self.conv = nn.ModuleList(
            [
                nn.Conv3d(
                    in_channels=1,
                    out_channels=self.num_filters,
                    kernel_size=filter_size,
                ) for i_subwindow in range(self.subwindow_nbins)
            ]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self._time_interval_and_maxpool(x)
        x_new_size = [
            x.shape[0],
            self.num_filters,
            self.subwindow_nbins,
            1,
            1,
        ]
        x_new = torch.empty(size=x_new_size, dtype=x.dtype, device=x.device)
        for i_bin in torch.arange(self.subwindow_nbins):
            i_start = i_bin * self.subwindow_size
            i_stop = (i_bin+1) * self.subwindow_size
            x_new[:, :, i_start:i_stop, :, :] = self.conv[i_bin](
                x[:, :, i_start:i_stop, :, :]
            )
        x = self._dropout_relu_flatten(x_new)
        if x.isnan().any():
            raise ValueError
        return x


class FFTFeatureModel(_FeatureBase):
    def __init__(self, *args, **kwargs):
        """
        Use the raw BES channels values as input and perform a Fast Fourier Transform
        to input signals. This function takes in a 5-dimensional
        tensor of size: `(N, 1, signal_window_size, 8, 8)`, N=batch_size and
        performs a FFT followed by an absolute value of the input tensor. It
        then performs a 3-d convolution with a filter size identical to the spatial
        dimensions of the input so that the receptive field is the same
        size as input in both spatial and temporal axes. Again, we will use the
        feature map and combine it with other features before feeding it into a
        classifier.

        Args:
        -----
            args (argparse.Namespace): Command line arguments containing the information
                about signal_window.
            dropout_rate (float, optional): Fraction of total hidden units that will
                be turned off for drop out. Defaults to 0.2.
            negative_slope (float, optional): Slope of LeakyReLU activation for negative
                `x`. Defaults to 0.02.
            num_filters (int, optional): Dimensionality of the output space.
                Essentially, it gives the number of output kernels after convolution.
                Defaults to 10.
        """
        super(FFTFeatureModel, self).__init__(*args, **kwargs)

        self.fft_nbins = self.args.fft_nbins
        assert np.log2(self.fft_nbins) % 1 == 0  # ensure power of 2

        self.nfft = self.subwindow_size // self.fft_nbins
        self.nfreqs = self.nfft // 2 + 1

        self.num_filters = self.args.fft_num_filters

        filter_size = (
            self.nfreqs, 
            8 // self.maxpool_size, 
            8 // self.maxpool_size,
        )

        # list of conv. filter banks (each with self.num_filters) with size self.subwindow_bins
        self.conv = nn.ModuleList(
            [
                nn.Conv3d(
                    in_channels=1,
                    out_channels=self.num_filters,
                    kernel_size=filter_size,
                ) for _ in range(self.subwindow_nbins)
            ]
        )

    def forward(self, x):
        x = x.to(self.args.device)  # needed for PowerPC architecture
        x = self._time_interval_and_maxpool(x)
        fft_features_size = [
            x.shape[0],
            self.subwindow_nbins,
            self.num_filters,
            1,
            1,
            1,
        ]
        fft_features = torch.empty(size=fft_features_size, dtype=x.dtype, device=x.device)
        for i_sw in torch.arange(self.subwindow_nbins):
            fft_bins_size = [
                x.shape[0],
                self.fft_nbins,
                self.nfreqs,
                x.shape[3],
                x.shape[4],
            ]
            fft_bins = torch.empty(size=fft_bins_size, dtype=x.dtype, device=x.device)
            x_sw = x[:, :, i_sw*self.subwindow_size:(i_sw+1)*self.subwindow_size, :, :]
            for i_bin in torch.arange(self.fft_nbins):
                fft_bins[:, i_bin: i_bin + 1, :, :, :] = torch.abs(
                    torch.fft.rfft(
                        x_sw[:, :, i_bin * self.nfft:(i_bin+1) * self.nfft, :, :], 
                        dim=2,
                    )
                )
            fft_sw = torch.mean(fft_bins, dim=1, keepdim=True)
            fft_sw_features = self.conv[i_sw](fft_sw)
            fft_features[:, i_sw:i_sw+1, :, :, :, :] = \
                torch.unsqueeze(fft_sw_features, 1)
        output_features = self._dropout_relu_flatten(fft_features)
        return output_features


class DCTFeatureModel(_FeatureBase):
    def __init__(self, *args, **kwargs):
        """
        Use the raw BES channels values as input and perform a Fast Fourier Transform
        to input signals. This function takes in a 5-dimensional
        tensor of size: `(N, 1, signal_window_size, 8, 8)`, N=batch_size and
        performs a DCT followed by an absolute value of the input tensor. It
        then performs a 3-d convolution with a filter size identical to the spatial
        dimensions of the input so that the receptive field is the same
        size as input in both spatial and temporal axes. Again, we will use the
        feature map and combine it with other features before feeding it into a
        classifier.

        Args:
        -----
            args (argparse.Namespace): Command line arguments containing the information
                about signal_window.
            dropout_rate (float, optional): Fraction of total hidden units that will
                be turned off for drop out. Defaults to 0.2.
            negative_slope (float, optional): Slope of LeakyReLU activation for negative
                `x`. Defaults to 0.02.
            num_filters (int, optional): Dimensionality of the output space.
                Essentially, it gives the number of output kernels after convolution.
                Defaults to 10.
        """
        super(DCTFeatureModel, self).__init__(*args, **kwargs)

        self.dct_nbins = self.args.dct_nbins
        assert np.log2(self.dct_nbins) % 1 == 0  # ensure power of 2

        self.ndct = self.subwindow_size // self.dct_nbins
        # self.nfreqs = self.ndct // 2 + 1
        self.nfreqs = self.ndct

        self.num_filters = self.args.dct_num_filters

        filter_size = (
            self.nfreqs, 
            8 // self.maxpool_size, 
            8 // self.maxpool_size,
        )

        # list of conv. filter banks (each with self.num_filters) with size self.subwindow_bins
        self.conv = nn.ModuleList(
            [
                nn.Conv3d(
                    in_channels=1,
                    out_channels=self.num_filters,
                    kernel_size=filter_size,
                ) for _ in range(self.subwindow_nbins)
            ]
        )

    def forward(self, x):
        x = x.to(self.args.device)  # needed for PowerPC architecture
        x = self._time_interval_and_maxpool(x)
        dct_features_size = [
            x.shape[0],
            self.subwindow_nbins,
            self.num_filters,
            1,
            1,
            1,
        ]
        dct_features = torch.empty(size=dct_features_size, dtype=x.dtype, device=x.device)
        for i_sw in torch.arange(self.subwindow_nbins):
            dct_bins_size = [
                x.shape[0],
                self.dct_nbins,
                self.nfreqs,
                x.shape[3],
                x.shape[4],
            ]
            dct_bins = torch.empty(size=dct_bins_size, dtype=x.dtype, device=x.device)
            x_subwindow = x[:, :, i_sw*self.subwindow_size:(i_sw+1)*self.subwindow_size, :, :]
            for i_bin in torch.arange(self.dct_nbins):
                dct_bins[:, i_bin: i_bin + 1, :, :, :] = dct.dct_3d(
                    x_subwindow[:, :, i_bin * self.ndct:(i_bin+1) * self.ndct, :, :]
                )
            dct_sw = torch.mean(dct_bins, dim=1, keepdim=True)
            dct_sw_features = torch.unsqueeze( self.conv[i_sw](dct_sw), 1)
            dct_features[:, i_sw:i_sw+1, :, :, :, :] = dct_sw_features
        output_features = self._dropout_relu_flatten(dct_features)
        return output_features
            

class DWTFeatureModel(_FeatureBase):
    def __init__(self, *args, **kwargs):
        """
        Use features from the output of 1D discrete wavelet transform. Based on:
        https://github.com/fbcotter/pytorch_wavelets
        The model architecture is similar to the `RawFeatureModel`. This model takes in a 6-dimensional
        tensor of size: `(N, 1, signal_window_size, n_scales, 8, 8)`, where `N`=batch_size, and
        `n_scales`=number of different scales used (which are equal to `signal_window_size`).
        For each signal block, only the scales and BES channels for the leading time
        steps are used as model input which is a 5-dimensional tensor of size (`N, 1, n_scales, 8, 8)`.
        The model then performs a 3-d convolution with a filter size identical to
        the spatial dimensions of the input so that the receptive field is the same
        size as input in both spatial and temporal axes. In the end, we will have
        a feature map that can be concatenated with other features.

        Args:
        -----
            args (argparse.Namespace): Command line arguments containing the information
                about signal_window.
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
        super(DWTFeatureModel, self).__init__(*args, **kwargs)

        max_level = pywt.dwt_max_level(
            self.subwindow_size, 
            self.args.dwt_wavelet
        )

        if self.args.dwt_level == -1:
            dwt_level = max_level
        else:
            dwt_level = self.args.dwt_level

        assert dwt_level <= max_level

        # DWT and sample calculation to get new time domain size
        self.dwt = DWT1DForward(
            wave=self.args.dwt_wavelet,
            J=dwt_level,
            mode="reflect",
        )
        x_tmp = torch.empty(1, 1, self.subwindow_size)
        x_lo, x_hi = self.dwt(x_tmp)
        self.dwt_output_length = sum(
            [x_lo.shape[2]] + [hi.shape[2] for hi in x_hi]
        )

        self.num_filters = self.args.dwt_num_filters

        filter_size = (
            self.dwt_output_length, 
            8 // self.maxpool_size, 
            8 // self.maxpool_size,
        )

        # list of conv. filter banks (each with self.num_filters) with size self.subwindow_bins
        self.conv = nn.ModuleList(
            [
                nn.Conv3d(
                    in_channels=1,
                    out_channels=self.num_filters,
                    kernel_size=filter_size,
                ) for _ in range(self.subwindow_nbins)
            ]
        )

    def forward(self, x):
        x = self._time_interval_and_maxpool(x)
        dwt_features_size = [
            x.shape[0],
            self.subwindow_nbins,
            self.num_filters,
            1,
            1,
            1,
        ]
        dwt_features = torch.empty(size=dwt_features_size, dtype=x.dtype, device=x.device)
        for i_sw in torch.arange(self.subwindow_nbins):
            x_sw = x[:, :, i_sw*self.subwindow_size:(i_sw+1)*self.subwindow_size, :, :]
            dwt_sw_size = [
                x_sw.shape[0],
                x_sw.shape[1],
                self.dwt_output_length,
                x_sw.shape[3],
                x_sw.shape[4],
            ]
            dwt_sw = torch.empty(dwt_sw_size, dtype=x.dtype, device=x.device)
            for i_batch in torch.arange(x.shape[0]):
                x_tmp = (
                    x_sw[i_batch, 0, :, :, :]
                    .permute(1, 2, 0)
                )  # make 3D and move time dim. to last
                x_lo, x_hi = self.dwt(x_tmp)  # multi-level DWT on last dim.
                coeff = [x_lo] + [hi for hi in x_hi]  # make list of coeff.
                dwt_sw[i_batch, 0, :, :, :] =  (
                    torch.cat(coeff, dim=2)
                    .permute(2, 0, 1)
                    .unsqueeze(0)
                    .unsqueeze(0)
                )  # concat list in dwt coeff. dim, unpermute, and expand
            dwt_sw_features = self.conv[i_sw](dwt_sw)
            dwt_features[:, i_sw:i_sw+1, :, :, :, :] = \
                torch.unsqueeze(dwt_sw_features, 1)
        output_features = self._dropout_relu_flatten(dwt_features)
        return output_features


class MultiFeaturesDsV2Model(nn.Module):
    def __init__(
        self,
        args: argparse.Namespace,
    ):
        """Encapsulate all the feature models to create a composite model that
        uses all the feature maps. It takes in the class instances of
        `RawFeatureModel`, `FFTFeatureModel` and `CWTFeatureModel` along with the
        dropout rate and negative slope of the LeakyReLU activation. Once all
        the feature maps are computed, it concatenates them together and pass them
        through a couple of fully connected layers wich act as the classifier.

        Args:
        -----
            args (argparse.Namespace): Command line arguments.
        """
        super(MultiFeaturesDsV2Model, self).__init__()
        self.args = args
        self.raw_features_model = (
            RawFeatureModel(args)
            if args.raw_num_filters > 0
            else None
        )
        self.fft_features_model = (
            FFTFeatureModel(args)
            if args.fft_num_filters > 0
            else None
        )
        self.dct_features_model = (
            DCTFeatureModel(args)
            if args.dct_num_filters > 0
            else None
        )
        self.dwt_features_model = (
                DWTFeatureModel(args)
                if args.dwt_num_filters > 0
                else None
        )

        input_features = 0
        for model in [
            self.raw_features_model,
            self.fft_features_model,
            self.dwt_features_model,
            self.dct_features_model
        ]:
            if model is not None:
                input_features += model.num_filters * model.subwindow_nbins
        self.fc1 = nn.Linear(in_features=input_features, out_features=args.fc1_size)
        self.fc2 = nn.Linear(in_features=args.fc1_size, out_features=args.fc2_size)
        self.fc3 = nn.Linear(in_features=args.fc2_size, out_features=1)
        self.dropout = nn.Dropout(p=self.args.mf_dropout_rate)
        self.relu = nn.LeakyReLU(negative_slope=self.args.mf_negative_slope)

    def forward(self, x):
        raw_features = (
            self.raw_features_model(x) if self.raw_features_model else None
        )
        fft_features = (
            self.fft_features_model(x) if self.fft_features_model else None
        )
        dwt_features = (
            self.dwt_features_model(x) if self.dwt_features_model else None
        )
        dct_features = (
            self.dct_features_model(x) if self.dct_features_model else None
        )

        active_features_list = [
            features
            for features in [raw_features, fft_features, dwt_features, dct_features]
            if features is not None
        ]

        x = torch.cat(active_features_list, dim=1)
        x = self.relu(self.dropout(self.fc1(x)))
        x = self.relu(self.dropout(self.fc2(x)))
        x = self.fc3(x)
        return x


if __name__ == "__main__":
    from torchinfo import summary

    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=str, default="cpu")
    parser.add_argument("--data_preproc", type=str, default="unprocessed")
    parser.add_argument("--signal_window_size", type=int)
    parser.add_argument("--mf_maxpool_size", type=int, default=2)
    parser.add_argument(
        "--mf_time_slice_interval",
        type=int,
        default=1,
        help="Time slice interval (data[::interval]): power of 2: 1(default)|2|4|8 ...",
    )
    parser.add_argument(
        "--mf_dropout_rate",
        type=float,
        default=0.4,
        help="Dropout rate",
    )
    parser.add_argument(
        "--mf_negative_slope",
        type=float,
        default=0.02,
        help="RELU negative slope",
    )
    parser.add_argument(
        "--raw_num_filters",
        type=int,
        default=48,
        help="Number of features for RawFeatureModel: int >= 0",
    )
    parser.add_argument(
        "--fft_num_filters",
        type=int,
        default=48,
        help="Number of features for FFTFeatureModel: int >= 0",
    )
    parser.add_argument(
        "--fft_nfft",
        type=int,
        default=0,
        help="FFT window for FFTFeatureModel; power of 2 <= signal window size; if 0, use signal_window_size",
    )
    parser.add_argument(
        "--wt_num_filters",
        type=int,
        default=48,
        help="Number of features for DWTFeatureModel: int >= 0",
    )
    parser.add_argument(
        "--dwt_wavelet",
        type=str,
        default="db4",
        help="Wavelet string for DWTFeatureModel: default `db4`",
    )
    parser.add_argument(
        "--dwt_level",
        type=int,
        default=7,
        help="Wavelet decomposition level: int >= 1; default=3",
    )
    args = parser.parse_args(["--signal_window_size", "128", "--device", "cpu"])
    shape_raw = (1, 1, 128, 8, 8)
    x_raw = torch.randn(*shape_raw)

    device = args.device
    x_raw = x_raw.to(device)
    raw_model = RawFeatureModel(args)
    fft_model = FFTFeatureModel(args)
    cwt_model = DWTFeatureModel(args)
    model = MultiFeaturesDsV2Model(args, raw_model, fft_model, cwt_model)
    print(summary(model, input_size=shape_raw, device=args.device))

    for param in list(model.named_parameters()):
        print(
            f"param name: {param[0]},\nshape: {param[1].shape}, requires_grad: {param[1].requires_grad}"
        )
    print(
        f"Total trainable parameters: {sum(p.numel() for p in model.parameters() if p.requires_grad)}"
    )
    y = model(x_raw)
    print(y.shape)
