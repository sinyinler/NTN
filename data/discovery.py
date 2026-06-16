from __future__ import annotations

"""数据发现与读取的纯 numpy helper（不依赖 torch）。

把这些和具体张量/增强无关的逻辑单独抽出来，既能被 N2NBootstrapTripletDataset 复用，
也能被 scripts/measure_noise.py 这类离线分析脚本在不安装 torch 的环境下直接调用。
"""

import re
from pathlib import Path

import numpy as np

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
