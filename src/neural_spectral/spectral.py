import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.utils.rnn as rnn_utils

from torchdiffeq import odeint_adjoint as odeint


class ODEFunc(nn.Module):
    """Model basis coefficients as a an ODE wrt time"""

    def __init__(self, K, nx, ny):
        super().__init__()
        self.K = K
        self.nx, self.ny = nx, ny
        self.net = nn.Sequential(
            nn.Linear(3*self.nx*self.ny, 512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 256),
            nn.ELU(inplace=True),
            nn.Linear(256, self.K),
        )

        for m in self.net.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, mean=0, std=0.1)
                nn.init.constant_(m.bias, val=0)

    def forward(self, t, grid):
        batch_size = grid.size(0)
        grid = grid.view(batch_size, 3*self.nx*self.ny)
        return self.net(x)


class PDEFunc(nn.Module):
    """
    Model solution to a PDE as 
        u(x,y,t) = sum_{k=0}^K w_k(t) * f_k(x,y)

    Model f_k(.) as a convolutional neural network.
    We learn the parameters w_k(.) over time as an ODE.

    Notice this is very similar to a dynamic mixture 
    of experts (or ensemble) model.
    """
    
    def __init__(self, K, nx, ny):
        super().__init__()
        self.K = K
        self.nx, self.ny = nx, ny
        self.basis_coeffs = ODEFunc(self.K * 3, self.nx, self.ny)
        # self.basis_fns = nn.ModuleList([BasisFunc(self.nx, self.ny)
        #                                 for _ in range(self.K) ])
        self.basis_fns = nn.ModuleList([
            nn.Parameter(torch.normal(torch.zeros(3, self.nx, self.ny), 1))
            for _ in range(self.K)
        ])

    def forward(self, grid0, t):
        # grid0 = mb x 3 x nx x ny
        # t     = nt
        # coeff = nt x mb x K*3
        
        mb, nt = grid0.size(0), t.size(0)
        coeff = odeint(self.basis_coeffs, grid0, t.float())
        coeff = coeff.view(nt, mb, self.K, 3)
        
        soln = 0
        for k in range(self.K):
            f_k = self.basis_fns[k]  # (grid)
            f_k = f_k.unsqueeze(0).repeat(nt * mb, 1, 1, 1)
            f_k = f_k.view(nt, mb, 3, self.nx, self.ny)
            w_k = coeff[:, :, k, :, None, None]
            soln = soln + f_k * w_k
        
        return soln

    def basis_weight_mat(self):
        W = []
        for k in range(self.K):
            theta = list(self.basis_fns[k].parameters())
            theta = torch.cat([w.flatten() for w in theta])
            W.append(theta)
        return torch.stack(W)

    def diversity_penalty(self):
        W = self.basis_weight_mat()
        penalty = 0
        for i in range(0, self.K):
            for j in range(i, self.K):
                penalty = penalty + torch.norm(W[i] - W[j], p=2)
        penalty = 1. / penalty
        return penalty


class BasisFunc(nn.Module):
    """A basis to build up a function."""

    def __init__(self, nx, ny):
        super().__init__()
        self.nx, self.ny = nx, ny
        self.net = nn.Sequential(
            nn.Conv2d(3, 16, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 16, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 3, 1),
        )

    def forward(self, grid):
        return self.net(grid) 


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--npz-path', type=str, default=CHORIN_FD_DATA_FILE, 
                        help='where dataset is stored [default: CHORIN_FD_DATA_FILE]')
    parser.add_argument('--out-dir', type=str, default='./checkpoints/spectral', 
                        help='where to save checkpoints [default: ./checkpoints/spectral]')
    parser.add_argument('--batch-time', type=int, default=20, help='default: 20')
    parser.add_argument('--batch-size', type=int, default=10, help='default: 10')
    parser.add_argument('--n-iters', type=int, default=1000, help='default: 1000')
    parser.add_argument('--n-coeffs', type=int, default=10, help='default: 10')
    parser.add_argument('--gpu-device', type=int, default=0, help='default: 0')
    args = parser.parse_args()

    if not os.path.isdir(args.out_dir):
        os.makedirs(args.out_dir)

    device = (torch.device('cuda:' + str(args.gpu_device)
              if torch.cuda.is_available() else 'cpu'))

    data = np.load(args.npz_path)
    u, v, p = data['u'], data['v'], data['p']
    u = torch.from_numpy(u).float()
    v = torch.from_numpy(v).float()
    p = torch.from_numpy(p).float()
    obs = torch.stack([u, v, p]).permute(1, 0, 2, 3).to(device)
    nt, nx, ny = obs.size(0), obs.size(1), obs.size(2)
    obs = obs.unsqueeze(1)  # add a batch size of 1
    obs0 = obs[0]  # first timestep - shape: mb x 3 x nx x ny
    t = (torch.arange(nt) + 1).to(device)
    K = args.n_coeffs

    model = PDEFunc(K, nx, ny).to(device)
    optimizer = optim.Adam(model.parameters(), lr=1e-3)

    loss_meter = AverageMeter()

    tqdm_batch = tqdm(total=args.n_iters, desc="[Iteration]")
    for itr in range(1, args.n_iters + 1):
        optimizer.zero_grad()

        obs_pred = model(obs0, t, obs)
        loss = torch.norm(obs_pred - obs, p=2)
        penalty = model.diversity_penalty()
        loss = loss + penalty

        loss.backward()
        optimizer.step()
        loss_meter.update(loss.item())
        
        tqdm_batch.set_postfix({"Loss": loss_meter.avg})
        tqdm_batch.update()
    tqdm_batch.close()

    with torch.no_grad():
        obs_pred = model(obs0, t, obs)  # nt x mb x 3 nx x ny
        obs_pred = obs_pred.squeeze(1)
        obs_pred = obs_pred.cpu().detach().numpy()
        
    np.save(os.path.join(args.out_dir, 'extrapolation.npy'), obs_pred)
