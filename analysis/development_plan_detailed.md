# 细化开发方案：Scoped Event System

> 基于 `proposal_scoped_eventbus.md` 的 4-Phase 实施路径，拆分为 10 个可独立交付、可独立测试的 Step。
> 每个 Step 产出明确的文件、API、测试，并标注需要讨论的核心设计决策。

## 0. 总览

### 包结构

```
src/agent_cdp/
├── __init__.py
├── _context.py                    # ContextVar 定义 (_current_event, _current_connection)
├── events/
│   ├── __init__.py
│   ├── result.py                  # Step 1.1: EventResult[T]
│   ├── base.py                    # Step 1.2: BaseEvent[T] 基类 + consume/await（~158 行，聚合已提取）
│   └── aggregation.py             # Step 1.3: 6种结果聚合自由函数 + HandlerError（~179 行，从 BaseEvent 提取）
├── connection/
│   ├── __init__.py
│   ├── types.py                   # Step 2.1: ConnectionType + EmitPolicy 枚举
│   └── connection.py              # Step 2.1: Connection 数据类 + connect() 函数
├── scope/
│   ├── __init__.py
│   ├── _helpers.py                # Step 2.2: _record helper（Connection→primitives 拆解）
│   ├── scope.py                   # Step 2.2: EventScope（emit, connection 管理）
│   ├── event_loop.py              # Step 2.3: ScopeEventLoop（Queued 事件处理循环）
│   └── group.py                   # Step 3.1: ScopeGroup（broadcast, connect_all_scopes, 生命周期）
├── advanced/
│   ├── __init__.py
│   ├── expect.py                  # Step 4.1: expect() 声明式未来事件等待
│   ├── timeout.py                 # Step 4.1: per-handler 超时 + 死锁监测
│   ├── event_log.py               # Step 4.2: EventLog per-scope JSONL 事件日志（原名 WAL，设计评审后更正）
│   └── cycle_detect.py            # Step 4.2: Direct 连接链循环/深度检测
└── _registry.py                   # conscribe 集成：EventRegistrar（事件类型自动注册）

tests/
├── conftest.py                    # 公共 fixtures（scope factory, event factory）
├── test_step_1_1_event_result.py
├── test_step_1_2_base_event.py
├── test_step_1_3_aggregation.py
├── test_step_2_1_connection.py
├── test_step_2_2_scope_emit.py
├── test_step_2_3_event_loop.py
├── test_step_3_1_scope_group.py
├── test_step_3_2_lifecycle.py
├── test_step_4_1_expect_timeout.py
└── test_step_4_2_event_log_cycle.py
```

### 依赖

```toml
[project]
dependencies = [
    "pydantic>=2.0",
    "conscribe>=0.4.0",
    "uuid-utils>=0.9",        # uuid7 生成
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.5",
    "pyright>=1.1",
    "anyio>=4.0",             # WAL 异步文件 I/O
]
```

### conscribe 集成策略

conscribe 0.4 从 Step 1.2 开始使用，为 BaseEvent 的所有子类提供自动注册。

**集成点：`_registry.py`**

```python
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from conscribe import create_registrar

@runtime_checkable
class EventProtocol(Protocol):
    """所有事件类必须满足的协议。"""
    event_id: str
    consumed: bool

EventRegistrar = create_registrar(
    name='event',
    protocol=EventProtocol,
    discriminator_field='event_type',
    strip_suffixes=['Event'],
    # skip_pydantic_generic=True 是 0.4 默认值，
    # 自动跳过 Pydantic Generic 特化中间类（如 BaseEvent[str]）
)

# bridge() 解决 AutoRegistrar metaclass 与 Pydantic ModelMetaclass 的冲突，
# 动态创建合并 metaclass：(AutoRegistrar[event], ModelMetaclass, ABCMeta, type)
EventBridge = EventRegistrar.bridge(BaseModel)
```

**使用方式：** BaseEvent 继承 `EventBridge` + `Generic[T_Result]`，所有具体子类自动注册。

```python
class BaseEvent(EventBridge, Generic[T_Result]):
    __abstract__ = True  # 基类不注册
    ...

# 具体事件类——自动注册，无需任何装饰器
class NavigateToUrlEvent(BaseEvent[str]):
    url: str

# 抽象中间基类——不注册
class LifecycleEvent(BaseEvent[None]):
    __abstract__ = True

class SessionStartEvent(LifecycleEvent):  # 自动注册为 'session_start'
    session_id: str

# 运行时按名查找
EventRegistrar.get('navigate_to_url')  # → NavigateToUrlEvent 类
EventRegistrar.keys()                  # → ['navigate_to_url', 'crash', 'session_start', ...]
```

**注册过滤规则（由 conscribe 0.4 metaclass 自动处理）：**
- `__abstract__ = True` → 跳过（基类、中间抽象类）
- 类名含 `[` → 跳过（Pydantic Generic 特化中间类，如 `BaseEvent[str]`）
- 其余具体子类 → 自动注册，key 由 CamelCase→snake_case + strip `Event` 后缀推断

**价值：**
1. WAL 反序列化时按 `event_type` 字符串查找对应的 class
2. 调试/监控工具按名列举所有已注册事件类型
3. 配置系统生成事件类型的 discriminated union（typed YAML 校验）
4. 未来插件机制：第三方 watchdog 定义的事件类型自动可发现

---

## Phase 1: 事件模型（Step 1.1 – 1.3）

> 目标：完整的事件数据模型，可独立测试，不依赖 Scope 和 Connection。

---

### Step 1.1 — EventResult[T] 结果容器

**产出文件：** `src/agent_cdp/events/result.py`

**核心 API：**

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar('T')

@dataclass
class EventResult(Generic[T]):
    """一个 handler 处理事件后的结果容器。"""

    handler_name: str
    connection_id: str
    result: T | None = None
    error: Exception | None = None
    status: ResultStatus = ResultStatus.PENDING
    event_children: list['BaseEvent'] = field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def mark_completed(self, result: T | None = None) -> 'EventResult[T]':
        """标记为完成，返回新实例（不可变）。"""
        ...

    def mark_failed(self, error: Exception) -> 'EventResult[T]':
        """标记为失败，返回新实例。"""
        ...

    def mark_timeout(self, error: TimeoutError) -> 'EventResult[T]':
        """标记为超时，返回新实例。"""
        ...

    @property
    def is_success(self) -> bool:
        return self.status == ResultStatus.COMPLETED and self.error is None

