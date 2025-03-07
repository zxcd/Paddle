# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved
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

import numpy as np
import tensorrt as trt

from paddle.tensorrt.converter_utils import (
    add_cast_reduce_layer,
    add_elementwise_layer,
    add_reduce_layer,
    broadcast,
    cast_tensor,
    fill_constant_layer,
    get_axes_for_reduce_op,
    trt_cast,
    trt_div,
    trt_expand,
    trt_floor_div,
    trt_max,
    trt_mul,
    trt_sub,
)
from paddle.tensorrt.register import converter_registry


@converter_registry.register("pd_op.add", trt_version="8.x")
@converter_registry.register("pd_op.add_", trt_version="8.x")
def add_converter(network, paddle_op, inputs):
    return add_elementwise_layer(
        network, paddle_op, inputs, trt.ElementWiseOperation.SUM
    )


@converter_registry.register("pd_op.scale", trt_version="8.x")
def scale_converter(network, paddle_op, inputs):
    scale = paddle_op.operands()[1].source().get_defining_op().attrs()["value"]
    bias = paddle_op.attrs().get("bias", 0.0)
    power = paddle_op.attrs().get("power", 1.0)

    # Convert scale, bias, and power to TensorRT weights
    scale_weight = trt.Weights(np.array([scale], dtype=np.float32))
    bias_weight = trt.Weights(np.array([bias], dtype=np.float32))
    power_weight = trt.Weights(np.array([power], dtype=np.float32))

    scale_layer = network.add_scale(
        inputs[0],
        mode=trt.ScaleMode.UNIFORM,
        shift=bias_weight,
        scale=scale_weight,
        power=power_weight,
    )
    return scale_layer.get_output(0)


@converter_registry.register("pd_op.max", trt_version="8.x")
def max_converter(network, paddle_op, inputs):
    input_tensor = inputs[0]
    axis = paddle_op.operands()[1].source().get_defining_op().attrs()["value"]
    input_shape = paddle_op.operands()[0].source().shape
    keepdim = paddle_op.attrs()["keepdim"]
    if network.has_implicit_batch_dimension:
        assert (
            axis != 0
        ), "can't reduce on axis == 0 when network has implicit batch dimension"
    output_shape = []
    if len(axis) == 0:
        axis = list(range(len(input_shape)))
    for i in range(len(axis)):
        if axis[i] < 0:
            axis[i] = len(input_shape) + axis[i]
    layer = network.add_reduce(
        input_tensor,
        trt.ReduceOperation.MAX,
        axes=get_axes_for_reduce_op(axis),
        keep_dims=keepdim,
    )
    return layer.get_output(0)


@converter_registry.register("pd_op.divide", trt_version="8.x")
def divide_converter(network, paddle_op, inputs):
    return add_elementwise_layer(
        network, paddle_op, inputs, trt.ElementWiseOperation.DIV
    )


@converter_registry.register("pd_op.subtract", trt_version="8.x")
def substract_converter(network, paddle_op, inputs):
    return add_elementwise_layer(
        network, paddle_op, inputs, trt.ElementWiseOperation.SUB
    )


@converter_registry.register("pd_op.multiply", trt_version="8.x")
def multiply_converter(network, paddle_op, inputs):
    return add_elementwise_layer(
        network, paddle_op, inputs, trt.ElementWiseOperation.PROD
    )


@converter_registry.register("pd_op.clip", trt_version="8.x")
def clip_converter(network, paddle_op, inputs):
    def _get_constant_or_expand_tensor(
        op, constant_inputs, input_shape_tensor, rank
    ):
        if op.name() == "pd_op.full":
            value = op.attrs()["value"]
            return fill_constant_layer(
                network, input_shape_tensor, rank, value, input_tensor.dtype
            )
        else:
            expanded_tensor = trt_expand(
                network, constant_inputs, 1, input_shape_tensor, rank
            )
            if expanded_tensor.dtype != input_tensor.dtype:
                expanded_tensor = cast_tensor(
                    network, expanded_tensor, input_tensor.dtype
                )
            return expanded_tensor

    input_tensor = inputs[0]
    input_shape = paddle_op.operands()[0].source().shape
    rank = len(input_shape)
    input_shape_tensor = network.add_shape(input_tensor).get_output(0)

    # handle min operation
    min_op = paddle_op.operands()[1].source().get_defining_op()
    alpha_t = _get_constant_or_expand_tensor(
        min_op, inputs[1], input_shape_tensor, rank
    )

    # handle max operation
    max_op = paddle_op.operands()[2].source().get_defining_op()
    beta_t = _get_constant_or_expand_tensor(
        max_op, inputs[2], input_shape_tensor, rank
    )

    # run the clip operation
    lower_clip = trt_max(network, input_tensor, alpha_t)
    layer = network.add_elementwise(
        lower_clip, beta_t, trt.ElementWiseOperation.MIN
    )
    return layer.get_output(0)


@converter_registry.register("pd_op.remainder", trt_version="8.x")
@converter_registry.register("pd_op.remainder_", trt_version="8.x")
def remainder_converter(network, paddle_op, inputs):
    weight_shape = paddle_op.operands()[1].source().shape
    input_shape = paddle_op.operands()[0].source().shape

    weight_tensor = inputs[1]
    input_tensor = inputs[0]
    if type(inputs[1]) == trt.Weights:
        weight_tensor = network.add_constant(
            weight_shape, inputs[1]
        ).get_output(0)
    if type(inputs[0]) == trt.Weights:
        input_tensor = network.add_constant(input_shape, inputs[0]).get_output(
            0
        )

    lhs_val, rhs_val = broadcast(
        network,
        input_tensor,
        weight_tensor,
        input_tensor.name,
        weight_tensor.name,
    )

    # Check if floor division is needed
    is_floor_div = input_tensor.dtype != trt.DataType.INT32

    # Floor division
    quotient = (
        trt_floor_div(network, lhs_val, rhs_val)
        if is_floor_div
        else trt_div(network, lhs_val, rhs_val)
    )

    # Multiply rhs by the quotient
    product = trt_mul(network, rhs_val, quotient)

    # Subtract the product from lhs to get the remainder
    remainder = trt_sub(network, lhs_val, product)

    return remainder


@converter_registry.register("pd_op.min", trt_version="8.x")
def min_converter(network, paddle_op, inputs):
    return add_reduce_layer(network, paddle_op, inputs, trt.ReduceOperation.MIN)


@converter_registry.register("pd_op.sum", trt_version="8.x")
def sum_converter(network, paddle_op, inputs):
    return add_reduce_layer(network, paddle_op, inputs, trt.ReduceOperation.SUM)


@converter_registry.register("pd_op.any", trt_version="8.x")
def any_converter(network, paddle_op, inputs):
    return add_cast_reduce_layer(
        network, paddle_op, inputs, trt.ReduceOperation.MAX
    )


@converter_registry.register("pd_op.all", trt_version="8.x")
def all_converter(network, paddle_op, inputs):
    return add_cast_reduce_layer(
        network, paddle_op, inputs, trt.ReduceOperation.MIN
    )


@converter_registry.register("pd_op.floor_divide", trt_version="8.x")
def floor_divide_converter(network, paddle_op, inputs):
    return add_elementwise_layer(
        network, paddle_op, inputs, trt.ElementWiseOperation.FLOOR_DIV
    )


@converter_registry.register("pd_op.log", trt_version="8.x")
def sqrt_converter(network, paddle_op, inputs):
    input_tensor = trt_cast(network, inputs[0], trt.float32)
    layer = network.add_unary(input_tensor, trt.UnaryOperation.LOG)
    return layer.get_output(0)
