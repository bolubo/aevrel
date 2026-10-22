# 尖刀 5：可验证迁移（M1）

状态：M1 完成，M2/M3 进行中
日期：2026-10-22  
前置：布局编译器、DCP runtime、CDP/cutover

## 1. 目标

把“迁移后 bitwise 验证”从“重新读完整 source + target”推进到：

```text
source leaf root + target leaf root + coverage root + plan hash
                         |
                  compact proof artifact
                         |
              target-only verification
```

第一版 proof 是可审计的 Merkleized manifest，不是零知识证明，也不声称
能在不读取 source 的情况下证明 reduction 的数学结果。它证明：

- proof 对应哪个 plan；
- target 文件/range 是否完整且未被改动；
- coverage 是否覆盖每个 target leaf；
- source leaf 是否与 proof 中的 root 一致（显式开启 source 验证时）。

## 2. 实现

- `src/shardferry/proof.py`
  - `build_proof(spec, plan, target_root, source=?, token=?)`
  - `verify_proof(proof, target_root, plan=?, source=?, token=?)`
  - canonical JSON + SHA-256 Merkle roots
  - target leaf / source leaf / coverage leaf
- `spec/proof.schema.json`
- CLI：
  - `shardferry proof-build`
  - `shardferry proof-verify`
- proof 中的 `plan_hash` 锚定 plan；传入 `plan` 时 verify 会校验。
- coverage 会检查每个 target leaf 恰好被引用一次，source ref 必须存在。

## 3. 清单

### M1：proof artifact（完成）

- [x] target/source/coverage Merkle roots
- [x] plan hash 锚定
- [x] proof schema
- [x] target-only verify
- [x] optional source verification
- [x] target corruption / source corruption tests
- [x] CLI build/verify

### M2：语义覆盖

- [ ] reduction 结果 witness（sum/avg/max/min 的 source-to-target 证明）
- [ ] partial/replicate 交叉证明
- [ ] 目标文件 size 与 plan 的独立对照
- [ ] proof 中的 target leaf 大小上限/分块策略参数化

### M3：规模与传递

- [ ] 128-rank / 1GB / 10GB proof build/verify
- [ ] proof size、verify IO、verify RSS 指标
- [ ] 将 proof 作为 report artifact 的可选附件
- [ ] K8s controller/agent 的 proof ref 生命周期

## 4. 验收

### A1 完整性

- target 任意 byte 损坏 -> verify 失败；
- target 文件缺失/短读 -> verify 失败；
- coverage 漏掉/重复 target leaf -> verify 失败；
- plan 与 proof 不匹配 -> verify 失败；
- source 损坏且开启 source verify -> verify 失败。

### A2 成本

- target-only verify 不读取 source；
- source verify 只读 proof 中引用的 source range；
- proof 大小随 leaf 数近似线性，单 leaf 不包含原始 checkpoint bytes；
- verify 内存不随 checkpoint 大小线性增长。

### A3 兼容

- 不改变现有 DCP、plan、report 的外部格式；
- proof 是 opt-in artifact；
- CLI/API 不依赖 controller 才能验证 target。

## 5. 非目标

- 不做 zk-SNARK/zk-STARK；
- 不把 trusted migration 进程的 reduction witness 变成不可信证明；
- 不替代现有 bitwise verify（M1 proof 是增量选项）。
