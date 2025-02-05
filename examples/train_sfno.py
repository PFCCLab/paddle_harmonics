# coding=utf-8

# SPDX-FileCopyrightText: Copyright (c) 2022 The torch-harmonics Authors. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import os
import time
from functools import partial

import matplotlib.pyplot as plt
import numpy as np
import paddle
import pandas as pd
from paddle import amp
from paddle.io import DataLoader

# wandb logging
import wandb
from paddle_harmonics.examples.sfno import PdeDataset
from paddle_harmonics.utils import paddle_aux  # noqa

wandb.login()


def l2loss_sphere(solver, prd, tar, relative=False, squared=True):
    loss = solver.integrate_grid((prd - tar) ** 2, dimensionless=True).sum(axis=-1)
    if relative:
        loss = loss / solver.integrate_grid(tar**2, dimensionless=True).sum(axis=-1)

    if not squared:
        loss = paddle.sqrt(loss)
    loss = loss.mean()

    return loss


def spectral_l2loss_sphere(solver, prd, tar, relative=False, squared=True):
    # compute coefficients
    coeffs = paddle.as_real(x=solver.sht(prd - tar))
    coeffs = coeffs[..., 0] ** 2 + coeffs[..., 1] ** 2
    norm2 = coeffs[..., :, 0] + 2 * paddle.sum(x=coeffs[..., :, 1:], axis=-1)
    loss = paddle.sum(x=norm2, axis=(-1, -2))
    if relative:
        tar_coeffs = paddle.as_real(x=solver.sht(tar))
        tar_coeffs = tar_coeffs[..., 0] ** 2 + tar_coeffs[..., 1] ** 2
        tar_norm2 = tar_coeffs[..., :, 0] + 2 * paddle.sum(x=tar_coeffs[..., :, 1:], axis=-1)
        tar_norm2 = paddle.sum(x=tar_norm2, axis=(-1, -2))
        loss = loss / tar_norm2
    if not squared:
        loss = paddle.sqrt(x=loss)
    loss = loss.mean()
    return loss


def spectral_loss_sphere(solver, prd, tar, relative=False, squared=True):
    # gradient weighting factors
    lmax = solver.sht.lmax
    ls = paddle.arange(end=lmax).astype(dtype="float32")
    spectral_weights = (ls * (ls + 1)).reshape(1, 1, -1, 1).to(prd.place)

    # compute coefficients
    coeffs = paddle.as_real(x=solver.sht(prd - tar))
    coeffs = coeffs[..., 0] ** 2 + coeffs[..., 1] ** 2
    coeffs = spectral_weights * coeffs
    norm2 = coeffs[..., :, 0] + 2 * paddle.sum(x=coeffs[..., :, 1:], axis=-1)
    loss = paddle.sum(x=norm2, axis=(-1, -2))

    if relative:
        tar_coeffs = paddle.as_real(x=solver.sht(tar))
        tar_coeffs = tar_coeffs[..., 0] ** 2 + tar_coeffs[..., 1] ** 2
        tar_coeffs = spectral_weights * tar_coeffs
        tar_norm2 = tar_coeffs[..., :, 0] + 2 * paddle.sum(x=tar_coeffs[..., :, 1:], axis=-1)
        tar_norm2 = paddle.sum(x=tar_norm2, axis=(-1, -2))
        loss = loss / tar_norm2

    if not squared:
        loss = paddle.sqrt(x=loss)
    loss = loss.mean()

    return loss


def h1loss_sphere(solver, prd, tar, relative=False, squared=True):
    # gradient weighting factors
    lmax = solver.sht.lmax
    ls = paddle.arange(end=lmax).astype(dtype="float32")
    spectral_weights = (ls * (ls + 1)).reshape(1, 1, -1, 1).to(prd.place)

    # compute coefficients
    coeffs = paddle.as_real(x=solver.sht(prd - tar))
    coeffs = coeffs[..., 0] ** 2 + coeffs[..., 1] ** 2
    h1_coeffs = spectral_weights * coeffs
    h1_norm2 = h1_coeffs[..., :, 0] + 2 * paddle.sum(x=h1_coeffs[..., :, 1:], axis=-1)
    l2_norm2 = coeffs[..., :, 0] + 2 * paddle.sum(x=coeffs[..., :, 1:], axis=-1)
    h1_loss = paddle.sum(x=h1_norm2, axis=(-1, -2))
    l2_loss = paddle.sum(x=l2_norm2, axis=(-1, -2))

    # strictly speaking this is not exactly h1 loss
    if not squared:
        loss = paddle.sqrt(x=h1_loss) + paddle.sqrt(x=l2_loss)
    else:
        loss = h1_loss + l2_loss

    if relative:
        raise NotImplementedError("Relative H1 loss not implemented")

    loss = loss.mean()

    return loss


