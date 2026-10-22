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

## 发布前必须确认

- Helm 默认镜像可拉取；
- 双集群示例可执行；
- target load 有可运行示例；
- support bundle 能复现问题；
- 兼容/升级说明与镜像版本一致。
