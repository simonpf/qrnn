"""
quantnn.models.pytorch.encoders
===============================

Generic encoder modules.
"""
from dataclasses import dataclass
from math import log
from typing import Optional, Callable, Union, Optional, List, Dict, Tuple

import torch
from torch import nn
from quantnn.packed_tensor import PackedTensor, forward
from quantnn.models.pytorch.base import ParamCount
from quantnn.models.pytorch.blocks import ConvBlockFactory
from quantnn.models.pytorch.aggregators import BlockAggregatorFactory


DEFAULT_BLOCK_FACTORY = ConvBlockFactory(
    kernel_size=3, norm_factory=nn.BatchNorm2d, activation_factory=nn.ReLU
)

DEFAULT_AGGREGATOR_FACTORY = BlockAggregatorFactory(
    ConvBlockFactory(kernel_size=1, norm_factory=None, activation_factory=None)
)


class SequentialStageFactory(nn.Sequential):
    """
    A stage consisting of a simple sequence of blocks.
    """

    def __call__(
        self,
        channels_in: int,
        channels_out: int,
        n_blocks: int,
        block_factory: Callable[[int], nn.Module],
        downsample: Optional[Union[Tuple[int], int]] = None,
        block_args: Optional[List] = None,
        block_kwargs: Optional[Dict] = None,
    ):
        """
        Args:
            channels_in: The number of channels in the input to the
                first block.
            channels_out: The number of channels in the input to
                all other blocks an the output from the last block.
            n_blocks: The number of blocks in the stage.
            block_factory: The factory to use to create the blocks
                in the stage.
            downsample: Whether to include a downsampling layer
                at the beginning of the stage.
            block_args: Optional list of object that will be passed as
                positional arguments to the block factory.
            block_kwargs: Optional dict mapping parameter names to objects
                that will be passed as additional keyword arguments to
                the block factory.
        """
        if block_args is None:
            block_args = []
        if block_kwargs is None:
            block_kwargs = {}

        blocks = []
        for block_ind in range(n_blocks):
            blocks.append(
                block_factory(
                    channels_in,
                    channels_out,
                    *block_args,
                    downsample=downsample,
                    block_index=block_ind,
                    **block_kwargs,
                )
            )
            channels_in = channels_out
            downsample = None

        if len(blocks) == 1:
            return blocks[0]

        return nn.Sequential(*blocks)

def _calculate_output_scales(base_scale, downsampling_factors):
    """
    Calculate output scales for skip connections.
    """
    scl = base_scale
    scales = []
    for f_d in downsampling_factors:
        scl = f_d * scl
        scales.append(scl)
    return scales


