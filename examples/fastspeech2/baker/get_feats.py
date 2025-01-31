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

import librosa
import numpy as np
import pyworld
from scipy.interpolate import interp1d

from config import get_cfg_default


class LogMelFBank():
    def __init__(self,
                 sr=24000,
                 n_fft=2048,
                 hop_length=300,
                 win_length=1200,
                 window="hann",
                 n_mels=80,
                 fmin=80,
                 fmax=7600,
                 eps=1e-10):
        self.sr = sr
        # stft
        self.n_fft = n_fft
        self.win_length = win_length
        self.hop_length = hop_length
        self.window = window
        self.center = True
        self.pad_mode = "reflect"

        # mel
        self.n_mels = n_mels
        self.fmin = fmin
        self.fmax = fmax

        self.mel_filter = self._create_mel_filter()

    def _create_mel_filter(self):
        mel_filter = librosa.filters.mel(sr=self.sr,
                                         n_fft=self.n_fft,
                                         n_mels=self.n_mels,
                                         fmin=self.fmin,
                                         fmax=self.fmax)
        return mel_filter

    def _stft(self, wav):
        D = librosa.core.stft(
            wav,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            win_length=self.win_length,
            window=self.window,
            center=self.center,
            pad_mode=self.pad_mode)
        return D

    def _spectrogram(self, wav):
        D = self._stft(wav)
        return np.abs(D)

    def _mel_spectrogram(self, wav):
        S = self._spectrogram(wav)
        mel = np.dot(self.mel_filter, S)
        return mel

    # We use different definition for log-spec between TTS and ASR
    #   TTS: log_10(abs(stft))
    #   ASR: log_e(power(stft))

    def get_log_mel_fbank(self, wav):
        mel = self._mel_spectrogram(wav)
        mel = np.clip(mel, a_min=1e-10, a_max=float("inf"))
        mel = np.log10(mel.T)
        # (num_frames, n_mels)
        return mel


class Pitch():
    def __init__(self, sr=24000, hop_length=300, f0min=80, f0max=7600):

        self.sr = sr
        self.hop_length = hop_length
        self.f0min = f0min
        self.f0max = f0max

    def _convert_to_continuous_f0(self, f0: np.array) -> np.array:
        if (f0 == 0).all():
            print("All frames seems to be unvoiced.")
            return f0

        # padding start and end of f0 sequence
        start_f0 = f0[f0 != 0][0]
        end_f0 = f0[f0 != 0][-1]
        start_idx = np.where(f0 == start_f0)[0][0]
        end_idx = np.where(f0 == end_f0)[0][-1]
        f0[:start_idx] = start_f0
        f0[end_idx:] = end_f0

        # get non-zero frame index
        nonzero_idxs = np.where(f0 != 0)[0]

        # perform linear interpolation
        interp_fn = interp1d(nonzero_idxs, f0[nonzero_idxs])
        f0 = interp_fn(np.arange(0, f0.shape[0]))

        return f0

    def _calculate_f0(self,
                      input: np.array,
                      use_continuous_f0=True,
                      use_log_f0=True) -> np.array:
        input = input.astype(np.float)
        frame_period = 1000 * self.hop_length / self.sr
        f0, timeaxis = pyworld.dio(input,
                                   fs=self.sr,
                                   f0_floor=self.f0min,
                                   f0_ceil=self.f0max,
                                   frame_period=frame_period)
        f0 = pyworld.stonemask(input, f0, timeaxis, self.sr)
        if use_continuous_f0:
            f0 = self._convert_to_continuous_f0(f0)
        if use_log_f0:
            nonzero_idxs = np.where(f0 != 0)[0]
            f0[nonzero_idxs] = np.log(f0[nonzero_idxs])
        return f0.reshape(-1)

    def _average_by_duration(self, input: np.array, d: np.array) -> np.array:
        d_cumsum = np.pad(d.cumsum(0), (1, 0), 'constant')
        arr_list = []
        for start, end in zip(d_cumsum[:-1], d_cumsum[1:]):
            arr = input[start:end]
            mask = arr == 0
            arr[mask] = 0
            avg_arr = np.mean(arr, axis=0) if len(arr) != 0 else np.array(0)
            arr_list.append(avg_arr)
        # shape (T,1)
        arr_list = np.expand_dims(np.array(arr_list), 0).T

        return arr_list

    def get_pitch(self,
                  wav,
                  use_continuous_f0=True,
                  use_log_f0=True,
                  use_token_averaged_f0=True,
                  duration=None):
        f0 = self._calculate_f0(wav, use_continuous_f0, use_log_f0)
        if use_token_averaged_f0 and duration is not None:
            f0 = self._average_by_duration(f0, duration)
        return f0


