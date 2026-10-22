# GPU Codec：可选传输加速

状态：G0，CPU 可验证；GPU 实测待环境
边界：只做数据面 codec，不改 CRD、planner、checkpoint 格式和 controller。

## 目标

在 source/target 节点有 GPU 时，对大 range/batch 做可选的 GPU 压缩，减少
跨集群传输字节。当前没有外部用户，不做旧客户端/旧 agent 兼容层；只保留
“没有 GPU、压缩比不够或协商失败时走 identity”的正确性/性能保护。

这不是 FSDP 验收，也不重复 `gpu_source_save.py` / `gpu_target_load.py`。

## G0 已落盘

- `src/shardferry/gpu_codec.py`：
  - `identity`、`zlib`、`nvcomp-zstd/lz4/gdeflate`；
  - `SFG1` 帧：原始长度、原始 SHA256、codec 名称和压缩 payload；
  - `NvcompCodec` 延迟导入 CUDA/nvCOMP，无 GPU 不会在 import 时失败。
- CUDA 可用性同时检查 `libcuda.so.1` 和 torch；CPU torch 不会把真实 GPU
  误判成不可用。
- `scripts/gpu_codec_probe.py`：测量压缩比、压缩/解压时间、网络投影；
  支持真实文件/目录或 zero/repeat/random/sparse 模式。
- `src/shardferry/gpu_policy.py`：只有在网络带宽已知、压缩比和端到端
  预计收益同时过线时才启用 codec；未知网络默认关闭。
- `src/shardferry/gpu_adaptive.py`：从 range 的 head/middle/tail 采样，
  用采样压缩时间和校准吞吐估算完整 range 收益；不满足相对收益门槛就走
  identity。
- `src/shardferry/gpu_batch.py`：把多个 range 合成 `SFB1` batch，统一采样、
  统一 decision、统一压缩，再按长度拆回。
- `nvidia-nvcomp-cu12>=6.0.0.12` 作为 optional `gpu` 依赖；基础安装不变。

## 使用

无 GPU 的 CPU fallback 冒烟：

```bash
python scripts/gpu_codec_probe.py \
  --codec zlib \
  --pattern zero \
  --size 16777216 \
  --range-bytes 4194304 \
  --json
```

GPU 机器上复用真实 DCP：

```bash
python scripts/gpu_codec_probe.py \
  --input /data/sf-gpu/b-4to3-fp32/source.dcp \
  --codec nvcomp-zstd \
  --range-bytes 4194304 \
  --max-ranges 16 \
  --network-gbps 10 \
  --json
```

只有 probe 在真实 checkpoint 上同时满足“压缩比足够 + 端到端网络投影更快”
才进入 HTTP 协商接入；不在 probe 通过前改主路径。

## 本地实测结论

RTX 5070 Ti Laptop 12GB，32 个 4MiB range：

- 高可压 1GB DCP fixture（128MB 样本）：`gdeflate` ratio 约 `0.232`；
  1Gbps 下策略收益 `+0.076s`，启用；10Gbps 下收益为负，禁用。
- 随机高熵 4-rank DCP（128MB 样本）：`zstd` ratio 约 `0.925`，
  `lz4` 约 `1.004`；1/10/25/100Gbps 下都没有正收益。

因此 **GPU 压缩不能作为默认路径**。它只适合已知慢 WAN + 可压 checkpoint；
随机权重 checkpoint 走 identity，避免为了“用了 GPU”制造负优化。

### 自适应 range 采样

`--adaptive` 会先采样再决定每个 range：

- 默认采样 1MiB，取 head/middle/tail；
- range 小于 1MiB 不压；
- 用一次同尺寸 calibration 得到 codec 吞吐，避免小样本的 CUDA/PCIe 固定
  开销被线性放大；
- per-range 门槛是 `max(绝对门槛, raw transfer * 10%)`，不是固定 50ms。

本机 RTX 5070 Ti 的验证结果：

- 4MiB range / 1Gbps：可压 range 也全部跳过；采样和 GPU 固定开销不划算。
- 8MiB range / 100Mbps：可压 fixture 8 个中启用 3 个；随机 DCP 8 个中启用 0 个。
- 首次 `.metadata` 小 range 全部走 identity。

### Batch 结果

本地 RTX 5070 Ti，8×4MiB batch，1Gbps：

- 可压 fixture：batch 启用，ratio `0.2347`，网络投影收益 `+0.058s`。
- 随机 DCP：batch 走 identity，ratio `1.0`，网络投影为负。
- 同一批 range 逐 range 自适应在 4MiB/1Gbps 下会跳过，batch 才把固定
  采样/决策开销摊薄。

云端 4×L20，同一 batch probe，128MB 样本，1Gbps：

- 可压重复数据：ratio `0.0198`，启用，投影收益 `+0.159s`。
- 随机高熵数据：ratio `1.000`，identity，投影收益 `-0.131s`。
- 硬件吞吐不同，但启用/禁用边界与本地一致。

`gpu_codec_probe.py --adaptive --batch-ranges N` 可复现；接入路径默认关闭。

## 接入现状

- source agent 已支持按 `X-Shard-Accept-Encoding: sfg1` 返回编码后的 shard；
- target `HttpStorage` 已支持解码并校验原始 SHA256；
- `SHARDFERRY_GPU_CODEC=off|identity|zlib|nvcomp-*` 默认 `off`；
- `SHARDFERRY_GPU_CODEC_NETWORK_GBPS` 必填；
- 启用 GPU codec 时 `SHARDFERRY_RUN_BATCH_BYTES` 默认提升到 16MiB，
  也可以显式覆盖；
- source/target 的 Helm 都暴露了 `gpuCodec`、`gpuCodecNetworkGbps`、
  `runBatchBytes`，默认关闭。

`SFB1` 多 range batch 已接到 `POST /v1/shards/batch`；target 的
`HttpStorage.read_batch` 解码后拆回，executor 会按一个 node 的多个 range
聚合请求。默认仍由 `SHARDFERRY_GPU_CODEC=off` 关闭。

GPUDirect Storage 不在 G0/G1 范围；它是硬件相关后端，等压缩路径和真实数据
证明有收益后再评估。
