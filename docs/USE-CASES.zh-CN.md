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

## 场景 D：B 已有数据，只缺服务（长期品类）

**发生了什么**

目标集群已经有同一份数据或可访问的共享存储，但没有对应的训练、推理或
业务服务。

**迁移对象**

manifests、镜像、配置、调度约束、依赖、服务接线和 secret 引用。数据不搬。

**成功标准**

- B 中的服务能启动并连接到已有数据；
- 报告明确说明没有发生数据搬运；
- 不兼容的 CPU/GPU、依赖版本、权限或网络提前报告。

**当前状态**：长期品类能力，不属于 0.2；当前版本只做 DCP state 迁移。

## 场景 E：B 缺服务，也缺状态（长期品类）

**发生了什么**

目标集群既没有对应服务，也没有它依赖的数据或 checkpoint。

**迁移对象**

工作负载定义 + 状态。执行顺序通常是基础资源/配置 → 数据或 checkpoint →
服务启动 → 依赖检查 → 目标验证。

**成功标准**

- B 能独立运行目标工作负载；
- 数据可被目标服务正确读取；
- 任一步失败都有明确阶段、阻塞项和回滚/修复路径。

**当前状态**：长期品类能力，不属于 0.2。

## 场景 F：在线 cutover

**发生了什么**

训练不能停，source 持续产生新 checkpoint。

**当前状态**：CDP、follower、fencing、promote/rollback 和 target-ready
是 experimental 能力，不是当前稳定产品承诺。需要真实 target load 和
RTO/RPO 证据后才对外宣传。