class ResultStatus(str, Enum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    TIMEOUT = 'timeout'
```

**设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| dataclass vs Pydantic | **dataclass** | EventResult 持有 `Exception` 引用，Pydantic 序列化 Exception 不方便。EventResult 是框架内部结构，不需要 JSON schema 校验 |
| 可变 vs 不可变 | **不可变**（mark_* 返回新实例） | 遵循 coding-style 中的 immutability 原则。`dataclass(frozen=True)` + replace 模式 |
| status 枚举 vs 字符串 | **StrEnum** | 类型安全 + 序列化友好 |

**测试清单（`test_step_1_1_event_result.py`）：**

```
- test_create_pending_result          # 默认状态
- test_mark_completed                 # pending → completed
- test_mark_failed                    # pending → failed，error 不为 None
- test_mark_timeout                   # pending → timeout
- test_immutability                   # mark_* 返回新实例，原实例不变
- test_is_success_property            # completed + no error = True
- test_add_child_event                # event_children 追加
```

---

### Step 1.2 — BaseEvent[T] 泛型事件基类

**产出文件：** `src/agent_cdp/events/base.py`, `src/agent_cdp/_context.py`, `src/agent_cdp/_registry.py`

**核心 API：**

```python
from pydantic import BaseModel, Field
from typing import Generic, TypeVar
from uuid_utils import uuid7
from ._registry import EventBridge

T_Result = TypeVar('T_Result')

def _make_set_event() -> asyncio.Event:
    """创建一个已 set 的 asyncio.Event。"""
    e = asyncio.Event()
    e.set()
    return e

class BaseEvent(EventBridge, Generic[T_Result]):
    """泛型事件基类。T_Result 声明 handler 返回的结果类型。

    通过 conscribe bridge(BaseModel) 同时获得：
    - Pydantic 的数据校验和 JSON 序列化
    - conscribe 的自动注册（具体子类自动注册到 EventRegistrar）
    """
    __abstract__ = True

    # --- 公共字段 ---
    event_id: str = Field(default_factory=lambda: str(uuid7()))
    event_timeout: float | None = 300.0
    consumed: bool = False
    event_parent_id: str | None = None
    event_results: dict[str, EventResult[T_Result]] = Field(default_factory=dict)

    # --- ClassVar（不序列化） ---
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST  # Direct handler 异常策略

    # --- 内部状态（不序列化，PrivateAttr） ---
    _completion: asyncio.Event = PrivateAttr(default_factory=_make_set_event)
    _pending_count: int = PrivateAttr(default=0)

    # --- 传播控制 ---
    def consume(self) -> None:
        """标记事件已被消费，阻止后续 handler 执行。"""
        # 返回带 consumed=True 的新 event 不现实（event 被多处引用），
        # 这里是例外：consumed 是 in-place mutation，因为它是控制流标志
        self.consumed = True

    # --- 状态查询 ---
    @property
    def has_pending(self) -> bool:
        """是否有尚未完成的 Queued handler。sync 上下文调用方用此做知情决策。"""
        return self._pending_count > 0

    # --- 结果记录（keyword-only，零外部依赖） ---
    def record_result(
        self,
        *,
        connection_id: str,
        handler_name: str,
        result: T_Result | None = None,
        error: Exception | None = None,
    ) -> None:
        """记录一个 handler 的结果。由框架通过 _record helper 调用。"""
        ...

    # --- Pending 计数（仅追踪 Queued handler） ---
    def _increment_pending(self) -> None:
        if self._pending_count == 0:
            self._completion.clear()     # 第一个 Queued → 进入等待
        self._pending_count += 1

    def _decrement_pending(self) -> None:
        """计数归零时 set _completion event。"""
        self._pending_count -= 1
        if self._pending_count == 0:
            self._completion.set()       # 最后一个完成 → 回到完成

    # --- Awaitable ---
    def __await__(self):
        """await event → 等待所有 handler（Direct + Queued）完成。"""
        return self._completion.wait().__await__()

    # --- Deep copy 支持（broadcast 场景） ---
    def __deepcopy__(self, memo: dict) -> BaseEvent[T_Result]:
        new = self.model_copy(deep=True)
        object.__setattr__(new, '__pydantic_private__', {
            **new.__pydantic_private__,
            '_completion': _make_set_event(),
            '_pending_count': 0,
        })
        return new
```

**需要讨论的核心设计点：**

#### ~~讨论点 1.2-A：Pydantic + conscribe metaclass 兼容性~~ ✅ 已解决

conscribe 0.4 通过 `bridge(BaseModel)` 动态创建合并 metaclass，完美解决了
`AutoRegistrar` 与 Pydantic `ModelMetaclass` 的冲突。同时 `skip_pydantic_generic=True`
（默认）在 metaclass 层面过滤掉 Generic 特化中间类（如 `BaseEvent[str]`）。

最终模式（已验证，见 `demo_conscribe_pydantic.py`）：
```python
EventBridge = EventRegistrar.bridge(BaseModel)

class BaseEvent(EventBridge, Generic[T_Result]):
    __abstract__ = True
    ...

class NavigateToUrlEvent(BaseEvent[str]):  # 自动注册为 'navigate_to_url'
    url: str
```

**不再是技术风险。**

#### ~~讨论点 1.2-B：`_completion` 的初始化时机~~ ✅ 已解决

**决定：构造时创建，默认 set（方案 A）。**

Python ≥3.11 中 `asyncio.Event()` 不需要 running event loop 即可构造（3.10 已移除 loop 参数），
因此"在 event loop 外无法创建"的顾虑不成立。

**实现方式：** 通过 `PrivateAttr(default_factory=...)` 创建已 set 的 Event，无需覆盖 `model_post_init`，
不影响 conscribe bridge metaclass 链：

```python
def _make_set_event() -> asyncio.Event:
    e = asyncio.Event()
    e.set()
    return e

class BaseEvent(EventBridge, Generic[T_Result]):
    _completion: asyncio.Event = PrivateAttr(default_factory=_make_set_event)
    _pending_count: int = PrivateAttr(default=0)
```

**状态机语义：**
- 出生即 set（"已完成"）→ 零 handler / 纯 Direct 场景 `await event` 立即返回
- `_increment_pending()` 第一次调用时 `clear()`（进入等待）
- `_decrement_pending()` 归零时 `set()`（回到完成）

**`model_copy(deep=True)` 处理（broadcast 场景）：**

覆盖 `__deepcopy__` 确保副本获得独立的、已 set 的 `asyncio.Event`：

```python
def __deepcopy__(self, memo: dict) -> BaseEvent[T_Result]:
    new = self.model_copy(deep=True)
    object.__setattr__(new, '__pydantic_private__', {
        **new.__pydantic_private__,
        '_completion': _make_set_event(),
        '_pending_count': 0,
    })
    return new
```

**否决的方案：**
- 方案 B（惰性创建）：复杂度分散在每个 `_increment` / `_decrement` / `__await__` 的 None 检查上，长期维护风险高。
  `model_copy` 天然安全是唯一优势，但不值得牺牲清晰度。
- 方案 C（构造 unset）：`_completion` unset 状态有歧义（"从未用过" vs "正在等待"），
  且可能需要 `model_post_init` hack 影响 conscribe bridge。

**Qt/GObject 参考：** 该问题在 Qt/GObject 中不存在——Qt sendEvent 全同步，postEvent fire-and-forget，
GObject g_signal_emit 全同步。`_completion` 机制是本系统原创设计，源于同时支持 Direct + Queued 并要求 `await event` 等待两者。

#### ~~讨论点 1.2-C：record_result 签名~~ ✅ 已解决

**决定：接受原始类型（keyword-only）+ `_record` helper 中间层。**

`record_result` 只认识 `str`，住在 events 包，零外部依赖：

```python
# events/base.py
def record_result(
    self,
    *,                              # keyword-only barrier，防止参数顺序错误
    connection_id: str,
    handler_name: str,
    result: T_Result | None = None,
    error: Exception | None = None,
) -> None:
    ...
```

scope 包通过 `_record` helper 集中 Connection → primitives 的拆解逻辑：

```python
# scope/_helpers.py（同时依赖 events + connection，但这些包本来就是 scope 的依赖）
def _record(
    event: BaseEvent,
    conn: Connection,
    *,
    result: Any = None,
    error: Exception | None = None,
) -> None:
    event.record_result(
        connection_id=conn.id,
        handler_name=get_handler_name(conn.handler),
        result=result,
        error=error,
    )
```

**依赖关系（单向、无环）：**

```
events/base.py       → 无外部依赖
connection/          → events（单向）
scope/_helpers.py    → events + connection（唯一的"双知"点）
scope/scope.py       → 通过 _helpers 间接
```

**Direct handler 异常处理：记录且 propagate（方案 Y）。**

```python
# emit() Direct path
try:
    result = conn.handler(event)
    _record(event, conn, result=result)
except Exception as e:
    _record(event, conn, error=e)   # event_results 有完整记录
    raise                            # 调用方仍获得即时异常反馈
```

- event_results 是完整记录（所有 handler 都有条目，无论成功/失败）
- 聚合方法的 `raise_if_any` 能看到 Direct handler 的错误
- 调用方也能通过 try/except 立即处理

**未来扩展：** 若 EventResult 需新增字段（如 `source_scope_id`），只改 `record_result` 签名 +
`_record` helper 两处，所有调用点不动。

**Qt/GObject 参考：** GObject 的 accumulator 模式最接近——由框架（而非 handler）调用聚合器，传递的是值而非对象引用。
但 GObject accumulator 不携带 handler 身份信息（无 connection ID 概念），我们比 GObject 多一层按 handler 索引的需求。

**测试清单（`test_step_1_2_base_event.py`）：**

```
- test_create_event_default_fields       # event_id 自动生成，consumed=False
- test_consume_sets_flag                 # consume() 设置 consumed=True
- test_record_result_stores_in_dict      # 按 connection_id 存储
- test_record_result_with_error          # error 结果
- test_await_completes_when_no_pending   # pending=0 → 立即完成
- test_await_blocks_until_pending_zero   # pending=2 → decrement 两次后完成
- test_parent_id_tracking                # 设置 event_parent_id
- test_event_subclass_auto_registers     # conscribe 自动注册验证
- test_event_registrar_lookup            # EventRegistrar.get('xxx') 查找
```

---

### Step 1.3 — 6 种结果聚合方法

**产出文件：** `src/agent_cdp/events/aggregation.py`（自由函数模块，设计评审后确定从 BaseEvent 提取）。

**核心 API：**

```python
# events/aggregation.py — 自由函数（设计评审后从 BaseEvent 提取）

from agent_cdp.events.result import EventResult

class HandlerError(Exception):
    """Wraps a handler exception with identity info for diagnostics."""
    def __init__(self, original: Exception, handler_name: str, connection_id: str) -> None: ...

async def event_result(
    event: BaseEvent,
    timeout: float | None = None,
    raise_if_any: bool = True,
    raise_if_none: bool = True,
) -> T_Result | None:
    """取第一个非 None 结果。await 确保所有 handler（含 Queued）完成。"""
    ...

async def event_results_list(event: BaseEvent, ...) -> list[T_Result]:
    """所有结果组成 list。"""
    ...

async def event_results_by_handler_name(event: BaseEvent, ...) -> dict[str, T_Result]:
    """按 handler 名分组。"""
    ...

async def event_results_flat_dict(
    event: BaseEvent,
    raise_if_conflicts: bool = True,
    ...
) -> dict[str, Any]:
    """合并所有 handler 返回的 dict。冲突检测。"""
    ...

async def event_results_flat_list(event: BaseEvent, ...) -> list[Any]:
    """合并所有 handler 返回的 list。"""
    ...

async def event_results_filtered(
    event: BaseEvent,
    include: Callable[[EventResult], bool] = _is_truthy,
    ...
) -> dict[str, EventResult[T_Result]]:
    """自定义过滤后的结果集。"""
    ...

# 内部辅助
async def _wait_for_completion(event: BaseEvent, timeout: float | None = None) -> None:
    """等待所有 handler 完成，fallback 到 event_timeout。"""
    ...
```

**设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| 聚合方法是 BaseEvent 方法还是自由函数 | **自由函数**（设计评审后变更） | 解决 God Object 问题——BaseEvent 从 ~300 行缩减到 ~158 行。聚合逻辑可独立修改不触及核心类。调用方式从 `await event.event_result()` 变为 `await event_result(event)` |
| 聚合方法是 sync 还是 async | **async** | 内部需要 `await _wait_for_completion()` 确保 Queued handler 完成。调用方如果确认全是 Direct，可以直接访问 `event.event_results` dict 绕过 await |
| flat_dict 冲突默认行为 | **raise** | 安全优先。显式合并需要调用方选择 `raise_if_conflicts=False` |
| raise_if_any 的语义 | **raise 第一个遇到的 error** | 与 bubus 行为一致。异常包装为 `HandlerError(original_error, handler_name, connection_id)` |
| _wait_for_completion 超时 | **fallback 到 event_timeout** | 每个方法可以覆盖，但默认用事件级超时 |

**测试清单（`test_step_1_3_aggregation.py`）：**

```
# event_result()
- test_event_result_returns_first_non_none
- test_event_result_raise_if_any_error
- test_event_result_raise_if_none_all_none
- test_event_result_no_raise_if_none

# event_results_list()
- test_results_list_excludes_none
- test_results_list_preserves_order

# event_results_by_handler_name()
- test_results_by_handler_name_mapping
- test_results_by_handler_name_duplicate_names

# event_results_flat_dict()
- test_flat_dict_merges_non_overlapping
- test_flat_dict_raises_on_conflict
- test_flat_dict_allows_override_when_no_raise

# event_results_flat_list()
- test_flat_list_concatenates
- test_flat_list_skips_non_iterable

# event_results_filtered()
- test_filtered_with_default_truthy
- test_filtered_with_custom_predicate
- test_filtered_empty_results

# 超时
- test_aggregation_timeout_raises
- test_aggregation_respects_event_timeout
```

---

## Phase 2: 连接与派发（Step 2.1 – 2.3）

> 目标：Qt 式 N:M 连接拓扑 + Direct/Queued/Auto 三种派发模式。

---

### Step 2.1 — ConnectionType + Connection + connect()

**产出文件：** `src/agent_cdp/connection/types.py`, `src/agent_cdp/connection/connection.py`

**核心 API：**

```python
# --- types.py ---

class ConnectionType(str, Enum):
    DIRECT = 'direct'      # handler 在 emit() 调用栈内同步执行
    QUEUED = 'queued'       # event 入队到目标 scope 的事件循环
    AUTO   = 'auto'         # 同 scope → Direct，跨 scope → Queued

class EmitPolicy(str, Enum):
    FAIL_FAST = 'fail_fast'            # 异常中断后续 handler，propagate 到调用方
    COLLECT_ERRORS = 'collect_errors'   # 异常记录到 event_results，继续后续 handler

# --- connection.py ---

@dataclass(frozen=True)
class Connection:
    """发布者和订阅者之间的一条连接。不可变。"""

    id: str
    source_scope: weakref.ref['EventScope']     # 弱引用避免循环
    event_type: type[BaseEvent]
    handler: Callable
    target_scope: weakref.ref['EventScope'] | None
    mode: ConnectionType
    filter: Callable[[BaseEvent], bool] | None
    priority: int

    _active: bool = field(default=True, repr=False)  # 内部：是否断开

    def disconnect(self) -> None:
        """断开此连接。"""
        # 通过 object.__setattr__ 修改 frozen dataclass 的 _active
        # 并通知 source_scope 移除
        ...

    @property
    def active(self) -> bool:
        return self._active


def connect(
    source: 'EventScope',
    event_type: type[BaseEvent],
    handler: Callable,
    *,
    mode: ConnectionType = ConnectionType.AUTO,
    target_scope: 'EventScope | None' = None,
    priority: int = 0,
    filter: Callable[[BaseEvent], bool] | None = None,
) -> Connection:
    """创建一条从 source 到 handler 的连接。

    返回 Connection 对象，可用于后续 disconnect()。
    """
    ...
```

**设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| Connection 持有 scope 引用方式 | **weakref.ref** | 避免 Connection ↔ Scope 循环引用导致内存泄漏。scope 被 GC 后 connection 自动失效 |
| Connection frozen vs mutable | **frozen dataclass** + `_active` 通过 `object.__setattr__` 修改 | 只有 `disconnect()` 一个变更点，其余字段创建后不变 |
| connect() 是自由函数 vs scope 方法 | **自由函数 + scope 方法双入口** | `connect(source, ...)` 是主 API，`scope.connect(event_type, handler)` 是便捷方法（内部调用自由函数） |
| event_type 匹配策略 | **MRO 索引（等价 isinstance 子类匹配）** | 连接按 type 分桶存储，emit 时遍历 MRO 收集匹配。O(MRO_depth × conns_per_type)。BaseEvent 不可直接 connect，用 connect_all 做 catch-all |

**测试清单（`test_step_2_1_connection.py`）：**

```
- test_create_connection_via_connect
- test_connection_is_frozen
- test_disconnect_sets_inactive
- test_disconnect_removes_from_scope
- test_weakref_scope_reference
- test_connection_with_filter
- test_connection_with_priority
- test_connection_type_enum_values
```

---

### Step 2.2 — EventScope 核心（emit + connection 管理）

**产出文件：** `src/agent_cdp/scope/scope.py`

**这是整个系统最核心的代码。**

**核心 API：**

```python
class EventScope:
    """隔离的事件处理域。"""

    scope_id: str
    metadata: dict[str, Any]

    # --- 连接存储（MRO 索引） ---
    _connections_by_type: dict[type[BaseEvent], list[Connection]]  # 按 event type 索引
    _catch_all_connections: list[Connection]   # connect_all 存这里，独立于 _connections_by_type
    _incoming: list[Connection]               # 以本 scope 为 target 的连接
    _event_loop: ScopeEventLoop               # Queued 事件循环（Step 2.3）

    # --- 连接管理 ---

    def connect(
        self,
        event_type: type[BaseEvent],
        handler: Callable,
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: 'EventScope | None' = None,
        priority: int = 0,
        filter: Callable[[BaseEvent], bool] | None = None,
    ) -> Connection:
        """便捷方法：以本 scope 为 source 创建连接。

        验证规则（conscribe 集成）：
        - event_type is BaseEvent → TypeError，用 connect_all() 代替
        - event_type 必须是已知事件类型（__abstract__ 分组节点或 EventRegistrar 已注册的具体类）
        """
        if event_type is BaseEvent:
            raise TypeError(
                'Cannot connect to BaseEvent directly — '
                'use scope.connect_all(handler) for catch-all.'
            )
        ...

    def connect_all(
        self,
        handler: Callable,
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: 'EventScope | None' = None,
        priority: int = 0,
        filter: Callable[[BaseEvent], bool] | None = None,
    ) -> Connection:
        """显式 catch-all：连接到所有事件类型。替代 bubus 的 '*'。

        存入 _catch_all_connections，不存入 _connections_by_type。
        BaseEvent 在 _connections_by_type 中永远不出现。
        """
        ...

    def _add_connection(self, conn: Connection) -> None: ...
    def _remove_connection(self, conn: Connection) -> None: ...
    def _add_incoming(self, conn: Connection) -> None: ...

    # --- 事件派发 ---

    def emit(self, event: BaseEvent) -> BaseEvent:
        """从本 scope 发布事件。同步函数。

        执行流程：
        1. 设置 ContextVar（父子追踪）
        2. MRO 索引收集匹配连接 + catch_all，按 priority 降序排列
        3. 遍历连接：
           a. 检查 connection.active
           b. 应用 connection.filter
           c. resolve Auto → Direct or Queued
           d. Direct: 同步调用 handler，_record，检查 consumed（受 EmitPolicy 控制）
           e. Queued: increment_pending，入队到 target_scope 的 event_loop
        4. 返回 event
        """
        ...

    def _get_matching_connections(self, event: BaseEvent) -> list[Connection]:
        """MRO 索引匹配：遍历 type(event).__mro__，收集每层的连接 + catch_all。

        复杂度：O(MRO_depth × avg_connections_per_type)，MRO 通常 3-5 层。
        """
        matching = []
        for cls in type(event).__mro__:
            if cls in self._connections_by_type:
                matching.extend(
                    conn for conn in self._connections_by_type[cls] if conn.active
                )
        matching.extend(conn for conn in self._catch_all_connections if conn.active)
        matching.sort(key=lambda c: c.priority, reverse=True)
        return matching

    def _resolve_mode(self, conn: Connection) -> ConnectionType:
        """解析 Auto 模式。

        Auto: source_scope is target_scope → Direct, 否则 → Queued。
        target_scope 为 None 的 Auto 连接 → Direct（无目标 scope 意味着 handler 无关联 scope）。
        """
        ...

    # --- 生命周期 ---

    async def close(self) -> None:
        """关闭 scope：停止事件循环，断开所有连接。"""
        await self._event_loop.stop()
        for conns in self._connections_by_type.values():
            for conn in list(conns):
                conn.disconnect()
        for conn in list(self._catch_all_connections):
            conn.disconnect()
        for conn in list(self._incoming):
            conn.disconnect()
```

**需要讨论的核心实现细节：**

#### ~~讨论点 2.2-A：emit() 是 sync 函数~~ ✅ 已解决

**决定：emit() 是 `def emit()`（sync），不是 `async def emit()`。**

理由：
- Direct handler 必须在 emit() 调用栈内同步完成
- Queued handler 入队操作是 `queue.put_nowait()`（同步）
- 如果 emit 是 async，调用方需要 `await scope.emit(event)`，但 Direct handler 的零延迟语义要求 emit 返回时 Direct 结果已可用
- Qt 的 signal emit 和 bubus 的 dispatch 都是 sync——emit 是统一入口，Direct/Queued 的区分在内部完成

**调用模式：**

```python
event = scope.emit(SomeEvent())         # sync，Direct handler 已完成
if event.has_pending:                    # 检查是否有 Queued handler 未完成
    await event                          # async，等待 Queued handler
result = await event.event_result()      # 取聚合结果
```

**`has_pending` property：** BaseEvent 暴露 `_pending_count > 0`，让 sync 上下文的调用方做知情决策。
这是 Qt `postEvent()` 的心智模型——投递后你知道它在队列里，但不在当前栈帧等。

```python
# events/base.py
@property
def has_pending(self) -> bool:
    """是否有尚未完成的 Queued handler。"""
    return self._pending_count > 0
```

**EmitPolicy — Direct handler 异常策略：**

不同类型的事件对 Direct handler 异常有不同的容忍度。通过 `EmitPolicy` ClassVar 声明在事件类上：

```python
class EmitPolicy(str, Enum):
    FAIL_FAST = 'fail_fast'            # Qt 语义：异常中断后续 handler，propagate 到调用方
    COLLECT_ERRORS = 'collect_errors'   # bubus 语义：异常记录到 event_results，继续执行后续 handler

class BaseEvent(EventBridge, Generic[T_Result]):
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST  # 默认 Qt 语义
```

使用示例：

```python
# 安全检查事件：handler 失败必须阻断后续操作
class SecurityCheckEvent(BaseEvent[bool]):
    # 继承默认 FAIL_FAST
    url: str

# 状态收集事件：多个 handler 各贡献一部分，一个失败不影响其他
class BrowserStateRequestEvent(BaseEvent[BrowserStateSummary]):
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.COLLECT_ERRORS
    include_dom: bool = True
```

**EmitPolicy 与 consume() 正交：**

| 机制 | 触发条件 | 效果 |
|------|---------|------|
| `consume()` | handler 主动调用 | 后续 handler 不执行，无异常 |
| FAIL_FAST + 异常 | handler 意外抛异常 | 后续 handler 不执行，异常 propagate |
| COLLECT_ERRORS + 异常 | handler 意外抛异常 | 后续 handler **继续执行**，错误仅记录到 event_results |

**Queued handler 不受 EmitPolicy 影响**——ScopeEventLoop 始终 collect errors（handler 异常不能 crash event loop）。

#### ~~讨论点 2.2-B：事件类型匹配——isinstance vs 严格相等~~ ✅ 已解决

**决定：isinstance 子类匹配 + MRO 索引 + BaseEvent 拦截 + connect_all + conscribe 验证。**

**匹配机制：MRO 索引（非 flat scan）**

连接按 event type 分桶存储在 `_connections_by_type: dict[type, list[Connection]]`。
emit 时遍历 `type(event).__mro__` 收集匹配连接，等价于 isinstance 但复杂度为
O(MRO_depth × avg_connections_per_type)，MRO 通常 3-5 层。

```python
# isinstance 匹配通过 MRO 索引实现
class LifecycleEvent(BaseEvent[None], abstract=True): ...   # 分组节点
class SessionStartEvent(LifecycleEvent): ...                 # 叶子事件

scope.connect(LifecycleEvent, handler)                       # 匹配所有 LifecycleEvent 子类
scope.emit(SessionStartEvent())                              # handler 被触发
```

**BaseEvent 不可直接 connect：**

`connect(scope, BaseEvent, handler)` → TypeError。catch-all 用 `scope.connect_all(handler)`。
`connect_all` 存入独立的 `_catch_all_connections` 列表——BaseEvent 在 `_connections_by_type`
中永远不出现，框架不做它禁止用户做的事。introspection 一致。

**conscribe 在 connect 时的验证角色：**

conscribe 的 `__abstract__` 标记和 `EventRegistrar` 提供类型合法性验证：
- `__abstract__ = True`（由 conscribe metaclass 管理）→ 合法的分组节点（LifecycleEvent 等）
- `EventRegistrar.is_registered(name)` → 合法的具体事件类型
- 不满足任一 → TypeError（可能是非事件类型或拼写错误）

conscribe 在 emit 时不参与——MRO 遍历是 Python 原生机制，比任何 registrar 查找都快。
**conscribe 管"什么是合法的事件类型"（connect 时），MRO 管"哪些连接匹配"（emit 时）。**

**exact match：不提供内置工厂，lambda 足够。**

```python
scope.connect(NavEvent, handler, filter=lambda e: type(e) is NavEvent)
```

exact match 是极少数场景。如果频繁需要，说明事件层次设计可能有问题。文档给 lambda 示例即可。

**Phase 4 可扩展：**
- 非 abstract 中间类被 connect 时 warn
- 新子类注册时检查是否影响已有连接（通过 conscribe metaclass hook）

**注意：** Qt 和 bubus 都用 exact match（Qt 用枚举，bubus 用字符串）。isinstance 是我们的增强设计。

#### ~~讨论点 2.2-C：ContextVar 父子追踪~~ ✅ 已解决

**决定：2 个 ContextVar（比 bubus 的 3 个少一个）。**

```python
# _context.py
_current_event: ContextVar[BaseEvent | None] = ContextVar('_current_event', default=None)
_current_connection: ContextVar[Connection | None] = ContextVar('_current_connection', default=None)
```

不需要 bubus 的 `inside_handler_context`——`_current_event.get() is not None` 等价。

**emit() 完整实现（含 EmitPolicy + ContextVar + _record helper）：**

```python
def emit(self, event: BaseEvent) -> BaseEvent:
    policy = type(event).emit_policy

    # 自动设置 parent
    parent = _current_event.get()
    if parent is not None:
        event.event_parent_id = parent.event_id

    token = _current_event.set(event)
    try:
        for conn in connections:
            if effective_mode == DIRECT:
                conn_token = _current_connection.set(conn)
                try:
                    result = conn.handler(event)
                    _record(event, conn, result=result)
                except Exception as e:
                    _record(event, conn, error=e)
                    if policy == EmitPolicy.FAIL_FAST:
                        raise                      # FAIL_FAST: propagate
                    # COLLECT_ERRORS: 继续下一个 handler
                finally:
                    _current_connection.reset(conn_token)

                if event.consumed:
                    break

            elif effective_mode == QUEUED:
                event._increment_pending()
                target_loop.enqueue(event, conn)
    finally:
        _current_event.reset(token)

    return event
```

**Direct handler 执行时间监测（设计评审后补充）：**

`_dispatch_direct` 在 handler 执行前后记录 `time.monotonic()`，超过 100ms 阈值输出 warning：

```python
handler_name = get_handler_name(conn.handler)
t0 = time.monotonic()
try:
    result = conn.handler(event)
    ...
finally:
    elapsed = time.monotonic() - t0
    if elapsed > _DIRECT_HANDLER_WARN_THRESHOLD:  # 0.1s
        logger.warning(
            'Direct handler %s took %.3fs (>100ms) on scope %r for %s',
            handler_name, elapsed, self.scope_id, type(event).__name__,
        )
```

不阻断执行，仅暴露违规行为。配合 typing 约束（`handler: Callable[[BaseEvent], T_Result]` 而非 `Awaitable`），pyright strict mode 下可在编译期拦截 `async def` handler。

**Queued handler 的 ContextVar 处理：** ScopeEventLoop 在 `_execute_handler` 中直接
`_current_event.set(event)` 设置当前事件。不需要 `copy_context()`——parent_id 在 emit() 时已确定，
Queued handler 内部 re-emit 时的 parent 是它正在处理的 event，不是最初 enqueue 时的 context。

**测试清单（`test_step_2_2_scope_emit.py`）：**

```
# Direct 派发
- test_direct_handler_executes_synchronously    # emit() 返回时 handler 已完成
- test_direct_handler_must_be_sync              # async handler + Direct → TypeError
- test_direct_handler_result_recorded           # event_results 有结果
- test_direct_handler_exception_recorded_and_propagated  # FAIL_FAST: 异常记录 + propagate

# EmitPolicy
- test_fail_fast_stops_on_exception             # FAIL_FAST: 异常中断后续 handler
- test_collect_errors_continues_on_exception    # COLLECT_ERRORS: 异常记录，继续执行
- test_collect_errors_all_errors_in_results     # 所有错误都在 event_results 中
- test_emit_policy_inherited_from_event_class   # ClassVar 从事件类读取

# has_pending
- test_has_pending_false_when_no_queued          # 纯 Direct 场景
- test_has_pending_true_when_queued_enqueued     # 有 Queued handler 入队

# Priority
- test_handlers_execute_by_priority_descending  # priority=100 先于 priority=0
- test_same_priority_preserves_registration_order

# Consume
- test_consume_stops_subsequent_handlers        # consume 后跳过低优先级 handler
- test_consume_only_affects_current_emit        # 不影响下次 emit
- test_consume_orthogonal_to_emit_policy        # consume 在 COLLECT_ERRORS 下也生效

# Filter
- test_filter_skips_non_matching_events
- test_filter_passes_matching_events

# MRO 索引匹配
- test_subclass_event_matches_parent_connection   # LifecycleEvent 连接匹配 SessionStartEvent
- test_exact_type_also_matches                    # SessionStartEvent 连接匹配自身
- test_mro_collects_connections_at_each_level     # 多层 MRO 各层的连接都被收集
- test_connect_base_event_raises_type_error       # connect(BaseEvent, ...) → TypeError
- test_connect_all_matches_all_events             # connect_all 匹配任何事件类型
- test_catch_all_not_in_connections_by_type       # _catch_all_connections 独立存储
- test_catch_all_respects_priority                # catch_all handler 参与 priority 排序
- test_connect_validates_event_type               # 非事件类型 → TypeError（conscribe 验证）

# Auto 模式
- test_auto_same_scope_uses_direct
- test_auto_cross_scope_uses_queued
- test_auto_no_target_scope_uses_direct

# ContextVar 父子追踪
- test_parent_id_set_when_emit_inside_handler
- test_no_parent_id_when_top_level_emit
```

---

### Step 2.3 — ScopeEventLoop（Queued 事件处理循环）

**产出文件：** `src/agent_cdp/scope/event_loop.py`

**核心 API：**

```python
class ScopeEventLoop:
    """一个 scope 的 Queued 事件处理循环。

    作为独立的 asyncio Task 运行，从 queue 中取出 (event, connection) 对，
    执行 handler 并 record_result。
    """

    _queue: asyncio.Queue[tuple[BaseEvent, Connection]]
    _task: asyncio.Task | None
    _running: bool

    async def start(self) -> None:
        """启动事件循环 task。"""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self, drain: bool = True) -> None:
        """停止事件循环。

        drain=True: 处理完队列中所有剩余事件后停止。
        drain=False: 立即停止，丢弃剩余事件（对每个丢弃的 event 调用 decrement_pending）。
        """
        self._running = False
        if drain:
            await self._queue.join()        # 等待队列清空
        else:
            self._discard_remaining()       # 丢弃 + decrement
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def enqueue(self, event: BaseEvent, connection: Connection) -> None:
        """将 (event, connection) 入队。同步调用（由 emit() 调用）。"""
        self._queue.put_nowait((event, connection))

    async def _run(self) -> None:
        """事件循环主体。"""
        while self._running:
            try:
                event, conn = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue                    # 定期检查 _running 标志
            try:
                await self._execute_handler(event, conn)
            finally:
                self._queue.task_done()

    async def _execute_handler(self, event: BaseEvent, conn: Connection) -> None:
        """执行一个 Queued handler。

        1. 设置 ContextVar（parent 追踪）
        2. await handler(event)
        3. record_result
        4. decrement_pending
        """
        token = _current_event.set(event)
        conn_token = _current_connection.set(conn)
        try:
            result = await conn.handler(event)
            event.record_result(conn.id, get_handler_name(conn.handler), result)
        except Exception as e:
            event.record_result(conn.id, get_handler_name(conn.handler), error=e)
        finally:
            _current_connection.reset(conn_token)
            _current_event.reset(token)
            event._decrement_pending()      # 可能触发 _completion.set()
