# Plugin API

规则扩展通过 Python entry point 安装。场景 YAML 只引用稳定规则 ID，不接受模块路径或任意代码。

## EffectHandler

实现对象必须提供：

```python
class EffectHandler(Protocol):
    kind: ClassVar[str]
    version: ClassVar[str]

    def validate_config(self, value: dict[str, Any]) -> StrictModel: ...
    def apply(
        self,
        context: EvaluationContext,
        source: PlacedItem,
        effect: EffectSpec,
    ) -> list[EffectEvent]: ...
    def render_zh(self, effect: EffectSpec) -> str: ...
    def orientation_signature(self, effect: EffectSpec, rotation: int) -> Any: ...
```

约束：

- `apply` 必须是确定性的，不能访问网络、时钟或全局随机数。
- 事件顺序必须稳定；需要去重时显式排序。
- `render_zh` 必须完整表达验证器实际执行的空间和叠加语义。
- `orientation_signature` 用于消除占用格相同但效果方向不同/相同的旋转，返回值必须可 canonical JSON 序列化。
- `kind`、`version` 和场景中的完整 `config` 都进入场景哈希。

### 可选 Oracle 上界

规则可额外实现：

```python
def optimistic_bonus(
    self,
    effect: EffectSpec,
    source_count: int,
    target_count: int,
    source_cell_count: int,
    target_cell_count: int,
) -> int | None: ...
```

返回值必须是该效果在剩余搜索空间中**不可能被超过**的额外目标值。低估会破坏精确性，因此不确定时应返回 `None` 或完全不实现该方法；求解器会关闭该分支限界并完整枚举。

## ObjectiveHandler

```python
class ObjectiveHandler(Protocol):
    kind: ClassVar[str]
    version: ClassVar[str]

    def validate_config(self, value: dict[str, Any]) -> StrictModel: ...
    def score(
        self,
        context: EvaluationContext,
        stats: dict[str, dict[str, int]],
        objective: ObjectiveSpec,
    ) -> int: ...
    def render_zh(self, objective: ObjectiveSpec) -> str: ...
```

未知目标函数会自动关闭通用分支上界；求解仍为完整枚举。

## 注册

扩展包的 `pyproject.toml`：

```toml
[project.entry-points."backpack_bench.effects"]
my_effect = "my_package.effects:MyEffectHandler"

[project.entry-points."backpack_bench.objectives"]
my_objective = "my_package.objectives:MyObjectiveHandler"
```

entry point 可返回 handler 类或实例。重复 `kind` 会拒绝加载。正式题集还必须把 ID 加入 `allowed_plugins`；安装插件本身不会自动获得正式计分权限。

## 测试清单

- 配置未知字段、边界值和非法组合；
- 每个旋转的形状/方向一致性；
- 同一来源对同一目标的去重与叠加规则；
- 不规则背包、空布局和多实例；
- 中文渲染快照；
- 相同输入多次执行得到相同事件和分数；
- 若实现上界，小规模完整枚举验证 `bound >= true optimum`。
