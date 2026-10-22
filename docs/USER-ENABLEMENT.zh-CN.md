# 用户交付与排障入口

这组文档面向“拿到发布物的平台工程师”，不是内部开发流程。

## 迁移前

- [迁移品类](CATEGORY.zh-CN.md)
- [定位](POSITIONING.zh-CN.md)
- [兼容性矩阵](COMPATIBILITY.zh-CN.md)

## 迁移后

- [目标 DCP 加载](TARGET-LOAD.zh-CN.md)
- [排障与 support bundle](SUPPORT.zh-CN.md)
- [错误目录](ERROR-CATALOG.zh-CN.md)

## GPU 环境

- [GPU/FSDP 实验卡](GPU-EXPERIMENT.zh-CN.md)
- 一次性验收：`scripts/gpu_acceptance.py`

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
