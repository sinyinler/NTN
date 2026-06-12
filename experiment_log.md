# Experiment Log

## 2026-06-12 初始化

- 改动：创建独立复现项目 `D:\Desktop\NTN`，采用 `paper-reproduction` skill 推荐的模块化结构。
- 目的：在不改动原 `lightweight_G` 项目的前提下，复刻并改造论文 NTN 为 `N2N-bootstrap` 无真 GT 版本。
- 关键设计：
  - 阶段一训练 Gaussian expert `D_prime`：`C_hat + synthetic Gaussian -> C_hat`。
  - 阶段二训练 Noise Translator `T`：`D_prime(T(I1))` 对齐 `I2` 或 `C_hat`，并加入 spatial/frequency Wasserstein explicit loss。
  - `C_hat` 默认使用多帧均值，也支持用已有 N2N checkpoint 在线生成。
- 配置：见 `configs/default.json`。
- git commit：`3d9f5cd`，初始化 N2N-bootstrap NTN 复现骨架。
- 验证：
  - `uv run --with torch python scripts\smoke_test.py`
  - 输出：`translated=(2, 1, 64, 64)`，`denoised=(2, 1, 64, 64)`，explicit loss 为有限值。
  - 纯语法解析检查通过 12 个新增入口/模块文件。
- 结果：项目骨架和核心 NTN 前向链路已可用；尚未开始真实数据训练。

## 2026-06-12 补齐原 N2N 训练策略

- 改动：
  - 新增 `train_n2n.py`，用于在独立 NTN 项目内重新训练 N2N baseline。
  - 将 `train_gaussian_expert.py` 与 `train_translator.py` 的 optimizer/scheduler 调整为原项目同款：`AdamW` + `OneCycleLR`，先 warmup 后 cosine annealing。
- 目的：保持 N2N、D_prime、T 三个训练阶段的学习率策略与原项目中反复调过的方案一致，避免训练参数漂移导致结论不可信。
- 验证：
  - `uv run --with torch python train_n2n.py --help`
  - `uv run --with torch python train_gaussian_expert.py --help`
  - `uv run --with torch python train_translator.py --help`
  - `uv run --with torch python scripts\smoke_test.py`
- 结果：命令行入口和核心前向链路均正常；尚未开始真实服务器训练。
