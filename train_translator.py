from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
import torch
from torch.optim.lr_scheduler import OneCycleLR
from torch.utils.data import DataLoader, random_split
from tqdm import tqdm

from data.ntn_dataset import N2NBootstrapTripletDataset
from losses.charbonnier import CharbonnierLoss
from losses.ntn_losses import ExplicitNoiseTranslationLoss
from models.denoiser import Denoiser
from models.ntn import NoiseTranslator
from utils.checkpoint import load_weights_flexible, save_training_checkpoint
from utils.intensity import append_condition_channel


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def condition_or_none(batch: dict, device: torch.device) -> torch.Tensor | None:
    condition = batch.get("condition")
    if condition is None or condition.numel() == 0:
        return None
    return condition.to(device)


def build_dataset(args) -> N2NBootstrapTripletDataset:
    return N2NBootstrapTripletDataset(
        root_dir=args.data_path,
        intervals=args.intervals,
        crop_size=args.crop_size,
        random_crop=bool(args.random_crop),
        pseudo_clean_frames=args.pseudo_clean_frames,
        data_subdirs=tuple(args.data_subdirs),
        strict_data_subdir=bool(args.strict_data_subdir),
        data_index_min=args.data_index_min if args.data_index_min >= 0 else None,
        data_index_max=args.data_index_max if args.data_index_max >= 0 else None,
        intensity_transform=args.intensity_transform,
        boxcox_lam=args.boxcox_lam,
        boxcox_eps=args.boxcox_eps,
        lambda_conditioned=bool(args.lambda_conditioned),
        lambda_min=args.lambda_min,
        lambda_max=args.lambda_max,
        lambda_candidates=args.lambda_candidates,
        vst_lut=args.vst_lut,
        augment=True,
    )


def load_frozen_denoiser(path: str, device: torch.device, input_channels: int) -> Denoiser:
    model = Denoiser(input_channels=input_channels).to(device)
    info = load_weights_flexible(model, path, device)
    print(f"[INFO] Loaded frozen denoiser from {path}: {info}")
    model.eval()
    for param in model.parameters():
        param.requires_grad_(False)
    return model


def maybe_data_parallel(model: torch.nn.Module, args, name: str) -> torch.nn.Module:
    """和原 N2N 训练保持一致：多卡可用时默认启用 DataParallel。"""

    if torch.cuda.device_count() > 1 and bool(args.data_parallel):
        print(f"[INFO] Using DataParallel for {name} on {torch.cuda.device_count()} GPUs")
        return torch.nn.DataParallel(model)
    return model


def build_onecycle(optimizer, steps_per_epoch: int, args):
    total_steps = max(1, steps_per_epoch * args.epochs)
    return OneCycleLR(
        optimizer,
        max_lr=args.lr_max,
        total_steps=total_steps,
        pct_start=args.warmup_pct,
        anneal_strategy="cos",
        div_factor=args.lr_max / args.lr_final,
        final_div_factor=1.0,
        cycle_momentum=False,
    )


