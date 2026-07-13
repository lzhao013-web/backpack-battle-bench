# Standalone Leaderboard

该目录保存可公开提交的聚合排行榜数据。新成绩按 Run 独立保存在
`runs/<run_id>.json`；GitHub Pages 工作流会把所有 Run 与只读的历史基线 `results.json`
统一聚合，再结合当前题集和视觉包构建完全静态的站点。页面不依赖本地 FastAPI，也不会发送
API Key、模型输出或原始请求。

更新公开成绩：

```powershell
.\scripts\publish-leaderboard.ps1
```

```bash
bash scripts/publish-leaderboard.sh
```

脚本会依次导出独立 Run 文件、聚合并在本地完整构建一次静态站点，只提交新增或变化的
`leaderboard/runs/*.json`，同步并推送 `main`，最后等待 GitHub Pages 完成。多个发布端并发时，
脚本会 rebase 合并其他机器的 Run 后重试推送。使用 `-NoWait` 可在推送后立即退出，使用
`-SkipBuild` 可跳过发布前的本地构建验证。

只在本地生成临时 Run 数据和聚合快照并完成静态构建校验，不修改公开数据、不提交也不推送：

```powershell
.\scripts\publish-leaderboard.ps1 -LocalOnly
```

```bash
bash scripts/publish-leaderboard.sh --local-only
```

只导出和聚合而不提交时，仍可直接运行：

```powershell
uv run bbbench site export-runs --database .\.bbbench\results.sqlite3
uv run bbbench site aggregate --output .\.bbbench\leaderboard-results.json
```

本地构建并预览：

```powershell
uv run bbbench site build `
  --snapshot .\.bbbench\leaderboard-results.json `
  --output .\.bbbench\pages
uv run python -m http.server 8080 --directory .\.bbbench\pages
```

打开 `http://127.0.0.1:8080/`。排行榜只展示与当前 `suite_hash` 完全一致的快照；每题不足
3 个 Trial 或未覆盖完整题集的结果会标为实验结果，不获得官方排名。
