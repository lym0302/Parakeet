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

from parakeet.models.fastspeech2 import FastSpeech2, FastSpeech2Loss
from parakeet.training.extensions.evaluator import StandardEvaluator
from parakeet.training.reporter import report
from parakeet.training.updaters.standard_updater import StandardUpdater


class FastSpeech2Updater(StandardUpdater):
    def __init__(self,
                 model,
                 optimizer,
                 dataloader,
                 init_state=None,
                 use_masking=False,
                 use_weighted_masking=False):
        super().__init__(model, optimizer, dataloader, init_state=None)
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

    def update_core(self, batch):
        before_outs, after_outs, d_outs, p_outs, e_outs, ys, olens = self.model(
            text=batch["text"],
            text_lengths=batch["text_lengths"],
            speech=batch["speech"],
            speech_lengths=batch["speech_lengths"],
            durations=batch["durations"],
            pitch=batch["pitch"],
            energy=batch["energy"], )

        criterion = FastSpeech2Loss(
            use_masking=self.use_masking,
            use_weighted_masking=self.use_weighted_masking)

        l1_loss, duration_loss, pitch_loss, energy_loss = criterion(
            after_outs=after_outs,
            before_outs=before_outs,
            d_outs=d_outs,
            p_outs=p_outs,
            e_outs=e_outs,
            ys=ys,
            ds=batch["durations"],
            ps=batch["pitch"],
            es=batch["energy"],
            ilens=batch["text_lengths"],
            olens=olens)

        loss = l1_loss + duration_loss + pitch_loss + energy_loss

        optimizer = self.optimizer
        optimizer.clear_grad()
        loss.backward()
        optimizer.step()

        report("train/loss", float(loss))
        report("train/l1_loss", float(l1_loss))
        report("train/duration_loss", float(duration_loss))
        report("train/pitch_loss", float(pitch_loss))
        report("train/energy_loss", float(energy_loss))


class FastSpeech2Evaluator(StandardEvaluator):
    def __init__(self,
                 model,
                 dataloader,
                 use_masking=False,
                 use_weighted_masking=False):
        super().__init__(model, dataloader)
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

    def evaluate_core(self, batch):
        before_outs, after_outs, d_outs, p_outs, e_outs, ys, olens = self.model(
            text=batch["text"],
            text_lengths=batch["text_lengths"],
            speech=batch["speech"],
            speech_lengths=batch["speech_lengths"],
            durations=batch["durations"],
            pitch=batch["pitch"],
            energy=batch["energy"])

        criterion = FastSpeech2Loss(
            use_masking=self.use_masking,
            use_weighted_masking=self.use_weighted_masking)
        l1_loss, duration_loss, pitch_loss, energy_loss = criterion(
            after_outs=after_outs,
            before_outs=before_outs,
            d_outs=d_outs,
            p_outs=p_outs,
            e_outs=e_outs,
            ys=ys,
            ds=batch["durations"],
            ps=batch["pitch"],
            es=batch["energy"],
            ilens=batch["text_lengths"],
            olens=olens, )
        loss = l1_loss + duration_loss + pitch_loss + energy_loss

        report("eval/loss", float(loss))
        report("eval/l1_loss", float(l1_loss))
        report("eval/duration_loss", float(duration_loss))
        report("eval/pitch_loss", float(pitch_loss))
        report("eval/energy_loss", float(energy_loss))
