# shardferry benchmark

## 对照定义

`baseline_transfer_bytes` 定义为：

```text
source checkpoint 全部字节 + target checkpoint 全部字节
```

对应用户故事里“先把所有 source 分片送到一个全局目录，再从全局目录重建
target”的网络搬运上界。

`shardferry_plan_bytes` 是 plan 实际要求搬运的字节。当前无 target replication
的 1D 场景下，它等于 target checkpoint 总字节。

这不是 wall-clock 网络基准；传输时间依赖机器、磁盘、网络和并发。当前脚本先
验证控制面、字节口径和本地 correctness，真实跨机时间在 A/B 上测。

## 运行

```bash
python scripts/benchmark.py \
  --size-mb 1024 \
  --source-world 8 --target-world 5 \
  --work-dir /tmp/shardferry-benchmark-1g
```

脚本会生成 synthetic source shard，执行 `plan -> apply -> verify`，输出
`benchmark.json`。

## 已跑结果

1GB、8→5、无 replication：

```json
{
  "source_bytes": 1073741824,
  "target_bytes": 1073741824,
  "shardferry_plan_bytes": 1073741824,
  "baseline_transfer_bytes": 2147483648,
  "saving_ratio": 2.0,
  "report_status": "success"
}
```

这证明的是搬运量口径上的 2x 收益，不是网络耗时 2x。真正的性能结论必须在
双机 A/B 上补。

## CDP / 增量迁移 benchmark

`scripts/cdp_benchmark.py` 区分两种口径，不能混用：

- `--mode plan`：只生成 sparse source 并跑真实 planner，测控制面规划字节与
  规划耗时，不执行 apply、不写 target、不读 checkpoint 数据。
- `--mode cdp`：真实执行 `plan -> apply -> target-layout manifest -> CAS ->
  shadow -> cutover`，测完整链路；成本随 checkpoint 字节和版本数线性增长。

### plan 口径

128→64，单版本，sparse source：

| checkpoint | plan bytes | plan 秒 | 进程 max RSS |
|---:|---:|---:|---:|
| 1 GiB | 1,073,741,824 | 0.037 | 45 MiB |
| 10 GiB | 10,737,418,240 | 0.024 | 45 MiB |

这说明 planner 本身不是 1G/10G 的瓶颈；它不等于 1G/10G 数据已经完成传输。

### 完整 CDP 口径

128→64，3 个版本，128 MiB/version，CDC 64K/256K/1M：

| version | missing bytes | transfer ratio | sync 秒 |
|---|---:|---:|---:|
| step-1000 | 134,217,728 | 1.0000 | 12.00 |
| step-2000 | 723,462 | 0.0054 | 10.23 |
| step-3000 | 1,171,171 | 0.0087 | 10.29 |

cutover：`promoted`，paused `0.057s`，进程 max RSS 约 `58 MiB`。

结论：第一版支付全量成本；后续版本只支付约 `0.5%–0.9%` 的增量字节。完整
1G/10G 多版本链路是 I/O bound，必须单独排期，不能用 plan-only 数字冒充。

### file-level canonical chunk 快路径

目标布局里的 DCP shard 文件适合做 file-level canonical chunk：整文件流式
哈希入 CAS，shadow 直接用 hardlink/reflink，不再对每个文件做逐字节 CDC。
CDC 继续保留给需要子文件增量的场景。

同一个 128→64、128MiB x 3 版本，优化前后：

| 路径 | wall | 后两版 sync |
|---|---:|---:|
| 旧 full apply + CDC materialize | ~34s | ~10s / 10s |
| Incremental provider + file chunk | **2.95s** | **0.31s / 0.39s** |

1GiB x 3 版本（128→64，random source，file-level chunk）：

| version | missing bytes | transfer ratio | sync 秒 |
|---|---:|---:|---:|
| step-1000 | 1,073,741,824 | 1.0000 | 7.92 |
| step-2000 | 16,777,216 | 0.0156 | 1.74 |
| step-3000 | 33,554,432 | 0.0313 | 1.99 |

