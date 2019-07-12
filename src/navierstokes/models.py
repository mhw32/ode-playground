import torch
import torch.nn as nn
import torch.nn.functional as F

# pip install git+https://github.com/rtqichen/torchdiffeq
from torchdiffeq import odeint_adjoint as odeint

# ----------------------------------------------------------------------
# Implementation of a Deterministic RNN

class RNNDiffEq(nn.Module):
    """
    Supervised approach to predict next element in a 
    differential equation.

    Args (in Forward)
    ----
    x_seq: torch.Tensor (size: batch_size x T x grid_dim x grid_dim)
           the input 
    """
    def __init__(self, grid_dim, rnn_dim=64, hidden_dim=64, n_filters=32):
        super(RNNDiffEq, self).__init__()
        self.bc_encoder = BoundaryConditionEncoder(
            grid_dim, rnn_dim, hidden_dim=hidden_dim, n_filters=n_filters)
        self.spatial_encoder = SpatialEncoder(grid_dim, hidden_dim=hidden_dim,
                                              n_filters=n_filters)
        self.spatial_decoder = SpatialDecoder(grid_dim, hidden_dim=hidden_dim,
                                              n_filters=n_filters)
        self.rnn = nn.GRU(hidden_dim, rnn_dim, batch_first=True)
    
    def forward(self, u_seq, v_seq, p_seq, rnn_h0=None):
        batch_size, T, grid_dim = u_seq.size(0), u_seq.size(1), u_seq.size(2)
        seq = torch.cat([u_seq.unsqueeze(2), v_seq.unsqueeze(2), 
                         p_seq.unsqueeze(2)], dim=2)
    
        if rnn_h0 is None:
            # pull out boundary conditions (which should be constant over time)
            bc_x0, bc_xn = seq[:, 0, :, 0, :], seq[:, 0, :, -1, :]
            bc_y0, bc_yn = seq[:, 0, :, :, 0], seq[:, 0, :, :, -1]
            rnn_h0 = self.bc_encoder(bc_x0, bc_xn, bc_y0, bc_yn)
            rnn_h0 = rnn_h0.unsqueeze(0)

        seq = seq.view(batch_size * T, 3, grid_dim, grid_dim)
        hidden_seq = self.spatial_encoder(seq)
        hidden_seq = hidden_seq.view(batch_size, T, -1)  # batch_size, T, hidden_dim
        output_seq, rnn_h = self.rnn(hidden_seq, rnn_h0)
        output_seq = output_seq.contiguous().view(batch_size * T, -1)
        out = self.spatial_decoder(output_seq)  # batch_size x channel x grid_dim**2
        out = out.view(batch_size, T, 3, grid_dim, grid_dim)

        next_u_seq, next_v_seq, next_p_seq = out[:, :, 0], out[:, :, 1], out[:, :, 2]
        next_u_seq = next_u_seq.contiguous()
        next_v_seq = next_v_seq.contiguous()
        next_p_seq = next_p_seq.contiguous()

        return next_u_seq, next_v_seq, next_p_seq, rnn_h


class SpatialEncoder(nn.Module):
    def __init__(self, grid_dim, channels=3, hidden_dim=64, n_filters=32):
        super(SpatialEncoder, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(channels, n_filters, 2, 2, padding=0))
            # nn.ReLU(),
            # nn.Conv2d(n_filters, n_filters*2, 2, 2, padding=0))
        self.cout = gen_conv_output_dim(grid_dim)
        self.fc = nn.Linear(n_filters*self.cout**2, hidden_dim)
    
    def forward(self, x):
        batch_size = x.size(0)
        hidden = F.relu(self.conv(x))
        hidden = hidden.view(batch_size, -1)
        return self.fc(hidden)


class SpatialDecoder(nn.Module):
    def __init__(self, grid_dim, channels=3, hidden_dim=64, n_filters=32):
        super(SpatialDecoder, self).__init__()
        self.conv = nn.Sequential(
            nn.ConvTranspose2d(n_filters, n_filters, 2, 2, padding=0),
            nn.ReLU(),
            # nn.ConvTranspose2d(n_filters*4, n_filters*2, 2, 2, padding=0),
            # nn.ReLU(),
            nn.Conv2d(n_filters, channels, 1, 1, padding=0))
        self.cout = gen_conv_output_dim(grid_dim)
        self.fc = nn.Linear(hidden_dim, n_filters*self.cout**2)
        self.grid_dim = grid_dim
        self.channels = channels
        self.n_filters = n_filters
    
    def forward(self, hidden):
        batch_size = hidden.size(0)
        out = self.fc(hidden)
        out = out.view(batch_size, self.n_filters, self.cout, self.cout)
        logits = self.conv(out)
        logits = logits.view(batch_size, self.channels, 
                             self.grid_dim, self.grid_dim)
        return logits


