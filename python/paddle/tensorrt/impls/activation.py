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
    get_trt_plugin,
    trt_mul,
)
from paddle.tensorrt.register import converter_registry

activation_type_map = {
    "pd_op.tanh": trt.ActivationType.TANH,
    "pd_op.relu": trt.ActivationType.RELU,
    "pd_op.sigmoid": trt.ActivationType.SIGMOID,
    "pd_op.silu": trt.ActivationType.SIGMOID,
    "pd_op.swish": trt.ActivationType.SIGMOID,
}


@converter_registry.register("pd_op.relu", trt_version="8.x")
@converter_registry.register("pd_op.tanh", trt_version="8.x")
@converter_registry.register("pd_op.sigmoid", trt_version="8.x")
def activation_converter(network, paddle_op, inputs):
    layer = network.add_activation(
        inputs[0], activation_type_map[paddle_op.name()]
    )
    return layer.get_output(0)


@converter_registry.register("pd_op.softmax", trt_version="8.x")
def softmax_converter(network, paddle_op, inputs):
    axis = paddle_op.attrs().get("axis", 0)
    if axis < 0:
        axis = len(inputs[0].shape) + axis

    softmax_layer = network.add_softmax(inputs[0])
    softmax_layer.axes = 1 << axis
    return softmax_layer.get_output(0)


@converter_registry.register("pd_op.gelu", trt_version="8.x")
def gelu_converter(network, paddle_op, inputs):
    input_val = inputs[0]
    approximate = paddle_op.attrs()["approximate"]
    if approximate:
        raise RuntimeError(
            "GeLU converter currently doesn't support fast gelu compute"
        )

    plugin_name = "CustomGeluPluginDynamic"
    type_id = trt.PluginField(
        "type_id", np.array(0, dtype=np.int32), trt.PluginFieldType.INT32
    )

    filed_collection = trt.PluginFieldCollection([type_id])
    plugin_version = "1"

    plugin = get_trt_plugin(plugin_name, filed_collection, plugin_version)

    layer = network.add_plugin_v2([input_val], plugin)
    return layer.get_output(0)


@converter_registry.register("pd_op.hardsigmoid", trt_version="8.x")
def hardsigmoid_converter(network, paddle_op, inputs):
    x = inputs[0]
    slope = paddle_op.attrs()["slope"]
    offset = paddle_op.attrs()["offset"]
    hardsigmoid_layer = network.add_activation(
        x, trt.ActivationType.HARD_SIGMOID
    )
    hardsigmoid_layer.alpha = slope
    hardsigmoid_layer.beta = offset
    return hardsigmoid_layer.get_output(0)


@converter_registry.register("pd_op.hardswish", trt_version="8.x")
def hardswish_converter(network, paddle_op, inputs):
    x = inputs[0]
    threshold = 6.0
    scale = 6.0
    offset = 3.0
    hardsigmoid_layer = network.add_activation(
        x, trt.ActivationType.HARD_SIGMOID
    )
    hardsigmoid_layer.alpha = 1.0 / scale
    hardsigmoid_layer.beta = offset / scale
    hardswish_layer = network.add_elementwise(
        x, hardsigmoid_layer.get_output(0), trt.ElementWiseOperation.PROD
    )
    return hardswish_layer.get_output(0)


@converter_registry.register("pd_op.softplus", trt_version="8.x")
def softplus_converter(network, paddle_op, inputs):
    x = inputs[0]
    beta = paddle_op.attrs()["beta"]
    threshold = paddle_op.attrs()["threshold"]
    layer_clip = network.add_activation(x, trt.ActivationType.CLIP)
    layer_clip.alpha = -3.40282e038
    layer_clip.beta = threshold / beta

    softplus_layer = network.add_activation(
        layer_clip.get_output(0), trt.ActivationType.SOFTPLUS
    )
    softplus_layer.alpha = 1.0 / beta
    softplus_layer.beta = beta
    return softplus_layer.get_output(0)


@converter_registry.register("pd_op.swish", trt_version="8.x")
@converter_registry.register("pd_op.silu", trt_version="8.x")
def swish_silu_converter(network, paddle_op, inputs):
    layer_output = network.add_activation(
        inputs[0], activation_type_map[paddle_op.name()]
    ).get_output(0)
    return trt_mul(network, inputs[0], layer_output)
