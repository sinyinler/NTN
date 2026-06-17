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

## 2026-06-14 补充 N2N baseline 推理入口

- 改动：新增 `inference_n2n.py`。
- 目的：训练完 N2N 后，可以在不训练 `D_prime/T` 的情况下先查看 baseline 去噪效果。
- 输出：
  - `view_png/*_input.png`
  - `view_png/*_n2n.png`
  - `data_npy/*_input.npy`
  - `data_npy/*_n2n.npy`
  - `comparison/*_input_vs_n2n.png`，包含原图/去噪图和局部放大。
- 结果：等待服务器拉取后在训练好的 N2N checkpoint 上运行。

## 2026-06-14 修复 D_prime/T 多卡训练

- 问题：`train_n2n.py` 默认启用 `DataParallel`，但 `train_gaussian_expert.py` 和 `train_translator.py` 没有包 `DataParallel`，导致后两阶段只使用单卡。
- 改动：
  - 为 `train_gaussian_expert.py` 增加 `--data_parallel` 参数，默认 1。
  - 为 `train_translator.py` 增加 `--data_parallel` 参数，默认 1。
  - 训练模型和冻结的 N2N/D_prime 辅助模型都在多 GPU 可用时包成 `torch.nn.DataParallel`。
- 目的：让 N2N、D_prime、T 三个阶段的硬件使用习惯保持一致。

## 2026-06-14 (注：本条原始记录因编码问题为乱码) D'/T 训练显示实时 loss

- 在 `train_gaussian_expert.py` / `train_translator.py` 的进度条上显示每个 batch 的 loss，
  方便 `tail -f` 观察。D' 显示 loss/avg/lr；T 显示 loss/avg/implicit/explicit/lr。

## 2026-06-16 复盘诊断 + 真实噪声测量 + 按论文重构

- 背景：之前版本「效果差、几乎无泛化」。对照论文重新审查，确认病根**不在数据/去噪网络**
  （N2N 本身效果好，数据 `/mnt2/songyd/5x5` 是干净的同场景多帧），而在 NTN 翻译设计：
  1. D' 被做成「盲高斯 + σ 上限 0.15」，恒等映射成捷径、且 σ 覆盖不到真实噪声；
  2. implicit 用 I2、explicit 用糊均值 Ĉ，锚不一致，且 `T(I1)−Ĉ` 含血管结构 → explicit 误伤血管；
  3. explicit loss 从第 0 步全程开（论文应后 50% 才开）；
  4. lr 0.01 偏高、GIBlock 注入初值为 0 形同虚设。

- 新增 `scripts/measure_noise.py`（+ 抽出无 torch 依赖的 `data/discovery.py`），
  在 log1p 域用同场景相邻帧差估噪声 `σ=std/MAD(f_{k+1}-f_k)/√2`，按叠加层级分组。
- **实测 `/mnt2/songyd/5x5`（log1p 域 sigma_mad 中位数）**：
  - level1=0.428, level2=0.212, level3=0.138, level4=0.102，几乎完美按 1/N 衰减；
  - 真实噪声跨度 ~4 倍（0.10 ~ 0.43，个别到 0.57），signal_std≈0.56~0.63。
- **据此确定的设计决定**：
  - 「泛化」验证方式：用 level 2/3/4 训练，**留出最噪的 level1 作 OOD 测试**。
  - D' 改为在**盲区间 σ∈[0.08, 0.6]** 上训练（覆盖真实跨度、下界远离 0 堵死恒等捷径），
    保留论文「经验 std」的 explicit 写法（T 保幅度，只做高斯化 + 去相关）。
  - Ĉ 改用 **N2N(I1)**（`--bootstrap_checkpoint`），内容对齐 → `T(I1)−Ĉ` 才是纯噪声；
    implicit 与 explicit 同锚（`--implicit_target pseudo_clean`）。
  - explicit loss 延迟到训练过半启用（`--explicit_start_frac 0.5`）。
  - T 的 lr 对齐论文 1e-3→1e-5 cosine；GIBlock `init_noise_scale=0.1` 让注入真正生效。