总 wall `25.9s`，cutover paused `0.426s`，max RSS `75MiB`。文件级 chunk 的
代价是变化 shard 按整文件传输；收益是 manifest/CAS/shadow 路径从逐字节
Python CDC 变成流式文件操作，完整 CDP 路径出现数量级加速。

### direct incremental provider（去掉 provider cache）

`DirectIncrementalApplyFileProvider` 直接以 target shadow 的上一版本作为
base，不再保留 provider cache；只把本次变化的 target rank 写进临时目录。

128MiB x 3：`2.65s`。  
1GiB x 3：总 wall `18.6s`，max RSS `76MiB`，cutover paused `0.406s`。

1GiB 目录构成：source 三个版本约 `3.1G`，target/CAS/shadow 约 `1.1G`，
合计约 `4.1G`；provider cache 已消失。

### CAS hardlink ingest

目标临时目录现在放在 target 同一文件系统；file-level CAS ingest 支持
hardlink，immutable target 文件不再先读一遍再复制一份 CAS。128MiB x 3
约 `1.8s`，1GiB x 3 约 `18.4s`，max RSS `77MiB`。小 rank 不并行；只有
单 target rank >= 64MiB 才启用 executor 并行，避免 Python 线程把小文件
任务拖慢。

### Rust native file ingest

`native/shardferry-fast` 用 Rust 流式 SHA-256 + hardlink/copy 写 CAS；Python
只做 manifest canonicalization 和版本元数据。native binary 不存在时自动
fallback 到 Python ingest。

同一组 128→64、file-level chunk：

| 路径 | 128MiB x 3 | 1GiB x 3 | 1GiB RSS |
|---|---:|---:|---:|
| Python ingest | 1.8s | 18.4s | 77MiB |
| Rust native ingest | **1.78s** | **17.15s** | **77MiB** |

10GiB x 3（128→64，file-level chunk，Rust native ingest）：

| 指标 | 结果 |
|---|---:|
| total wall | 181s |
| first sync | 57.9s |
| later sync | 12.1s / 11.9s |
| delta ratio | 1.56% / 3.13% |
| cutover pause | 3.37s |
| max RSS | 286MiB |
| source versions | 31G |
| target/CAS/shadow | 11G |

10GiB 下 4 个 target-rank worker 与单 executor 基本同速，说明瓶颈已经是
本地 I/O/源生成，而不是 Python 线程；不再增加 Python 并发。

### P1 acceptance

性能契约和硬门槛见
`docs/superpowers/specs/2026-10-22-rto-performance-contract.md`。完整 CDP
benchmark 支持：

```bash
.venv/bin/python scripts/cdp_benchmark.py \
  --size-mb 1024 --source-world 128 --target-world 64 \
  --versions 3 --fill random --chunk-alg file --mode cdp \
  --assert-p1 --work-dir /tmp/shardferry-accept-1g
```

128MiB、1GiB、10GiB 三次 `--assert-p1` 均通过。10GiB 最新 acceptance：
wall `190.3s`、cutover pause `3.87s`、max RSS `287MiB`、target storage
ratio `1.0x`。

### native CDC vs file-level

`--chunk-alg cdc` 现在走 Rust stream chunker，不再走 Python逐字节 CDC。

| 1GiB x 3 | wall | delta v2/v3 | max RSS | pause |
|---|---:|---:|---:|---:|
| file-level | 17.15s | 1.56% / 3.13% | 77MiB | 0.33s |
| native CDC | 25.3s | 0.051% / 0.076% | 77MiB | 0.40s |

结论：file-level 是 RTO 优先，native CDC 是额外数据/带宽优先。下一步应由
cost model 按文件大小、变化比例和 I/O 能力自动选择，而不是固定一种策略。

### adaptive policy

`AdaptiveChunkingPolicy` 支持 `speed / bytes / balanced`。策略在每个
checkpoint 流开始时选定并保持稳定；未变化文件直接复用上一版 manifest
chunk，只有变化文件重新选择 chunk 策略。不能在不同版本间任意切换
file/CDC，否则 chunk 身份会整体变化，导致 delta 失真。

