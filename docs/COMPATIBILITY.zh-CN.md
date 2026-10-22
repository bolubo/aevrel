# 兼容性矩阵

## 当前支持

| 维度 | Alpha 支持 |
|---|---|
| Checkpoint 格式 | PyTorch DCP |
| Source 拓扑 | 1D/2D DeviceMesh，Shard/Replicate/Partial |
| Target 拓扑 | world size 和 placement 可变化 |
| 数据位置 | 单机文件、共享文件系统、HTTP source agent |
| 执行后端 | CPU/PyTorch CPU DCP；GPU 真实训练待验证 |
| Kubernetes | controller + source/target agents + selector 发现 |
| 验证 | bitwise target verification、rank-local DCP load smoke |
| 进度/恢复 | staging、journal、rank task、resume |

## 暂不支持或未验证

- 任意非 DCP checkpoint 格式；
- 没有统一 `.metadata` 的 custom shard 格式；
- S3 作为 source checkpoint 的直接全量迁移；
- 真实 128 节点、1GB/10GB local-disk 的 Kubernetes 验收；
- 真实 GPU/FSDP target load 的完整矩阵；
- 在线 cutover 的生产 SLA；
- 多租户隔离的完整产品化。

## 选择建议

- 共享盘 + DCP：优先原生 DCP；
- local-disk + 多节点 + 不同拓扑：考虑 shardferry；
- 只复制字节：rsync/rclone 更合适；
- 进程/GPU 现场恢复：CRIU/cuda-checkpoint 更合适。
