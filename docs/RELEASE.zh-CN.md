# 发布检查清单

## 本地发布候选

```bash
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl
helm lint deploy/helm/shardferry
helm template shardferry deploy/helm/shardferry >/tmp/shardferry.yaml
uv build --wheel --out-dir dist
```

真实 DCP 验证至少应覆盖：

- 4→3 迁移并加载；
- target rank 本地 DCP finalize/load smoke；
- 失败后重跑或 commit repair；
- source 文件树前后 SHA256 不变。

## GitHub

1. 确认仓库定位和 README 的 experimental/production 表述。
2. 配置 remote，确认默认分支。
3. 推送发布候选提交。
4. 等 CI 的 Python、Go、Helm 三个 job 全绿。
5. 在仓库设置中启用 Secret scanning 和 private vulnerability reporting。
6. 创建 `v0.1.0` tag 和 release notes。

## 镜像

发布镜像前固定 tag 和 digest；Helm values 不应指向不存在的
`ghcr.io/shardferry/...:0.1.0`。在镜像 workflow 成功前，README 应明确说明
“需要本地构建镜像”。

## 未验证项

不要把 kind、物理多节点或 GPU 验证写成已完成；在得到证据前保留
“environment-dependent / pending”标记。
