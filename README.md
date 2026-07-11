# Backpack Battle Bench

面向 LLM 的纯文字二维空间规划 benchmark。题目、提示词、精确 Oracle、模型调用、验证、计分和报告使用同一套声明式语义，避免“题面规则”和验证器分叉。

核心范围是：**单轮纯文本、严格 JSON、本地批跑、静态报告、CI 验证**，并提供一个只监听本机的图形化题目实验台和 Run 控制台。正式计分语言为 `zh-CN`；不包含公网服务、分布式 worker 或工具型智能体。

## 快速开始

要求 Python 3.11–3.13 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --frozen
uv run bbbench --help
uv run bbbench suite validate .\suites\smoke-v1.yaml
uv run bbbench run .\configs\run.example.yaml --dry-run
uv run bbbench web
```

真实批跑前复制环境变量示例并编辑 key：

```powershell
Copy-Item .env.example .env
uv run bbbench run .\configs\run.example.yaml
```

`models.yaml` 只保存 `api_key_env` 的变量名。API key 不参与模型身份哈希，也不会写入 SQLite、artifact、报告或示例配置。

## 项目结构

```text
src/backpack_bench/       可安装 Python 包和 bbbench CLI
scenarios/                人工题与固定种子生成题
oracles/                  精确最优值和见证布局
suites/                   smoke-v1 / core-v1 / ladder-v1 / ladder-v2 manifest
configs/                  generator / models / run 示例
schemas/                  CLI 导出的 JSON Schema
tests/                    单元、契约和 24-job mock 集成测试
```

旧单文件入口已移除；已有 `runs/` 历史目录不会被导入或修改。

## CLI

```text
bbbench schema export
bbbench scenario validate <scenario> [--show-prompt]
bbbench generate <generator-config>
bbbench oracle solve <scenario> [--timeout 60] [-o oracle.json]
bbbench suite validate <suite>
bbbench run <run-config> [--dry-run] [--resume RUN_ID]
bbbench report <RUN_ID> --format console|json|csv|html
bbbench leaderboard <suite-id> --format console|json|csv|html
bbbench web [--host 127.0.0.1] [--port 8000] [--no-open]
```

报告还支持 `--group-by difficulty|tag|model|thinking_effort`：

```powershell
uv run bbbench report RUN_ID --database .\.bbbench\results.sqlite3 `
  --group-by difficulty --format csv --output reports\difficulty.csv
```

## 图形化实验台

```powershell
uv run bbbench web
```

这一条命令会同时启动后端 API、前端静态资源服务，并自动打开浏览器。二者共用同一个本地进程和端口，无需另外运行 npm 命令。默认地址为 `http://127.0.0.1:8000/`；服务器环境不应自动打开浏览器时使用 `uv run bbbench web --no-open`。Web 界面包含：

- 从 `smoke-v1` / `core-v1` / `ladder-v1` / `ladder-v2` 选题，按真实形状、旋转和不规则格子图形化摆放；拖动时会实时标出物品占位、效果覆盖范围和当前命中的目标，按住左键拖动时用右键旋转会立即重算预览；类别、基础属性和效果说明均使用直白的中文描述；
- 随时用后端的同一个严格验证器计算攻击、合法性和 Oracle 比例；
- 一键载入精确 Oracle 见证，并查看完整模型提示词；
- 直接在前端填写 OpenAI / Anthropic 兼容 API 的 URL、Key、模型名、思考强度、输出上限和限流参数；填写完成后自动记录，并可选择或删除历史 API；
- 发起 `configs/` 中的 `run.yaml`，跟踪 job 进度、主动中断运行、查看模型得分、恢复中断运行并打开 JSON/CSV/HTML 报告。

“前端填写 API”模式使用单个前端模型覆盖 `models.yaml` 中的模型矩阵，但仍由所选 `run.yaml` 决定题集、trial、全局并发和产物位置。历史记录保存在当前浏览器的 `localStorage`；“在当前浏览器保存 API Key”可单独关闭。Key 仅在发起或恢复 Run 时传给本地后端并以内存方式使用，不写入 SQLite、artifact、报告或模型配置。切换到“使用 models.yaml / .env”后，行为与 CLI 一致。

Web 进程也会从项目根目录 `.env` 读取配置文件模式所需的 API key，前端 API 不返回 key 或 `api_key_env`。一个 Web 进程同时只发起一个批跑；题内并发仍由 `run.yaml` 和当前模型配置控制。调试 API 文档位于 `/api/docs`。

## 场景格式

`scenario.yaml` 由 Pydantic 严格校验（未知字段报错）。矩形背包省略 `cells`；不规则背包显式列出合法格。

```yaml
schema_version: 1
id: tiny-example
version: 1.0.0
title: 最小示例
locale: zh-CN
board:
  width: 3
  height: 3
  # cells: [[0, 0], [0, 1], ...]  # 可选
items:
  - id: sword
    display_name: 铁剑
    count: 2
    shape: [[0, 0], [0, 1], [0, 2]]
    rotations: [0, 90, 180, 270]
    category: weapon
    stats: {attack: 5}
objective:
  type: sum_stat
  config: {category: weapon, stat: attack}
tags: [curated]
difficulty: easy
```

