# Resume Closure：让恢复后的训练状态真正闭包

状态：0.3 实现中，第一版只做显式、受控的 rank-local state provider

## 一句话

一个 checkpoint 只有同时包含模型、优化器、scheduler、每 rank RNG 和
dataloader/sampler 状态，并且能在 target rank 上按固定顺序恢复、验证
版本和 hash，才算 `state-complete`。

这不是 CUDA 进程热迁移，也不自动序列化任意 Python 对象。

## 为什么需要

DCP 的 tensor model/optimizer state 已经可以跨存储和 world size 重分片，
但非 tensor 状态通常是“同 key 所有 rank 一份”：

- PyTorch DCP 的 save plan 会把同 key 的非 tensor 值视为 replicated；
- 不同 rank 的 RNG 会被合并成一份；
- `initial_lr` 等 scheduler/optimizer 元数据可能丢失；
- dataloader/sampler 恢复位置不一致；
- target-ready 只能证明模型能加载和跑 N step，不能证明后续训练轨迹一致。

因此要区分：

```text
L3 = model + optimizer load + N steps
L4 = L3 + rank-local state closure + deterministic restore proof
```

0.3 第一版只承诺 L4 的显式子集，不承诺任意 dataloader/闭包自动捕获。

## 数据模型

```text
state/
  manifest.json
  blobs/sha256/<digest>
```

manifest 关键字段：

- `schemaVersion`；
- `checkpointId`、`version`；
- `sourceWorldSize`、`targetWorldSize`；
- `rankMap`（source rank -> target rank）；
- 每个 entry：rank、provider name、format、digest、size；
- `complete`：当前 rank map 下是否形成完整 closure；
- `root`：manifest canonical JSON 的 SHA256。

第一版 provider：

- `python.random`；
- `numpy.random`；
- `torch.cpu.rng`；
- `torch.cuda.rng`；
- `JsonStateProvider`：显式 JSON-safe scheduler/dataloader adapter。

## 捕获与恢复

```bash
shardferry state capture \
  --root /checkpoints/job/state \
  --checkpoint-id job-1 \
  --checkpoint-version step-100 \
  --rank 0 \
  --source-world 4 \
  --providers python.random,numpy.random,torch.cpu.rng

shardferry state verify \
  --root /checkpoints/job/state \
  --require-complete

shardferry state restore \
  --root /checkpoints/job/state \
  --target-rank 0 \
  --providers python.random,numpy.random,torch.cpu.rng
```

多 rank 捕获必须让每个 rank 使用自己的 rank 编号；manifest 按
`(rank, provider)` 去重，不按 provider key 全局去重。

## 恢复顺序

固定顺序是：

1. 加载模型；
2. 加载优化器；
3. 恢复 scheduler/optimizer 非 tensor 元数据；
4. 恢复 rank RNG；
5. 恢复 dataloader/sampler；
6. 进入下一步训练。

不要让模型初始化或 warmup 代码在恢复 RNG 前消耗随机数。

## 第一版验收

### 单 rank CPU/GPU

- Python/NumPy RNG 下一个输出一致；
- Torch CPU RNG 下一个 tensor 一致；
- Torch CUDA RNG 在 CUDA venv 下一致；
- 缺失、损坏、版本错误必须 fail closed。

### 多 rank 语义

- 4 个 CPU rank 使用不同 seed；
- 捕获后每个 rank 的 RNG 值不同；
- 恢复后每个 rank 复现自己的下一个值，而不是 rank0 的值；
- 少一个 rank 时 `complete=false`，`--require-complete` 必须退出非零。

### 迁移边界

- 只有 state bundle 完整且 rank map 合法时，target-ready 才能把
  `stateComplete=true`；
- target rank 无 source mapping 时必须报告 `StateMissing`，不能猜测；
- 跨 world size 只接受显式 rank map，不做隐式 broadcast。

### 故障矩阵

- blob 缺失；
- blob hash 不匹配；
- manifest root 不匹配；
- entry rank 重复；
- target rank 重复映射；
- provider 版本不兼容；
- 恢复过程中进程重启；
- 重复 restore 幂等。

## 不做

- 任意 Python 对象 pickle；
- 自动捕获任意 dataloader 内部 generator；
- 进程内存/CUDA context 热迁移；
- 无 provider 的全部训练框架 L4 承诺；
- 没有显式 rank map 的任意 TP/PP 重排。

## Target-ready L4（2026-10-22）

### target-load

新增参数：

- `--state-root`：rank-local state bundle；
- `--state-providers`：逗号分隔的 provider；
- `--state-target-rank`：默认 `--rank` 或 0；
- `--require-state-complete`：不完整直接失败。

顺序：

1. DCP model/optimizer load；
2. `verify_state(require_complete=...)`；
3. `restore_state(strict=True)`；
4. `verify_restored`：恢复后重新 capture，digest 必须与 entry 一致；
5. 结果 JSON 带 `state_complete/state_root/state_restored`。

缺失 rank、缺失 provider、损坏 blob、digest 不一致全部 exit 2，
不会把半状态报成成功。

### target job

`TargetJobConfig` 新增 `state_template/state_providers/require_state`；
Job env 注入 `SHARDFERRY_STATE_TEMPLATE/.../REQUIRE_STATE`；
`evaluate_target_reports(require_state=True)` 要求每个 rank
`stateComplete=true`。

### 验收

- 7 条 L4 测试；
- RNG 恢复后下一输出与 source 一致；
- 不完整/缺 provider/损坏 blob 全部 fail closed；
- resume-closure 全仓：`368 passed, 5 skipped`。

### 还没做

- 真实 DCP/FSDP 训练循环的 N-step RNG/dataloader 轨迹证明；
- Go controller/CRD 的 `stateMode` 和 target job 模板；
- 任意 dataloader 的显式 `JsonStateProvider` 适配；
- 4 卡/NCCL/跨节点。

### 与 A3.1 TrainingState 的衔接

- LRS 侧的 `TrainingStateProvider` 把 A3.1 的
  model/Adam/RNG/generator/scaler/extra 作为 state bundle 的一个 entry；
- `RankSampler` 放在 `TrainingState.extra["rank_sampler"]`；
- target 训练代码应注册这个 provider；`--state-providers` 的内置列表
  只覆盖 RNG/Json，不自动构造自定义 training.state provider；
- bundle 的 `verify_restored` 支持 provider 级 `verify`，A3.1 用语义
  tensor/dict 比较，不用 torch.save 字节 digests。
