# LRS M3：HTTP 跨集群传输与断链恢复

状态：已通过（2026-10-22）
范围：真实 HTTP 传输、两个 netns、CPU；无 GPU/NCCL
代码：`src/shardferry/lrs/transport.py`、`faults.py`、`scenario.py`
测试：`tests/unit/test_lrs_m3_transport.py`、`tests/integration/test_lrs_m3_http.py`
脚本：`scripts/lrs_m3_{server,client,demo,netns_test}.py`
证据：`evidence/lrs/2026-10-22-m3/{proof.json,netns.json}`

## 1. M3 要证明什么

M3 把 M1/M2 的 `InMemoryTransfer` 换成真实 HTTP：

> W=8，F=2，两个独立网络域；target 允许落后 `K=2` 步；传输中丢连接/
> 断链后，sink 能重连、从 ack 续传；cutover 前 target 必须追平；最终
> 模型状态与 baseline bitwise 相同，且 target 的持久化 digest 与 proof 一致。

## 2. Transport 设计

### `DeltaSink`

统一接口：

- `send(blob)`：非阻塞入队；
- `wait_for(step)`：阻塞到 target ack 该 step；
- `acked_digest(step)`：取 target 侧该 step 的 digest；
- `last_acked_step`：用于有界 lag；
- `close()`：停止 worker。

### `HttpDeltaSink`

- 每个 channel 一个有序 worker；
- `send` 不阻塞训练 step；
- POST 失败时先 `GET /ack`，若 target 已经收到该 step 就跳过，否则重试；
- 4xx（digest 冲突/非法）直接失败，5xx/网络错误退避重试；
- `wait_timeout` 防止 server 永久不可达时挂死；
- 绕过环境代理（`ProxyHandler({})`），避免本地/内网请求被代理劫持。

### `HttpDeltaServer`

- `POST /v1/channels/<channel>/blobs`：校验 body 与 digest，原子写
  `<step>.blob` 和 `latest.json`；
- `GET /v1/channels/<channel>/ack`：返回最新 step/digest；
- `GET /health`：健康检查；
- 幂等：重复 step 且 digest 相同直接 ack；digest 冲突返回 409；
- server 重启后从 `latest.json` 恢复 ack，不依赖内存状态。

### 有界 lag

模拟器新增：

- `max_lag=K`：每个 step 结束等待 `step-K` 被 ack，lag 超过 K 直接 fail；
- `lag_history`：记录每个 step 的 target lag；
- `cutover_wait_seconds`：记录 cutover 等 target 追平的真实时间；
- `step_delay`：仅测试用，保证断链窗口落在迁移过程中。

## 3. 验收结果

### 24-step HTTP + 丢 3 次连接

`scripts/lrs_m3_demo.py`：

- `ok=True`，`lag_max=2`，代理丢 3 次 POST 后恢复；
- theta digest `fe11bb6fffcb84f5`，与 baseline bitwise；
- 两段 proof：`in_flight_at_cutover=[2,1]`，digest 全部匹配；
- cutover 等待：rank 0 `0.0129s`，rank 1 `0.0000s`；
- 最终布局：6A + 2B。

### 120-step 真 netns + veth down/up

`scripts/lrs_m3_netns_test.py`：

- `sf-a`：`10.200.0.1/24`，客户端；
- `sf-b`：`10.200.0.2/24`，HTTP receiver；
- 迁移 steps 4..100，`F=2`，`max_lag=2`；
- 中途 `veth-a down` 0.5s，再 up；
- `ok=True`，`lag_max=2`；
- temp owners `[2,3]`；
- cutover 等待 `[0.0014, 0.0]`；
- 120-step theta digest `ab2d47425f463e7b...`，与 baseline bitwise；
- target 的 `latest.json` step=100，digest 与 proof 一致。

### 测试

- M3 transport：`5 passed`（roundtrip、丢连接续传、server 重启、幂等、超时）；
- M3 integration：`1 passed`（HTTP + DroppingProxy + W=8/F=2）；
- LRS 合计：`32 passed`；
- 全仓：`384 passed, 4 skipped`。

## 4. WSL 回环说明

本机 WSL 的 `127.0.0.1` 回环出现异常：server 已 listen，client 能 connect，
但 accept/recv 不返回；最小 stdlib HTTP server 和裸 socket 都能复现。

现有仓库的 HTTP 测试一直用 `0.0.0.0` + 局域网 IP，因此 M3 也采用：

- in-process/集成测试：`0.0.0.0:0` + `_local_ip()`；
- netns：`10.200.0.1` / `10.200.0.2`。

这是 WSL/本机网络环境问题，不是协议或 Windows 的问题。Windows 原生
`127.0.0.1` 正常；Linux 正常回环也应正常。

## 5. M3 不证明什么

- 仍是 HTTP/1.0、每个 blob 新建连接；没有 QUIC、长连接、压缩、带宽优化；
- 不接真实 DCP/FSDP/DCP checkpoint，只是 toy model；
- 不证明 NCCL/GPU、TP/PP；
- 不证明 source death takeover；M4；
- 不证明网络分区下的 quorum 降级；M4/M6；
- 不证明时钟偏移、controller 重启、磁盘满、OOM；
- target 存的是完整 shard blob，不是参数 diff；
- 不给吞吐/延迟性能结论。

## 6. Windows

- LRS 核心、HTTP sink/server、demo/client 都是纯 Python + stdlib，Windows
  原生可跑；
- `scripts/lrs_m3_netns_test.py` 是 Linux-only；Windows 用两个进程/两个端口
  或两台机器；
- K8s controller 仍是 Linux 容器；Windows 侧是 CLI/客户端；
- 单卡 GPU 对 M3 不是瓶颈，M5/NCCL 验证时再用。

## 7. 下一步

M4：source death takeover。

- 在 netns 里直接杀 source 进程/断开 source；
- B 侧用已提交 snapshot+delta 组成 degraded world；
- 只有 takeover quorum + coverage 同时成立才继续，否则 fail closed；
- 证明不会出现“看起来在跑、实际 shard 残缺”的静默成功。