```

**设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| stop() 默认行为 | **drain=True** | 安全优先。默认处理完剩余事件确保不丢数据。显式 drain=False 用于崩溃场景 |
| 循环退出机制 | **timeout + _running 标志** | `await queue.get()` 是无限阻塞的，用 `wait_for(timeout=1.0)` 定期检查停止标志 |
| handler 异常处理 | **捕获，record_result(error=e)** | Queued handler 的异常不应 crash 事件循环。异常记录在 EventResult 中，调用方通过 `raise_if_any` 获取 |
| 背压控制 | **maxsize=1024 + drop-newest**（设计评审后补充） | safe by default。满队列时丢弃新事件 + warning（含 queue size 和 event type） + `_decrement_pending`（避免 `await event` 挂起）。`emit()` 是 sync 函数无法 block，需要无界队列时调用方显式 `maxsize=0` |

**测试清单（`test_step_2_3_event_loop.py`）：**

```
- test_queued_handler_executes_async           # handler 在 emit() 返回后执行
- test_queued_handlers_execute_in_order        # 同 scope 内按入队顺序
- test_queued_handler_result_recorded
- test_queued_handler_exception_recorded       # 不 crash loop
- test_await_event_waits_for_queued            # await event 等待 Queued 完成
- test_stop_drain_processes_remaining
- test_stop_no_drain_discards_remaining
- test_mixed_direct_queued                     # Direct 立即完成，Queued 异步完成
- test_context_var_parent_tracking_in_queued   # Queued handler 内 emit 的 child 有 parent_id
```

---

## Phase 3: Scope 生命周期与并发（Step 3.1 – 3.2）

> 目标：多 scope 管理、广播、auto-disconnect、并发验证。

---

### Step 3.1 — ScopeGroup + broadcast + connect_all_scopes

**产出文件：** `src/agent_cdp/scope/group.py`

**核心 API：**

```python
class ScopeGroup:
    """管理一组 EventScope，提供便捷的连接管理和广播。"""

    group_id: str
    _scopes: dict[str, EventScope]

    def create_scope(self, scope_id: str, **metadata) -> EventScope:
        """创建新 scope 并启动其事件循环。"""
        ...

    async def close_scope(self, scope_id: str) -> None:
        """关闭 scope 并自动断开所有相关连接。"""
        ...

    def get_scope(self, scope_id: str) -> EventScope:
        """获取 scope，不存在则 raise KeyError。"""
        ...

    def broadcast(
        self,
        event: BaseEvent,
        *,
        exclude: set[str] | None = None,
    ) -> list[BaseEvent]:
        """向所有 scope 广播事件（每个 scope 收到深拷贝）。

        exclude: 排除的 scope_id 集合。
        返回每个 scope 收到的 event 副本列表。
        """
        results = []
        for scope_id, scope in self._scopes.items():
            if exclude and scope_id in exclude:
                continue
            copy = event.model_copy(deep=True)
            scope.emit(copy)
            results.append(copy)
        return results

    def connect_all_scopes(
        self,
        event_type: type[BaseEvent],
        handler: Callable,
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: EventScope | None = None,
        priority: int = 0,
        filter: Callable[[BaseEvent], bool] | None = None,
    ) -> list[Connection]:
        """将 handler 连接到所有 scope 的指定事件类型。

        注意：这是 ScopeGroup 级别的"所有 scope"，
        不同于 EventScope.connect_all() 的"所有事件类型"。
        """
        ...

    @property
    def scope_ids(self) -> list[str]: ...

    @property
    def scope_count(self) -> int: ...

    async def close_all(self) -> None:
        """关闭所有 scope。"""
        ...
