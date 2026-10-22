# Rescue Demo：中断后继续迁移

状态：本地演示通过（2026-10-22）
脚本：`scripts/rescue_demo.py`
测试：`tests/integration/test_rescue_demo.py`

## 1. 这个 demo 演示什么

用户已经有一份 local-disk DCP，迁移过程中进程被中断。下一次运行同一条
命令时，shardferry 不应该从零开始，而应该：

1. 保留 journal 和未完成 staging；
2. 跳过已经完成的 node；
3. 只搬剩余数据；
4. 重新校验并提交完整 target DCP；
5. 证明 source 没有变化、target 可以被 DCP 加载。

运行：

```bash
python scripts/rescue_demo.py --work-dir ./shardferry-rescue
```

输出：

- `rescue-proof.json`：journal 前后、重跑耗时、source digest、target load；
- `timeline.html`：中断、续跑、提交的可视化时间线。

## 2. 这不是 K8s 故障矩阵

当前脚本使用确定性的本地用户中断，不模拟 controller restart、target agent
死亡或跨节点网络断开。这些场景仍由 kind/e2e 故障矩阵负责；本 demo 只证明
用户最常遇到的“进程中断后重跑”路径。

## 3. 验收

- 第一次运行必须有 `in_progress` journal 和至少一个 completed node；
- 第二次运行日志中的 `resumes > 0`；
- target DCP load 状态为 `ok`；
- source SHA256 前后完全一致；
- 失败时保留 JSON/timeline，不把半状态报告成成功。
