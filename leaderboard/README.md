# Standalone Leaderboard

该目录保存可公开提交的聚合排行榜快照。GitHub Pages 工作流会从当前题集、视觉包和
`results.json` 构建完全静态的站点；页面不依赖本地 FastAPI，也不会发送 API Key、模型输出或
原始请求。

更新公开成绩：

```powershell
.\scripts\publish-leaderboard.ps1
```

```bash
bash scripts/publish-leaderboard.sh
```

脚本会依次导出聚合快照、在本地完整构建一次静态站点、只提交
`leaderboard/results.json`、推送 `main` 并等待 GitHub Pages 完成。使用 `-NoWait`
可在推送后立即退出，使用 `-SkipBuild` 可跳过发布前的本地构建验证。

只在本地生成临时快照并完成静态构建校验，不修改公开快照、不提交也不推送：

```powershell
.\scripts\publish-leaderboard.ps1 -LocalOnly
```

```bash
bash scripts/publish-leaderboard.sh --local-only
```

只更新快照而不提交时，仍可直接运行：

```powershell
uv run bbbench site snapshot `
  --database .\.bbbench\results.sqlite3 `
  --output .\leaderboard\results.json
```

本地构建并预览：

```powershell
uv run bbbench site build --output .\.bbbench\pages
uv run python -m http.server 8080 --directory .\.bbbench\pages
```

打开 `http://127.0.0.1:8080/`。排行榜只展示与当前 `suite_hash` 完全一致的快照；每题不足
3 个 Trial 或未覆盖完整题集的结果会标为实验结果，不获得官方排名。