实例 ID 按类型稳定展开为 `sword_1`、`sword_2`。内置规则：

- `adjacent_stat_bonus`：旋转后的指定方向紧邻加成；
- `ray_stat_bonus`：沿旋转方向延伸至边界的射线区域加成；
- `sum_stat`：指定类别、指定属性求和。

场景会规范化为 canonical JSON。场景内容、规则插件 ID/版本/配置共同进入 SHA-256；提示词、题集和引擎版本另行记录哈希。

模型只能返回严格 JSON，不能带 Markdown：

```json
{
  "placements": [
    {"item_id": "sword_1", "row": 0, "col": 0, "rotation": 0}
  ]
}
```

坐标是**旋转后外接矩形的左上角**。物品可以不放；越界、进入不规则背包无效格、重叠、未知实例、重复实例和不支持的角度都会令该 trial 得 0 分。

## 公开题集与 Oracle

- `smoke-v1`：4 道诊断题，供 CI 和本地快速检查。
- `core-v1`：5 道人工题 + 15 道固定种子生成题，权重均为 `1.0`。
- `ladder-v1`：专门用于观察区分度的 L1–L5 五级阶梯题，最大背包为 5×5，权重均为 `1.0`。
- `ladder-v2`：保留原五题，并为每个等级增加两道不同考点的题，共 15 题；旧版不原地修改，以便历史运行可复现。
- `mixed-3x3` 是最初的 3×3 铁剑/宝石/匕首/魔法阵题，精确最优攻击固定为 **21**。

`ladder-v1` 逐级增加的不是单纯搜索规模，而是需要同时维护的空间约束：

| 等级 | 题目 | 空间与规则重点 | 精确最优攻击 |
|---|---|---|---:|
| L1 | 宝石夹击 | 3×3、单一相邻增益、基础旋转 | 16 |
| L2 | 缺角背包的取舍 | 4×4 缺角、物品总面积超过背包、L 形拼装与弃物 | 32 |
| L3 | 交叉火力 | 4×4、相邻增益与可穿透射线同时转向 | 50 |
| L4 | 诅咒与阻挡 | 5×5 不规则背包、负面效果、会被物品挡住的射线 | 52 |
| L5 | 棱镜迷阵 | 5×5、同一物品正负双效果、旋转诅咒、穿透射线与挡板诱饵 | 71 |

完整设计意图和结构检查见 [`docs/ladder-v1.md`](docs/ladder-v1.md)。实际难度仍应以多模型、多 trial 的得分分布校准，而不是只看 Oracle 求解耗时。

扩展版的 15 个互不重复主考点见 [`docs/ladder-v2.md`](docs/ladder-v2.md)，涵盖纯拼装、非对称方向、重复命中、多来源叠加、遮挡顺序、稀疏距离偏移、负面屏蔽和光束迷宫等能力。

精确求解器预生成合法摆放、去除相同实例排列和等价旋转、允许不放，并对具有安全上界的规则执行分支限界。内置规则还会使用已经固定的来源位置和目标位置计算布局感知上界，避免把已经摆错位置的武器继续当作“仍可能被增益”；重复命中规则另有安全上界回归测试。插件没有声明安全上界时自动退化为完整枚举。Oracle 记录最优攻击、见证、节点数、耗时、求解器版本和场景哈希；Oracle 证明哈希不包含墙钟耗时，因此重新求解不会因机器快慢改变 suite hash。

```powershell
uv run bbbench oracle solve .\scenarios\curated\mixed_3x3.yaml --timeout 60
uv run bbbench generate .\configs\generator.core.yaml
uv run bbbench suite validate .\suites\ladder-v1.yaml
uv run bbbench suite validate .\suites\ladder-v2.yaml
```

正式题必须在预算内完成精确证明且最优攻击大于 0。生成器使用稳定版本和局部 `random.Random(seed)`；普通生成时候选超时后按确定顺序继续。`core-v1` 还在配置中冻结了已录取的 `candidate_indices`：慢机器会明确失败，而不会悄然换成另一组题。

## 模型配置

`configs/models.example.yaml` 同时演示 OpenAI Chat Completions 和 Anthropic Messages 兼容协议：

```yaml
schema_version: 1
profiles:
  - id: openai-compatible-medium
    protocol: openai_chat
    base_url: https://gateway.example.com/v1
    model: replace-me
    api_key_env: OPENAI_COMPATIBLE_API_KEY
    params:
      thinking_effort: medium
      # max_tokens: 8192  # 只有显式配置才发送
    limits:
      concurrency: 1
      qps: 1
      timeout_seconds: 120
      retries: 3
```

映射规则：

- OpenAI：`thinking_effort` → `reasoning_effort`；
- Anthropic：`thinking_effort` → `output_config.effort`；
- Anthropic 自适应思考：`thinking_mode: adaptive`；
- Anthropic 手动思考：`thinking_mode: enabled`、`thinking_budget >= 1024`，且必须配置更大的 `max_tokens`；
- 默认不发送 `max_tokens`，避免客户端在 2048 等固定值截断输出。

