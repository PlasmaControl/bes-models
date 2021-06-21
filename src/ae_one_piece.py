import torch
from torch.utils.data import DataLoader
# from torch.utils.tensorboard import SummaryWriter
from torchinfo import summary

import matplotlib.pyplot as plt
from typing import Tuple

import data, config


device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} device'.format(device))

# Autoencoder class
class Autoencoder_OP(torch.nn.Module):
    # Constructor - sets up encoder and decoder layers
    def __init__(self,
        latent_dim: int, 
        encoder_hidden_layers: Tuple,
        decoder_hidden_layers: Tuple, 
        relu_negative_slope: float = 0.1,
        signal_window_shape: Tuple = (1,8,8,8),
        signal_window_size: int = 8,
        learning_rate: float = .0001,
        l2_factor: float = 5e-3,
        dropout_rate: float = 0.3):

        super(Autoencoder_OP, self).__init__()

        self.latent_dim = latent_dim
        self.encoder_hidden_layers = encoder_hidden_layers
        self.decoder_hidden_layers = decoder_hidden_layers
        self.signal_window_shape = signal_window_shape # (channels, signal window size, height, width)
        self.signal_window_size = signal_window_size # Initialized to 8 frames 
        self.relu_negative_slope = relu_negative_slope
        self.dropout_rate = dropout_rate

        # 1x8x8x8 = 512 input features
        self.num_input_features = self.signal_window_shape[0]
        for i in range(1, len(self.signal_window_shape)):
            self.num_input_features *= self.signal_window_shape[i]
        # print(f'total number of features: {self.num_input_features}')

        self.flatten = torch.nn.Flatten()
        self.model = torch.nn.Sequential()

        self.create_encoder()
        self.create_decoder()

        self.loss_fn = torch.nn.MSELoss()
        self.optimizer = torch.optim.SGD(
            self.parameters(), 
            lr=learning_rate, 
            momentum=0.9, 
            weight_decay=l2_factor)
        
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer,
            mode="min",
            factor=0.5,
            patience=2,
            verbose=True,
            eps=1e-6,
        )

        return

    def create_encoder(self):
        # Add the requested number of encoder hidden dense layers + relu layers
        for i, layer_size in enumerate(self.encoder_hidden_layers):
            if i == 0:
                d_layer = torch.nn.Linear(self.num_input_features, self.encoder_hidden_layers[i])    
            else:
                d_layer = torch.nn.Linear(self.encoder_hidden_layers[i-1], self.encoder_hidden_layers[i])

            # Add fully connected, then dropout, then relu layers
            self.model.add_module(f'Encoder Dense Layer {i+1}', d_layer)
            self.model.add_module(f'Encoder Dropout Layer {i+1}', torch.nn.Dropout(p=self.dropout_rate))
            self.model.add_module(f'Encoder LeakyReLU Layer {i+1}', torch.nn.LeakyReLU(negative_slope=self.relu_negative_slope))

        # Add latent dim layer after encoder hidden layers
        latent = torch.nn.Linear(self.encoder_hidden_layers[i], self.latent_dim)
        self.model.add_module(f'Latent Layer', latent)
        # self.encoder.add_module(f'Latent Dropout Layer {i+1}', torch.nn.Dropout(p=self.dropout_rate))
        # self.model.add_module(f'Latent ReLU Layer', torch.nn.LeakyReLU(negative_slope=self.relu_negative_slope))

        return

    def create_decoder(self):
        # Add the requested number of decoder hidden dense layers
        for i, layer_size in enumerate(self.decoder_hidden_layers):
            if i == 0:
                d_layer = torch.nn.Linear(self.latent_dim, self.decoder_hidden_layers[i])    
            else:
                d_layer = torch.nn.Linear(self.decoder_hidden_layers[i-1], self.decoder_hidden_layers[i])

            self.model.add_module(f'Decoder Dense Layer {i+1}', d_layer)
            self.model.add_module(f'Decoder Dropout Layer {i+1}', torch.nn.Dropout(p=self.dropout_rate))
            self.model.add_module(f'Decoder LeakyReLU Layer {i+1}', torch.nn.LeakyReLU(negative_slope=self.relu_negative_slope))

        # Add last layer after decoder hidden layers
        last = torch.nn.Linear(self.decoder_hidden_layers[i], self.num_input_features)
        self.model.add_module(f'Last Layer', last)
        # self.decoder.add_module(f'Last Dropout Layer {i+1}', torch.nn.Dropout(p=self.dropout_rate))
        # self.decoder.add_module(f'Last ReLU Layer', torch.nn.ReLU())

        return

    # Forward pass of the autoencoder - returns the reshaped output of net
    def forward(self, x):
        shape = x.shape
        # print(shape)
        x = self.flatten(x)
        output = self.model(x)
        reconstructed = output.view(*shape)
        # print(reconstructed.shape)
        return reconstructed

    @staticmethod
    def train_loop(model, dataloader: DataLoader, print_output: bool = True):
        model.train()
        size = len(dataloader.dataset)
        for batch, (X, y) in enumerate(dataloader):
            # Compute prediction and loss
            X = X.to(device)
            y = y.to(device)
            pred = model(X)
            loss = model.loss_fn(pred, y)

            # Backpropagation
            model.optimizer.zero_grad()
            loss.backward()
            model.optimizer.step()

            if batch % 1000 == 0:
                loss, current = loss.item(), batch * len(X)
                if(print_output):
                    print(f"loss: {loss:>7f}  [{current:>5d}/{size:>5d}]")


    @staticmethod
    def test_loop(model, dataloader: DataLoader, print_output: bool = True):
        size = len(dataloader.dataset)
        test_loss, correct = 0, 0

        model.eval()
        
        with torch.no_grad():
            for X, y in dataloader:
                X = X.to(device)
                y = y.to(device)
                pred = model(X)
                test_loss += model.loss_fn(pred, y).item()

        test_loss /= size

        if(print_output):
            print(f"Test Dataset Avg loss: {test_loss:>8f} \n")

        return test_loss


    @staticmethod
    def train_model(
        model,  
        train_dataloader: DataLoader, 
        test_dataloader: DataLoader,
        epochs: int = 10, 
        print_output: bool = True):

        all_losses = []

        for t in range(epochs):
            if(print_output):
                print(f"Epoch {t+1}\n-------------------------------")
            model.train_loop(model, train_dataloader,)
            epoch_loss = model.test_loop(model, test_dataloader)

            all_losses.append(epoch_loss)

            # Change optimizer learning rate
            model.scheduler.step(epoch_loss)
        
        if(print_output):
            print("Done Training!")

        return all_losses