- 代码改动：`data/discovery.py` 加 `include_levels`；数据集 + 两个 train 脚本加 `--levels`；
  D' 默认 σ∈[0.08,0.6]；T 加 explicit 延迟、改默认锚与 lr；`models/ntn.py` GIBlock 非零初值。
- 验证：py_compile 全过；`include_levels` 过滤逻辑单元测试通过（合成目录树）。
- 待办：需要用户提供服务器上的 N2N checkpoint 路径以跑 stage-1/2；确认各层级是否共享 scene 索引
  （决定 level1 OOD 评估能否用同场景高叠加帧当参考）。结果（指标+血管放大图）待训练后回填。

## 2026-06-16 确定泛化对照协议 + N2N 支持按层级训练

- 用户确认：各叠加层级是**同一批场景、只是帧数不同** → level1 的 OOD 评估可用同场景高叠加层当参考。
- 关键发现：`train_n2n.py` 用 `CrossLevelIntervalPairDataset`（[data/legacy_pairs.py](data/legacy_pairs.py)）
  做**跨层级配对**（target_level >= input_level），所以现有 N2N 是在**含 level1 的全部层级**上训练的。
- 据此确定对照协议（写入 README「泛化对照实验协议」）：
  - 必须**重训一个只用 level 2/3/4 的 N2N**（`--levels 2 3 4`，存到 `results/checkpoints/n2n_lv234`）。
  - 该 N2N 同时作为：(1) NTN 的 Ĉ 生成器；(2) 公平 OOD 基线。两者训练数据一致，只差 NTN 框架。
  - 原全层级 N2N 不能用：既会泄漏 level1 给 NTN，也不能当 OOD 基线。
- 代码：给 `legacy_pairs.py` 的 `CrossLevelIntervalPairDataset` / `create_train_dataset` /
  `SpeckleN2NLogDataset` 加 `include_levels`，并给 `train_n2n.py` 加 `--levels`。py_compile 通过。
- 评估计划（待写脚本）：level1 上对比 N2N-baseline vs NTN，用同场景最高叠加层(level4)的多帧均值当伪 GT
  算 PSNR/SSIM，并出血管局部放大对比图（图像效果为准）。

## 2026-06-17 修复 D'/T 数据加载性能灾难

- 现象：Step0 的 N2N 正常（4.75 s/it）；但 D' 阶段慢到 169 s/it、ETA 335h，且显存几乎不用 → GPU 空转、卡在读盘。
- 根因：`N2NBootstrapTripletDataset` 默认 `pseudo_clean_frames=0` = 每个样本把**整条序列(~500 帧)**读出求均值当 Ĉ；
  而我们用 `--bootstrap_checkpoint` 时 Ĉ 会被 N2N(I1) 覆盖，这 ~500 帧/样本读取全是无用功；叠加 `num_workers=0` 单进程读盘。
- 修复（代码）：数据集加 `compute_pseudo_clean` 开关，train 脚本在提供 bootstrap 时自动置 False（Ĉ 用 I1 占位、不读全序列）；
  `train_gaussian_expert/train_translator` 的 `--num_workers` 默认 0→8。
- 临时绕过（无需改码）：`--pseudo_clean_frames 1 --num_workers 8`，每样本只读 3 帧。
- 影响：仅性能，不改训练语义（Ĉ 始终是 N2N(I1)）。

## 2026-06-17 crop/batch 默认值对齐

- D'：与 N2N 同网络/同数据/同任务，默认改为 `crop 512 / batch 48`（原 128/8）——上下文一致、
  BatchNorm 统计更稳、用满空闲 GPU。
- T：对齐论文（附录 7），默认 `crop 256 / batch 4`（原 128/8）——crop 大利于 explicit loss
  的分布统计；batch 小因梯度要穿过冻结 D'、T+D' 激活同时驻留、显存重。24GB 有余量可上调 batch。
- 仅默认值调整，未改训练逻辑。