128MiB x 3、`adaptive + bytes`：wall `2.54s`，后续 delta `1.56% / 1.81%`。
1GiB 下 native CDC 仍是额外数据优先选项（`0.051% / 0.076%`）；file-level
仍是 RTO 优先选项（17.15s）。

### kernel copy_file_range

`FileStorage.copy_range` 已实现，但当前 WSL/本地文件系统实测 1GiB 约
19s，慢于默认 read/write 路径（约 17s），所以默认关闭。需要时设置
`SHARDFERRY_COPY_FILE_RANGE=1` 开启；它保留给跨文件系统或支持的平台验证。

### direct CAS staging

`cas_executor.py` 提供绕过 `executor.apply` target tree/journal 的 staging
执行器，并已用 bitwise 测试对齐普通 apply。但 1GiB/64 target 场景下，64 个
小 rank 的 staging 调用开销使总 wall 约 19.5s、RSS 120MiB；默认只在单 rank
>=128MiB 时启用，大于该阈值的超大 rank 才尝试 direct CAS。

10GiB/64 target（单 rank 160MiB）direct CAS staging 最新 acceptance：
wall `186.2s`、RSS `262MiB`、cutover pause `3.68s`，通过 P1 门槛。

### HTTP range keep-alive

source agent 已切到 HTTP/1.1，`HttpStorage` 对同一 host 复用
`HTTP(S)Connection`。range-heavy 的 128-rank 场景不再每个 range 都新建
TCP/TLS 连接；现有 HTTP source/agent/executor 测试通过。实际吞吐收益需要在
真实网络/多节点上补测。

### target-ready probe RTO

`cdp_benchmark.py` 默认在 cutover 后运行
`python -m shardferry.target_probe`，校验 target current、manifest root、
CAS chunk 和 shadow；报告新增 `target_probe=true`、`target_ready_seconds`
和 `rto_seconds`。可用 `--no-target-probe` 关闭。

128MiB x 3、8→4、file-level 的本地验收：

- full CDP wall `2.68s`
- max RSS `94.3MiB`
- cutover pause / rto `0.0913s`
- target-ready `0.0352s`
- `--assert-p1` passed

这证明 target probe 已进入可重复 benchmark，而不是只存在于单测。

`--target-dcp-rank N` 会启动一个 warm target-job 子进程，触发该 rank 的
local DCP load，再通过 `/readyz` 等待完成。预热 DCP runtime 后，4MB
本地试跑的 target-ready 约 `0.13s`；预热前约 `0.514s`，差异主要来自
首次 import/进程调度。当前仍是本机诊断，物理 target job 需要单独补测。

warm target job 预热完成后的正式本地结果：

| 场景 | active wall | target-ready | max RSS | P1 |
|---|---:|---:|---:|---|
| 128MiB x3、8→4、rank0 DCP | 1.91s | 0.132s | 93.2MiB | pass |
| 1GiB x3、128→64、rank0 DCP | 21.83s | 0.114s | 69.5MiB | pass |

1GiB 的 target prepare（生成/启动 warm target job）为 3.57s，单独记录在
`target_prepare_seconds`；`active_wall_seconds` 从预热完成后开始计，避免
把 setup 混进故障恢复时间。

1GiB x 3、128→64、file-level 的同口径验收：

- full CDP wall `21.92s`
- max RSS `73.9MiB`
- cutover pause / rto `0.547s`
- target-ready `0.062s`
- `--assert-p1` passed

修复 benchmark 随机源生成后，1GiB x 3、16→8（单 rank 64MiB）也通过 P1：
`active_wall=10.41s`、RSS `56.2MiB`、target-ready `0.046s`。之前的
`237.8MiB` 已确认是源生成一次性大块造成的 benchmark artifact，不是数据面
RSS；这个旧数字不能再作为产品结论。
