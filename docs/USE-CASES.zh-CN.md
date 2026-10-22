# 典型使用场景

## 场景 A：单机或共享盘上的 DCP 迁移

**发生了什么**

训练任务留下了完整的 DCP 目录，但目标环境只需要更少的 rank。

**用户操作**

```bash
shardferry doctor --checkpoint /data/job/checkpoint
shardferry migrate \
  --checkpoint /data/job/checkpoint \
  --target-world 3 \
  --output /data/job/checkpoint-3rank
```

**成功标准**

- source 不变；
- 目标 DCP 可加载；
- bitwise verification 通过；
- 中断后可以重跑。

**当前状态**：Alpha 路径，真实 DCP 测试通过。

## 场景 B：Kubernetes local-disk 128→64

**发生了什么**

128 个 rank 的 shard 分散在节点本地盘；没有共享 checkpoint 目录；目标使用
64 个 rank。

**平台侧一次性准备**

- 部署 controller、source/target agents；
- 准备 token 和可选 mTLS；
- source 节点挂载本地 checkpoint；
- source `.metadata` 在所有 source 节点可见；
- target 节点挂载 `/targets`。

**用户操作**

```bash
shardferryctl preflight -f migration.yaml
shardferryctl migrate -f migration.yaml --wait
```

**成功标准**

- selector 发现完整 source rank 覆盖；
- 每个 target rank 完成 staging 和 bitwise；
- 全部 rank 成功后才 commit；
- 每个 target rank 有本地 DCP 目录；
- 失败不会误报成功。

**当前状态**：代码闭环已具备，真实多节点和 GPU 验收待补。

## 场景 C：灾难恢复 / 集群替换

**发生了什么**

源训练集群不可用，但源本地盘仍可读，或者可以短暂启动 source agents。

**价值**

不需要先把全部 checkpoint 汇总到对象存储；只需要保持 source 可读，按目标
拓扑搬运必要 ranges。

**当前状态**：单机路径可用；跨集群 selector 需要环境验证。

## 场景 D：在线 cutover

**发生了什么**

训练不能停，source 持续产生新 checkpoint。

**当前状态**：CDP、follower、fencing、promote/rollback 和 target-ready
是 experimental 能力，不是当前稳定产品承诺。需要真实 target load 和
RTO/RPO 证据后才对外宣传。
