import torch
from matplotlib import pyplot as plt
import seaborn as sb
import numpy as np

from autoencoder import Autoencoder, AE_simple, device
import data, config

# Get data and form dataset
data_ = data.Data(kfold=False, balance_classes=config.balance_classes)
train_data, test_data, _ = data_.get_data(shuffle_sample_indices=False)

train_dataset = data.ELMDataset(
        *train_data,
        config.signal_window_size,
        config.label_look_ahead,
        stack_elm_events=False,
        transform=None,
        for_autoencoder = True
    )

# Load model
PATH = './trained_models/latent_300_no_activation.pth'
model = torch.load(PATH)
model = model.to(device)
model.eval()

# Plots the actual vs model reconstructed frames (0, 2, 4, 6)
def plot(index):
    actual_window = train_dataset[index][0].to(device)
    pred_window = train_dataset[index][1]
    model_window = model(actual_window)
    
    number_frames = 4
    number_rows = 2
    fig, ax = plt.subplots(nrows = number_rows, ncols = number_frames)

    # Plot the actual frames 0,2,4,6
    actual = actual_window.cpu().detach().numpy()[0]
    for i in range(number_frames):
        cur_ax = ax[0][i]
        cur_ax.imshow(actual[2*i], cmap = 'hot')
        cur_ax.set_title(f'A {2*i}')
        cur_ax.axis('off')

    # Plot the prediction frames 0,2,4,6
    pred = model_window.cpu().detach().numpy()[0]
    for i in range(number_frames):
        cur_ax = ax[1][i]
        cur_ax.imshow(pred[2*i], cmap = 'hot')
        cur_ax.set_title(f'P {2*i}')
        cur_ax.axis('off')

    fig.tight_layout()
    # fig.savefig('plot.png') 
    plt.show()

if __name__ == '__main__':
    # Plot 10 model predictions
    for i in range(0, 11000, 1000):
        # print(i)
        plot(i)
