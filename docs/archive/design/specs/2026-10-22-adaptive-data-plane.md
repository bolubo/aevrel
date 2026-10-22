# 尖刀 6：自适应数据面（M1）

状态：M1 完成（请求级 token bucket + 429/Retry-After 背压）  
日期：2026-10-22  
前置：布局编译器、零物化 DCP runtime、CDP/cutover

## 1. 目标

让数据面在远端 source 变慢、返回 429 或出现瞬时错误时，不是简单地无限重试，
而是：

- 限制每个 controller/agent 的 HTTP 请求速率；
- 对 429 做降速，并在成功请求后逐步恢复；
- 保留现有客户端重试和 `404/416` 不重试的语义；
- 为后续按链路、按 rank、按磁盘拓扑调度提供统一控制点。

第一版不改变 plan 格式、不改变 checkpoint 格式，也不依赖 GPU。

## 2. 实现

- `src/shardferry/adaptive.py`
  - `AdaptiveTokenBucket`：线程安全、按请求取 token；
  - `AdaptiveRateController`：成功后渐进升速，429 后减半；
  - 快照包含 rate、burst、tokens、throttles、successes、errors。
- `storage/http.py`
  - 可选 `rate_controller`；每次请求前 `acquire(1)`；
  - 429 调用 `on_throttle()`，5xx 调用 `on_error()`，成功调用 `on_success()`；
  - 429 带 `Retry-After` 时至少等待该 hint（上限 30s）；
  - 现有重试和 backoff 保留。
- `storage/router.py`
  - 透传 rate controller；
  - 支持 `SHARDFERRY_RATE_LIMIT_RPS` 环境变量。
- Helm：
  - `source.rateLimitRPS` / `target.rateLimitRPS` 注入环境变量。

## 3. 验收

- token bucket 能 timeout、按时间 refill；
- 429 会降低 rate，后续成功会恢复；
- `Retry-After` hint 会进入真实 retry delay；
- HTTP 429 重试后成功时统计到 `throttles=1/successes=1`；
- `StorageRouter` 能透传甚至从环境变量创建 limiter；
- 未配置限速时行为与旧版本一致；
- Python 全量测试、Go 全量测试、vet/build 通过。

## 4. 后续

- 按 source/target endpoint 分桶，而不是只有全局 limiter；
- 将 `Retry-After` 从 HTTP client 上报到 controller 调度器；
- per-link EWMA latency/congestion；
- 将计划中的 `cost.local_reads/remote_reads` 与运行时 limiter 合并；
- source cache policy 与 GPUDirect Storage 留到硬件阶段验证。
