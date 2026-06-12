from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset

from utils.intensity import IntensityTransform, lambda_condition_value
from utils.lbfreadnew import lbfreadnew


SUPPORTED_ARRAY_EXTS = (".npy", ".lbf")
DEFAULT_DATA_SUBDIRS = ("npy", "lbf")


def natural_key(path: Path):
    return [int(token) if token.isdigit() else token.lower() for token in re.split(r"(\d+)", path.name)]


def list_supported_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_ARRAY_EXTS],
        key=natural_key,
    )


def load_2d(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".npy":
        arr = np.load(path, allow_pickle=False)
    elif path.suffix.lower() == ".lbf":
        arr = lbfreadnew(str(path))
    else:
        raise ValueError(f"Unsupported file type: {path}")

    arr = np.asarray(arr)
    if arr.ndim == 3:
        if arr.shape[0] == 1:
            arr = arr[0]
        elif arr.shape[-1] == 1:
            arr = arr[..., 0]
        else:
            arr = arr[..., 0]
    if arr.ndim != 2:
        raise ValueError(f"{path} shape {arr.shape} is not 2D after squeeze")
    return arr.astype(np.float32, copy=False)


def center_crop(img: np.ndarray, crop_size: int | None) -> np.ndarray:
    if crop_size is None or crop_size <= 0:
        return img
    h, w = img.shape
    if h < crop_size or w < crop_size:
        raise ValueError(f"Image size {(h, w)} is smaller than crop_size={crop_size}")
    top = (h - crop_size) // 2
    left = (w - crop_size) // 2
    return img[top:top + crop_size, left:left + crop_size]


def random_crop_same(images: list[np.ndarray], crop_size: int | None) -> list[np.ndarray]:
    if crop_size is None or crop_size <= 0:
        return images
    h, w = images[0].shape
    if h < crop_size or w < crop_size:
        raise ValueError(f"Image size {(h, w)} is smaller than crop_size={crop_size}")
    top = random.randint(0, h - crop_size)
    left = random.randint(0, w - crop_size)
    return [img[top:top + crop_size, left:left + crop_size] for img in images]


def augment_same(images: list[torch.Tensor]) -> list[torch.Tensor]:
    """对 I1/I2/Ĉ 使用相同几何增强，避免错位。"""

    if random.random() < 0.5:
        images = [torch.flip(x, dims=(-1,)) for x in images]
    if random.random() < 0.5:
        images = [torch.flip(x, dims=(-2,)) for x in images]
    k = random.randint(0, 3)
    if k:
        images = [torch.rot90(x, k=k, dims=(-2, -1)) for x in images]
    return images


def parse_level_name(name: str) -> int | None:
    match = re.match(r"^\d+x\d+x(\d+)$", name)
    return int(match.group(1)) if match else None


def parse_index_name(name: str) -> int | None:
    return int(name) if re.fullmatch(r"\d+", name) else None


def index_allowed(name: str, index_min: int | None, index_max: int | None) -> bool:
    if index_min is None and index_max is None:
        return True
    index = parse_index_name(name)
    if index is None:
        return False
    if index_min is not None and index < index_min:
        return False
    if index_max is not None and index > index_max:
        return False
    return True


def discover_sequence_dirs(
    root: str | Path,
    data_subdirs: tuple[str, ...] = DEFAULT_DATA_SUBDIRS,
    strict_data_subdir: bool = False,
    data_index_min: int | None = None,
    data_index_max: int | None = None,
) -> list[Path]:
    """发现可形成 N2N pair 的具体帧序列目录。

    兼容两类数据：
    1. root 本身就是一个帧序列目录；
    2. 原项目的 mix/5x5x100/0/npy 这类层级结构。
    """

    root = Path(root)
    if list_supported_files(root):
        return [root]

    sequence_dirs: list[Path] = []
    for level_dir in sorted((p for p in root.iterdir() if p.is_dir()), key=natural_key):
        level = parse_level_name(level_dir.name)
        if level is None:
            if not strict_data_subdir and list_supported_files(level_dir):
                sequence_dirs.append(level_dir)
            continue

        for scene_dir in sorted((p for p in level_dir.iterdir() if p.is_dir()), key=natural_key):
            if not index_allowed(scene_dir.name, data_index_min, data_index_max):
                continue
            if not strict_data_subdir and list_supported_files(scene_dir):
                sequence_dirs.append(scene_dir)
            for subdir_name in data_subdirs:
                candidate = scene_dir / subdir_name
                if list_supported_files(candidate):
                    sequence_dirs.append(candidate)

    if sequence_dirs:
        return sequence_dirs

    for child in sorted((p for p in root.rglob("*") if p.is_dir()), key=natural_key):
        if list_supported_files(child):
            sequence_dirs.append(child)
    return sequence_dirs


@dataclass(frozen=True)
class SequenceRecord:
    folder: Path
    files: tuple[Path, ...]


class N2NBootstrapTripletDataset(Dataset):
    """返回 NTN 训练需要的三元组：I1、I2、Ĉ。

    I1/I2 是同一场景的两路独立 noisy observation；Ĉ 是无 GT 条件下的伪干净图。
    默认 Ĉ 用多帧均值获得，也可以在训练脚本里用已训练 N2N 模型输出替换。
    """

    def __init__(
        self,
        root_dir: str,
        intervals: list[int] | tuple[int, ...] = (5, 7, 9),
        crop_size: int = 128,
        random_crop: bool = True,
        pseudo_clean_frames: int = 0,
        data_subdirs: list[str] | tuple[str, ...] = DEFAULT_DATA_SUBDIRS,
        strict_data_subdir: bool = False,
        data_index_min: int | None = None,
        data_index_max: int | None = None,
        intensity_transform: str = "log1p",
        boxcox_lam: float = -0.15,
        boxcox_eps: float = 1e-6,
        lambda_conditioned: bool = False,
        lambda_min: float = -0.3,
        lambda_max: float = 0.2,
        lambda_candidates: list[float] | tuple[float, ...] | None = None,
        vst_lut: str = "",
        augment: bool = True,
    ):
        self.root_dir = root_dir
        self.intervals = [int(x) for x in intervals if int(x) > 0]
        if not self.intervals:
            raise ValueError("At least one positive interval is required")
        self.crop_size = int(crop_size)
        self.random_crop = bool(random_crop)
        self.pseudo_clean_frames = int(pseudo_clean_frames)
        self.lambda_conditioned = bool(lambda_conditioned) and intensity_transform == "boxcox"
        self.lambda_min = float(lambda_min)
        self.lambda_max = float(lambda_max)
        self.lambda_candidates = tuple(float(x) for x in lambda_candidates) if lambda_candidates else None
        self.augment = bool(augment)
        self.transform = IntensityTransform(
            name=intensity_transform,
            boxcox_lam=boxcox_lam,
            boxcox_eps=boxcox_eps,
            vst_lut=vst_lut,
        )

        folders = discover_sequence_dirs(
            root=root_dir,
            data_subdirs=tuple(data_subdirs),
            strict_data_subdir=strict_data_subdir,
            data_index_min=data_index_min,
            data_index_max=data_index_max,
        )
        self.records: list[SequenceRecord] = []
        self.items: list[tuple[int, int]] = []
        for folder in folders:
            files = tuple(list_supported_files(folder))
            if len(files) <= min(self.intervals):
                continue
            record_idx = len(self.records)
            self.records.append(SequenceRecord(folder=folder, files=files))
            for frame_idx in range(len(files)):
                self.items.append((record_idx, frame_idx))

        if not self.items:
            raise RuntimeError(f"No usable frame sequences found under {root_dir}")

    def __len__(self) -> int:
        return len(self.items)

    def _pair_index(self, frame_idx: int, interval: int, length: int) -> int:
        if frame_idx + interval < length:
            return frame_idx + interval
        if frame_idx - interval >= 0:
            return frame_idx - interval
        return frame_idx

    def _pseudo_indices(self, center_idx: int, length: int) -> list[int]:
        if self.pseudo_clean_frames <= 0 or self.pseudo_clean_frames >= length:
            return list(range(length))
        half = self.pseudo_clean_frames // 2
        start = max(0, center_idx - half)
        end = min(length, start + self.pseudo_clean_frames)
        start = max(0, end - self.pseudo_clean_frames)
        return list(range(start, end))

    def _sample_lambda(self) -> float:
        if not self.lambda_conditioned:
            return self.transform.boxcox_lam
        if self.lambda_candidates is not None:
            return random.choice(self.lambda_candidates)
        return random.uniform(self.lambda_min, self.lambda_max)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor | str]:
        record_idx, frame_idx = self.items[idx]
        record = self.records[record_idx]
        files = record.files
        interval = random.choice(self.intervals)
        target_idx = self._pair_index(frame_idx, interval, len(files))

        i1 = load_2d(files[frame_idx])
        i2 = load_2d(files[target_idx])
        pseudo_stack = [load_2d(files[j]) for j in self._pseudo_indices(frame_idx, len(files))]
        chat = np.mean(np.stack(pseudo_stack, axis=0), axis=0).astype(np.float32, copy=False)

        if self.random_crop:
            i1, i2, chat = random_crop_same([i1, i2, chat], self.crop_size)
        else:
            i1, i2, chat = [center_crop(x, self.crop_size) for x in (i1, i2, chat)]

        tensors = [torch.from_numpy(np.ascontiguousarray(x)).float().unsqueeze(0) for x in (i1, i2, chat)]
        if self.augment:
            tensors = augment_same(tensors)

        lam = self._sample_lambda()
        i1_t, i2_t, chat_t = [self.transform.forward(x, lam=lam) for x in tensors]
        condition = None
        if self.lambda_conditioned:
            value = lambda_condition_value(lam, self.lambda_min, self.lambda_max)
            condition = torch.full_like(i1_t, fill_value=value)

        return {
            "input": i1_t,
            "target": i2_t,
            "pseudo_clean": chat_t,
            "condition": condition if condition is not None else torch.empty(0),
            "folder": str(record.folder),
        }
