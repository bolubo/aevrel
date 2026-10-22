# 兼容性矩阵

## 当前支持

| 维度 | 0.2.0-rc.1 候选支持 |
|---|---|
| Checkpoint 格式 | PyTorch DCP |
| Source 拓扑 | 1D/2D DeviceMesh，Shard/Replicate/Partial |
| Target 拓扑 | world size 和 placement 可变化 |
| 数据位置 | 单机文件、共享文件系统、HTTP source agent |
| 执行后端 | CPU/PyTorch DCP；4×L20 单节点 GPU DCP load 已验证；跨主机 GPU 待验证 |
| Kubernetes | controller + source/target agents；同集群 selector 和跨集群 `ShardferryCluster`/gateway |
| 验证 | bitwise target verification、rank-local DCP load smoke、target-ready Indexed Job、state bundle |
| 进度/恢复 | staging、journal、rank task、resume、commit repair、target-ready 失败关闭 |
| 跨集群证据 | 两套 kind、16→8、128→64、1GiB 控制面/数据面闭环 |
| 鉴权 | per-cluster token + end-to-end mTLS 已实现；生产多租户隔离未完成 |

## 暂不支持或未验证

- 任意非 DCP checkpoint 格式；
- 没有统一 `.metadata` 的 custom shard 格式；
- S3 作为 source checkpoint 的直接全量迁移；
- 真实 128 节点、10GiB local-disk 的 Kubernetes 验收；
- 跨主机 GPU 和真实 4-GPU NCCL/live cutover；
- 任意 TP/PP 拓扑的完整自动转换；
- LRS 的生产 SLA；
- 多租户隔离的完整产品化。

## 选择建议

- 共享盘 + DCP：优先原生 DCP；
- local-disk + 多节点 + 不同拓扑：考虑 shardferry；
- 只复制字节：rsync/rclone 更合适；
- 进程/GPU 现场恢复：CRIU/cuda-checkpoint 更合适。
