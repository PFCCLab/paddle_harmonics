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

import paddle
import paddle.nn as nn

# complex activation functions


class ComplexCardioid(nn.Layer):
    """
    Complex Cardioid activation function
    """

    def __init__(self):
        super(ComplexCardioid, self).__init__()

    def forward(self, z: paddle.Tensor) -> paddle.Tensor:
        out = 0.5 * (1.0 + paddle.cos(z.angle())) * z
        return out


class ComplexReLU(nn.Layer):
    """
    Complex-valued variants of the ReLU activation function
    """

    def __init__(self, negative_slope=0.0, mode="real", bias_shape=None, scale=1.0):
        super(ComplexReLU, self).__init__()

        # store parameters
        self.mode = mode
        if self.mode in ["modulus", "halfplane"]:
            if bias_shape is not None:
                self.bias = nn.Parameter(scale * paddle.ones(bias_shape, dtype=paddle.float32))
            else:
                self.bias = nn.Parameter(scale * paddle.ones((1), dtype=paddle.float32))
        else:
            self.bias = 0

        self.negative_slope = negative_slope
        self.act = nn.LeakyReLU(negative_slope=negative_slope)

    def forward(self, z: paddle.Tensor) -> paddle.Tensor:

        if self.mode == "cartesian":
            zr = paddle.as_real(z)
            za = self.act(zr)
            out = paddle.as_complex(za)

        elif self.mode == "modulus":
            zabs = paddle.sqrt(paddle.square(z.real) + paddle.square(z.imag))
            out = paddle.where(zabs + self.bias > 0, (zabs + self.bias) * z / zabs, 0.0)

        elif self.mode == "cardioid":
            out = 0.5 * (1.0 + paddle.cos(z.angle())) * z

        # elif self.mode == "halfplane":
        #     # bias is an angle parameter in this case
        #     modified_angle = paddle.angle(z) - self.bias
        #     condition = paddle.logical_and( (0. <= modified_angle), (modified_angle < np.pi/2.) )
        #     out = paddle.where(condition, z, self.negative_slope * z)

        elif self.mode == "real":
            zr = paddle.as_real(z)
            outr = zr.clone()
            outr[..., 0] = self.act(zr[..., 0])
            out = paddle.as_complex(outr)
        else:
            raise NotImplementedError

        return out