class BoundaryConditionEncoder(nn.Module):
    def __init__(self, grid_dim, out_dim, channels=3, 
                 hidden_dim=64, n_filters=32):
        super().__init__()
        self.x0_bc = BoundaryConditionNetwork(
            grid_dim, channels=channels, 
            hidden_dim=hidden_dim, n_filters=n_filters)
        self.xn_bc = BoundaryConditionNetwork(
            grid_dim, channels=channels, 
            hidden_dim=hidden_dim, n_filters=n_filters)
        self.y0_bc = BoundaryConditionNetwork(
            grid_dim, channels=channels, 
            hidden_dim=hidden_dim, n_filters=n_filters)
        self.yn_bc = BoundaryConditionNetwork(
            grid_dim, channels=channels, 
            hidden_dim=hidden_dim, n_filters=n_filters)
        self.fc = nn.Linear(hidden_dim*4, out_dim)

    def forward(self, x0, xn, y0, yn):
        h_x0 = self.x0_bc(x0)
        h_xn = self.xn_bc(xn)
        h_y0 = self.y0_bc(y0)
        h_yn = self.yn_bc(yn)
        h_bc = torch.cat([h_x0, h_xn, h_y0, h_yn], dim=1)
        return self.fc(F.relu(h_bc))


class BoundaryConditionNetwork(nn.Module):
    """
    Encode the boundary conditions as 1 dimensional
    convolutions over a single boundary.
    """
    def __init__(self, grid_dim, channels=3, 
                 hidden_dim=64, n_filters=32):
        super().__init__()
        self.boundary_encoder = nn.Sequential(
            nn.Conv1d(channels, n_filters // 2, 3, padding=0),
            nn.ReLU(),
            nn.Conv1d(n_filters // 2, n_filters, 3, padding=0))
        self.fc = nn.Linear(n_filters*6, hidden_dim)
    
    def forward(self, bc):
        batch_size = bc.size(0)
        hid = F.relu(self.boundary_encoder(bc))
        hid = hid.view(batch_size, 32 * 6)
        return self.fc(hid)


def gen_conv_output_dim(s):
    s = _get_conv_output_dim(s, 2, 0, 2)
    # s = _get_conv_output_dim(s, 2, 0, 2)
    # s = _get_conv_output_dim(s, 2, 0, 2)
    return s


def _get_conv_output_dim(I, K, P, S):
    # I = input height/length
    # K = filter size
    # P = padding
    # S = stride
    # O = output height/length
    O = (I - K + 2*P)/float(S) + 1
    return int(O)

# ----------------------------------------------------------------------
# Implementation of NeuralODEs in PyTorch (adapted from TorchDiffEq)

class ODEDiffEq(nn.Module):
    """
    Supervised model with built-in ODE to predict next element 
    in a differential equation. Turns out this is almost the same
    except we need to handle the input/output scheme a little differently.
    """
    def __init__(self, grid_dim, rnn_dim=64, hidden_dim=64, n_filters=32):
        super(RNNDiffEq, self).__init__()
        self.bc_encoder = BoundaryConditionEncoder(
            grid_dim, rnn_dim, hidden_dim=hidden_dim, n_filters=n_filters)
        self.spatial_encoder = SpatialEncoder(grid_dim, hidden_dim=hidden_dim,
                                              n_filters=n_filters)
        self.spatial_decoder = SpatialDecoder(grid_dim, hidden_dim=hidden_dim,
                                              n_filters=n_filters)
        self.rnn = nn.GRU(hidden_dim, rnn_dim, batch_first=True)

    def forward(self, t_seq, obs_seq):
        batch_size, T, comp_dim, grid_dim = obs_seq.size()
    
        # pull out boundary conditions (which should be constant over time)
        bc_x0, bc_xn = obs_seq[:, 0, :, 0, :], obs_seq[:, 0, :, -1, :]
        bc_y0, bc_yn = obs_seq[:, 0, :, :, 0], obs_seq[:, 0, :, :, -1]
        rnn_h0 = self.bc_encoder(bc_x0, bc_xn, bc_y0, bc_yn)
        rnn_h0 = rnn_h0.unsqueeze(0)

        obs_seq = obs_seq.view(batch_size * T, 3, grid_dim, grid_dim)
        hidden_seq = self.spatial_encoder(obs_seq)
        hidden_seq = hidden_seq.view(batch_size, T, -1)  # batch_size, T, hidden_dim
        output_seq, rnn_h = self.rnn(hidden_seq, rnn_h0)
        output_seq = output_seq.contiguous().view(batch_size * T, -1)
        output_seq = self.spatial_decoder(output_seq)  # batch_size x channel x grid_dim**2
        output_seq = output_seq.view(batch_size, T, 3, grid_dim, grid_dim)

        return output_seq