```

**设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| broadcast 用深拷贝 | **model_copy(deep=True)** | 每个 scope 独立处理，结果互不可见（proposal §9 已论证） |
| broadcast 支持 exclude | **exclude: set[str]** | 常见场景：crash recovery 不需要通知已崩溃的 scope |
| create_scope 自动启动 event loop | **是** | scope 创建即可用，不需要显式 start |

**测试清单（`test_step_3_1_scope_group.py`）：**

```
- test_create_scope_and_retrieve
- test_close_scope_removes_from_group
- test_broadcast_reaches_all_scopes
- test_broadcast_deep_copies_event         # 各 scope 的 event 独立
- test_broadcast_exclude_skips_scope
- test_connect_all_scopes_connects_to_every_scope
- test_close_all_closes_every_scope
- test_scope_count_and_ids
```

---

### Step 3.2 — auto-disconnect + 并发验证

**产出文件：** 完善 `scope.py` 和 `connection.py`，加集成测试

**核心验证：**

```python
# --- auto-disconnect ---
# close scope 后，所有连接（outgoing + incoming）自动断开

tab1 = group.create_scope('tab1')
tab2 = group.create_scope('tab2')

# tab1 → handler_a（outgoing）
conn_out = tab1.connect(NavEvent, handler_a)
# tab2 → monitor（incoming to tab1's handler）
conn_in = connect(tab2, NavEvent, tab1_handler, target_scope=tab1)

