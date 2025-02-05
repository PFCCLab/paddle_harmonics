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

import abc
import math
from functools import partial
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import numpy as np
import paddle
import paddle.nn as nn

from paddle_harmonics._disco_convolution import _disco_s2_contraction_paddle
from paddle_harmonics._disco_convolution import _disco_s2_contraction_triton
from paddle_harmonics._disco_convolution import _disco_s2_transpose_contraction_paddle
from paddle_harmonics._disco_convolution import _disco_s2_transpose_contraction_triton
from paddle_harmonics.quadrature import _precompute_grid  # noqa
from paddle_harmonics.quadrature import _precompute_latitudes
from paddle_harmonics.utils import paddle_aux  # noqa


def _compute_support_vals_isotropic(
    r: paddle.Tensor, phi: paddle.Tensor, nr: int, r_cutoff: float, norm: str = "s2"
):
    """
    Computes the index set that falls into the isotropic kernel's support and returns both indices and values.
    """

    # compute the support
    dr = (r_cutoff - 0.0) / nr
    ikernel = paddle.arange(nr).reshape(-1, 1, 1)
    ir = ikernel * dr

    if norm == "none":
        norm_factor = 1.0
    elif norm == "2d":
        norm_factor = (
            math.pi * (r_cutoff * nr / (nr + 1)) ** 2
            + math.pi * r_cutoff**2 * (2 * nr / (nr + 1) + 1) / (nr + 1) / 3
        )
    elif norm == "s2":
        norm_factor = (
            2
            * math.pi
            * (
                1
                - math.cos(r_cutoff - dr)
                + math.cos(r_cutoff - dr)
                + (math.sin(r_cutoff - dr) - math.sin(r_cutoff)) / dr
            )
        )
    else:
        raise ValueError(f"Unknown normalization mode {norm}.")

    # find the indices where the rotated position falls into the support of the kernel
    iidx = paddle.nonzero(((r - ir).abs() <= dr) & (r <= r_cutoff))
    vals = (1 - (r[iidx[:, 1], iidx[:, 2]] - ir[iidx[:, 0], 0, 0]).abs() / dr) / norm_factor
    return iidx, vals


