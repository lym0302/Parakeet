# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import paddle
from typing import Union


def is_broadcastable(shp1, shp2):
    for a, b in zip(shp1[::-1], shp2[::-1]):
        if a == 1 or b == 1 or a == b:
            pass
        else:
            return False
    return True


def masked_fill(xs: paddle.Tensor,
                mask: paddle.Tensor,
                value: Union[float, int]):
    assert is_broadcastable(xs.shape, mask.shape) is True
    bshape = paddle.broadcast_shape(xs.shape, mask.shape)
    mask = mask.broadcast_to(bshape)
    trues = paddle.ones_like(xs) * value
    mask = mask.cast(dtype=paddle.bool)
    xs = paddle.where(mask, trues, xs)
    return xs