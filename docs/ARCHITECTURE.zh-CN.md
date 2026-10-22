# 架构概览

## 控制面与数据面

```text
用户 / CI / shardferryctl
          |
   Kubernetes API
          |
  CheckpointMigration CR
          |
     Go controller
     /           \
source agents   target agents
     \           /
      range data plane
```

- controller 负责发现、plan、任务编排、commit barrier 和状态；
- source agent 只读 checkpoint，提供 inventory 和 range；
- target agent 在本地 root 写入 staging，校验后 commit；
- plan/report/spec 放 artifact store，不进入 checkpoint 数据面。

## 单机路径

```text
source DCP
  -> inspect metadata
  -> planner
  -> executor (range)
  -> staging + bitwise verify
  -> target DCP export
```

## Target DCP finalize

local-disk 场景中，target rank 不能共享一个全局目录。每个 target agent
在 commit 后把自己的本地 rank 文件 finalize 成：

```text
target-root/
  .metadata
  __rank_tensor.distcp
  w/rankN.bin
```

`.metadata` 和 `.distcp` 使该 rank 可以作为本地 DCP 目录读取；原始
`w/rankN.bin` 保留为校验证据。

## 核心不变量

- source 只读；
- plan 必须覆盖所有 target bytes；
- 未通过 bitwise 校验的数据不进入最终 target；
- 任意单 rank 失败都不能让整个迁移显示成功；
- 失败后的 work/journal 不能静默丢弃；
- source/work/output 目录不能互相重叠。