def plot_loss(losses):
    plt.plot(losses)
    plt.title('Training Loss')
    plt.ylabel('Avg Loss')
    plt.xlabel('epochs')
    # plt.show()
    plt.savefig('loss_plot.png')


if __name__== '__main__':
    # Autoencoder Model
    model = Autoencoder_OP(32, 
        encoder_hidden_layers = (250,100,50), 
        decoder_hidden_layers = (50,100,250))

    model = model.to(device)

    # Writer for tensorboard
    # writer = SummaryWriter('runs/practicing')

    batch_size = 4
    input_size = (4,1,8,8,8)
    summary(model, input_size)

    fold = 1
    data_ = data.Data(kfold=True, balance_classes=config.balance_classes)
    train_data, test_data, _ = data_.get_data(shuffle_sample_indices=True, fold=fold)
    
    train_dataset = data.ELMDataset(
        *train_data,
        config.signal_window_size,
        config.label_look_ahead,
        stack_elm_events=False,
        transform=None,
        for_autoencoder = True
    )

    # print(f'Length of train dataset: {train_dataset.__len__()}')

    test_dataset = data.ELMDataset(
        *test_data,
        config.signal_window_size,
        config.label_look_ahead,
        stack_elm_events=False,
        transform=None,
        for_autoencoder = True
    )

    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    test_dataloader = DataLoader(test_dataset, batch_size=4, shuffle=False)

    # Train the model
    losses = Autoencoder_OP.train_model(model, train_dataloader, test_dataloader, epochs  = 5, print_output = True)
    plot_loss(losses)

    # Save the model - weights and structure
    model_save_path = './models/trained_model.pth'
    torch.save(model, model_save_path)
    
        
