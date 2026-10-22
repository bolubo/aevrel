# 发布检查清单

当前发布：`v0.1.1`。当前候选：`v0.2.0-rc.1`，只在 GPU/cross-machine
门槛通过后发布为正式版本。`v0.1.0` 保留为历史预览，不移动、不覆盖。

## 本地发布候选

```bash
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl
helm lint deploy/helm/shardferry
helm template shardferry deploy/helm/shardferry >/tmp/shardferry.yaml
uv build --wheel --out-dir dist
python scripts/check_release.py --json
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
6. 创建 `v0.2.0-rc.1` tag 和
   [`release notes`](RELEASE-NOTES-0.2.0-rc.1.md)；正式发布前保持 prerelease。
7. 挂载 Python wheel、验证附件和 release notes。
8. 确认 GHCR 的 controller/gateway/agent/live-worker 镜像与 Helm chart 可拉取。

发布前额外运行：

```bash
python scripts/check_release.py --json
```

它会检查 chart/appVersion、Helm 默认镜像、console scripts、必需的用户文档
和 release workflow，避免发布一个“代码能跑但用户装不上”的版本。

## 镜像

发布 tag 会触发 `.github/workflows/release.yml`，构建并推送
`ghcr.io/bolubo/shardferry-controller`、`shardferry-gateway`、
`shardferry-agent`、`shardferry-live-worker`、`shardferry-kueue-adapter`。
Helm values 必须指向本次 tag 对应的真实镜像；GHCR 包需要设置为 public，
否则用户安装会失败。

Python wheel 作为 GitHub Release asset 上传，不依赖 PyPI：

```bash
uv build --wheel --out-dir dist
gh release upload v0.2.0-rc.1 dist/shardferry-0.2.0rc1-py3-none-any.whl
gh release upload v0.2.0-rc.1 dist/shardferry-0.2.0-rc.1.tgz
gh release upload v0.2.0-rc.1 dist/shardferry-0.2.0-rc.1-evidence.tar.gz
```

## 未验证项

不要把 kind、物理多节点或 GPU 验证写成已完成；在得到证据前保留
“environment-dependent / pending”标记。0.1.1 的已验证范围以
[`GPU-VALIDATION-2026-10-22.md`](GPU-VALIDATION-2026-10-22.md) 和
[`LIVE-ACCEPTANCE.zh-CN.md`](LIVE-ACCEPTANCE.zh-CN.md) 为准。
