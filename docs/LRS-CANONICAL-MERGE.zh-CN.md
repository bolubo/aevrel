# LRS Canonical Merge / Gap Matrix

状态：只读盘点（2026-10-22）；不改 A1–A3/A4、target-ready、GPU 实现

## 1. Worktree 盘点

| worktree | branch / base | 状态 | 独有内容 |
|---|---|---|---|
| `shardferry` | `main` @ `14a6909` | clean | canonical 产品树；0.1/0.2/0.3、target-ready、GPU codec 都已合入 |
| `shardferry-lrs` | `feature/lrs-m1` @ `c6f6b56` | 全部未提交/untracked | 我们的 LRS/RANK/A3.2；A1–A3/A4 会话的 layout/VPS/state/memory/rng/snapshot |
| `shardferry-resume-closure` | `feature/resume-closure` @ `9a3b996` | 5 个 modified + 4 个 untracked | canonical `state/` bundle、state CLI、L4 target-load/target-job |
| `shardferry-handoff` | `feature/0.3-online-handoff` @ `c6f6b56` | pushed/clean | 0.3 online handoff；内容已在 main |
| `shardferry-target-ready` | `feature/0.2-target-ready` @ `cdf84ae` | clean | 0.2 target-ready；已在 main |
| `shardferry-gpu` | `gpu-codec` @ `034200a` | clean | GPU codec/transport；已在 main |

祖先关系（已核）：

- `c6f6b56`、`9a3b996`、`cdf84ae`、`034200a` 都是 `main` 的祖先；
- `main..c6f6b56` 的 diff 为空：**`main` 与 LRS base 的树内容相同**；
- 所以 LRS 分支不需要 `git merge main`，它缺的只是未提交改动；
- resume-closure 基于更早的 `9a3b996`，L4 diff 需要 rebase/重新应用到 main。

## 2. 关键结论

1. **没有可直接 merge 的分支**：LRS 和 resume-closure 的核心工作都在工作区，
   不在 commit 里。
2. **canonical 产品树是 `main`**，不是任何 feature 分支。
3. **LRS 的集成必须在 main 上做 working-tree merge/patch**，不是在
   `feature/lrs-m1` 上继续堆。
4. `state/` 有两份：resume-closure 是 canonical，LRS 里是副本；合并时
   只能留 resume 那份。

## 3. 重复/冲突点

| 文件/目录 | 冲突 | 处理 |
|---|---|---|
| `src/shardferry/state/` | LRS 副本 vs resume canonical | 以 resume 为准；LRS 副本不迁移 |
| `docs/INDEX.zh-CN.md` | 两边都 modified | 手工合并 LRS + resume + A2/A3 行 |
| `src/shardferry/lrs/__init__.py` | 我们的 LRS 导出 vs A1–A3/A4 会话文件 | 以冻结快照为准，手工合并 `__all__` |
| `commands/target_load.py` | resume L4 vs main base | 只应用 L4 hunks，不整文件覆盖 |
| `target_job.py` | resume L4 vs main base | 同上 |
| `cli.py` / `test_cli.py` | resume state CLI + verify 误判修复 | 应用 resume diff；保留 main 后续改动 |
| `docs/LRS-*` | 我们的 M1–M6/RANK/INTEGRATION vs A1–A3/A4 文档 | 两边都保留，按主题合并 |

## 4. 合并顺序

1. **冻结 A1–A3/A4 会话**：拿到他们当前 `lrs/layout.py`/`model_vps.py`/
   `planning.py`/`state_closure.py`/`memory.py`/`rng_reshard.py`/
   `snapshot_ring.py` 和测试的明确快照；不要在编辑中合并。
2. 从 `main` @ `14a6909` 新建 integration worktree。
3. 先应用 **resume-closure** 的 working-tree diff：
   - `state/`（canonical）；
   - `cli.py` state 命令；
   - `target_load.py` / `target_job.py` L4；
   - `test_state_closure.py` / `test_target_state_l4.py` / `test_cli.py`；
   - `RESUME-CLOSURE` 文档；
   - 跑 resume L4 定向（26 passed / 1 skipped）。
4. 再应用 **我们的 LRS/RANK/A3.2** 文件：
   - `lrs/protocol.py`、`state.py`、`toy.py`、`sim.py`、`sampler.py`、
     `training_provider.py`、`transport.py`、`scenario.py`、`faults.py`、
     `tp_pp.py`；
   - `lrs/` 测试、scripts、docs、evidence；
   - **不**迁移 LRS 的 `state/` 副本；
   - `lrs/__init__.py` 与 A1–A3/A4 的导出手工合并。
5. 最后应用 **A1–A3/A4 冻结快照**（layout/VPS/state/memory/rng/snapshot +
   测试/docs），跑 A2/A3/A4 测试。
6. 合并 `docs/INDEX.zh-CN.md`；跑全仓。
7. 组合验收：A2 layout→M5、A3.1 TrainingState→L4、RANK 迁移、HTTP/netns；
   最后 4 卡/2 节点。

## 5. Gap Matrix

| 缺口 | 现状 | 归属 |
|---|---|---|
| A2 VPS → M5 完整 adapter | 只有 1 条 layout 一致性 test；M5 仍 hardcode | A2 会话 + 我们做验收 |
| A3.1 TrainingState → L4 | A3.2 provider + integration test 已有；target-load 内置 provider 不含 training.state | 我们 + target 训练代码 |
| 真实 DCP/FSDP target N-step | target-load 能 load DCP + restore state；真实 N-step 轨迹未跑 | target-ready/state 会话 |
| RANK → 真实训练 | LRS toy 已有 `RankSampler`/`ShardBlob.aux`；真实 DCP/FSDP sampler provider 未接 | 我们做接口，训练侧提供 provider |
| Go controller `stateMode` | Python target job 有 require_state；Go CRD/controller 未接 | target-ready 会话 |
| NCCL rank handoff / 4 卡 / 2 节点 | probe/TP-PP baseline 脚本已就绪；communicator rebuild、RoCE/IB 未做 | GPU 会话 + 我们做验收 |
| Canonical merge | 未做；关键改动都未提交 | 我们 |

## 6. 不重复边界

- **A1/A2/A3.1/A4**：A1–A3/A4 会话；我们只用其 API 做集成/验收。
- **RANK/A3.2/LRS**：我们；不再扩 A2/A4。
- **L4 target-load/target-job（Python）**：resume-closure/我们；Go/controller
  归 target-ready 会话。
- **GPU codec/transport/4 卡**：GPU 会话。
- **Canonical merge/组合验收**：我们。

## 7. 验收门槛

合并后的 integration worktree 必须同时满足：

- main 产品测试全过；
- resume L4 定向全过；
- A2/A3.1/A4 测试全过；
- 我们的 LRS/RANK/A3.2 测试全过；
- A2→M5、A3.1→L4 的跨层 test 全过；
- 4 卡/2 节点实机前，不声称端到端完成。
