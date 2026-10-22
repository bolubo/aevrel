# 尖刀 2：零物化 DCP range runtime

状态：M1/M2/M3/M4b 已完成（仍有固定 Python/torch 进程开销，不按 shard 线性增长）  
日期：2026-10-22  
前置：尖刀 1 的布局编译器

## 1. 一句话目标

让 source agent 以 **O(1) 额外内存** 读取 DCP `.distcp` 的任意 tensor byte
range，不通过 `torch.load` 反序列化整个 chunk，也不把整个 chunk 放进内存。

## 2. 关键事实

PyTorch DCP 的 `.distcp` 是一个 ZIP archive，导出的 raw tensor 位于：

```text
<chunk-prefix>/data/0
```

当前实现的问题：

- `_read_storage_tensor` 读取整个 ZIP entry 后 `torch.load`；
- `_tensor_bytes` 又把 tensor 转成完整 bytes；
- `inventory_dcp_descriptors` 对每个本地 chunk 做上述两步；
- `AgentApp.read_shard` 先取完整 chunk，再切 range。

## 3. 实施清单

### M1：ZIP range descriptor（已完成）

- [x] `inspect_zip_tensor` 解析 ZIP central directory + local header
- [x] 支持 stored `data/*` entry 的 `pread`
- [x] 流式 checksum，不加载完整 chunk
- [x] `DcpChunkRef` 保存 `data_offset/data_size/compression/name`
- [x] `inventory_dcp_descriptors` 不再反序列化 tensor
- [x] 新增 monkeypatch 回归：禁止 `_read_storage_tensor` 仍可 inventory

### M2：Agent range path（已完成 2026-10-22）

- [x] `read_dcp_chunk_range`
- [x] `AgentApp.read_shard` 对 DCP 直接 range read，绕过 full chunk cache
- [x] 小 cache（1 byte）仍可读单字节 range
- [x] range-level `X-Range-SHA256` 证据
- [x] 多 chunk/rank 的 HTTP source smoke
- [x] 压缩 ZIP fallback 的明确语义（stored 零物化，deflate 走 zipfile fallback）

### M3：规模与观测（已完成 2026-10-22）

- [x] 1GB chunk range smoke，记录 RSS
- [x] 10GB/多 chunk range smoke
- [x] per-chunk 读取字节/请求数指标（`/v1/metrics`）
- [x] source agent cache 只用于非 DCP legacy 路径
- [x] 失败注入：offset 越界、ZIP 损坏（短读/checksum 变化继续补）
- [x] 短读、checksum 变化故障注入

基准（sparse stored Zip64，range=16MB）：

```text
1GB  : checksum 0.28s, range 8ms,  Python peak 16.9MB, RSS 42MB
10GB : checksum 3.05s, range 8.6ms, Python peak 16.9MB, RSS 42.9MB
```

### M4：DCP 写侧（M4b 完成 2026-10-22）

- [x] `export_dcp` 不再 `read_bytes()` 整个 target shard
- [x] 自定义 ZIP writer：手写 `data.pkl` + stored `data/0`
- [x] 保持现有 DCP metadata/zip 兼容（4→3 回归通过）
- [ ] 继续降低固定 Python/torch 进程开销（不属于 shard 线性复制）

1GB/64MB export：Python peak 约 111–120MB，RSS 约 382–391MB；两者同量级，
说明不再按 shard 大小线性分配；剩余部分是进程/torch 固定开销。

## 4. 验收条件

### A1 语义正确

- range read 的字节与完整 chunk 的对应切片 bitwise 一致；
- `inventory` 的 checksum 与完整 chunk checksum 一致；
- DCP 4→3 完整迁移仍通过；
- 多 chunk/rank 仍能通过 plan/executor/verify。

### A2 零物化证据

- monkeypatch `_read_storage_tensor` 为异常时，inventory 和 range read 仍工作；
- `max_cache_bytes=1` 时，DCP range read 成功；
- 单 chunk 大小增加时，inventory 的额外 Python bytes 不随 chunk size 增长；
- 读取 1 字节 range 不产生完整 chunk bytes 对象。

### A3 兼容与性能

- 现有 DCP tests、HTTP source tests、agent inventory tests 全绿；
- stored ZIP 走 fast path；compressed ZIP 显式 fallback 或拒绝；
- range 读写有清晰错误码：越界、短读、ZIP 损坏、checksum 变化；
- 不改变 DCP 文件和 manifest 的外部格式。

## 5. 非目标

- 本刀不改布局编译器；
- 不改 controller 提交协议；
- 不承诺所有 ZIP compression 方法都能零物化；
- 不把 `torch` 从环境里移除，metadata/dtype 仍可使用 PyTorch。
