import os
import argparse
import numpy as np
from tqdm import tqdm
from itertools import chain

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.utils.rnn as rnn_utils

from torchdiffeq import odeint_adjoint as odeint


class SpectralCoeffODEFunc(nn.Module):
    """
    Function from a latent variable at time t to a 
    latent variable at time (t+1).

    @param latent_dim: integer
                       number of latent variables
    @param hidden_dim: integer [default: 256]
                       number of hidden dimensions
    """

    def __init__(self, latent_dim, hidden_dim=256):
        super().__init__()

        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim

        self.net = nn.Sequential(
            nn.Linear(self.latent_dim, self.hidden_dim),
            self.ELU(inplace=True),
            nn.Linear(self.hidden_dim, self.hidden_dim),
            self.ELU(inplace=True),
            nn.Linear(self.hidden_dim, self.latent_dim))

    def forward(self, t, x):
        return self.net(x)


class InferenceNetwork(nn.Module):
    r"""
    Given a sequence of observations, encode them into a 
    latent variable distributed as a Gaussian distribution.

    @param latent_dim:  integer
                        number of latent variables
    @param obs_dim: integer
                    number of observed variables
    @param hidden_dim: integer [default: 256]
                       number of hidden nodes in GRU
    """

    def __init__(self, latent_dim, obs_dim, hidden_dim=256):
        super(InferenceNetwork, self).__init__()
    
        self.latent_dim = latent_dim
        self.obs_dim = obs_dim
        self.hidden_dim = hidden_dim
        self.gru = nn.GRU(self.obs_dim, self.hidden_dim, batch_first=True)
        self.linear = nn.Linear(self.hidden_dim, self.latent_dim * 2)

    def forward(self, obs_seq, obs_len):
        batch_size = obs_seq.size(0)

        if batch_size > 1:
            sorted_len, sorted_idx = torch.sort(obs_len, descending=True)
            obs_seq = obs_seq[sorted_idx]

        packed_len = (  sorted_len.detach().tolist() if batch_size > 1
                        else obs_len.detach().tolist()  )

        packed = rnn_utils.pack_padded_sequence(obs_seq, packed_len)

        _, hidden = self.gru(packed, None)
        hidden = hidden[-1, ...]

        if batch_size > 1:
            _, reversed_idx = torch.sort(sorted_idx)
            hidden = hidden[reversed_idx]

        latent = self.linear(hidden)
        mu, logvar = torch.chunk(latent, 2, dim=1)

        return mu, logvar


class RunningAverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, momentum=0.99):
        self.momentum = momentum
        self.reset()

    def reset(self):
        self.val = None
        self.avg = 0

    def update(self, val):
        if self.val is None:
            self.avg = val
        else:
            self.avg = self.avg * self.momentum + val * (1 - self.momentum)
        self.val = val


def get_gauss_lobatto_points(N, k=1):
    # N => number of points
    i = np.arange(N + 1)
    x_i = np.cos(k * np.pi * i / float(N))
    return x_i


def get_T_matrix(N):
    """
    Matrix of Chebyshev coefficients at collocation points.

    Matrix to convert back and forth between spectral coefficients,
    \hat{u}_k, and the values at the collocation points, u_N(x_i).
    This is just a matrix multiplication.

    \mathcal{T} = [\cos k\pi i / N], k,i = 0, ..., N
    \mathcal{U} = \mathcal{T}\hat{\mathcal{U}}

    where \mathcal{U} = [u(x_0), ..., u(x_N)], the values of the
    function at the coordinate points.
    """
    T = np.stack( [ get_gauss_lobatto_points(N, k=k)
                    for k in np.arange(0, N + 1) ] )
    # N(k) x N(i) since this will be multiplied by 
    # the matrix of spectral coefficients (k)
    return torch.from_numpy(T)


def get_inv_T_matrix(N):
    """
    Inverse matrix to translate collocation to 
    Chebyshev coefficients.

    \mathcal{T}^{-1} = [2(\cos \pi i / N)/(\bar{c}_k \bar{c}_i N)]
    \hat{\mathcal{U}} = \mathcal{T}\mathcal{U}

    where \hat{\mathcal{U}} = [\hat{u}_0, ..., \hat{u}_N], the
    coefficients of the truncated spectral approximation.
    """
    def get_constants(k, N):
        assert k >= 0
        return 2 if (k == 0 or k == N) else 1

    inv_T = np.stack([  get_gauss_lobatto_points(N, k=k)
                        for k in np.arange(0, N + 1)  ])
    inv_T = inv_T.T  # size N(i) x N(k)

    # bar_c_i is size N(i) x N(k)
    bar_c_i = np.stack([np.repeat(get_constants(i, N), N + 1)
                        for i in np.arange(0, N + 1)])
    bar_c_k = bar_c_i.T

    inv_T = 2 * inv_T / (bar_c_k * bar_c_i * N)
    # N(i) x N(k) since this will be multiplied 
    # by the matrix of coordinate values
    return torch.from_numpy(inv_T)


