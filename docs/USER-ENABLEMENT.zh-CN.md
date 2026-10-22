# 用户交付与排障入口

这组文档面向“拿到发布物的平台工程师”，不是内部开发流程。

## 迁移前

- [定位](POSITIONING.zh-CN.md)
- [兼容性矩阵](COMPATIBILITY.zh-CN.md)

## 迁移后

- [目标 DCP 加载](TARGET-LOAD.zh-CN.md)
- [排障与 support bundle](SUPPORT.zh-CN.md)
- [错误目录](ERROR-CATALOG.zh-CN.md)
- [升级与版本偏差](UPGRADE.zh-CN.md)
- [跨集群 live acceptance](LIVE-ACCEPTANCE.zh-CN.md)
- [Rescue Demo：中断后续跑](RESCUE-DEMO.zh-CN.md)

## GPU 环境

- 预检：`scripts/gpu_preflight.py`
- GPU 原始实验记录保留在私有验证目录，不进入公开文档树。

## 用户最短路径

```text
安装 -> preflight -> migrate --wait -> describe
     -> target_load_smoke -> 真实训练 load
     -> 失败则 support_bundle + ERROR-CATALOG
```

仓库内可以用下面的命令做一次本地/发布候选验收：

```bash
python scripts/product_acceptance.py --work-dir /tmp/shardferry-product
```

它覆盖 demo、真实 DCP 4→3、target load、release consistency 和 support
命令入口；跨集群 E2E 仍以 `examples/cross-cluster/` 为准。

## 发布前必须确认

- Helm 默认镜像可拉取；
- 双集群示例可执行；
- target load 有可运行示例；
- support bundle 能复现问题；
- 兼容/升级说明与镜像版本一致。