def _compute_support_vals_anisotropic(
    r: paddle.Tensor,
    phi: paddle.Tensor,
    nr: int,
    nphi: int,
    r_cutoff: float,
    norm: str = "s2",
):
    """
    Computes the index set that falls into the anisotropic kernel's support and returns both indices and values.
    """

    # compute the support
    dr = (r_cutoff - 0.0) / nr
    dphi = 2.0 * math.pi / nphi
    kernel_size = (nr - 1) * nphi + 1
    ikernel = paddle.arange(kernel_size).reshape(-1, 1, 1)
    ir = ((ikernel - 1) // nphi + 1) * dr
    iphi = ((ikernel - 1) % nphi) * dphi

    if norm == "none":
        norm_factor = 1.0
    elif norm == "2d":
        norm_factor = (
            math.pi * (r_cutoff * nr / (nr + 1)) ** 2
            + math.pi * r_cutoff**2 * (2 * nr / (nr + 1) + 1) / (nr + 1) / 3
        )
    elif norm == "s2":
        norm_factor = (
            2
            * math.pi
            * (
                1
                - math.cos(r_cutoff - dr)
                + math.cos(r_cutoff - dr)
                + (math.sin(r_cutoff - dr) - math.sin(r_cutoff)) / dr
            )
        )
    else:
        raise ValueError(f"Unknown normalization mode {norm}.")

    # find the indices where the rotated position falls into the support of the kernel
    cond_r = ((r - ir).abs() <= dr) & (r <= r_cutoff)
    cond_phi = (
        (ikernel == 0) | ((phi - iphi).abs() <= dphi) | ((2 * math.pi - (phi - iphi).abs()) <= dphi)
    )
    iidx = paddle.nonzero(cond_r & cond_phi)
    vals = (1 - (r[iidx[:, 1], iidx[:, 2]] - ir[iidx[:, 0], 0, 0]).abs() / dr) / norm_factor
    vals *= paddle.where(
        iidx[:, 0] > 0,
        (
            1
            - paddle.minimum(
                (phi[iidx[:, 1], iidx[:, 2]] - iphi[iidx[:, 0], 0, 0]).abs(),
                (2 * math.pi - (phi[iidx[:, 1], iidx[:, 2]] - iphi[iidx[:, 0], 0, 0]).abs()),
            )
            / dphi
        ),
        1.0,
    ).astype(
        vals.dtype
    )  # 202411: add astype
    return iidx, vals


def _precompute_convolution_tensor_s2(
    in_shape,
    out_shape,
    kernel_shape,
    grid_in="equiangular",
    grid_out="equiangular",
    theta_cutoff=0.01 * math.pi,
):
    """
    Precomputes the rotated filters at positions $R^{-1}_j \omega_i = R^{-1}_j R_i \nu = Y(-\theta_j)Z(\phi_i - \phi_j)Y(\theta_j)\nu$.
    Assumes a tensorized grid on the sphere with an equidistant sampling in longitude as described in Ocampo et al.
    The output tensor has shape kernel_shape x nlat_out x (nlat_in * nlon_in).

    The rotation of the Euler angles uses the YZY convention, which applied to the northpole $(0,0,1)^T$ yields
    $$
    Y(\alpha) Z(\beta) Y(\gamma) n =
        {\begin{bmatrix}
            \cos(\gamma)\sin(\alpha) + \cos(\alpha)\cos(\beta)\sin(\gamma) \\
            \sin(\beta)\sin(\gamma) \\
            \cos(\alpha)\cos(\gamma)-\cos(\beta)\sin(\alpha)\sin(\gamma)
        \end{bmatrix}}
    $$
    """

    assert len(in_shape) == 2
    assert len(out_shape) == 2

    if len(kernel_shape) == 1:
        kernel_handle = partial(
            _compute_support_vals_isotropic,
            nr=kernel_shape[0],
            r_cutoff=theta_cutoff,
            norm="s2",
        )
    elif len(kernel_shape) == 2:
        kernel_handle = partial(
            _compute_support_vals_anisotropic,
            nr=kernel_shape[0],
            nphi=kernel_shape[1],
            r_cutoff=theta_cutoff,
            norm="s2",
        )
    else:
        raise ValueError("kernel_shape should be either one- or two-dimensional.")

    nlat_in, nlon_in = in_shape
    nlat_out, nlon_out = out_shape

    lats_in, _ = _precompute_latitudes(nlat_in, grid=grid_in)
    lats_in = paddle.to_tensor(lats_in).astype(dtype="float32")
    lats_out, _ = _precompute_latitudes(nlat_out, grid=grid_out)
    lats_out = paddle.to_tensor(lats_out).astype(dtype="float32")

    # array for accumulating non-zero indices
    out_idx = paddle.empty([3, 0], dtype="int64")
    out_vals = paddle.empty([0], dtype="int64")

    # compute the phi differences
    # It's imporatant to not include the 2 pi point in the longitudes, as it is equivalent to lon=0
    lons_in = paddle.linspace(0, 2 * math.pi, nlon_in + 1)[:-1]

    for t in range(nlat_out):
        # the last angle has a negative sign as it is a passive rotation, which rotates the filter around the y-axis
        alpha = -lats_out[t]
        beta = lons_in
        gamma = lats_in.reshape(-1, 1)

        # compute cartesian coordinates of the rotated position
        # This uses the YZY convention of Euler angles, where the last angle (alpha) is a passive rotation,
        # and therefore applied with a negative sign
        z = -paddle.cos(beta) * paddle.sin(alpha) * paddle.sin(gamma) + paddle.cos(
            alpha
        ) * paddle.cos(gamma)
        x = paddle.cos(alpha) * paddle.cos(beta) * paddle.sin(gamma) + paddle.cos(
            gamma
        ) * paddle.sin(alpha)
        y = paddle.sin(beta) * paddle.sin(gamma)

        # normalization is emportant to avoid NaNs when arccos and atan are applied
        # this can otherwise lead to spurious artifacts in the solution
        norm = paddle.sqrt(x * x + y * y + z * z)
        x = x / norm
        y = y / norm
        z = z / norm

        # compute spherical coordinates, where phi needs to fall into the [0, 2pi) range
        theta = paddle.acos(z)
        phi = paddle.atan2(y, x) + np.pi

        # find the indices where the rotated position falls into the support of the kernel
        iidx, vals = kernel_handle(theta, phi)

        # add the output latitude and reshape such that psi has dimensions kernel_shape x nlat_out x (nlat_in*nlon_in)
        idx = paddle.stack(
            [
                iidx[:, 0],
                t * paddle.ones_like(iidx[:, 0]),
                iidx[:, 1] * nlon_in + iidx[:, 2],
            ],
            axis=0,
        )

        # append indices and values to the COO datastructure
        out_idx = paddle.concat([out_idx, idx], axis=-1)
        out_vals = paddle.concat([out_vals, vals], axis=-1)

    return out_idx, out_vals


def _precompute_convolution_tensor_2d(
    grid_in, grid_out, kernel_shape, radius_cutoff=0.01, periodic=False
):
    """
    Precomputes the translated filters at positions $T^{-1}_j \omega_i = T^{-1}_j T_i \nu$. Similar to the S2 routine,
    only that it assumes a non-periodic subset of the euclidean plane
    """

    # check that input arrays are valid point clouds in 2D
    assert len(grid_in) == 2
    assert len(grid_out) == 2
    assert grid_in.shape[0] == 2
    assert grid_out.shape[0] == 2

    n_in = grid_in.shape[-1]
    n_out = grid_out.shape[-1]

    if len(kernel_shape) == 1:
        kernel_handle = partial(
            _compute_support_vals_isotropic,
            nr=kernel_shape[0],
            r_cutoff=radius_cutoff,
            norm="2d",
        )
    elif len(kernel_shape) == 2:
        kernel_handle = partial(
            _compute_support_vals_anisotropic,
            nr=kernel_shape[0],
            nphi=kernel_shape[1],
            r_cutoff=radius_cutoff,
            norm="2d",
        )
    else:
        raise ValueError("kernel_shape should be either one- or two-dimensional.")

    grid_in = grid_in.reshape(2, 1, n_in)
    grid_out = grid_out.reshape(2, n_out, 1)

    diffs = grid_in - grid_out
    if periodic:
        periodic_diffs = paddle.where(diffs > 0.0, diffs - 1, diffs + 1)
        diffs = paddle.where(diffs.abs() < periodic_diffs.abs(), diffs, periodic_diffs)

    r = paddle.sqrt(diffs[0] ** 2 + diffs[1] ** 2)
    phi = paddle.atan2(diffs[1], diffs[0]) + np.pi

    idx, vals = kernel_handle(r, phi)
    idx = idx.permute(1, 0)

    return idx, vals


class DiscreteContinuousConv(nn.Layer, metaclass=abc.ABCMeta):
    """
    Abstract base class for DISCO convolutions
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_shape: Union[int, List[int]],
        groups: Optional[int] = 1,
        bias: Optional[bool] = True,
    ):
        super().__init__()

        if isinstance(kernel_shape, int):
            self.kernel_shape = [kernel_shape]
        else:
            self.kernel_shape = kernel_shape

        if len(self.kernel_shape) == 1:
            self.kernel_size = self.kernel_shape[0]
        elif len(self.kernel_shape) == 2:
            self.kernel_size = (self.kernel_shape[0] - 1) * self.kernel_shape[1] + 1
        else:
            raise ValueError("kernel_shape should be either one- or two-dimensional.")

        # groups
        self.groups = groups

        # weight tensor
        if in_channels % self.groups != 0:
            raise ValueError(
                "Error, the number of input channels has to be an integer multiple of the group size"
            )
        if out_channels % self.groups != 0:
            raise ValueError(
                "Error, the number of output channels has to be an integer multiple of the group size"
            )
        self.groupsize = in_channels // self.groups
        scale = math.sqrt(1.0 / self.groupsize)
        self.weight = paddle.base.framework.EagerParamBase.from_tensor(
            scale * paddle.randn([out_channels, self.groupsize, self.kernel_size])
        )

        if bias:
            self.bias = paddle.base.framework.EagerParamBase.from_tensor(
                paddle.zeros([out_channels])
            )
        else:
            self.bias = None

    @abc.abstractmethod
    def forward(self, x: paddle.Tensor):
        raise NotImplementedError


class DiscreteContinuousConvS2(DiscreteContinuousConv):
    """
    Discrete-continuous convolutions (DISCO) on the 2-Sphere as described in [1].

    [1] Ocampo, Price, McEwen, Scalable and equivariant spherical CNNs by discrete-continuous (DISCO) convolutions, ICLR (2023), arXiv:2209.13603
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        in_shape: Tuple[int],
        out_shape: Tuple[int],
        kernel_shape: Union[int, List[int]],
        groups: Optional[int] = 1,
        grid_in: Optional[str] = "equiangular",
        grid_out: Optional[str] = "equiangular",
        bias: Optional[bool] = True,
        theta_cutoff: Optional[float] = None,
    ):
        super().__init__(in_channels, out_channels, kernel_shape, groups, bias)

        self.nlat_in, self.nlon_in = in_shape
        self.nlat_out, self.nlon_out = out_shape

        # compute theta cutoff based on the bandlimit of the input field
        if theta_cutoff is None:
            theta_cutoff = (self.kernel_shape[0] + 1) * np.pi / float(self.nlat_in - 1)

        if theta_cutoff <= 0.0:
            raise ValueError("Error, theta_cutoff has to be positive.")

        # integration weights
        _, wgl = _precompute_latitudes(self.nlat_in, grid=grid_in)
        quad_weights = (
            2.0
            * np.pi
            * paddle.to_tensor(wgl).astype(dtype="float32").reshape(-1, 1)
            / self.nlon_in
        )
        self.register_buffer("quad_weights", quad_weights, persistable=False)

        idx, vals = _precompute_convolution_tensor_s2(
            in_shape,
            out_shape,
            self.kernel_shape,
            grid_in=grid_in,
            grid_out=grid_out,
            theta_cutoff=theta_cutoff,
        )

        self.register_buffer("psi_idx", idx, persistable=False)
        self.register_buffer("psi_vals", vals, persistable=False)

    def get_psi(self):
        psi = paddle.sparse.sparse_coo_tensor(
            self.psi_idx,
            self.psi_vals,
            shape=(self.kernel_size, self.nlat_out, self.nlat_in * self.nlon_in),
        ).coalesce()
        return psi

    def forward(self, x: paddle.Tensor, use_triton_kernel: bool = True) -> paddle.Tensor:
        # pre-multiply x with the quadrature weights
        x = self.quad_weights * x

        psi = self.get_psi()

        if x.place.is_gpu_place() and use_triton_kernel:
            x = _disco_s2_contraction_triton(x, psi, self.nlon_out)
        else:
            x = _disco_s2_contraction_paddle(x, psi, self.nlon_out)

        # extract shape
        B, C, K, H, W = x.shape
        x = x.reshape(B, self.groups, self.groupsize, K, H, W)

        # do weight multiplication
        out = paddle.einsum(
            "bgckxy,gock->bgoxy",
            x,
            self.weight.reshape(self.groups, -1, self.weight.shape[1], self.weight.shape[2]),
        )
        out = out.reshape(out.shape[0], -1, out.shape[-2], out.shape[-1])

        if self.bias is not None:
            out = out + self.bias.reshape(1, -1, 1, 1)

        return out


