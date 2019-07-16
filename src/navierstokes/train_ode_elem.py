"""
Trying to predict the entire system with an ODE 
fails rather miserably. Instead, maybe we can 
apply finite difference to define a spatial grid 
on the PDE. Then for each element in the spatial
grid, we can try to approximate it locally with 
an ODE. In other words, instead of a single ODE, 
use an ODE per element. 
"""

import os
import sys
import copy
import numpy as np
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from src.navierstokes.generate import DATA_DIR, DATA_SM_DIR
from src.navierstokes.models import ODEDiffEqElement
from src.navierstokes.utils import (
    spatial_coarsen, AverageMeter, save_checkpoint, 
    MODEL_DIR, dynamics_prediction_error_torch, 
    mean_squared_error, load_systems, numpy_to_torch)
from src.navierstokes.baseline import coarsen_fine_systems
from torchdiffeq import odeint_adjoint as odeint


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-time', type=int, default=50, 
                        help='batch of timesteps [default: 50]')
    parser.add_argument('--batch-size', type=int, default=100,
                        help='batch size [default: 100]')
    parser.add_argument('--epochs', type=int, default=2000,
                        help='number of epochs [default: 2000]')
    parser.add_argument('--lr', type=float, default=3e-4,
                        help='learning rate [default: 3e-4]')
    parser.add_argument('--test-only', action='store_true', default=False)
    args = parser.parse_args()

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # for reproducibility
    torch.manual_seed(1337)
    np.random.seed(1337)

    model_dir = os.path.join(MODEL_DIR, 'ode_grid')
    os.makedirs(model_dir, exist_ok=True)

    print('loading fine systems')
    u_fine, v_fine, p_fine = load_systems(DATA_DIR, fine=True)

    N = u_fine.shape[0]
    nx, ny = u_fine.shape[2], u_fine.shape[3]
    x_fine = np.linspace(0, 2, nx)  # slightly hardcoded
    y_fine = np.linspace(0, 2, ny)
    X_fine, Y_fine = np.meshgrid(x_fine, y_fine)
    u_coarsened, v_coarsened, p_coarsened = coarsen_fine_systems(
        X_fine, Y_fine, u_fine, v_fine, p_fine)

    # set some hyperparameters
    grid_dim = u_coarsened.shape[2]
    T = u_coarsened.shape[1]
    dt = 0.001
    timesteps = np.arange(T) * dt

    N = u_fine.shape[0]
    N_train = int(0.8 * N)
    N_val = int(0.1 * N)

    print('Divide data into train/val/test sets.')

    # get all momentum and pressure sequences in a matrix
    # shape: N_train x T x grid_size x grid_size
    train_u_mat = u_coarsened[:N_train, ...]
    train_v_mat = v_coarsened[:N_train, ...]
    train_p_mat = p_coarsened[:N_train, ...]

    val_u_mat = u_coarsened[N_train:(N_train+N_val), ...]
    val_v_mat = v_coarsened[N_train:(N_train+N_val), ...]
    val_p_mat = p_coarsened[N_train:(N_train+N_val), ...]

    test_u_mat = u_coarsened[N_train+N_val:, ...]
    test_v_mat = v_coarsened[N_train+N_val:, ...]
    test_p_mat = p_coarsened[N_train+N_val:, ...]

    print('Initialize model and optimizer.')

    module_list = []
    for i in range(grid_dim):
        for j in range(grid_dim):
            module = ODEDiffEqElement(
                i, j, grid_dim, hidden_dim=64, n_filters=32)
            module_list.append(module)
    model = nn.ModuleList(module_list)
    model = model.to(device)

    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    best_loss = np.inf
    val_loss_item = np.inf

    if not args.test_only:
        store_val_loss = np.zeros(args.epochs // 10 - 1) 

        pbar = tqdm(total=args.epochs)
        for iteration in range(args.epochs):
            model.train()
            # sample a batch of contiguous timesteps
            start_T = np.random.choice(np.arange(T - args.batch_time), size=args.batch_size)
            batch_I = np.random.choice(np.arange(N_train), size=args.batch_size)

            def build_batch(A, batch_indices, start_time_batch, time_lapse):
                # A = batch_size, T, grid_dim, grid_dim
                subA = A[batch_indices]
                batch_size = subA.shape[0]
                batchA = np.stack([
                    subA[i, start_time_batch[i]:start_time_batch[i]+time_lapse, ...]
                    for i in range(batch_size)
                ])
                return batchA

            batch_u = numpy_to_torch(build_batch(train_u_mat, batch_I, start_T, args.batch_time), device)
            batch_v = numpy_to_torch(build_batch(train_v_mat, batch_I, start_T, args.batch_time), device)
            batch_p = numpy_to_torch(build_batch(train_p_mat, batch_I, start_T, args.batch_time), device)
            # batch_obs (shape: B x T x 3 x C x H x W)
            batch_obs = torch.cat([ batch_u.unsqueeze(2), batch_v.unsqueeze(2), 
                                    batch_p.unsqueeze(2)], dim=2)
            # batch_obs (shape: T x B x 3 x C x H x W)
            batch_obs = batch_obs.permute(1, 0, 2, 3, 4).contiguous()
            batch_obs0 = batch_obs[0].clone()  # shape: B x 3 x C x H x W
            
            # we pretend each batch_t starts from 0
            batch_t = numpy_to_torch(timesteps[:args.batch_time], device)

            optimizer.zero_grad()

            loss = 0
            for i in range(grid_dim):
                for j in range(grid_dim):
                    ij = i * grid_dim + j
                    batch_obs0_ij = batch_obs0[:, :, i, j].clone()
                    batch_obs_ij = batch_obs[:, :, :, i, j].clone()
                    batch_obs_pred_ij = odeint(model[ij], batch_obs0_ij, batch_t)
                    loss_ij = torch.mean(torch.pow(batch_obs_pred_ij - 
                                                   batch_obs_ij, 2))
                    loss = loss + loss_ij

            loss.backward()
            optimizer.step()
            pbar.update() 
            pbar.set_postfix({'train loss': loss.item(),
                              'val_loss': val_loss_item})

            if iteration % 10 == 0 and iteration > 0:
                model.eval()
                with torch.no_grad():
                    # test on validation dataset as metric
                    val_u = numpy_to_torch(val_u_mat, device)
                    val_v = numpy_to_torch(val_v_mat, device)
                    val_p = numpy_to_torch(val_p_mat, device)
                    # val_obs (shape: N x T x 3 x C x H x W)
                    val_obs = torch.cat([val_u.unsqueeze(2), val_v.unsqueeze(2), 
                                         val_p.unsqueeze(2)], dim=2)
                    # val_obs (shape: T x N x 3 x C x H x W)
                    val_obs = val_obs.permute(1, 0, 2, 3, 4).contiguous()
                    val_obs0 = val_obs[0].clone()  # shape: N x 3 x C x H x W
                    t = numpy_to_torch(timesteps, device)

                    val_loss = 0
                    for i in range(grid_dim):
                        for j in range(grid_dim):
                            ij = i * grid_dim + j
                            val_obs0_ij = val_obs0[:, :, i, j].clone()
                            val_obs_ij = val_obs[:, :, :, i, j].clone()
                            val_obs_pred_ij = odeint(model[ij], val_obs0_ij, t)
                            val_loss_ij = torch.mean(torch.pow(
                                val_obs_pred_ij -  val_obs_ij, 2))
                            val_loss = val_loss + val_loss_ij

                    val_loss_item = val_loss.item()
                    pbar.set_postfix({'train loss': loss.item(),
                                      'val_loss': val_loss_item}) 

                    store_val_loss[iteration // 10 - 1] = val_loss_item

                    if val_loss.item() < best_loss:
                        best_loss = val_loss.item()
                        is_best = True

                    save_checkpoint({
                        'state_dict': model.state_dict(),
                        'val_loss': val_loss.item(),
                    }, is_best, model_dir)
                    
                    np.save(os.path.join(model_dir, 'val_loss.npy'), store_val_loss)

        pbar.close()

    # load the best model
    print('Loading best weights (by validation error).')
    checkpoint = torch.load(os.path.join(model_dir, 'model_best.pth.tar'))
    model.load_state_dict(checkpoint['state_dict'])
    model = model.eval()

    with torch.no_grad():
        print('Applying model to test set (no teacher forcing)')
        test_u = numpy_to_torch(test_u_mat, device)  # B x T x H x W
        test_v = numpy_to_torch(test_v_mat, device)  # B x T x H x W
        test_p = numpy_to_torch(test_p_mat, device)  # B x T x H x W
        
        test_u = test_u.permute(1, 0, 2, 3, 4)  # T x B x H x W
        test_v = test_v.permute(1, 0, 2, 3, 4)  # T x B x H x W
        test_p = test_p.permute(1, 0, 2, 3, 4)  # T x B x H x W

        t = numpy_to_torch(timesteps, device)

        test_obs = torch.cat([test_u.unsqueeze(2), test_v.unsqueeze(2), 
                              test_p.unsqueeze(2)], dim=2)
        obs0 = test_obs[0]

        pred_obs = torch.zeros_like(test_obs).numpy()
        for i in range(grid_dim):
            for j in range(grid_dim):
                ij = i * grid_dim + j
                obs0_ij = obs0[:, :, i, j].clone()
                pred_obs_ij = odeint(model[ij], obs0_ij, t)
                pred_obs[:, :, :, i, j] = pred_obs_ij.cpu().numpy()
        pred_obs = torch.from_numpy(pred_obs).float()
        pred_obs = pred_obs.to(device)

        pred_u, pred_v, pred_p = torch.chunk(pred_obs, 3, dim=2)
        pred_u, pred_v, pred_p = pred_u.contiguous(), pred_v.contiguous(), pred_p.contiguous()

        test_u_mse, test_v_mse, test_p_mse = dynamics_prediction_error_torch(
            test_u, test_v, test_p, pred_u, pred_v, pred_p, dim=2)
        
        test_u_mse = test_u_mse.cpu().numpy()
        test_v_mse = test_v_mse.cpu().numpy()
        test_p_mse = test_p_mse.cpu().numpy()

        np.savez(os.path.join(model_dir, 'test_error_no_teacher_forcing.npz'),
                 u_mse=test_u_mse, v_mse=test_v_mse, p_mse=test_p_mse)