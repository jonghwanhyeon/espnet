# Copyright 2020 Nagoya University (Tomoki Hayashi)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""F0 extractor using DIO + Stonemask algorithm."""

from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple
from typing import Union

import humanfriendly
import librosa
import numpy as np
import pyworld
import torch
import torch.nn.functional as F

from scipy.interpolate import interp1d
from typeguard import check_argument_types

from espnet.nets.pytorch_backend.nets_utils import pad_list
from espnet2.tts.feats_extract.abs_feats_extract import AbsFeatsExtract


class Dio(AbsFeatsExtract):
    """F0 estimation with dio + stonemask algortihm."""

    def __init__(
        self,
        fs: Union[int, str] = 22050,
        n_fft: int = 1024,
        hop_length: int = 256,
        f0min: Optional[int] = 80,
        f0max: Optional[int] = 400,
        use_token_averaged_f0: bool = True,
        use_continuous_f0: bool = True,
        use_log_f0: bool = True,
    ):
        assert check_argument_types()
        super().__init__()
        if isinstance(fs, str):
            fs = humanfriendly.parse_size(fs)
        self.fs = fs
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.frame_period = 1000 * hop_length / fs
        self.f0min = f0min
        self.f0max = f0max
        self.use_token_averaged_f0 = use_token_averaged_f0
        self.use_continuous_f0 = use_continuous_f0
        self.use_log_f0 = use_log_f0

    def output_size(self) -> int:
        return 1

    def get_parameters(self) -> Dict[str, Any]:
        return dict(
            fs=self.fs,
            n_fft=self.n_fft,
            n_shift=self.n_shift,
            f0min=self.f0min,
            f0max=self.f0max,
        )

    def forward(
        self,
        input: torch.Tensor,
        input_lengths: torch.Tensor,
        durations: torch.Tensor = None,
        durations_lengths: torch.Tensor = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        pitch = [self._calculate_f0(input_) for input_ in input]
        if self.use_token_averaged_f0:
            pitch = [self._average_by_duration(p, d) for p, d in zip(pitch, durations)]
            pitch_lengths = durations_lengths
        else:
            pitch_lengths = input.new_tensor([len(p) for p in pitch], dtype=torch.long)
        pitch = pad_list(pitch, 0.0)
        return pitch, pitch_lengths

    def _calculate_f0(self, input: torch.Tensor) -> torch.Tensor:
        x = input.cpu().numpy().astype(np.double)
        f0, timeaxis = pyworld.dio(
            x,
            self.fs,
            f0_floor=self.f0min,
            f0_ceil=self.f0max,
            frame_period=self.frame_period,
        )
        f0 = pyworld.stonemask(x, f0, timeaxis, self.fs)
        f0 = self._adjust_num_frames(x, f0)
        if self.use_continuous_f0:
            f0 = self._convert_to_continuous_f0(f0)
        if self.use_log_f0:
            nonzero_idxs = np.where(f0 != 0)[0]
            f0[nonzero_idxs] = np.log(f0[nonzero_idxs])
        return input.new_tensor(f0.reshape(-1, 1), dtype=torch.float)

    def _adjust_num_frames(self, x: np.array, f0: np.array) -> np.array:
        num_frames = librosa.samples_to_frames(len(x), self.hop_length, self.n_fft)
        num_frames += 1
        if num_frames > len(f0):
            f0 = np.pad(f0, ((0, num_frames - len(f0))))
        elif num_frames < len(f0):
            f0 = f0[:num_frames]
        return f0

    @staticmethod
    def _convert_to_continuous_f0(f0: np.array) -> np.array:
        assert not (f0 == 0).all(), "All frames seems to be unvoiced."

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

    @staticmethod
    def _average_by_duration(x: torch.Tensor, d: torch.Tensor) -> torch.Tensor:
        assert d.sum() == len(x)
        d_cumsum = F.pad(d.cumsum(dim=0), (1, 0))
        x_avg = [
            x[start:end].masked_select(x[start:end].ne(0.0)).mean(dim=0)
            if len(x[start:end].masked_select(x[start:end].ne(0.0))) != 0
            else x.new_tensor(0.0)
            for start, end in zip(d_cumsum[:-1], d_cumsum[1:])
        ]
        return torch.stack(x_avg).unsqueeze(-1)