await group.close_scope('tab1')

assert not conn_out.active   # outgoing 断开
assert not conn_in.active    # incoming 断开
# tab2.emit(NavEvent) 不再触发 tab1_handler

# --- 并发验证 ---
# 多个 scope 同时 emit，互不干扰

results = await asyncio.gather(
    emit_and_await(tab1, NavEvent(url='a')),
    emit_and_await(tab2, NavEvent(url='b')),
    emit_and_await(tab3, NavEvent(url='c')),
)
# 各 scope 的 handler 独立执行，结果互不混淆
```

**测试清单（`test_step_3_2_lifecycle.py`）：**

```
# auto-disconnect
- test_close_scope_disconnects_outgoing
- test_close_scope_disconnects_incoming
- test_emit_after_close_raises_or_noop
- test_handler_refs_released_after_close    # weakref + GC 验证

# 并发
- test_parallel_emit_across_scopes          # asyncio.gather 多 scope 同时 emit
- test_no_global_lock_bottleneck            # 验证无共享队列
- test_concurrent_scope_creation_and_close  # 同时创建和关闭 scope

# ContextVar 跨 scope 交替链（设计评审后补充）
- test_cross_scope_direct_queued_alternating_chain   # Direct→Queued→Direct 交替链中 parent_id 验证
```

---

## Phase 4: 高级能力（Step 4.1 – 4.2）

> 目标：expect、超时、WAL、循环检测。

---

### Step 4.1 — expect() + per-handler 超时 + 死锁监测

**产出文件：** `src/agent_cdp/advanced/expect.py`, `src/agent_cdp/advanced/timeout.py`

#### expect()

```python
class EventScope:
    async def expect(
        self,
        event_type: type[T_Event],
        *,
        include: Callable[[T_Event], bool] = lambda _: True,
        exclude: Callable[[T_Event], bool] = lambda _: False,
        timeout: float | None = None,
    ) -> T_Event:
        """等待本 scope 中下一个匹配的事件。

        实现：创建临时 Direct 连接（高优先级），handler 内部 set 一个 asyncio.Event。
        expect 返回后自动 disconnect 临时连接。
        """
        ...
