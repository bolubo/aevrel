# 尖刀 1：分布式布局编译器

状态：M1/M2/M3a-d 已完成；请求级 1.2x 下界移交第二把数据面成本模型  
日期：2026-10-22  
范围：只做 planner/compiler，不碰 DCP 零物化数据面、在线迁移和 K8s 控制器。

## 1. 一句话目标

把 source/target 的 DeviceMesh、placement 和本地 shard 布局，编译成
**字节覆盖可证明、请求数可解释、无全局 gather** 的 dataflow plan。

第一版不追求所有代价维度都最优，但必须做到：

- 每个 target byte 恰好被覆盖一次；
- 有独立于实际计划的理论下界；
- 能识别“一个 source 覆盖整个 target region”，不再被无关 source 边界切碎；
- plan 中的成本可解释：bytes、range reads、max fan-in、nodes；
- 现有 executor/plan schema 不需要破坏性升级。

## 2. 设计

### 2.1 输入

- `checkpoint.tensors`：tensor 名、shape、dtype；
- `source.mesh`、`source.shards`：source rank、region、checksum；
- `target.mesh`：target world size、shape、placements；
- 后续可选：network/disk cost hints。

### 2.2 编译器 pass

1. **Catalog pass**：规范化 source shard，保留 rank/region/checksum；
2. **Relevance pass**：只保留与 target region 相交的 source，禁止无关 shard
   参与边界切分；
3. **Full-cover fast path**：如果一个 source 完整覆盖 target region，直接生成
   一个 `copy` 节点；
4. **Partial-group fast path**：如果完整 Partial 组覆盖 target region，直接生成
   一个 `sum/avg` 节点；
5. **Boundary fallback**：否则按 source 边界切分，保证精确覆盖；
6. **Lower-bound pass**：独立计算理论最小 bytes，不能直接把实际 bytes 复制过去；
7. **Cost pass**：统计 `range_reads`、`max_fan_in`、`nodes`；
8. **Validation pass**：schema、覆盖、Partial 组、checksum、下界关系全部校验。

### 2.3 成本定义

```json
{
  "cost": {
    "bytes": 128,
    "range_reads": 8,
    "max_fan_in": 1,
    "nodes": 8
  }
}
```

- `bytes`：所有节点的输入字节数之和；
- `range_reads`：节点输入数之和的第一版近似值；
- `max_fan_in`：单节点最大输入数；
- `nodes`：plan 节点数。

后续版本会把 `range_reads` 升级为真实 coalesced range 数，并加入
`source_rank_affinity`、`target_disk_pressure` 和 `network_hop`。

### 2.4 下界

`theoretical_min_bytes` 独立计算，不读取实际节点：

- 普通 copy：target region 字节数；
- source Partial(n 路)：target region 字节数 × n；
- target `Partial(sum)` 的非 owner：0；
- target `Partial(avg)`：target region 字节数 × source Partial 路数；
- Replicate：按“一个副本足够”计，不把副本数乘进去。

第一版不承诺所有布局都达到下界，但承诺：

```text
theoretical_min_bytes <= total_bytes
```

并且对 full-cover case，节点数和字节数都应达到该布局的最小值。

## 3. 实施清单

### M1：编译器 v0（已完成 2026-10-22）

- [x] C1 相关 source 过滤
- [x] C2 full-cover copy fast path
- [x] C3 full-cover Partial group fast path
- [x] C4 独立 `theoretical_min_bytes`
- [x] C5 plan `cost` 字段与 schema
- [x] C6 现有 plan ID、legacy plan ID、schema 兼容
- [x] C7 现有 property/oracle/mutation/128-rank 测试全绿
- [x] C8 新增“无关 source 边界不应放大节点”的回归
- [x] C9 新增 full-cover/partial fast path 回归
- [x] C10 `WORKLOG.md` 与线卡沉淀

### M2：多 chunk/rank（已完成 2026-10-22）

- [x] 支持同一 `(tensor, rank)` 多个 source chunk
- [x] plan input 增加稳定的 `source_id`（tensor/rank/offset/size/checksum）
- [x] executor、oracle、verify、Go agent client 同步
- [x] 多个 chunk 的 exact-cover 与 checksum 校验
- [x] Partial 源多 chunk 时每个 source rank 选择一个覆盖 chunk 后组成完整组

### M3：代价优化

- [x] 真实 coalesced range 计数（executor 同款 4MB coalescing）
- [x] source/target 亲和性可观测（`local_reads` / `remote_reads`）
- [x] range-count-aware source selection（本地优先，再比 coalesced ranges）
- [x] adjacent same-source node coalescing（任意轴，source/target 双连续）
- [~] 全局 fan-in/请求数加权优化（移交第二把数据面）
- [~] 请求级 1.2x 上界（移交第二把数据面）

第一把 A1–A5 已通过当前门禁。请求级上界不再单独作为布局编译器的硬 gate：
它依赖真实 range 读取、压缩方式和存储介质，放到第二把的 runtime cost model。

## 4. 验收条件

### A1 正确性

- 每个 target region 的每个 byte 恰好覆盖一次；
- source checksum 与 plan input 一致；
- Partial 组完整；
- property test 与 independent oracle 逐字节一致；
- `full_gather=false`。

### A2 编译质量

- 构造“1 个 full source + 128 个无关/局部 source”的 case：
  - 旧 planner 不应成为基准；
  - 新 planner 的节点数不超过 target region 数的 2 倍；
- full-cover copy case 每个 target region 只生成 1 个节点；
- full-cover Partial case 每个 target owner region 只生成 1 个 reduce 节点；
- `total_bytes` 与独立下界的关系可解释。

### A3 规模与确定性

- 128 source → 64/256 target 仍无 full gather；
- 同一 spec 重复编译结果完全一致；
- 迁移目录/endpoint 变化不影响 migration ID；
- 旧 `shardferry-plan-v2` ID 仍可校验通过。

### A4 兼容性

- 现有 `plan.schema.json` 校验通过；
- 现有 CLI、agent `/v1/plan`、executor、verify 不需要改调用方；
- Python 默认测试全绿。

### A5 可观测性

- plan 顶层出现 `cost`；
- `cost.nodes == len(nodes)`；
- `cost.bytes == total_bytes`；
- `cost.max_fan_in` 等于所有节点输入数的最大值；
- `theoretical_min_bytes <= total_bytes`。

## 5. 非目标

- 不做 DCP 零物化读取；
- 不做在线增量迁移；
- 不做 GPU/网络实测；
- 不修改控制器提交协议；
- 不为了“更优”牺牲 executor 已有的确定性输出。
