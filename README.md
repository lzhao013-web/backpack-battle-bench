# Backpack Battle Bench

Backpack Battle Bench 是一个用于评估大语言模型二维空间规划能力的基准项目。

模型需要在有限的背包格子中摆放、旋转和组合装备，并根据物品属性与效果争取更高得分。项目同时提供纯文字和视觉题面，可以自动调用 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 兼容接口，验证答案并生成报告。

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

编辑 `configs/models.example.yaml`，填写接口地址、模型名、协议和请求参数。`protocol` 可选 `openai_chat`、`openai_responses` 或 `anthropic_messages`。API Key 通过环境变量提供：

```powershell
Copy-Item .env.example .env
```

然后在 `.env` 中填写对应的 Key。

OpenAI Responses API 配置示例：

```yaml
protocol: openai_responses
base_url: https://api.openai.com/v1
model: gpt-5
api_key_env: OPENAI_COMPATIBLE_API_KEY
```

如需让某个模型的请求走代理，可在模型配置或 Web 高级参数中设置 `proxy_url`：

```yaml
proxy_url: http://127.0.0.1:7890
# 也支持 socks5://127.0.0.1:1080 和 socks5h://127.0.0.1:1080
```

未设置 `proxy_url` 时，HTTPX 仍会读取标准的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY` 环境变量。为避免代理凭据写入运行记录，`proxy_url` 不允许内嵌用户名或密码；有鉴权的代理请通过标准代理环境变量配置。

视觉题面较大时，可在模型的 `params` 中开启图片切分：

```yaml
params:
  split_image: true
```

开启后，请求会先发送一张不超过 640000 像素的低分辨率完整总览图，再发送均等切分、每张不超过 640000 像素的高清分片。分片按从左到右、从上到下的顺序排列；请求提示词会要求模型先通过总览确认整体位置，再组合全部分片查看，并特别核对跨越分片边界的物品形状和灰色叉格。Web 界面也可通过“切分大图”选项开启。

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

# 导出独立 Run 数据、聚合并构建静态站点
uv run bbbench site export-runs --database .\.bbbench\results.sqlite3
uv run bbbench site aggregate --output .\.bbbench\leaderboard-results.json
uv run bbbench site build --snapshot .\.bbbench\leaderboard-results.json --output .\.bbbench\pages
```

本地检查排行榜并预览构建结果：

```powershell
.\scripts\publish-leaderboard.ps1 -LocalOnly
```

```bash
bash scripts/publish-leaderboard.sh --local-only
```

更新公开排行榜并触发 GitHub Pages 部署：

```powershell
.\scripts\publish-leaderboard.ps1
```

```bash
bash scripts/publish-leaderboard.sh
```

发布时每个完成的 Run 会写入独立的 `leaderboard/runs/<run_id>.json`。不同机器发布的文件会在
Git 同步后由 GitHub Actions 统一聚合，不会再用某一台机器的本地数据库覆盖整个排行榜。

## 开发与测试

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
```

更多题集说明见 [`docs/ladder-v2.md`](docs/ladder-v2.md)。

项目采用 MIT License。