`temperature` 默认也省略。第三方网关的非标准字段可放入 `params.extra_body`；非敏感自定义头放入 `extra_headers`。鉴权头禁止写入配置，必须使用 `api_key_env`。

模型榜单身份是协议、实际 endpoint、模型名、鉴权模式和完整请求参数的 canonical hash；显示名、限流、价格和 API key 不参与身份。

## 自动化运行与恢复

`run.yaml` 展开 `模型配置 × 场景 × trial` 为稳定 job ID。默认全局并发为 1；每个 profile 还可独立限制并发和 QPS。

实际并发槽位上限是 `min(run.yaml 的全局 concurrency, 模型 limits.concurrency)`。QPS 控制的是请求启动频率：例如并发槽位为 5、`qps: 1` 时，请求仍会每秒只启动一个；只有单次请求耗时超过 1 秒时才会逐渐出现多个同时在途的请求。

`configs/run.all.yaml` 已配置为覆盖当前全部 20 道唯一题目的 `core-v1`；按示例中 2 个模型配置、3 次 trial 展开为 120 个 jobs：

```powershell
uv run bbbench run .\configs\run.all.yaml --dry-run
```

`configs/run.ladder.yaml` 覆盖 L1–L5；按示例中 2 个模型配置、3 次 trial 展开为 30 个 jobs：

```powershell
uv run bbbench run .\configs\run.ladder.yaml --dry-run
```

扩展版使用 `configs/run.ladder-v2.yaml`；`2 模型 × 15 题 × 3 trials = 90 jobs`：

```powershell
uv run bbbench run .\configs\run.ladder-v2.yaml --dry-run
```

只有网络异常、HTTP 408/429/5xx 会重试，并遵循 `Retry-After`；响应协议错误、非 JSON、答案结构错误和非法摆放不重试。CLI 可用 `Ctrl+C` 中断，Web 运行详情中也提供“中断”按钮；正在请求的 job 会回到 pending，已完成结果保留。`--resume RUN_ID` 跳过已完成 job，并继续 pending job，不生成重复结果。

每次失败 attempt 都会立即在控制台打印原始错误类型和消息（API Key 会脱敏），并记录 HTTP 状态、错误、延迟和重试序号。即使整个 Run 失败或被中断，也会生成当时状态的静态报告。

SQLite 开启 WAL、外键和唯一 job 约束；并发结果通过单 writer 队列写入。artifact 采用临时文件 + 原子重命名，按下列结构保存：

```text
.bbbench/artifacts/RUN_ID/
  run.json
  prompts/<scenario-hash>.txt
  <job-id>/attempt_001/
    request.json          # 脱敏
    response.json
    reasoning.txt         # 若协议提供
    model_output.txt
    validation.json
    summary.json
```

成功批跑还会在 `run.yaml` 的 `reports/RUN_ID/` 下原子写入 JSON、CSV 和静态 HTML；也可以随时用 `bbbench report` 从 SQLite 重建。

错误分类包括传输、HTTP、响应协议、截断、非 JSON、答案结构、非法摆放和成功计分。若实际攻击超过精确 Oracle，运行会以 `engine_oracle_inconsistency` 失败并终止正式计分。

## 计分与报告

对场景 `s`、trial `t`：

```text
actual[s,t] = 合法布局的实际攻击力，否则为 0
ratio[s,t] = actual[s,t] / exact_oracle[s]
scenario_score[s] = 所有 trial 的 ratio 均值
overall_score = 100 × Σ(weight[s] × scenario_score[s]) / Σ(weight[s])
```

报告包含单题实际攻击的均值/最好/最差/标准差、合法率、最优命中率、错误分布、重试率、截断率、延迟 P50/P95、输入/输出/推理/缓存 token 和可选成本估算。HTML 报告还会按模型展开每道题和每次 trial，并继续展开每次 attempt 的 HTTP 状态、原始失败原因、延迟和重试顺序，同时显示攻击、Oracle 比例、Job ID 及脱敏后的完整验证明细。未配置价格时成本为 `null`。

## 扩展规则

YAML 只能引用注册过的规则 ID，不能加载任意 Python 路径。第三方包通过 entry point 注册：

```toml
[project.entry-points."backpack_bench.effects"]
my_effect = "my_package.effects:MyEffectHandler"

[project.entry-points."backpack_bench.objectives"]
my_objective = "my_package.objectives:MyObjectiveHandler"
```

正式 suite 必须在 `allowed_plugins` 中逐项白名单。接口和安全上界约定见 [`docs/plugin-api.md`](docs/plugin-api.md)。

## 开发与验证

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run bbbench suite validate .\suites\smoke-v1.yaml
uv run bbbench suite validate .\suites\core-v1.yaml
uv run bbbench suite validate .\suites\ladder-v1.yaml
uv run bbbench suite validate .\suites\ladder-v2.yaml
uv build
```

CI 在 Windows/Linux、Python 3.11/3.13 上执行冻结依赖同步、Ruff、mypy、pytest、schema 导出、题集完整性检查和构建；测试只使用 mock API，不调用付费接口。

贡献方式见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。项目采用 MIT License。