```

**实现策略：**

```python
async def expect(self, event_type, *, include, exclude, timeout):
    future: asyncio.Future[T_Event] = asyncio.get_running_loop().create_future()

    def _waiter(event: T_Event) -> None:
        if not future.done() and include(event) and not exclude(event):
            future.set_result(event)

    conn = self.connect(
        event_type, _waiter,
        mode=ConnectionType.DIRECT,
        priority=_EXPECT_PRIORITY,  # 非常低，不干扰正常 handler
    )
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        conn.disconnect()
```

#### per-handler 超时

在 `ScopeEventLoop._execute_handler` 中增加 `asyncio.wait_for` + 死锁监测 task（proposal §7.6）。

**测试清单（`test_step_4_1_expect_timeout.py`）：**

```
# expect
- test_expect_returns_matching_event
- test_expect_with_include_filter
- test_expect_with_exclude_filter
- test_expect_timeout_raises
- test_expect_auto_disconnects

# per-handler 超时
- test_handler_timeout_records_error
- test_handler_timeout_cancels_children
- test_deadlock_warning_logged               # 15s 后 warning

# Direct handler 无框架级超时
- test_direct_handler_no_timeout_enforcement
```

---

### Step 4.2 — EventLog + 循环检测 + 事件历史

**产出文件：** `src/agent_cdp/advanced/event_log.py`（原名 `wal.py`，设计评审后更正）, `src/agent_cdp/advanced/cycle_detect.py`

#### EventLog（原名 WAL，设计评审后更正）

> **命名变更理由：** 当前实现是 write-behind（事件完成后写入），不是 write-ahead（handler 执行前写入）。命名为 EventLog 避免概念混淆。browser agent 场景下 crash recovery 靠 CDP session reconnect，不需要 event replay。如果未来需要真正的 write-ahead 语义（deterministic replay debugging），可在此基础上扩展。

```python
class EventLogWriter:
    """Per-scope JSONL 事件日志。"""

    def __init__(self, path: Path): ...

    async def write(self, event: BaseEvent) -> None:
        """事件完成后追加写入 JSONL。"""
        ...

    async def read_all(self) -> list[BaseEvent]:
        """读取 EventLog 文件中的所有事件（使用 EventRegistrar 反序列化）。"""
        for line in lines:
            event_type_name = json.loads(line)['event_type']
            event_cls = EventRegistrar.get(event_type_name)  # ← conscribe 价值点
            events.append(event_cls.model_validate_json(line))
        ...