def fluct_l2loss_sphere(solver, prd, tar, inp, relative=False, polar_opt=0):
    # compute the weighting factor first
    fluct = solver.integrate_grid((tar - inp) ** 2, dimensionless=True, polar_opt=polar_opt)
    weight = fluct / paddle.sum(x=fluct, axis=-1, keepdim=True)
    # weight = weight.reshape(*weight.shape, 1, 1)

    loss = weight * solver.integrate_grid((prd - tar) ** 2, dimensionless=True, polar_opt=polar_opt)
    if relative:
        loss = loss / (
            weight * solver.integrate_grid(tar**2, dimensionless=True, polar_opt=polar_opt)
        )
    loss = paddle.mean(x=loss)
    return loss


# rolls out the FNO and compares to the classical solver
def autoregressive_inference(
    model,
    dataset,
    path_root,
    nsteps,
    autoreg_steps=10,
    nskip=1,
    plot_channel=0,
    nics=20,
):

    model.eval()

    losses = np.zeros(nics)
    fno_times = np.zeros(nics)
    nwp_times = np.zeros(nics)

    for iic in range(nics):
        ic = dataset.solver.random_initial_condition(mach=0.2)
        inp_mean = dataset.inp_mean
        inp_var = dataset.inp_var

        prd = (dataset.solver.spec2grid(ic) - inp_mean) / paddle.sqrt(inp_var)
        prd = prd.unsqueeze(axis=0)
        uspec = ic.clone()

        # ML model
        start_time = time.time()
        for i in range(1, autoreg_steps + 1):
            # evaluate the ML model
            prd = model(prd)

            if iic == nics - 1 and nskip > 0 and i % nskip == 0:

                # do plotting
                fig = plt.figure(figsize=(7.5, 6))
                dataset.solver.plot_griddata(prd[0, plot_channel], fig, vmax=4, vmin=-4)
                plt.savefig(path_root + "_pred_" + str(i // nskip) + ".png")
                plt.clf()

        fno_times[iic] = time.time() - start_time

        # classical model
        start_time = time.time()
        for i in range(1, autoreg_steps + 1):

            # advance classical model
            uspec = dataset.solver.timestep(uspec, nsteps)

            if iic == nics - 1 and i % nskip == 0 and nskip > 0:
                ref = (dataset.solver.spec2grid(uspec) - inp_mean) / paddle.sqrt(inp_var)

                fig = plt.figure(figsize=(7.5, 6))
                dataset.solver.plot_griddata(ref[plot_channel], fig, vmax=4, vmin=-4)
                plt.savefig(path_root + "_truth_" + str(i // nskip) + ".png")
                plt.clf()

        nwp_times[iic] = time.time() - start_time

        # ref = (dataset.solver.spec2grid(uspec) - inp_mean) / paddle.sqrt(inp_var)
        ref = dataset.solver.spec2grid(uspec)
        prd = prd * paddle.sqrt(inp_var) + inp_mean
        losses[iic] = l2loss_sphere(dataset.solver, prd, ref, relative=True).item()

    return losses, fno_times, nwp_times


# convenience function for logging weights and gradients
def log_weights_and_grads(model, iters=1):
    """
    Helper routine intended for debugging purposes
    """
    root_path = os.path.join(os.path.dirname(__file__), "weights_and_grads")

    weights_and_grads_fname = os.path.join(root_path, f"weights_and_grads_step{iters:03d}.tar")
    print(weights_and_grads_fname)

    weights_dict = {k: v for k, v in model.named_parameters()}
    grad_dict = {k: v.grad for k, v in model.named_parameters()}

    store_dict = {"iteration": iters, "grads": grad_dict, "weights": weights_dict}
    paddle.save(obj=store_dict, path=weights_and_grads_fname)


# training function
def train_model(
    model,
    dataloader,
    optimizer,
    gscaler,
    scheduler=None,
    nepochs=20,
    nfuture=0,
    num_examples=256,
    num_valid=8,
    loss_fn="l2",
    enable_amp=False,
    log_grads=0,
):

    train_start = time.time()

    # count iterations
    iters = 0

    for epoch in range(nepochs):

        # time each epoch
        epoch_start = time.time()

        dataloader.dataset.set_initial_condition("random")
        dataloader.dataset.set_num_examples(num_examples)

        # get the solver for its convenience functions
        solver = dataloader.dataset.solver

        # do the training
        acc_loss = 0
        model.train()

        for inp, tar in dataloader:

            with amp.auto_cast(enable=enable_amp):

                prd = model(inp)
                for _ in range(nfuture):
                    prd = model(prd)

                if loss_fn == "l2":
                    loss = l2loss_sphere(solver, prd, tar, relative=False)
                elif loss_fn == "spectral l2":
                    loss = spectral_l2loss_sphere(solver, prd, tar, relative=False)
                elif loss_fn == "h1":
                    loss = h1loss_sphere(solver, prd, tar, relative=False)
                elif loss_fn == "spectral":
                    loss = spectral_loss_sphere(solver, prd, tar, relative=False)
                elif loss_fn == "fluct":
                    loss = fluct_l2loss_sphere(solver, prd, tar, inp, relative=True)
                else:
                    raise NotImplementedError(f"Unknown loss function {loss_fn}")

            acc_loss += loss.item() * inp.shape[0]

            optimizer.clear_gradients(set_to_zero=not True)
            gscaler.scale(loss).backward()

            if log_grads and iters % log_grads == 0:
                log_weights_and_grads(model, iters=iters)

            gscaler.step(optimizer)
            gscaler.update()

            iters += 1

        acc_loss = acc_loss / len(dataloader.dataset)

        dataloader.dataset.set_initial_condition("random")
        dataloader.dataset.set_num_examples(num_valid)

        # perform validation
        valid_loss = 0
        model.eval()
        with paddle.no_grad():
            for inp, tar in dataloader:
                prd = model(inp)
                for _ in range(nfuture):
                    prd = model(prd)
                loss = l2loss_sphere(solver, prd, tar, relative=True)

                valid_loss += loss.item() * inp.shape[0]

        valid_loss = valid_loss / len(dataloader.dataset)

        if scheduler is not None:
            scheduler.step(valid_loss)

        epoch_time = time.time() - epoch_start

        print("--------------------------------------------------------------------------------")
        print(f"Epoch {epoch} summary:")
        print(f"time taken: {epoch_time}")
        print(f"accumulated training loss: {acc_loss}")
        print(f"relative validation loss: {valid_loss}")

        if wandb.run is not None:
            current_lr = optimizer.get_lr()
            wandb.log(
                {
                    "loss": acc_loss,
                    "validation loss": valid_loss,
                    "learning rate": current_lr,
                }
            )

    train_time = time.time() - train_start

    print("--------------------------------------------------------------------------------")
    print(f"done. Training took {train_time}.")  # noqa
    return valid_loss


def main(train=True, load_checkpoint=False, enable_amp=False, log_grads=0):

    # set seed
    paddle.seed(seed=333)

    # set device
    device = str("cuda:0" if paddle.device.cuda.device_count() >= 1 else "cpu").replace(
        "cuda", "gpu"
    )
    if paddle.device.cuda.device_count() >= 1:
        paddle.device.set_device(device=device)

    # 1 hour prediction steps
    dt = 1 * 3600
    dt_solver = 150
    nsteps = dt // dt_solver
    dataset = PdeDataset(dt=dt, nsteps=nsteps, dims=(256, 512), device=device, normalize=True)
    # There is still an issue with parallel dataloading. Do NOT use it at the moment
    # dataloader = DataLoader(dataset, batch_size=4, shuffle=True, num_workers=4, persistent_workers=True)
    dataloader = DataLoader(
        dataset,
        batch_size=4,
        shuffle=True,
        num_workers=0,
    )

    nlat = dataset.nlat
    nlon = dataset.nlon

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if not p.stop_gradient)

    # prepare dicts containing models and corresponding metrics
    models = {}
    metrics = {}

    from paddle_harmonics.examples.sfno import SphericalFourierNeuralOperatorNet as SFNO

    models["sfno_sc3_layer4_e16_linskip_nomlp"] = partial(
        SFNO,
        spectral_transform="sht",
        img_size=(nlat, nlon),
        grid="equiangular",
        num_layers=4,
        scale_factor=3,
        embed_dim=16,
        operator_type="driscoll-healy",
        big_skip=False,
        pos_embed=False,
        use_mlp=False,
        normalization_layer="none",
    )
    # models["sfno_sc3_layer4_e256_noskip_mlp"]   = partial(SFNO, spectral_transform='sht', img_size=(nlat, nlon),  grid="equiangular",
    #                                                       num_layers=4, scale_factor=3, embed_dim=256, operator_type='driscoll-healy',
    #                                                       big_skip=False, pos_embed=False, use_mlp=True, normalization_layer="none")
    # from torch_harmonics.examples.sfno.models.unet import UNet
    # models['unet_baseline'] = partial(UNet)

    # # U-Net if installed
    # from models.unet import UNet
    # models['unet_baseline'] = partial(UNet)

    # SFNO models
    # models['sfno_sc3_layer4_edim256_linear']    = partial(SFNO, spectral_transform='sht', img_size=(nlat, nlon), grid="equiangular",
    #                                                  num_layers=4, scale_factor=3, embed_dim=256, operator_type='driscoll-healy')
    # # FNO models
    # models['fno_sc3_layer4_edim256_linear']     = partial(SFNO, spectral_transform='fft', img_size=(nlat, nlon), grid="equiangular",
    #                                                  num_layers=4, scale_factor=3, embed_dim=256, operator_type='diagonal')

    # iterate over models and train each model
    root_path = os.path.dirname(__file__)
    for model_name, model_handle in models.items():

        model = model_handle().to(device)

        print(model)

        metrics[model_name] = {}

        num_params = count_parameters(model)
        print(f"number of trainable params: {num_params}")
        if isinstance(num_params, paddle.Tensor):
            num_params = num_params.item()
        metrics[model_name]["num_params"] = num_params

        if load_checkpoint:
            model.set_state_dict(
                state_dict=paddle.load(
                    path=str(os.path.join(root_path, "checkpoints/" + model_name))
                )
            )

        # run the training
        if train:
            run = wandb.init(
                project="sfno ablations spherical swe",
                group=model_name,
                name=model_name + "_" + str(time.time()),
                config=model_handle.keywords,
            )

            # optimizer:
            optimizer = paddle.optimizer.Adam(
                parameters=model.parameters(), learning_rate=0.003, weight_decay=0.0
            )
            tmp_lr = paddle.optimizer.lr.ReduceOnPlateau(
                mode="min", learning_rate=optimizer.get_lr()
            )
            optimizer.set_lr_scheduler(tmp_lr)
            scheduler = tmp_lr
            gscaler = paddle.amp.GradScaler(
                enable=enable_amp, incr_every_n_steps=2000, init_loss_scaling=65536.0
            )
            start_time = time.time()

            print(f"Training {model_name}, single step")
            train_model(
                model,
                dataloader,
                optimizer,
                gscaler,
                scheduler,
                nepochs=10,
                loss_fn="l2",
                enable_amp=enable_amp,
                log_grads=log_grads,
            )

            # # multistep training
            # print(f'Training {model_name}, two step')
            # optimizer = paddle.optim.Adam(model.parameters(), lr=5E-5)
            # scheduler = paddle.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min')
            # gscaler = amp.GradScaler(enabled=enable_amp)
            # dataloader.dataset.nsteps = 2 * dt//dt_solver
            # train_model(model, dataloader, optimizer, gscaler, scheduler, nepochs=20, nfuture=1, enable_amp=enable_amp)
            # dataloader.dataset.nsteps = 1 * dt//dt_solver

            training_time = time.time() - start_time

            run.finish()

            paddle.save(
                obj=model.state_dict(),
                path=os.path.join(root_path, "checkpoints/" + model_name),
            )

        # set seed
        paddle.seed(seed=333)

        with paddle.no_grad():
            losses, fno_times, nwp_times = autoregressive_inference(
                model,
                dataset,
                os.path.join(root_path, "figures/" + model_name),
                nsteps=nsteps,
                autoreg_steps=10,
            )
            metrics[model_name]["loss_mean"] = np.mean(losses)
            metrics[model_name]["loss_std"] = np.std(losses)
            metrics[model_name]["fno_time_mean"] = np.mean(fno_times)
            metrics[model_name]["fno_time_std"] = np.std(fno_times)
            metrics[model_name]["nwp_time_mean"] = np.mean(nwp_times)
            metrics[model_name]["nwp_time_std"] = np.std(nwp_times)
            if train:
                metrics[model_name]["training_time"] = training_time

    df = pd.DataFrame(metrics)
    df.to_pickle(os.path.join(root_path, "output_data/metrics.pkl"))


if __name__ == "__main__":
    main(train=True, load_checkpoint=False, enable_amp=False, log_grads=0)
