# 代码结构约定

日期：2026-10-22
状态：Alpha 结构约定；不是一次性大重构

## 原则

当前阶段优先保证：

1. 改动能定位；
2. 包之间依赖方向清楚；
3. 新能力不再继续堆到 `src/shardferry/` 根部；
4. 不为了目录好看制造跨分支冲突。

结构目标不是“层级越深越专业”，而是让一个维护者能在十分钟内回答：
这个功能属于哪个包、它依赖谁、谁可以导入它。

## 当前结构判断

- Python 顶层模块较多，`cli.py`、`cdp.py`、`planner.py`、`dcp.py`、
  `agent.py` 已经超过适合继续膨胀的体量；
- `scripts/` 混合了开发、benchmark、验收和运维脚本；
- `docs/` 混合了产品、用户、设计、实验和排障文档；
- Go 的 `api/v1`、`cmd/*`、`internal/*` 边界目前相对健康，不需要现在拆。

这些是“后续变更成本”，不是当前 P0 的用户阻塞。

## 目标结构

只列方向，不要求一次性搬迁：

```text
src/shardferry/
  cli/                 # shardferry 命令分发
  commands/            # 安装后的用户命令
    target_load.py
    support_bundle.py
  dcp/                 # DCP metadata/range/write/inventory
  agent/               # agent HTTP、task state、executor
  planning/            # mesh/layout/planner/cost
  transport/           # file/http/router
  verification/        # verify/proof/target-ready
```

已开始的第一小步：`target_load` 和 `support_bundle` 已放入
`src/shardferry/commands/`。

第二步已完成：`dcp.py`、`dcp_range.py`、`dcp_local.py`、`dcp_provider.py`
已收进 `src/shardferry/dcp/`；旧导入路径保留兼容 shim。

第三步已完成：`agent.py`、`agent_state.py`、`agent_executor.py` 已收进
`src/shardferry/agent/`；`agent_state`/`agent_executor` 旧路径保留兼容 shim。

第四步已开始：`planner.py` 已收进 `src/shardferry/planning/`；`layout`/
`mesh`/`tensor_io` 暂留原路径，等 planner 稳定后再迁。

第五步已完成：`verify.py`、`proof.py`、`target_ready.py` 已收进
`src/shardferry/verification/`；旧路径保留兼容 shim。

## 迁移规则

- 一次只搬一个包，行为不变；
- 先加兼容 shim，再改调用方，最后删除旧路径；
- 每次搬迁前后跑相关单测和 `product_acceptance`；
- 不把控制面、planner 和数据面同时搬；
- 不在正在滚动的 live/TLS 分支上做大搬迁；
- 新模块优先放入已有子包，不新增根目录平铺模块；
- 旧模块超过约 800 行时，下一次改动前先说明它承担了几个变化原因。

## 依赖方向

```text
cli / commands
      |
planning / verification / transport
      |
dcp / agent / storage
      |
contracts / manifest / identity
```

- 上层可以依赖下层；
- 下层不能反向导入 CLI 或用户命令；
- `commands/` 只做用户入口和编排，不承载核心算法；
- 核心迁移库不依赖 `scripts/` 或 tests。

## scripts 与 docs

暂时保留平铺，避免破坏已有链接和 CI。脚本数量继续增长后再分：

```text
scripts/dev/
scripts/acceptance/
scripts/bench/
scripts/ops/
```

`docs/` 同理，等发布链接稳定后再考虑 `product/`、`user/`、`design/`、
`ops/` 子目录。

## 不做

- 不为了“像人类项目”重排所有目录；
- 不把每个函数拆成独立包；
- 不在没有测试保护时移动 `cli.py`、`dcp.py`、`planner.py`；
- 不移动 Go `internal/controller` 直到文件职责或测试边界真正变复杂。