class DiscreteContinuousConvTransposeS2(DiscreteContinuousConv):
    """
    Discrete-continuous transpose convolutions (DISCO) on the 2-Sphere as described in [1].

    [1] Ocampo, Price, McEwen, Scalable and equivariant spherical CNNs by discrete-continuous (DISCO) convolutions, ICLR (2023), arXiv:2209.13603
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        in_shape: Tuple[int],
        out_shape: Tuple[int],
        kernel_shape: Union[int, List[int]],
        groups: Optional[int] = 1,
        grid_in: Optional[str] = "equiangular",
        grid_out: Optional[str] = "equiangular",
        bias: Optional[bool] = True,
        theta_cutoff: Optional[float] = None,
    ):
        super().__init__(in_channels, out_channels, kernel_shape, groups, bias)

        self.nlat_in, self.nlon_in = in_shape
        self.nlat_out, self.nlon_out = out_shape

        # bandlimit
        if theta_cutoff is None:
            theta_cutoff = (self.kernel_shape[0] + 1) * np.pi / float(self.nlat_in - 1)

        if theta_cutoff <= 0.0:
            raise ValueError("Error, theta_cutoff has to be positive.")

        # integration weights
        _, wgl = _precompute_latitudes(self.nlat_in, grid=grid_in)
        quad_weights = (
            2.0
            * np.pi
            * paddle.to_tensor(wgl).astype(dtype="float32").reshape(-1, 1)
            / self.nlon_in
        )
        self.register_buffer("quad_weights", quad_weights, persistable=False)

        # switch in_shape and out_shape since we want transpose conv
        idx, vals = _precompute_convolution_tensor_s2(
            out_shape,
            in_shape,
            self.kernel_shape,
            grid_in=grid_out,
            grid_out=grid_in,
            theta_cutoff=theta_cutoff,
        )

        self.register_buffer("psi_idx", idx, persistable=False)
        self.register_buffer("psi_vals", vals, persistable=False)

    def get_psi(self):
        psi = paddle.sparse.sparse_coo_tensor(
            self.psi_idx,
            self.psi_vals,
            shape=(self.kernel_size, self.nlat_in, self.nlat_out * self.nlon_out),
        ).coalesce()
        return psi

    def forward(self, x: paddle.Tensor, use_triton_kernel: bool = True) -> paddle.Tensor:
        # extract shape
        B, C, H, W = x.shape
        x = x.reshape(B, self.groups, self.groupsize, H, W)

        # do weight multiplication
        x = paddle.einsum(
            "bgcxy,gock->bgokxy",
            x,
            self.weight.reshape(self.groups, -1, self.weight.shape[1], self.weight.shape[2]),
        )
        x = x.reshape(x.shape[0], -1, x.shape[-3], x.shape[-2], x.shape[-1])

        # pre-multiply x with the quadrature weights
        x = self.quad_weights * x

        psi = self.get_psi()

        if x.place.is_gpu_place() and use_triton_kernel:
            out = _disco_s2_transpose_contraction_triton(x, psi, self.nlon_out)
        else:
            out = _disco_s2_transpose_contraction_paddle(x, psi, self.nlon_out)

        if self.bias is not None:
            out = out + self.bias.reshape(1, -1, 1, 1)

        return out
