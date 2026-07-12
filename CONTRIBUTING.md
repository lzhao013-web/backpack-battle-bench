# Contributing

## 开发环境

```powershell
uv sync --frozen
uv run pytest
```

提交前执行：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run bbbench suite validate suites/smoke-v1.yaml
uv run bbbench suite validate suites/ladder-v2.yaml
uv build
```

## 修改语义

- 几何、效果、目标函数、提示词或 canonical hash 的修改都视为语义变更。
- 语义变更必须更新对应版本、提示快照、场景哈希、Oracle 和 suite manifest。
- 修改共享物品时还必须更新目录哈希、引用场景、视觉包覆盖和视觉包哈希。
- 视觉包使用 PNG 文件哈希冻结；重新渲染后必须更新 suite 的视觉包哈希和 suite hash。
- 不得只修改题面而不修改验证逻辑，或反过来；提示词必须继续由场景结构渲染。
- 新插件应使用新的稳定规则 ID，并提供配置校验、中文渲染和确定性测试。
- Oracle 上界只能高估，不能低估；无法证明安全时返回 `None` 或不实现该能力。

## 新增正式题

1. 将可复用物品加入 suite 的共享目录，并使用 `inventory` 在场景中引用。
2. 使用 `bbbench scenario validate` 校验题目。
3. 使用 `bbbench oracle solve --timeout 60` 得到精确 Oracle。
4. 确认 `exact=true`、`timed_out=false`、最优值大于 0，且见证可重放。
5. 为新物品补充视觉资产及其 SHA-256，将题目、Oracle 及哈希显式加入 suite。
6. 更新确定性测试；正式 suite 只能使用白名单插件。

生成题必须固定生成器版本和随机种子。不要手工编辑已生成场景；改变生成逻辑时升级生成器版本。

## 安全与隐私

- 不得提交 `.env`、API key、Authorization header、真实响应 artifact 或历史 `runs/`。
- `models.yaml` 只能保存 `api_key_env` 环境变量名。
- 测试必须使用 mock transport，禁止 CI 调用真实付费 API。
- 运行 `tests/test_no_secrets.py` 所覆盖的 secret scan 后再提交。

## 兼容性

首版支持 Python 3.11 和 3.13，Windows 与 Linux。文件内容使用 UTF-8 和 LF；公开 JSON/YAML 的语义必须跨平台稳定。