```

**这里是 conscribe 的第二个核心价值点：** EventLog 反序列化时，通过 `EventRegistrar.get(name)` 按事件类型名查找对应的 Python class，无需维护手动映射表。

#### 循环检测

```python
_MAX_DIRECT_DEPTH = 16  # Direct 连接链最大深度

_emit_depth: ContextVar[int] = ContextVar('_emit_depth', default=0)

def emit(self, event):
    depth = _emit_depth.get()
    if depth >= _MAX_DIRECT_DEPTH:
        raise RecursionError(
            f'Direct emit depth {depth} exceeds limit {_MAX_DIRECT_DEPTH}. '
            f'Possible cycle in Direct connections.'
        )
    token = _emit_depth.set(depth + 1)
    try:
        ...
    finally:
        _emit_depth.reset(token)
```

**测试清单（`test_step_4_2_event_log_cycle.py`）：**

```
# EventLog
- test_event_log_write_jsonl_format
- test_event_log_read_all_deserializes          # 使用 EventRegistrar 反序列化
- test_event_log_per_scope_isolation            # 不同 scope 不同文件
- test_event_log_empty_file

# 循环检测
- test_direct_cycle_raises_recursion_error
- test_queued_does_not_trigger_cycle_check   # Queued 入队不增加深度
- test_depth_limit_configurable

