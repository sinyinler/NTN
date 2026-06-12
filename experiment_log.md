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