def log_normal_pdf(x, mean, logvar):
    const = torch.from_numpy(np.array([2. * np.pi])).float().to(x.device)
    const = torch.log(const)
    return -.5 * (const + logvar + (x - mean) ** 2. / torch.exp(logvar))


def normal_kl(mu1, lv1, mu2, lv2):
    v1 = torch.exp(lv1)
    v2 = torch.exp(lv2)
    lstd1 = lv1 / 2.
    lstd2 = lv2 / 2.

    kl = lstd2 - lstd1 + ((v1 + (mu1 - mu2) ** 2.) / (2. * v2)) - .5
    return kl


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('npz_path', type=str, help='where dataset is stored')
    parser.add_argument('out_dir', type=str, default='./checkpoints', 
                        help='where to save checkpoints')
    parser.add_argument('--batch-time', type=int, default=20)
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--niters', type=int, default=100)
    parser.add_argument('--gpu-device', type=int, default=0)
    args = parser.parse_args()

    device = (torch.device('cuda:' + str(args.gpu)
              if torch.cuda.is_available() else 'cpu'))

    data = np.load(args.npz_path)
    u, v, p = data['u'], data['v'], data['p']
    u = torch.from_numpy(u)
    v = torch.from_numpy(v)
    p = torch.from_numpy(p)
    x = torch.stack([u, v, p])
    x = x.to(device)
    nt, nx, ny = x.size(1), x.size(2), x.size(3)
    t = torch.arange(nt)
    t = t.to(device)

    # Chebyshev collocation and series
    Tx = get_T_matrix(nx).to(device)
    Ty = get_T_matrix(ny).to(device)

    def build_u(_lambda):
        return (Tx @ _lambda) @ Ty.T

    def build_v(_omega):
        return (Tx @ _omega) @ Ty.T
    
    def build_p(_gamma):
        return (Tx @ _gamma) @ Ty.T

    latent_dim = nx * ny * 3

    inf_net = InferenceNetwork(latent_dim, obs_dim, hidden_dim=256)
    ode_net = SpectralCoeffODEFunc(nx, ny)
    inf_net, ode_net = inf_net.to(device), ode_net.to(device)
    parameters = [inf_net.parameters(), ode_net.parameters()]
    optimizer = optim.Adam(chain(*parameters), lr=1e-3)

    loss_meter = RunningAverageMeter(0.97)

    def get_batch():
        s = np.random.choice(np.arange(nt - args.batch_time, dtype=np.int64),
                             args.batch_size, replace=False)
        s = torch.from_numpy(s)
        batch_t = t[:args.batch_time]
        batch_x = torch.stack([batch_x0[s+i] for i in range(args.batch_time)], dim=0)
        return batch_t, batch_x

    try:
        tqdm_batch = tqdm(total=args.niters, desc="[Iteration]")
        for itr in range(1, args.niters + 1):
            optimizer.zero_grad()
            batch_t, batch_obs = get_batch()
            batch_size = batch_obs.size(0)
            batch_len = torch.ones(batch_size) * args.batch_time
            batch_len = batch_len.to(device)
            
            qz0_mean, qz0_logvar = inf_net(batch_obs, batch_len)
            epsilon = torch.randn(qz0_mean.size()).to(device)
            pred_z0 = epsilon * torch.exp(0.5 * qz0_logvar) + qz0_mean

            # forward in time and solve ode for reconstructions
            pred_z = odeint(ode_net, pred_z0, batch_t)
            pred_z = pred_z.permute(1, 0, 2)  # batch_size x t x dim
            pred_z = pred_z.view(batch_size, -1, nx, ny, 3)
            batch_lambda = pred_z[:, :, :, :, 0]
            batch_omega  = pred_z[:, :, :, :, 1]
            batch_gamma  = pred_z[:, :, :, :, p]
            pred_u = build_u(pred_lambda)
            pred_v = build_v(pred_lambda)
            pred_p = build_p(pred_lambda)
            pred_obs = torch.cat([pred_u, pred_v, pred_p])

            noise_std_ = torch.zeros(pred_obs.size()).to(device) + noise_std
            noise_logvar = 2. * torch.log(noise_std_).to(device)

            logpx = log_normal_pdf(batch_obs, pred_obs, noise_logvar).sum(-1).sum(-1)
            pz0_mean = pz0_logvar = torch.zeros(pred_z0.size()).to(device)
            analytic_kl = normal_kl(qz0_mean, qz0_logvar,
                                    pz0_mean, pz0_logvar).sum(-1)
            loss = torch.mean(-logpx + analytic_kl, dim=0)
            loss.backward()
            optimizer.step()
            
            loss_meter.update(loss.item())
            tqdm_batch.set_postfix({"Loss": loss_meter.avg})
            tqdm_batch.update()
        tqdm_batch.close()
    except KeyboardInterrupt:
        checkpoint_path = os.path.join(args.out_dir, 'checkpoint.pth.tar')
        torch.save({
            'ode_net_state_dict': ode_net.state_dict(),
            'inf_net_state_dict': inf_net.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'config': args,
        }, checkpoint_path)