# 事件历史
- test_scope_event_history_records
- test_event_history_max_size
```

---

## 交付顺序与依赖关系

```
Step 1.1 EventResult ─────────────────────────────┐
Step 1.2 BaseEvent + conscribe ───────────────────┤
Step 1.3 聚合方法 ────────────────────────────────┤
                                                   │
Step 2.1 ConnectionType + Connection ──────┐      │
Step 2.2 EventScope.emit() ────────────────┤      │
Step 2.3 ScopeEventLoop ──────────────────┤      │
                                           │      │
Step 3.1 ScopeGroup ──────────────────────┤      │
Step 3.2 auto-disconnect + 并发 ──────────┤      │
                                           │      │
Step 4.1 expect + timeout ────────────────┤      │
Step 4.2 WAL + cycle detect ──────────────┘      │
                                                   │
                      所有 Step 依赖 Phase 1 ←─────┘
```

**每个 Step 独立可测试。Step 间的依赖是线性的（同 Phase 内）或只依赖 Phase 1（跨 Phase）。**

---

## 关键技术风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| ~~Pydantic + conscribe metaclass 冲突~~ | ~~Step 1.2 无法启动~~ | ✅ **已解决**：conscribe 0.4 `bridge(BaseModel)` + `skip_pydantic_generic=True` |
| ~~asyncio.Event 在非 async 上下文初始化~~ | ~~BaseEvent 无法在模块级创建~~ | ✅ **已解决**：Python ≥3.11 中 `asyncio.Event()` 不需要 running loop。采用 `PrivateAttr(default_factory=_make_set_event)` 构造时创建，默认 set。`model_copy` 通过 `__deepcopy__` 重建 |
| Direct handler 内部意外 await | 违反同步语义 | `emit()` 中检查 `isawaitable(result)` 并 raise TypeError |
| frozen dataclass 的 disconnect mutation | 不符合 frozen 约定 | `object.__setattr__` 或改为非 frozen + slots |

**已缓解的风险（设计评审后）：**

| 风险 | 影响 | 缓解 |
|------|------|------|
| BaseEvent God Object | Step 1.2-1.3 | 聚合方法提取为自由函数（aggregation.py），BaseEvent ~300 → ~158 行 |
| Queued 队列无界 OOM | Step 2.3 | ScopeEventLoop 默认 maxsize=1024，满队列 drop-newest + warning |
| Direct handler 执行时间无监控 | Step 2.2 | time.monotonic() 监测，>100ms 输出 warning |
| WAL 命名与实际语义不符 | Step 4.2 | 重命名为 EventLog（write-behind 不是 write-ahead） |
| ContextVar 跨 scope 交替链 | Step 3.2 | 验证正确，补充集成测试 |

---

## 开始开发的下一步

1. Step 1.1：实现 EventResult（最简单，无依赖）
2. Step 1.2：实现 BaseEvent + conscribe bridge 集成
3. Step 1.3：实现 6 种结果聚合方法
4. 逐 Step 推进，每个 Step 先写测试（TDD）