def train(args) -> None:
    if not args.gaussian_expert_checkpoint:
        raise ValueError("--gaussian_expert_checkpoint is required")

    set_seed(args.seed)
    device = torch.device(args.device if args.device else ("cuda" if torch.cuda.is_available() else "cpu"))
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    dataset = build_dataset(args)
    val_size = max(1, int(len(dataset) * args.val_fraction)) if len(dataset) > 1 else 0
    train_size = len(dataset) - val_size
    if val_size > 0:
        train_dataset, val_dataset = random_split(dataset, [train_size, val_size], generator=torch.Generator().manual_seed(args.seed))
    else:
        train_dataset, val_dataset = dataset, None

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)
    val_loader = None if val_dataset is None else DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers, pin_memory=True)

    input_channels = 2 if args.lambda_conditioned and args.intensity_transform == "boxcox" else 1
    gaussian_expert = load_frozen_denoiser(args.gaussian_expert_checkpoint, device, input_channels=input_channels)
    bootstrap_model = load_frozen_denoiser(args.bootstrap_checkpoint, device, input_channels=input_channels) if args.bootstrap_checkpoint else None
    gaussian_expert = maybe_data_parallel(gaussian_expert, args, name="frozen Gaussian expert D_prime")
    if bootstrap_model is not None:
        bootstrap_model = maybe_data_parallel(bootstrap_model, args, name="frozen N2N bootstrap")

    translator = NoiseTranslator(
        input_channels=input_channels,
        width=args.width,
        middle_blocks=args.middle_blocks,
        inject_sigma=args.inject_sigma,
        residual_scale=args.residual_scale,
    ).to(device)
    translator = maybe_data_parallel(translator, args, name="Noise Translator T")
    implicit_criterion = CharbonnierLoss(eps=args.charbonnier_eps).to(device)
    explicit_criterion = ExplicitNoiseTranslationLoss(beta=args.beta, highpass_ratio=args.highpass_ratio).to(device)
    optimizer = torch.optim.AdamW(
        translator.parameters(),
        lr=args.lr_final,
        betas=(0.9, 0.999),
        eps=1e-8,
        weight_decay=args.weight_decay,
    )
    scheduler = build_onecycle(optimizer, len(train_loader), args)

    metadata = vars(args).copy()
    metadata.update({"dataset_size": len(dataset), "train_size": train_size, "val_size": val_size})
    (save_dir / "run_config.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    def forward_batch(batch: dict, train_mode: bool) -> dict[str, torch.Tensor]:
        i1 = batch["input"].to(device)
        i2 = batch["target"].to(device)
        pseudo_clean = batch["pseudo_clean"].to(device)
        condition = condition_or_none(batch, device)

        if bootstrap_model is not None:
            with torch.no_grad():
                pseudo_clean = bootstrap_model(append_condition_channel(i1, condition)).detach()

        if train_mode and args.aug_sigma > 0:
            sigma = torch.empty(i1.shape[0], 1, 1, 1, device=device).uniform_(0.0, args.aug_sigma)
            i1 = i1 + torch.randn_like(i1) * sigma

        translated = translator(append_condition_channel(i1, condition))
        pred = gaussian_expert(append_condition_channel(translated, condition))
        implicit_target = i2 if args.implicit_target == "i2" else pseudo_clean
        loss_implicit = implicit_criterion(pred, implicit_target)
        translated_noise = translated - pseudo_clean
        loss_explicit, loss_spatial, loss_freq = explicit_criterion(translated_noise)
        loss = loss_implicit + args.alpha * loss_explicit
        return {
            "loss": loss,
            "loss_implicit": loss_implicit.detach(),
            "loss_explicit": loss_explicit.detach(),
            "loss_spatial": loss_spatial.detach(),
            "loss_freq": loss_freq.detach(),
        }

    for epoch in range(1, args.epochs + 1):
        translator.train()
        totals = {"loss": 0.0, "loss_implicit": 0.0, "loss_explicit": 0.0, "loss_spatial": 0.0, "loss_freq": 0.0}
        pbar = tqdm(train_loader, desc=f"T epoch {epoch}/{args.epochs}")
        for step, batch in enumerate(pbar, start=1):
            out = forward_batch(batch, train_mode=True)
            optimizer.zero_grad(set_to_none=True)
            out["loss"].backward()
            if args.grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(translator.parameters(), args.grad_clip)
            optimizer.step()
            scheduler.step()
            for key in totals:
                totals[key] += float(out[key].item())
            current_lr = scheduler.get_last_lr()[0]
            pbar.set_postfix(
                {
                    "loss": f"{float(out['loss'].item()):.6f}",
                    "avg": f"{totals['loss'] / step:.6f}",
                    "impl": f"{float(out['loss_implicit'].item()):.6f}",
                    "expl": f"{float(out['loss_explicit'].item()):.6f}",
                    "lr": f"{current_lr:.3g}",
                }
            )

        train_metrics = {key: value / max(1, len(train_loader)) for key, value in totals.items()}
        val_loss = 0.0
        if val_loader is not None:
            translator.eval()
            with torch.no_grad():
                for batch in val_loader:
                    val_loss += float(forward_batch(batch, train_mode=False)["loss"].item())
            val_loss /= max(1, len(val_loader))

        print(
            f"[EPOCH {epoch}] loss={train_metrics['loss']:.6f} "
            f"implicit={train_metrics['loss_implicit']:.6f} explicit={train_metrics['loss_explicit']:.6f} "
            f"val={val_loss:.6f}"
        )
        save_training_checkpoint(
            save_dir / f"translator_epoch_{epoch}.pth",
            translator,
            epoch,
            args,
            extra={"train_metrics": train_metrics, "val_loss": val_loss},
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 2: train N2N-bootstrap Noise Translator T.")
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--data_subdirs", nargs="*", default=["npy", "lbf"])
    parser.add_argument("--strict_data_subdir", type=int, default=0)
    parser.add_argument("--data_index_min", type=int, default=-1)
    parser.add_argument("--data_index_max", type=int, default=-1)
    parser.add_argument("--intervals", type=int, nargs="*", default=[5, 7, 9])
    parser.add_argument("--crop_size", type=int, default=128)
    parser.add_argument("--random_crop", type=int, default=1)
    parser.add_argument("--pseudo_clean_frames", type=int, default=0)
    parser.add_argument("--bootstrap_checkpoint", type=str, default="", help="Optional trained N2N model used as C_hat generator.")
    parser.add_argument("--gaussian_expert_checkpoint", type=str, required=True)
    parser.add_argument("--implicit_target", choices=["i2", "pseudo_clean"], default="i2")
    parser.add_argument("--intensity_transform", choices=["none", "log1p", "boxcox", "learned_vst"], default="log1p")
    parser.add_argument("--vst_lut", type=str, default="")
    parser.add_argument("--boxcox_lam", type=float, default=-0.15)
    parser.add_argument("--boxcox_eps", type=float, default=1e-6)
    parser.add_argument("--lambda_conditioned", type=int, default=0)
    parser.add_argument("--lambda_min", type=float, default=-0.3)
    parser.add_argument("--lambda_max", type=float, default=0.2)
    parser.add_argument("--lambda_candidates", type=float, nargs="*", default=None)
    parser.add_argument("--width", type=int, default=32)
    parser.add_argument("--middle_blocks", type=int, default=2)
    parser.add_argument("--inject_sigma", type=float, default=1.0)
    parser.add_argument("--residual_scale", type=float, default=1.0)
    parser.add_argument("--alpha", type=float, default=5e-2)
    parser.add_argument("--beta", type=float, default=2e-3)
    parser.add_argument("--highpass_ratio", type=float, default=0.0)
    parser.add_argument("--aug_sigma", type=float, default=0.0)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch_size", type=int, default=8)
    parser.add_argument("--num_workers", type=int, default=0)
    parser.add_argument("--val_fraction", type=float, default=0.02)
    parser.add_argument("--lr", type=float, default=0.01, help="Compatibility alias for lr_max.")
    parser.add_argument("--lr_max", type=float, default=None)
    parser.add_argument("--lr_final", type=float, default=0.0005)
    parser.add_argument("--warmup_pct", type=float, default=0.1)
    parser.add_argument("--weight_decay", type=float, default=1e-2)
    parser.add_argument("--charbonnier_eps", type=float, default=1e-3)
    parser.add_argument("--grad_clip", type=float, default=1.0)
    parser.add_argument("--data_parallel", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", type=str, default="")
    parser.add_argument("--save_dir", type=str, default="results/checkpoints/translator")
    args = parser.parse_args()
    if args.lr_max is None:
        args.lr_max = args.lr
    if args.lr_max <= args.lr_final:
        raise ValueError(f"lr_max must be greater than lr_final, got lr_max={args.lr_max}, lr_final={args.lr_final}")
    return args


if __name__ == "__main__":
    train(parse_args())
