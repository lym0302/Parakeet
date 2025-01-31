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
"""Fastspeech2 related modules for paddle"""

from typing import Dict, Sequence, Tuple

import numpy as np
import paddle
from paddle import nn
from parakeet.modules.fastspeech2_predictor.duration_predictor import DurationPredictor, DurationPredictorLoss
from parakeet.modules.fastspeech2_predictor.length_regulator import LengthRegulator
from parakeet.modules.fastspeech2_predictor.postnet import Postnet
from parakeet.modules.fastspeech2_predictor.variance_predictor import VariancePredictor
from parakeet.modules.fastspeech2_transformer.embedding import PositionalEncoding, ScaledPositionalEncoding
from parakeet.modules.fastspeech2_transformer.encoder import Encoder as TransformerEncoder
from parakeet.modules.nets_utils import initialize, make_non_pad_mask, make_pad_mask
from typeguard import check_argument_types


class FastSpeech2(nn.Layer):
    """FastSpeech2 module.

    This is a module of FastSpeech2 described in `FastSpeech 2: Fast and
    High-Quality End-to-End Text to Speech`_. Instead of quantized pitch and
    energy, we use token-averaged value introduced in `FastPitch: Parallel
    Text-to-speech with Pitch Prediction`_.

    .. _`FastSpeech 2: Fast and High-Quality End-to-End Text to Speech`:
        https://arxiv.org/abs/2006.04558
    .. _`FastPitch: Parallel Text-to-speech with Pitch Prediction`:
        https://arxiv.org/abs/2006.06873

    """

    def __init__(
            self,
            # network structure related
            idim: int,
            odim: int,
            adim: int=384,
            aheads: int=4,
            elayers: int=6,
            eunits: int=1536,
            dlayers: int=6,
            dunits: int=1536,
            postnet_layers: int=5,
            postnet_chans: int=512,
            postnet_filts: int=5,
            positionwise_layer_type: str="conv1d",
            positionwise_conv_kernel_size: int=1,
            use_scaled_pos_enc: bool=True,
            use_batch_norm: bool=True,
            encoder_normalize_before: bool=True,
            decoder_normalize_before: bool=True,
            encoder_concat_after: bool=False,
            decoder_concat_after: bool=False,
            reduction_factor: int=1,
            encoder_type: str="transformer",
            decoder_type: str="transformer",
            # duration predictor
            duration_predictor_layers: int=2,
            duration_predictor_chans: int=384,
            duration_predictor_kernel_size: int=3,
            # energy predictor
            energy_predictor_layers: int=2,
            energy_predictor_chans: int=384,
            energy_predictor_kernel_size: int=3,
            energy_predictor_dropout: float=0.5,
            energy_embed_kernel_size: int=9,
            energy_embed_dropout: float=0.5,
            stop_gradient_from_energy_predictor: bool=False,
            # pitch predictor
            pitch_predictor_layers: int=2,
            pitch_predictor_chans: int=384,
            pitch_predictor_kernel_size: int=3,
            pitch_predictor_dropout: float=0.5,
            pitch_embed_kernel_size: int=9,
            pitch_embed_dropout: float=0.5,
            stop_gradient_from_pitch_predictor: bool=False,
            # training related
            transformer_enc_dropout_rate: float=0.1,
            transformer_enc_positional_dropout_rate: float=0.1,
            transformer_enc_attn_dropout_rate: float=0.1,
            transformer_dec_dropout_rate: float=0.1,
            transformer_dec_positional_dropout_rate: float=0.1,
            transformer_dec_attn_dropout_rate: float=0.1,
            duration_predictor_dropout_rate: float=0.1,
            postnet_dropout_rate: float=0.5,
            init_type: str="xavier_uniform",
            init_enc_alpha: float=1.0,
            init_dec_alpha: float=1.0,
            use_masking: bool=False,
            use_weighted_masking: bool=False, ):
        """Initialize FastSpeech2 module."""
        assert check_argument_types()
        super().__init__()

        # store hyperparameters
        self.idim = idim
        self.odim = odim
        self.eos = idim - 1
        self.reduction_factor = reduction_factor
        self.encoder_type = encoder_type
        self.decoder_type = decoder_type
        self.stop_gradient_from_pitch_predictor = stop_gradient_from_pitch_predictor
        self.stop_gradient_from_energy_predictor = stop_gradient_from_energy_predictor
        self.use_scaled_pos_enc = use_scaled_pos_enc

        # use idx 0 as padding idx
        self.padding_idx = 0

        # initialize parameters
        initialize(self, init_type)

        # get positional encoding class
        pos_enc_class = (ScaledPositionalEncoding
                         if self.use_scaled_pos_enc else PositionalEncoding)

        # define encoder
        encoder_input_layer = nn.Embedding(
            num_embeddings=idim,
            embedding_dim=adim,
            padding_idx=self.padding_idx)

        if encoder_type == "transformer":
            self.encoder = TransformerEncoder(
                idim=idim,
                attention_dim=adim,
                attention_heads=aheads,
                linear_units=eunits,
                num_blocks=elayers,
                input_layer=encoder_input_layer,
                dropout_rate=transformer_enc_dropout_rate,
                positional_dropout_rate=transformer_enc_positional_dropout_rate,
                attention_dropout_rate=transformer_enc_attn_dropout_rate,
                pos_enc_class=pos_enc_class,
                normalize_before=encoder_normalize_before,
                concat_after=encoder_concat_after,
                positionwise_layer_type=positionwise_layer_type,
                positionwise_conv_kernel_size=positionwise_conv_kernel_size, )
        else:
            raise ValueError(f"{encoder_type} is not supported.")

        # define duration predictor
        self.duration_predictor = DurationPredictor(
            idim=adim,
            n_layers=duration_predictor_layers,
            n_chans=duration_predictor_chans,
            kernel_size=duration_predictor_kernel_size,
            dropout_rate=duration_predictor_dropout_rate, )

        # define pitch predictor
        self.pitch_predictor = VariancePredictor(
            idim=adim,
            n_layers=pitch_predictor_layers,
            n_chans=pitch_predictor_chans,
            kernel_size=pitch_predictor_kernel_size,
            dropout_rate=pitch_predictor_dropout, )
        #  We use continuous pitch + FastPitch style avg
        self.pitch_embed = nn.Sequential(
            nn.Conv1D(
                in_channels=1,
                out_channels=adim,
                kernel_size=pitch_embed_kernel_size,
                padding=(pitch_embed_kernel_size - 1) // 2, ),
            nn.Dropout(pitch_embed_dropout), )

        # define energy predictor
        self.energy_predictor = VariancePredictor(
            idim=adim,
            n_layers=energy_predictor_layers,
            n_chans=energy_predictor_chans,
            kernel_size=energy_predictor_kernel_size,
            dropout_rate=energy_predictor_dropout, )
        # We use continuous enegy + FastPitch style avg
        self.energy_embed = nn.Sequential(
            nn.Conv1D(
                in_channels=1,
                out_channels=adim,
                kernel_size=energy_embed_kernel_size,
                padding=(energy_embed_kernel_size - 1) // 2, ),
            nn.Dropout(energy_embed_dropout), )

        # define length regulator
        self.length_regulator = LengthRegulator()

        # define decoder
        # NOTE: we use encoder as decoder
        # because fastspeech's decoder is the same as encoder
        if decoder_type == "transformer":
            self.decoder = TransformerEncoder(
                idim=0,
                attention_dim=adim,
                attention_heads=aheads,
                linear_units=dunits,
                num_blocks=dlayers,
                # in decoder, don't need layer before pos_enc_class (we use embedding here in encoder)
                input_layer=None,
                dropout_rate=transformer_dec_dropout_rate,
                positional_dropout_rate=transformer_dec_positional_dropout_rate,
                attention_dropout_rate=transformer_dec_attn_dropout_rate,
                pos_enc_class=pos_enc_class,
                normalize_before=decoder_normalize_before,
                concat_after=decoder_concat_after,
                positionwise_layer_type=positionwise_layer_type,
                positionwise_conv_kernel_size=positionwise_conv_kernel_size, )
        else:
            raise ValueError(f"{decoder_type} is not supported.")

        # define final projection
        self.feat_out = nn.Linear(adim, odim * reduction_factor)

        # define postnet
        self.postnet = (None if postnet_layers == 0 else Postnet(
            idim=idim,
            odim=odim,
            n_layers=postnet_layers,
            n_chans=postnet_chans,
            n_filts=postnet_filts,
            use_batch_norm=use_batch_norm,
            dropout_rate=postnet_dropout_rate, ))

        self._reset_parameters(
            init_enc_alpha=init_enc_alpha,
            init_dec_alpha=init_dec_alpha, )

        # define criterions
        self.criterion = FastSpeech2Loss(
            use_masking=use_masking, use_weighted_masking=use_weighted_masking)

    def forward(
            self,
            text: paddle.Tensor,
            text_lengths: paddle.Tensor,
            speech: paddle.Tensor,
            speech_lengths: paddle.Tensor,
            durations: paddle.Tensor,
            pitch: paddle.Tensor,
            energy: paddle.Tensor, ) -> Sequence[paddle.Tensor]:
        """Calculate forward propagation.

        Parameters
        ----------
            text : Tensor
                Batch of padded token ids (B, Tmax).
            text_lengths : Tensor)
                Batch of lengths of each input (B,).
            speech : Tensor
                Batch of padded target features (B, Lmax, odim).
            speech_lengths : Tensor
                Batch of the lengths of each target (B,).
            durations : Tensor
                Batch of padded durations (B, Tmax).
            pitch : Tensor
                Batch of padded token-averaged pitch (B, Tmax, 1).
            energy : Tensor
                Batch of padded token-averaged energy (B, Tmax, 1).
        Returns
        ----------
            Tensor
                mel outs before postnet
            Tensor
                mel outs after postnet
            Tensor
                duration predictor's output
            Tensor
                pitch predictor's output
            Tensor
                energy predictor's output
            Tensor
                speech
            Tensor
                speech_lengths, modified if reduction_factor >1
        """

        xs = text
        ilens = text_lengths
        ys, ds, ps, es = speech, durations, pitch, energy
        olens = speech_lengths

        # forward propagation
        before_outs, after_outs, d_outs, p_outs, e_outs = self._forward(
            xs, ilens, ys, olens, ds, ps, es, is_inference=False)
        # modify mod part of groundtruth
        if self.reduction_factor > 1:
            olens = paddle.to_tensor([
                olen - olen % self.reduction_factor for olen in olens.numpy()
            ])
            max_olen = max(olens)
            ys = ys[:, :max_olen]

        return before_outs, after_outs, d_outs, p_outs, e_outs, ys, olens

    def _forward(
            self,
            xs: paddle.Tensor,
            ilens: paddle.Tensor,
            ys: paddle.Tensor=None,
            olens: paddle.Tensor=None,
            ds: paddle.Tensor=None,
            ps: paddle.Tensor=None,
            es: paddle.Tensor=None,
            is_inference: bool=False,
            alpha: float=1.0, ) -> Sequence[paddle.Tensor]:
        # forward encoder
        x_masks = self._source_mask(ilens)

        hs, _ = self.encoder(xs, x_masks)  # (B, Tmax, adim)
        # forward duration predictor and variance predictors
        d_masks = make_pad_mask(ilens)

        if self.stop_gradient_from_pitch_predictor:
            p_outs = self.pitch_predictor(hs.detach(), d_masks.unsqueeze(-1))
        else:
            p_outs = self.pitch_predictor(hs, d_masks.unsqueeze(-1))
        if self.stop_gradient_from_energy_predictor:
            e_outs = self.energy_predictor(hs.detach(), d_masks.unsqueeze(-1))
        else:
            e_outs = self.energy_predictor(hs, d_masks.unsqueeze(-1))

        if is_inference:
            # (B, Tmax)
            d_outs = self.duration_predictor.inference(hs, d_masks)
            # use prediction in inference
            # (B, Tmax, 1)
            p_embs = self.pitch_embed(p_outs.transpose((0, 2, 1))).transpose(
                (0, 2, 1))
            e_embs = self.energy_embed(e_outs.transpose((0, 2, 1))).transpose(
                (0, 2, 1))
            hs = hs + e_embs + p_embs
            # (B, Lmax, adim)
            hs = self.length_regulator(hs, d_outs, alpha)
        else:
            d_outs = self.duration_predictor(hs, d_masks)
            # use groundtruth in training
            p_embs = self.pitch_embed(ps.transpose((0, 2, 1))).transpose(
                (0, 2, 1))
            e_embs = self.energy_embed(es.transpose((0, 2, 1))).transpose(
                (0, 2, 1))
            hs = hs + e_embs + p_embs
            # (B, Lmax, adim)
            hs = self.length_regulator(hs, ds)

        # forward decoder
        if olens is not None and not is_inference:
            if self.reduction_factor > 1:
                olens_in = paddle.to_tensor(
                    [olen // self.reduction_factor for olen in olens.numpy()])
            else:
                olens_in = olens
            h_masks = self._source_mask(olens_in)
        else:
            h_masks = None
        # (B, Lmax, adim)
        zs, _ = self.decoder(hs, h_masks)
        # (B, Lmax, odim)
        before_outs = self.feat_out(zs).reshape((zs.shape[0], -1, self.odim))

        # postnet -> (B, Lmax//r * r, odim)
        if self.postnet is None:
            after_outs = before_outs
        else:
            after_outs = before_outs + self.postnet(
                before_outs.transpose((0, 2, 1))).transpose((0, 2, 1))

        return before_outs, after_outs, d_outs, p_outs, e_outs

    def inference(
            self,
            text: paddle.Tensor,
            speech: paddle.Tensor=None,
            durations: paddle.Tensor=None,
            pitch: paddle.Tensor=None,
            energy: paddle.Tensor=None,
            alpha: float=1.0,
            use_teacher_forcing: bool=False, ) -> paddle.Tensor:
        """Generate the sequence of features given the sequences of characters.

        Parameters
        ----------
            text : Tensor
                Input sequence of characters (T,).
            speech : Tensor, optional
                Feature sequence to extract style (N, idim).
            durations : Tensor, optional
                Groundtruth of duration (T,).
            pitch : Tensor, optional
                Groundtruth of token-averaged pitch (T, 1).
            energy : Tensor, optional
                Groundtruth of token-averaged energy (T, 1).
            alpha : float, optional
                 Alpha to control the speed.
            use_teacher_forcing : bool, optional
                 Whether to use teacher forcing.
                 If true, groundtruth of duration, pitch and energy will be used.

        Returns
        ----------
            Tensor
                Output sequence of features (L, odim).
        """
        x, y = text, speech
        d, p, e = durations, pitch, energy

        # setup batch axis
        ilens = paddle.to_tensor(
            [x.shape[0]], dtype=paddle.int64, place=x.place)
        xs, ys = x.unsqueeze(0), None

        if y is not None:
            ys = y.unsqueeze(0)

        if use_teacher_forcing:
            # use groundtruth of duration, pitch, and energy
            ds, ps, es = d.unsqueeze(0), p.unsqueeze(0), e.unsqueeze(0)
            # (1, L, odim)
            _, outs, *_ = self._forward(
                xs,
                ilens,
                ys,
                ds=ds,
                ps=ps,
                es=es, )
        else:
            # (1, L, odim)
            _, outs, *_ = self._forward(
                xs,
                ilens,
                ys,
                is_inference=True,
                alpha=alpha, )

        return outs[0]

    def _source_mask(self, ilens: paddle.Tensor) -> paddle.Tensor:
        """Make masks for self-attention.

        Parameters
        ----------
            ilens : Tensor
                Batch of lengths (B,).

        Returns
        -------
            Tensor
                Mask tensor for self-attention.
                dtype=paddle.bool

        Examples
        -------
            >>> ilens = [5, 3]
            >>> self._source_mask(ilens)
            tensor([[[1, 1, 1, 1, 1],
                     [1, 1, 1, 0, 0]]]) bool

        """
        x_masks = make_non_pad_mask(ilens)
        return x_masks.unsqueeze(-2)

    def _reset_parameters(self, init_enc_alpha: float, init_dec_alpha: float):

        # initialize alpha in scaled positional encoding
        if self.encoder_type == "transformer" and self.use_scaled_pos_enc:
            init_enc_alpha = paddle.to_tensor(init_enc_alpha)
            self.encoder.embed[-1].alpha = paddle.create_parameter(
                shape=init_enc_alpha.shape,
                dtype=str(init_enc_alpha.numpy().dtype),
                default_initializer=paddle.nn.initializer.Assign(
                    init_enc_alpha))
        if self.decoder_type == "transformer" and self.use_scaled_pos_enc:
            init_dec_alpha = paddle.to_tensor(init_dec_alpha)
            self.decoder.embed[-1].alpha = paddle.create_parameter(
                shape=init_dec_alpha.shape,
                dtype=str(init_dec_alpha.numpy().dtype),
                default_initializer=paddle.nn.initializer.Assign(
                    init_dec_alpha))


class FastSpeech2Inference(nn.Layer):
    def __init__(self, normalizer, model):
        super().__init__()
        self.normalizer = normalizer
        self.acoustic_model = model

    def forward(self, text):
        normalized_mel = self.acoustic_model.inference(text)
        logmel = self.normalizer.inverse(normalized_mel)
        return logmel


class FastSpeech2Loss(nn.Layer):
    """Loss function module for FastSpeech2."""

    def __init__(self,
                 use_masking: bool=True,
                 use_weighted_masking: bool=False):
        """Initialize feed-forward Transformer loss module.

        Parameters
        ----------
            use_masking : bool
                Whether to apply masking for padded part in loss calculation.
            use_weighted_masking : bool
                Whether to weighted masking in loss calculation.
        """
        assert check_argument_types()
        super().__init__()

        assert (use_masking != use_weighted_masking) or not use_masking
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

        # define criterions
        reduction = "none" if self.use_weighted_masking else "mean"
        self.l1_criterion = nn.L1Loss(reduction=reduction)
        self.mse_criterion = nn.MSELoss(reduction=reduction)
        self.duration_criterion = DurationPredictorLoss(reduction=reduction)

    def forward(
            self,
            after_outs: paddle.Tensor,
            before_outs: paddle.Tensor,
            d_outs: paddle.Tensor,
            p_outs: paddle.Tensor,
            e_outs: paddle.Tensor,
            ys: paddle.Tensor,
            ds: paddle.Tensor,
            ps: paddle.Tensor,
            es: paddle.Tensor,
            ilens: paddle.Tensor,
            olens: paddle.Tensor, ) -> Tuple[paddle.Tensor, paddle.Tensor,
                                             paddle.Tensor, paddle.Tensor]:
        """Calculate forward propagation.

        Parameters
        ----------
            after_outs : Tensor
                Batch of outputs after postnets (B, Lmax, odim).
            before_outs : Tensor
                Batch of outputs before postnets (B, Lmax, odim).
            d_outs : Tensor
                 Batch of outputs of duration predictor (B, Tmax).
            p_outs : Tensor
                Batch of outputs of pitch predictor (B, Tmax, 1).
            e_outs : Tensor
                Batch of outputs of energy predictor (B, Tmax, 1).
            ys : Tensor
                Batch of target features (B, Lmax, odim).
            ds : Tensor
                Batch of durations (B, Tmax).
            ps : Tensor
                Batch of target token-averaged pitch (B, Tmax, 1).
            es : Tensor
                Batch of target token-averaged energy (B, Tmax, 1).
            ilens : Tensor
                Batch of the lengths of each input (B,).
            olens : Tensor
                Batch of the lengths of each target (B,).

        Returns
        ----------
            Tensor
                L1 loss value.
            Tensor
                Duration predictor loss value.
            Tensor
                Pitch predictor loss value.
            Tensor
                Energy predictor loss value.

        """
        # apply mask to remove padded part
        if self.use_masking:
            out_masks = make_non_pad_mask(olens).unsqueeze(-1)
            before_outs = before_outs.masked_select(
                out_masks.broadcast_to(before_outs.shape))
            if after_outs is not None:
                after_outs = after_outs.masked_select(
                    out_masks.broadcast_to(after_outs.shape))
            ys = ys.masked_select(out_masks.broadcast_to(ys.shape))
            duration_masks = make_non_pad_mask(ilens)
            d_outs = d_outs.masked_select(
                duration_masks.broadcast_to(d_outs.shape))
            ds = ds.masked_select(duration_masks.broadcast_to(ds.shape))
            pitch_masks = make_non_pad_mask(ilens).unsqueeze(-1)
            p_outs = p_outs.masked_select(
                pitch_masks.broadcast_to(p_outs.shape))
            e_outs = e_outs.masked_select(
                pitch_masks.broadcast_to(e_outs.shape))
            ps = ps.masked_select(pitch_masks.broadcast_to(ps.shape))
            es = es.masked_select(pitch_masks.broadcast_to(es.shape))

        # calculate loss
        l1_loss = self.l1_criterion(before_outs, ys)
        if after_outs is not None:
            l1_loss += self.l1_criterion(after_outs, ys)
        duration_loss = self.duration_criterion(d_outs, ds)
        pitch_loss = self.mse_criterion(p_outs, ps)
        energy_loss = self.mse_criterion(e_outs, es)

        # make weighted mask and apply it
        if self.use_weighted_masking:
            out_masks = make_non_pad_mask(olens).unsqueeze(-1)
            out_weights = out_masks.cast(
                dtype=paddle.float32) / out_masks.cast(
                    dtype=paddle.float32).sum(axis=1, keepdim=True)
            out_weights /= ys.shape[0] * ys.shape[2]
            duration_masks = make_non_pad_mask(ilens)
            duration_weights = (duration_masks.cast(dtype=paddle.float32) /
                                duration_masks.cast(dtype=paddle.float32).sum(
                                    axis=1, keepdim=True))
            duration_weights /= ds.shape[0]

            # apply weight

            l1_loss = l1_loss.multiply(out_weights)
            l1_loss = l1_loss.masked_select(
                out_masks.broadcast_to(l1_loss.shape)).sum()
            duration_loss = (duration_loss.multiply(duration_weights)
                             .masked_select(duration_masks).sum())
            pitch_masks = duration_masks.unsqueeze(-1)
            pitch_weights = duration_weights.unsqueeze(-1)
            pitch_loss = pitch_loss.multiply(pitch_weights)
            pitch_loss = pitch_loss.masked_select(
                pitch_masks.broadcast_to(pitch_loss.shape)).sum()
            energy_loss = energy_loss.multiply(pitch_weights)
            energy_loss = energy_loss.masked_select(
                pitch_masks.broadcast_to(energy_loss.shape)).sum()

        return l1_loss, duration_loss, pitch_loss, energy_loss
