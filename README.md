# Backpack Battle Bench

Backpack Battle Bench 是一个用于评估大语言模型二维空间规划能力的基准项目。

模型需要在有限的背包格子中摆放、旋转和组合装备，并根据物品属性与效果争取更高得分。项目同时提供纯文字和视觉题面，可以自动调用 OpenAI / Anthropic 兼容接口、验证答案并生成报告。

主要功能：

- 纯文字与视觉空间规划测试
- L1–L5 难度阶梯题集
- 图形化拖放、旋转和即时计分
- 从前端或配置文件发起模型批跑
- 多 Run 并行、中断、恢复和单个零分 Job 重跑
- JSON、CSV、HTML 报告和独立排行榜站点

## 环境要求

- Python 3.11–3.13
- [uv](https://docs.astral.sh/uv/)

安装依赖：

```powershell
uv sync --frozen
```

## 使用图形化界面

推荐从 Web 界面开始：

```powershell
uv run bbbench web
```

浏览器会自动打开 `http://127.0.0.1:8000/`。如果不希望自动打开浏览器：

```powershell
uv run bbbench web --no-open
```

在页面中可以：

1. 浏览 `smoke-v1` 和 `ladder-v2` 的全部题目。
2. 拖动物品进行摆放，拖动时右键旋转。
3. 查看纯文字、视觉形状和完整视觉题面。
4. 填写 OpenAI 或 Anthropic 兼容 API 信息。
5. 发起 Run，实时查看进度、得分、耗时和输出 Token。
6. 中断、恢复、删除 Run，或单独重跑得分为 0 的 Job。
7. 打开 JSON、CSV 和 HTML 报告。

前端填写过的 API 配置可以保存在当前浏览器中，之后直接从历史记录选择。

## 使用配置文件批跑

### 1. 配置模型

编辑 `configs/models.example.yaml`，填写接口地址、模型名、协议和请求参数。API Key 通过环境变量提供：

```powershell
Copy-Item .env.example .env
```

然后在 `.env` 中填写对应的 Key。

### 2. 检查任务

单题冒烟测试：

```powershell
uv run bbbench run .\configs\run.example.yaml --dry-run
```

完整纯文字阶梯题集：

```powershell
uv run bbbench run .\configs\run.ladder-v2.yaml --dry-run
```

视觉阶梯题集：

```powershell
uv run bbbench run .\configs\run.visual-ladder.yaml --dry-run
```

### 3. 开始运行

移除 `--dry-run` 即可发送真实模型请求：

```powershell
uv run bbbench run .\configs\run.ladder-v2.yaml
```

使用运行输出中的 `RUN_ID` 查看报告：

```powershell
uv run bbbench report RUN_ID --format console
uv run bbbench report RUN_ID --format html
```

恢复被中断的运行：

```powershell
uv run bbbench run .\configs\run.ladder-v2.yaml --resume RUN_ID
```

## 内置题集

| 题集 | 内容 | 用途 |
|---|---|---|
| `smoke-v1` | 1 道简单题 | 检查安装、接口和运行流程 |
| `ladder-v2` | L1–L5 共 15 道题 | 正式比较模型的空间规划能力 |

`ladder-v2` 支持三种测试模式：

- `text`：完整纯文字题面
- `visual_shape`：通过图片识别背包和物品形状
- `visual_full`：通过图片识别形状、属性和效果

## 常用命令

```powershell
# 查看全部命令
uv run bbbench --help

# 校验题集
uv run bbbench suite validate .\suites\smoke-v1.yaml
uv run bbbench suite validate .\suites\ladder-v2.yaml

# 查看排行榜
uv run bbbench leaderboard ladder-v2 --format console

# 导出公开排行榜数据并构建静态站点
uv run bbbench site snapshot --database .\.bbbench\results.sqlite3
uv run bbbench site build --output .\.bbbench\pages
```

本地检查排行榜并预览构建结果：

```powershell
.\scripts\publish-leaderboard.ps1 -LocalOnly
```

更新公开排行榜并触发 GitHub Pages 部署：

```powershell
.\scripts\publish-leaderboard.ps1
```

## 开发与测试

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
```

更多题集说明见 [`docs/ladder-v2.md`](docs/ladder-v2.md)。

项目采用 MIT License。