class SpatialEncoder(nn.Module, ParamCount):
    """
    An encoder for spatial information.

    The encoder takes a 4D input (batch x channel x height x width),
    and encodes the input  into one or more feature maps with reduced
    spatial extent but typically higher number of features.

    Due to the downsampling applied between stages of the encoder, each stage
    produces feature maps at different spatial scales. These intermediate
    feature maps are commonly forwarded to a decoder through skip connection.
    The SpatialEncoder class supports both extracting only the encoding at the
    highest spatial scale, i.e. the output from the last encoder stage, or
    extracting features at all intermediate and the final scale of the encoder.
    """
    def __init__(
        self,
        channels: List[int],
        stage_depths: List[int],
        downsampling_factors: Optional[Union[List[int], int]] = None,
        block_factory: Optional[Callable[[int, int], nn.Module]] = None,
        stage_factory: Optional[Callable[[int, int], nn.Module]] = None,
        downsampler_factory: Callable[[int, int], nn.Module] = None,
        stem_factory: Callable[[int], nn.Module] = None,
        base_scale: int = 1
    ):
        """
        Args:
            channels: A list specifying the number of features (or channels)
                in each stage of the encoder.
            stage_depths: A list containing the number of block in each stage
                of the encoder.
            downsampling_factors: A list of downsampling factors specifying
                the degree of spatial downsampling applied between all stages
                in the encoder. For a constant downsampling factor
                between all layers this can be set to a single 'int'. Otherwise
                a list of length ``len(channels) - 1`` should be provided.
            block_factory: Factory to create the blocks in each stage.
            stage_factory: Optional stage factory to create the encoder
                stages. Defaults to ``SequentialStageFactory``.
            downsampler_factory: Optional factory to create downsampling
                layers. If not provided, the block factory must provide
                downsampling functionality.
            stem_factory: A factory that takes a number of output channels and
                produces a stem module that is applied to the inputs prior
                to feeding them into the first stage of the encoder.
            base_scale: An integer representing the scale of the input to
                the encoder. Will be used to calculate the scales corresponding
                to each stage of the encoder.
        """
        super().__init__()

        n_stages = len(stage_depths)
        self.n_stages = n_stages

        if not len(channels) == self.n_stages:
            raise ValueError(
                "The list of given channel numbers must match the number "
                "of stages."
            )
        self.channels = channels

        if downsampling_factors is None:
            downsampling_factors = [2] * (n_stages - 1)
        if len(stage_depths) != len(downsampling_factors) + 1:
            raise ValueError(
                "The list of downsampling factors numbers must have one "
                "element less than the number of stages."
            )

        # No downsampling applied in first layer.
        downsampling_factors = [1] + downsampling_factors

        self.base_scale = base_scale
        self.scales = _calculate_output_scales(base_scale, downsampling_factors)

        if block_factory is None:
            block_factory = DEFAULT_BLOCK_FACTORY
        if stage_factory is None:
            stage_factory = SequentialStageFactory()

        # Populate list of down samplers and stages.
        self.downsamplers = nn.ModuleList()
        self.stages = nn.ModuleList()
        channels_in = channels[0]
        for scale, stage_depth, channels_out, f_dwn in zip(
                self.scales,
                stage_depths,
                channels,
                downsampling_factors
        ):
            if downsampler_factory is None:
                self.downsamplers.append(nn.Identity())
                self.stages.append(
                    stage_factory(
                        channels_in,
                        channels_out,
                        stage_depth,
                        block_factory,
                        downsample=f_dwn,
                        block_kwargs={"scale": scale},
                    )
                )
            else:
                down = f_dwn if isinstance(f_dwn, int) else max(f_dwn)
                if down > 1:
                    self.downsamplers.append(
                        downsampler_factory(channels_in, channels_out, f_dwn)
                    )
                else:
                    self.downsamplers.append(nn.Identity())

                self.stages.append(
                    stage_factory(
                        channels_out,
                        channels_out,
                        stage_depth,
                        block_factory,
                        downsample=None,
                        block_kwargs={"scale": scale},
                    )
                )
            channels_in = channels_out

        if stem_factory is not None:
            self.stem = stem_factory(channels[0])
        else:
            self.stem = None

    @property
    def skip_connections(self) -> Dict[int, int]:
        """
        Dictionary specifying the number of channels in the skip tensors
        produced by this encoder.
        """
        return {scl: chans for scl, chans in zip(self.scales, self.channels)}

    def forward_with_skips(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Legacy implementation of the forward_with_skips function of the
        SpatialEncoder.

        Args:
            x: A ``torch.Tensor`` to feed into the encoder.

        Return:
            A list containing the outputs of each encoder stage with the
            last element in the list corresponding to the output of the
            last encoder stage.
        """
        y = x
        skips = {}
        for scl, down, stage in zip(self.scales, self.downsamplers, self.stages):
            y = stage(down(y))
            skips[scl] = y
        return skips

    def forward(
        self, x: torch.Tensor, return_skips: bool = False
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """
        Args:
            x: A ``torch.Tensor`` to feed into the encoder.
            return_skips: Whether or not the feature maps from all
                stages of the encoder should be returned.

        Return:
            If ``return_skips`` is ``False`` only the feature maps output
            by the last encoder stage are returned. Otherwise a dict mapping
            scales to feature maps of the corresponding stage are returned.
        """
        if self.stem is not None:
            x = self.stem(x)

        if return_skips:
            return self.forward_with_skips(x)

        y = x

        for down, stage in zip(self.downsamplers, self.stages):
            y = stage(down(y))

        return y


class MultiInputSpatialEncoder(SpatialEncoder, ParamCount):
    """
    The MultiIinputSpatialEncoder is a special SpatialEncoder that supports
    multiple inputs at possibly different scales.

    Each input has its own stem. The inputs encoder by the respective
    stems are then merged with feature maps of the encoder using special
    aggregator blocks.
    """
    def __init__(
        self,
        inputs: Dict[str, int],
        channels: Union[int],
        stage_depths: List[int],
        downsampling_factors: List[int] = None,
        stem_factories: Optional[Dict[str, Callable[[int], nn.Module]]] = None,
        block_factory: Optional[Callable[[int, int], nn.Module]] = None,
        aggregator_factory: Optional[Callable[[int], nn.Module]] = None,
        stage_factory: Optional[Callable[[int, int], nn.Module]] = None,
        downsampler_factory: Callable[[int, int, int], nn.Module] = None,
        base_scale: int = 1
    ):
        """
        inputs: A dictionary mapping input names to either to the
             corresponding scale of the encoder input.
        stem_factories: A dictionary mapping input names to stem
             factories.
        channels: A list specifying the channels in each stage of the
             encoder.
        stage_depths: The depth of each stage in the encoder.
        block_factory: Factory to create the blocks in each stage.
        stage_factory: Optional stage factory to create the encoder
            stages. Defaults to ``SequentialStageFactory``.
        downsampler_factory: Optional factory to create downsampling
            layers. If not provided, the block factory must provide
            downsampling functionality.
        aggregator_factory: Factory to create block to merge inputs.
        base_scale: The scale of the input with the highest resolution.
        """
        super().__init__(
            channels,
            stage_depths,
            downsampling_factors=downsampling_factors,
            block_factory=block_factory,
            stage_factory=stage_factory,
            downsampler_factory=downsampler_factory,
            stem_factory=None,
            base_scale=base_scale,
        )

        if downsampler_factory is None:
            self.aggregate_after = True
        else:
            self.aggregate_after = False

        if aggregator_factory is None:
            aggregator_factory = DEFAULT_AGGREGATOR_FACTORY

        self.stems = nn.ModuleDict()
        self.aggregators = nn.ModuleDict()

        # Parse inputs into stage_inputs, which maps stage indices to
        # input names, and create stems.
        self.stems = nn.ModuleDict()
        self.stage_inputs = {
            scale: [] for scale in self.scales
        }

        if stem_factories is None:
            stem_factories = {
                name: lambda x: nn.Identity() for name in inputs
            }

        # Mapping of scales to corresponding channels numbers.
        scale_channels = {
            scl: channels for scl, channels in zip(self.scales, channels)
        }

        for input_name, scale in inputs.items():

            if not scale in self.scales:
                raise ValueError(
                    f"Input '{input_name}' has scale {scale}, which doesn't "
                    f" match any stage of the encoder."
                )

            self.stems[input_name] = stem_factories[input_name](
                scale_channels[scale]
            )
            self.stage_inputs[scale].append(input_name)

        # Create aggregators for all inputs at each scale.
        for ind, (scale, names) in enumerate(self.stage_inputs.items()):
            if scale > self.base_scale:
                # If downsampling happens within stage the number of
                # channels in the encoder feature maps is still that of
                # the previous stage.
                #if downsampler_factory is None:
                #    self.aggregators[str(scale)] = aggregator_factory(
                #        (channels[ind - 1],) * (len(names) + 1),
                #        channels[ind - 1]
                #    )
                #else:
                self.aggregators[str(scale)] = aggregator_factory(
                    (channels[ind],) * (len(names) + 1),
                    channels[ind]
                )
            # Multiple inputs a base scale.
            elif len(names) > 1:
                self.aggregators[str(scale)] = aggregator_factory(
                    (channels[scale],) * len(names), channels[ind]
                )


    def forward_with_skips(self, x: torch.Tensor) -> Dict[set, torch.Tensor]:
        """
        Args:
            x: A ``torch.Tensor`` to feed into the encoder.

        Return:
            A list containing the outputs of each encoder stage with the
            last element in the list corresponding to the output of the
            last encoder stage.
        """
        skips = {}
        y = None

        for scale, down, stage in zip(self.scales, self.downsamplers, self.stages):

            # Apply downsampling
            if down is not None:
                y = down(y)

            # Collect inputs
            agg_inputs = []
            inputs = self.stage_inputs[scale]
            for inpt in inputs:
                x_in = self.stems[inpt](x[inpt])
                agg_inputs.append(x_in)

            # Aggregate and propagate through stage
            if y is not None and self.aggregate_after:
                y = stage(y)

            if len(agg_inputs) > 1:
                y = self.aggregators[str(scale)](*agg_inputs)
            elif len(agg_inputs) == 1:
                y = agg_inputs[0]

            if not self.aggregate_after:
                y = stage(y)

            skips[scale] = y

        return skips

    def forward(
        self, x: torch.Tensor, return_skips: bool = False
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """
        Args:
            x: A dictionary mapping input names to corresponding input
                tensors.
            return_skips: Whether or not the feature maps from all
                stages of the encoder should be returned.

        Return:
            If ``return_skips`` is ``False`` only the feature maps output
            by the last encoder stage are returned. Otherwise a list containing
            the feature maps from all stages are returned with the last element
            in the list corresponding to the output of the last encoder stage.
        """
        if not isinstance(x, dict):
            raise ValueError(
                "A multi-input encoder expects a dict of tensors "
                " mapping input names to corresponding input "
                " tensors."
            )

        if return_skips:
            return self.forward_with_skips(x)

        y = None

        for scale, down, stage in zip(self.scales, self.downsamplers, self.stages):

            # Apply downsampling
            if down is not None:
                y = down(y)

            # Collect inputs
            agg_inputs = []
            inputs = self.stage_inputs[scale]
            for inpt in inputs:
                x_in = self.stems[inpt](x[inpt])
                agg_inputs.append(x_in)

            # Aggregate and propagate through stage
            if y is not None and self.aggregate_after:
                y = stage(y)

            if len(agg_inputs) > 1:
                y = self.aggregators[str(scale)](*agg_inputs)
            elif len(agg_inputs) == 1:
                y = agg_inputs[0]
            if not self.aggregate_after:
                y = stage(y)

        return y

class DenseEncoder(nn.Module, ParamCount):
    """
    Implements a vertical slice of a dense encoder.
    """
    def __init__(
        self,
        channels: List[int],
        scales: List[int],
        level_index: int,
        depth: int,
        block_factory: Callable[[int, int], nn.Module],
        downsampler_factory: Callable[[int, int, int], nn.Module],
        upsampler_factory: Callable[[int, int, int], nn.Module],
        aggregator_factory: Callable[[int, int, int], nn.Module],
        input_aggregator_factory: Callable[[int, int, int], nn.Module] = None,
    ):
        """
        Args:
            channels: A list containing the number of channels at each scale.
            scales: A list defining the scales of the encoder.
            level_index: Index specifying the level of the encoder.

        """
        super().__init__()
        blocks = []
        aggregators = []
        projections = []
        downsamplers = []
        upsamplers = []

        if level_index >= depth + len(scales):
            raise ValueError(
                "The level index of the parallel encoder level must be "
                " strictly  less than the sum of the encoder's depth "
                "and number of scales."
            )

        scales_start = max(0, level_index - depth + 1)
        scales_end = min(level_index + 1, len(channels))
        self.scales_start = scales_start
        self.level_index = level_index
        self.depth = depth

        for scale_index in range(scales_start, scales_end):

            ch_in_all = []

            if scale_index > 0:
                ch_in_lower = channels[scale_index - 1]
                ch_in_all.append(ch_in_lower)
                f_down = scales[scale_index] // scales[scale_index - 1]
                downsamplers.append(downsampler_factory(ch_in_lower, ch_in_lower, f_down))
            else:
                downsamplers.append(None)

            ch_in_all.append(channels[scale_index])

            if scale_index < scales_end - 1:
                ch_in_higher = channels[scale_index + 1]
                ch_in_all.append(ch_in_lower)
                f_up = scales[scale_index + 1] // scales[scale_index]
                upsamplers.append(downsampler_factory(ch_in_higher, ch_in_higher, f_up))

            aggregators.append(aggregator_factory(ch_in_all, ch_out))
            blocks.append(block_factory(ch_out, ch_out))

        self.downsamplers = nn.ModuleList(downsamplers)
        self.upsamplers = nn.ModuleList(upsamplers)
        self.blocks = nn.ModuleList(blocks)


    def forward(self, x, x_in=None):
        results = x[: max(self.level_index - self.depth + 1, 0)]
        for offset, block in enumerate(self.blocks):

            scale_index = self.scales_start + offset
            up = self.upsamplers[offset]
            down = self.upsamplers[offset]

            inputs = []
            if up is not None:
                x_up = forward(up, x[scale_index - 1])
                inputs.append(x_up)
            inputs.append(x[scale_index])
            if down is not None:
                x_down = forward(down, x_scale_index + 1)
                inputs.append(x_down)

            agg = self.aggregators[offset]
            y = forward(block, agg(*inputs))
            results.append(y)

        return results


class ParallelEncoderLevel(nn.Module, ParamCount):
    """
    Implements a single level of a parallel encoder. Each level
    processes features maps of a certain total processing depth until the
    given depth at the highest scale is reached.
    """

    def __init__(
        self,
        channels: List[int],
        scales: List[int],
        level_index: int,
        depth: int,
        block_factory: Callable[[int, int], nn.Module],
        downsampler_factory: Callable[[int, int, int], nn.Module],
        aggregator_factory: Callable[[int, int, int], nn.Module],
        input_aggregator_factory: Callable[[int, int, int], nn.Module] = None,
    ):
        """
        Args:
            channels: A list containing the number of channels at each scale.
            scales: A list defining the scales of the encoder.
            level_index: Index specifying the level of the encoder.

        """
        super().__init__()
        blocks = []
        aggregators = []
        projections = []
        downsamplers = []

        if level_index >= depth + len(scales):
            raise ValueError(
                "The level index of the parallel encoder level must be "
                " strictly  less than the sum of the encoder's depth "
                "and number of scales."
            )

        scales_start = max(0, level_index - depth + 1)
        scales_end = min(level_index + 1, len(channels))
        self.scales_start = scales_start
        self.level_index = level_index
        self.depth = depth

        for scale_index in range(scales_start, scales_end):
            if scale_index == 0:
                ch_in = channels[scale_index]
                blocks.append(block_factory(ch_in, ch_in))
            else:
                ch_in_1 = channels[scale_index - 1]
                ch_in_2 = channels[scale_index]

                # Project inputs to same channel number.
                if ch_in_1 != ch_in_2:
                    projections.append(nn.Conv2d(ch_in_1, ch_in_2, kernel_size=1))
                else:
                    projections.append(nn.Identity())

                # Downsample inputs from lower scale
                f_down = scales[scale_index] // scales[scale_index - 1]
                downsamplers.append(downsampler_factory(ch_in_2, ch_in_2, f_down))

                # Combine inputs
                aggregators.append(aggregator_factory((ch_in_2,) * 2, ch_in_2))

                blocks.append(block_factory(ch_in_2, ch_in_2))

        self.downsamplers = nn.ModuleList(downsamplers)
        self.projections = nn.ModuleList(projections)
        self.aggregators = nn.ModuleList(aggregators)
        self.blocks = nn.ModuleList(blocks)

        if input_aggregator_factory is not None:
            self.agg_in = input_aggregator_factory(
                (channels[level_index],) * 2,
                channels[level_index],
            )

    def forward(self, x, x_in=None):
        results = x[: max(self.level_index - self.depth + 1, 0)]
        for offset, block in enumerate(self.blocks):
            scale_index = self.scales_start + offset
            if scale_index == 0:
                results.append(forward(block, x[scale_index]))
            elif scale_index < len(x):
                index = offset
                if self.scales_start == 0:
                    index -= 1
                proj = self.projections[index]
                downsampler = self.downsamplers[index]
                agg = self.aggregators[index]
                x_1 = forward(downsampler, forward(proj, x[scale_index - 1]))
                x_2 = x[scale_index]
                results.append(forward(block, agg(x_1, x_2)))
            else:
                index = offset
                if self.scales_start == 0:
                    index -= 1
                proj = self.projections[index]
                downsampler = self.downsamplers[index]
                y = forward(downsampler, forward(proj, x[scale_index - 1]))
                if x_in is not None:
                    y = self.agg_in(y, x_in)
                results.append(forward(block, y))
        return results


class ParallelEncoder(nn.Module):
    """
    The parallel encoder produces a multi-scale representation of
    the input but processes scales in parallel.
    """

    def __init__(
        self,
        inputs: Dict[int, int],
        channels: List[int],
        scales: List[int],
        depth: int,
        block_factory: Callable[[int, int], nn.Module],
        downsampler_factory: Callable[[int, int, int], nn.Module],
        input_aggregator_factory: Callable[[int, int, int], nn.Module],
        aggregator_factory: Callable[[int, int, int], nn.Module],
    ):
        """
        Args:
            inputs: A dict mapping the levels of the encoder to the corresponding
                input channels.
            channels: The number of channels for each encoder level.
            n_stages: The number of stages in the encoder. For the parallel
                encoder this means.

        """
        super().__init__()
        self.depth = depth
        self.n_stages = len(channels)
        self.inputs = inputs
        stages = []
        for level_index in range(self.depth + self.n_stages - 1):
            agg_in = None
            if level_index in inputs:
                agg_in = input_aggregator_factory
            stages.append(
                ParallelEncoderLevel(
                    channels=channels,
                    scales=scales,
                    level_index=level_index,
                    depth=depth,
                    block_factory=block_factory,
                    downsampler_factory=downsampler_factory,
                    aggregator_factory=aggregator_factory,
                    input_aggregator_factory=agg_in,
                )
            )
        self.stages = nn.ModuleList(stages)

        self.stems = nn.ModuleDict({})
        self.aggregators = nn.ModuleDict({})
        for stage, ch_in in inputs.items():
            self.stems[f"stem_{stage}"] = block_factory(ch_in, channels[stage])
            if len(self.stems) > 1:
                self.aggregators[f"aggregator_{stage}"] = input_aggregator_factory(
                    (channels[stage],) * 2, channels[stage]
                )

    def forward(self, x):

        y = [forward(self.stems[f"stem_0"], x[0])]
        input_index = 1

        for stage_index in range(self.depth + self.n_stages - 1):
            x_in = None
            if stage_index > 0 and stage_index in self.inputs:
                stem = self.stems[f"stem_{stage_index}"]
                x_in = forward(stem, x[input_index])
                input_index += 1
            y = self.stages[stage_index](y, x_in=x_in)
        return y


class CascadingEncoder(nn.Module):
    def __init__(
            self,
            channels: Union[int, List[int]],
            stage_depths: List[int],
            block_factory: Optional[Callable[[int, int], nn.Module]] = None,
            channel_scaling: int = 2,
            max_channels: int = None,
            downsampler_factory: Callable[[int, int], nn.Module] = None,
            upsampler_factory: Callable[[int, int], nn.Module] = None,
            downsampling_factors: List[int] = None,
            stem_factory: Callable[[int], nn.Module] = None,
            base_scale: int = 1,
            **kwargs
    ):
        """
        A cascading encoder processes stages in an overlapping fashion. Each
        stage begins processing as soon as the output from the first
        convolutional block of the previous stage is available. The cascading
        encoder also includes densely connects block across different stages.

        Args:
            channels: A list specifying the number of features (or channels)
                at the end of each stage of the encoder.
            stages: A list containing the stage specifications for each
                stage in the encoder.
            block_factory: Factory to create the blocks in each stage.
            channel_scaling: Scaling factor specifying the increase of the
                number of channels after every downsampling layer. Only used
                if channels is an integer.
            max_channels: Cutoff value to limit the number of channels. Only
                used if channels is an integer.
            downsampler_factory: The downsampler factory is currently ignored.
            downsampling_factors: The downsampling factors applied to the outputs
                of all but the last stage. For a constant downsampling factor
                between all layers this can be set to a single 'int'. Otherwise
                a list of length ``len(channels) - 1`` should be provided.
            stem_factory: A factory that takes a number of output channels and
                produces a stem module that is applied to the inputs prior
                to feeding them into the first stage of the encoder.
        """
        super().__init__()

        if block_factory is None:
            block_factory = DEFAULT_BLOCK_FACTORY

        self.channel_scaling = channel_scaling
        self.downsamplers = nn.ModuleList()
        self.upsamplers = nn.ModuleList()

        n_stages = len(stages)
        if isinstance(channels, int):
            channels = [channels * channel_scaling**i for i in range(n_stages)]
            if max_channels is not None:
                channels = [min(ch, max_channels) for ch in channels]
        self.channels = channels

        if not len(channels) == len(stages):
            raise ValueError(
                "The list of given channel numbers must match the number " "of stages."
            )

        if downsampling_factors is None:
            downsampling_factors = [2] * (n_stages - 1)
        if len(stages) != len(downsampling_factors) + 1:
            raise ValueError(
                "The list of downsampling factors numbers must have one "
                "element less than the number of stages."
            )

        # No downsampling applied in first layer.
        downsampling_factors = [1] + downsampling_factors
        scale = 1
        self.scales = []
        for f_d in downsampling_factors:
            self.scales.append(scale)
            scale *= f_d

        try:
            stages = [
                stage if isinstance(stage, StageConfig) else StageConfig(stage)
                for stage in stages
            ]
        except ValueError:
            raise ValueError(
                "'stages' must be a list of 'StageConfig' or 'int'  objects."
            )

        self.scales = _calculate_output_scales(base_scale, downsampling_factors)

        channels_in = channels[0]
        modules = []
        module_map = {}

        if downsampler_factory is None:
            def downsampler_factory(channels_in, channels_out, f_down):
                return nn.AvgPool2d(kernel_size=f_down, stride=f_down)

        def upsampler_factory(f_up):
            return nn.Upsample(scale_factor=f_up)


        stage_ind = 0
        for stage, channels_out, f_dwn in zip(stages, channels, downsampling_factors):
            # Downsampling layer is included in stage.


            down = f_dwn if isinstance(f_dwn, int) else max(f_dwn)
            if down > 1:
                self.downsamplers.append(
                    downsampler_factory(channels_in, channels_out, f_dwn)
                )
                self.upsamplers.append(
                    upsampler_factory(f_dwn)
                )
            else:
                self.downsamplers.append(None)
                self.upsamplers.append(None)


            for block_ind in range(stage.n_blocks):

                chans_combined = channels_in
                if (block_ind > 1) and (stage_ind < n_stages - 1):
                    chans_combined += channels[stage_ind + 1]
                if (
                        (stage_ind > 0) and
                        (block_ind > 0) and
                        (stages[stage_ind - 1].n_blocks > block_ind)
                ):
                    chans_combined += channels[stage_ind - 1]

                mod = block_factory(
                    chans_combined,
                    channels_out,
                    downsample=None,
                    *stage.block_args,
                    **stage.block_kwargs
                )
                modules.append(mod)
                depth_map = module_map.setdefault(block_ind + stage_ind, [])
                depth_map.append((stage_ind, mod))
                channels_in = channels_out
            stage_ind += 1

        self.modules = nn.ModuleList(modules)
        self.module_map = module_map


        if stem_factory is not None:
            self.stem = stem_factory(channels[0])
        else:
            self.stem = None

        self.depth = max(list(self.module_map.keys())) + 1

    @property
    def skip_connections(self) -> Dict[int, int]:
        """
        Dictionary specifying the number of channels in the skip tensors
        produced by this encoder.
        """
        return {scl: chans for scl, chans in zip(self.scales, self.channels)}


    def forward(self, x: torch.Tensor, **kwargs) -> Dict[int, torch.Tensor]:
        """
        Forward input through encoder.

        Args:
            x: The input tensor.

        Return:
            A dict mapping stage indices to corresponding tensors.
        """
        if self.stem is None:
            x_in = {0: x}
        else:
            x_in = {0: self.stem(x)}

        results = {}

        for d_i in range(self.depth):

            y = {}
            for stage_ind, mod in self.module_map[d_i]:

                inputs = []

                if stage_ind - 1 in x_in:
                    down = self.downsamplers[stage_ind]
                    inputs.append(down(x_in[stage_ind - 1]))

                if stage_ind in x_in:
                    inputs.append(x_in[stage_ind])

                if stage_ind + 1 in x_in:
                    up = self.upsamplers[stage_ind + 1]
                    inputs.append(up(x_in[stage_ind + 1]))

                y[stage_ind] = mod(torch.cat(inputs, 1))

            x_in = y
            results.update(y)

        return {self.scales[ind]: tensor for ind, tensor in results.items()}


class DenseCascadingEncoder(nn.Module):
    def __init__(
            self,
            channels: Union[int, List[int]],
            stages: List[int],
            block_factory: Optional[Callable[[int, int], nn.Module]] = None,
            channel_scaling: int = 2,
            max_channels: int = None,
            downsampler_factory: Callable[[int, int], nn.Module] = None,
            upsampler_factory: Callable[[int, int], nn.Module] = None,
            downsampling_factors: List[int] = None,
            stem_factory: Callable[[int], nn.Module] = None,
            base_scale: int = 1,
            **kwargs
    ):
        """
        A dense cascading encoder adds dense connections between convolution
        blocks along each stage to the cascading encoder.

        Args:
            channels: A list specifying the number of features (or channels)
                at the end of each stage of the encoder.
            stages: A list containing the stage specifications for each
                stage in the encoder.
            block_factory: Factory to create the blocks in each stage.
            channel_scaling: Scaling factor specifying the increase of the
                number of channels after every downsampling layer. Only used
                if channels is an integer.
            max_channels: Cutoff value to limit the number of channels. Only
                used if channels is an integer.
            downsampler_factory: The downsampler factory is currently ignored.
            downsampling_factors: The downsampling factors applied to the outputs
                of all but the last stage. For a constant downsampling factor
                between all layers this can be set to a single 'int'. Otherwise
                a list of length ``len(channels) - 1`` should be provided.
            stem_factory: A factory that takes a number of output channels and
                produces a stem module that is applied to the inputs prior
                to feeding them into the first stage of the encoder.
        """
        super().__init__()

        if block_factory is None:
            block_factory = DEFAULT_BLOCK_FACTORY

        self.channel_scaling = channel_scaling
        self.downsamplers = nn.ModuleList()
        self.upsamplers = nn.ModuleList()
        self.projections = nn.ModuleList()

        n_stages = len(stages)
        if isinstance(channels, int):
            channels = [channels * channel_scaling**i for i in range(n_stages)]
            if max_channels is not None:
                channels = [min(ch, max_channels) for ch in channels]
        self.channels = channels

        if not len(channels) == len(stages):
            raise ValueError(
                "The list of given channel numbers must match the number " "of stages."
            )

        if downsampling_factors is None:
            downsampling_factors = [2] * (n_stages - 1)
        if len(stages) != len(downsampling_factors) + 1:
            raise ValueError(
                "The list of downsampling factors numbers must have one "
                "element less than the number of stages."
            )

        # No downsampling applied in first layer.
        downsampling_factors = [1] + downsampling_factors
        scale = 1
        self.scales = []
        for f_d in downsampling_factors:
            self.scales.append(scale)
            scale *= f_d

        try:
            stages = [
                stage if isinstance(stage, StageConfig) else StageConfig(stage)
                for stage in stages
            ]
        except ValueError:
            raise ValueError(
                "'stages' must be a list of 'StageConfig' or 'int'  objects."
            )

        modules = []
        module_map = {}

        if downsampler_factory is None:
            def downsampler_factory(channels_in, channels_out, f_down):
                return nn.AvgPool2d(kernel_size=f_down, stride=f_down)

        def upsampler_factory(f_up):
            return nn.Upsample(scale_factor=f_up)

        self.scales = _calculate_output_scales(base_scale, downsampling_factors)

        stage_ind = 0
        for stage, channels_out, f_dwn in zip(stages, channels, downsampling_factors):
            # Downsampling layer is included in stage.

            if channels_out % stage.n_blocks > 0:
                raise ValueError(
                    "The number of each stage's channels must be divisible by "
                    " the number of blocks."
                )
            growth_rate = channels_out // stage.n_blocks

            if stage_ind == 0:
                channels_in = growth_rate
            else:
                n_blocks = stages[stage_ind - 1].n_blocks
                if n_blocks == 1:
                    channels_in = channels[stage_ind - 1]
                else:
                    channels_in = channels[stage_ind - 1] // n_blocks


            down = f_dwn if isinstance(f_dwn, int) else max(f_dwn)
            if down > 1:
                self.downsamplers.append(
                    downsampler_factory(channels_in, channels_out, f_dwn)
                )
                self.upsamplers.append(
                    upsampler_factory(f_dwn)
                )
            else:
                self.downsamplers.append(None)
                self.upsamplers.append(None)


            for block_ind in range(stage.n_blocks):

                chans_combined = channels_in + block_ind * growth_rate

                if (
                        (stage_ind < n_stages - 1) and
                        (block_ind > 1) and
                        (block_ind < stages[stage_ind + 1].n_blocks + 1)
                ):
                    n_blocks = stages[stage_ind + 1].n_blocks
                    if block_ind > n_blocks:
                        chans_combined += channels[stage_ind + 1]
                    else:
                        chans_combined += channels[stage_ind + 1] // n_blocks

                if (
                        (stage_ind > 0) and
                        (block_ind > 0) and
                        (stages[stage_ind - 1].n_blocks > block_ind)
                ):
                    n_blocks = stages[stage_ind - 1].n_blocks
                    if block_ind > n_blocks - 2:
                        chans_combined += channels[stage_ind - 1]
                    else:
                        chans_combined += channels[stage_ind - 1] // n_blocks

                if block_ind < stage.n_blocks - 1:
                    mod = block_factory(
                        chans_combined,
                        growth_rate,
                        downsample=None,
                        *stage.block_args,
                        **stage.block_kwargs
                    )
                else:
                    mod = block_factory(
                        chans_combined,
                        channels_out,
                        downsample=None,
                        *stage.block_args,
                        **stage.block_kwargs
                    )

                modules.append(mod)
                depth_map = module_map.setdefault(block_ind + stage_ind, [])
                depth_map.append((stage_ind, mod))

            stage_ind += 1

        self.modules = nn.ModuleList(modules)
        self.module_map = module_map


        if stem_factory is not None:
            self.stem = stem_factory(channels[0] // stages[0].n_blocks)
            self.input_channels = channels[0]
        else:
            self.stem = None
            self.input_channels = channels[0] // stages[0].n_blocks

        self.depth = max(list(self.module_map.keys())) + 1
        self.input_channels = channels[0] // stages[0].n_blocks


    @property
    def skip_connections(self) -> Dict[int, int]:
        """
        Dictionary specifying the number of channels in the skip tensors
        produced by this encoder.
        """
        return {scl: chans for scl, chans in zip(self.scales, self.channels)}


    def forward(self, x: torch.Tensor, **kwargs) -> Dict[int, torch.Tensor]:
        """
        Forward input through encoder.

        Args:
            x: The input tensor.

        Return:
            A dict mapping stage indices to corresponding tensors.
        """
        if self.stem is None:
            x_in = {0: [x]}
        else:
            x_in = {0: [self.stem(x)]}

        results = {}

        for d_i in range(self.depth):

            y = {}
            for stage_ind, mod in self.module_map[d_i]:

                inputs = []
                propagate = []

                if stage_ind - 1 in x_in:
                    down = self.downsamplers[stage_ind]
                    x_d = down(x_in[stage_ind - 1][-1])
                    inputs.append(x_d)
                    propagate.append(x_d)

                if stage_ind in x_in:
                    inputs += x_in[stage_ind]
                    propagate = x_in[stage_ind]

                if stage_ind + 1 in x_in:
                    up = self.upsamplers[stage_ind + 1]
                    inputs.append(up(x_in[stage_ind + 1][-1]))

                y[stage_ind] = propagate + [mod(torch.cat(inputs, 1))]

            results.update(y)
            x_in = y

        return {self.scales[ind]: tensors[-1] for ind, tensors in results.items()}