class Energy():
    def __init__(self,
                 sr=24000,
                 n_fft=2048,
                 hop_length=300,
                 win_length=1200,
                 window="hann",
                 center=True,
                 pad_mode="reflect"):

        self.sr = sr
        self.n_fft = n_fft
        self.win_length = win_length
        self.hop_length = hop_length
        self.window = window
        self.center = center
        self.pad_mode = pad_mode

    def _stft(self, wav):
        D = librosa.core.stft(
            wav,
            n_fft=self.n_fft,
            hop_length=self.hop_length,
            win_length=self.win_length,
            window=self.window,
            center=self.center,
            pad_mode=self.pad_mode)
        return D

    def _calculate_energy(self, input):
        input = input.astype(np.float32)
        input_stft = self._stft(input)
        input_power = np.abs(input_stft)**2
        energy = np.sqrt(
            np.clip(
                np.sum(input_power, axis=0), a_min=1.0e-10, a_max=float(
                    'inf')))
        return energy

    def _average_by_duration(self, input: np.array, d: np.array) -> np.array:
        d_cumsum = np.pad(d.cumsum(0), (1, 0), 'constant')
        arr_list = []
        for start, end in zip(d_cumsum[:-1], d_cumsum[1:]):
            arr = input[start:end]
            avg_arr = np.mean(arr, axis=0) if len(arr) != 0 else np.array(0)
            arr_list.append(avg_arr)
        # shape (T,1)
        arr_list = np.expand_dims(np.array(arr_list), 0).T
        return arr_list

    def get_energy(self, wav, use_token_averaged_energy=True, duration=None):
        energy = self._calculate_energy(wav)
        if use_token_averaged_energy and duration is not None:
            energy = self._average_by_duration(energy, duration)
        return energy


if __name__ == "__main__":
    C = get_cfg_default()
    filename = "../raw_data/data/format.1/000001.flac"
    wav, _ = librosa.load(filename, sr=C.fs)
    mel_extractor = LogMelFBank(
        sr=C.fs,
        n_fft=C.n_fft,
        hop_length=C.n_shift,
        win_length=C.win_length,
        window=C.window,
        n_mels=C.n_mels,
        fmin=C.fmin,
        fmax=C.fmax, )
    mel = mel_extractor.get_log_mel_fbank(wav)
    print(mel)
    print(mel.shape)

    pitch_extractor = Pitch(
        sr=C.fs, hop_length=C.n_shift, f0min=C.f0min, f0max=C.f0max)
    duration = "2 8 8 8 12 11 10 13 11 10 18 9 12 10 12 11 5"
    duration = np.array([int(x) for x in duration.split(" ")])
    avg_f0 = pitch_extractor.get_pitch(wav, duration=duration)
    print(avg_f0)
    print(avg_f0.shape)

    energy_extractor = Energy(
        sr=C.fs,
        n_fft=C.n_fft,
        hop_length=C.n_shift,
        win_length=C.win_length,
        window=C.window)
    duration = "2 8 8 8 12 11 10 13 11 10 18 9 12 10 12 11 5"
    duration = np.array([int(x) for x in duration.split(" ")])
    avg_energy = energy_extractor.get_energy(wav, duration=duration)
    print(avg_energy)
    print(avg_energy.sum())
