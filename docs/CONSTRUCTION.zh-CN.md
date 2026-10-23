# Aevrel 项目建设方向

状态：Aevrel 0.3.0 当前建设阶段。这里描述工作方向和完成标准，不承诺日期或版本内容。

## 为什么不按版本承诺

项目的时间、人力和真实环境都取决于 owner 的投入，过早承诺“下个版本一定有什么”
会让路线图变成空话。当前更重要的是把项目基础建设做好：

1. UI/UX；
2. 必要测试覆盖；
3. 代码和语言边界重构。

这三项完成后再讨论下一阶段的产品能力，否则新功能只会继续堆在不可维护的基础上。

## 1. UI / UX

### 目标

让一个第一次接触项目的人，在没有读完所有文档的情况下，知道：

- 当前控制面在看什么；
- 哪些状态是真的、哪些还没验证；
- 下一步能做什么；
- 点一个动作会发生什么、需要什么、失败会怎样。

### 页面结构

- 中间：控制面主视图，显示 fleet、job、checkpoint、readiness 和证据；
- 左侧：传输，static 和 live 两种模式都直接显示；
- 右侧：上下游关系，显示 Kueue、Kubernetes、存储、训练运行时、target load 等；
- 底部：当前对象的说明、参数解释、前置条件、成功标准和失败路径；
- Query：按 job、checkpoint、版本、状态和证据查询。

### 交互要求

- Demo 和真实使用分开，但都在同一套页面里；
- 参数不藏在多层弹窗里；空间够就放在旁边；
- 每个实验能力明确标注实验性质和当前验证边界；
- 任何按钮都必须有可用路径，不能出现“看起来能点、实际没有实现”；
- UI 只消费控制面 API/状态，不从日志猜测状态。

### 完成标准

- 新用户可用 Demo 模式跑通一次完整控制面故事；
- 真实 checkpoint/集群模式能完成 inventory、readiness、drill 和迁移入口；
- static/live 两种传输都有 Demo 和真实路径；
- 所有公开参数有就地说明；
- 断网、缺依赖、缺权限、缺资源时有明确下一步。

## 2. 测试覆盖

### 目标

不是追求一个漂亮的全局覆盖率数字，而是保证关键语义、失败边界和用户路径不被
回归破坏。

### 测试层次

| 层次 | 必须覆盖 |
|---|---|
| Unit | planner、chunk、manifest、proof、状态机、错误分类 |
| Contract | JSON schema、CRD、CLI 参数、HTTP 接口、跨语言字段 |
| Integration | demo、readiness、restore drill、migrate、target load |
| Failure | source 只读、半提交、重试、断连、缺失 rank、错误报告 |
| Kubernetes | controller/agent/Helm 渲染和受控 fake client 流程 |
| Opt-in environment | 真实 kind、GPU、multi-node；未跑必须明确跳过 |

### 完成标准

- 每个公开 CLI/`aevrelctl` 命令至少有成功和失败测试；
- source 只读、fail-closed、commit 边界、drill 版本绑定有回归测试；
- 用户主路径在无 GPU、无集群环境可重复；
- 真实环境测试明确标记为 opt-in，不伪造通过；
- 新功能必须先说明测试缺口，不能只写 demo。

## 3. 语言与代码边界重构

### 目标

先冻结行为，再小步重构。不是“全部换语言”，而是把每层放到正确的工具里。

### 语言边界

| 层 | 默认语言 | 说明 |
|---|---|---|
| Kubernetes/control plane | Go | controller、CRD、状态机、policy、ctl、gateway |
| PyTorch/DCP/runtime | Python | 训练运行时、DCP、drill、state bundle、data-plane adapter |
| 热路径/native 加速 | Rust | 只有基准证明 Python/Go 不够时才引入 |
| ABI/底层绑定 | C | 只在必须对接系统/GPU/既有 ABI 时使用 |

### 重构原则

- 一次只改一个边界，不同时改协议、目录和行为；
- 控制面状态只有一个权威来源；
- adapter 不能反向污染核心状态机；
- 大文件按职责拆分，不按“AI 生成方便”拆分；
- 先补测试，再移动代码；
- 不为了“看起来高级”引入 Rust/C。

### 完成标准

- Go/Python/Rust/C 的职责边界有文档且与依赖方向一致；
- 核心状态机没有重复实现；
- 目录、包和 CLI 入口符合职责；
- 重构前后测试、CLI 输出和安全语义保持一致；
- 不出现大爆炸式搬家。

## 当前推进顺序

1. UI/UX 与测试覆盖可以并行；
2. 重构必须建立在测试安全网之上，按小切片推进；
3. 三条线都完成前，不把新 provider、新调度器或生产化 LRS 当作主线。

UI 由独立工作流推进；控制面、测试和重构工作不应把未完成的 UI 代码当作稳定契约。

## 后续产品方向

三条建设线完成后，再讨论真正的产品扩展：

- 更多 provider 和 scheduler adapter；
- 跨控制域状态移动；
- LRS 的 GPU/跨机验证；
- 嵌入式控制面和 1.0 契约。

这些是方向，不是版本承诺；见 [公开路线图](ROADMAP.zh-CN.md)。
