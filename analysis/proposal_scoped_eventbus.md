# Proposal: Scoped Event System for Agent-Era Browser Pipeline

> 一个新的事件驱动包，借鉴bubus的事件模型和Qt的连接拓扑，
> 为多agent共享CDP、监控与执行分离等场景提供tab-level isolation、concurrent dispatch、shared observation。

## 1. 设计目标

### 目标场景

| 场景 | 需要什么 |
|------|---------|
| 多agent各操作自己的tab | 隔离的事件scope，独立的事件循环 |
| 同时导航5个tab | scope间并行dispatch，无全局串行瓶颈 |
| Chrome崩溃通知所有agent | 全局事件广播到所有scope |
| 监控tab + 执行tab分离 | 两个scope独立运行，监控scope观察执行scope的事件 |
| CDP事件零延迟处理（弹窗、崩溃） | Direct模式同步执行handler，不经过队列 |
| 一个handler监听多个tab的事件 | 多对多连接拓扑 |

### 核心设计原则

1. **连接（Connection）是一等公民**——借鉴Qt signal/slot，发布者和订阅者之间的关系通过显式连接建立，支持多对多
2. **Direct/Queued/Auto三种连接类型**——借鉴Qt ConnectionType，让使用者在零延迟和有序性之间按需选择
3. **Scope是隔离和并发的单位**——每个scope有独立的事件循环，不同scope真正并行
4. **保留bubus的事件模型优势**——泛型事件结果、结果聚合、超时、EventLog

---

## 2. 概念模型

### Qt vs bubus vs 本设计

```
Qt:      Signal ──connect()──→ Slot        (点对点连接，无中心bus)
bubus:   Publisher ──→ Bus ──→ Subscriber  (中心bus中介，单队列)
本设计:   Source ──connect()──→ Handler     (Qt式连接拓扑)
         + Scope内EventLoop保证有序性       (bubus式队列处理)
```

**关键区别：**

| 维度 | bubus | Qt | 本设计 |
|------|-------|----|--------|
| 连接拓扑 | N:1:M（N个publisher → 1个bus → M个handler） | N:M（多对多直连） | **N:M（多对多显式连接）** |
| 派发模式 | 仅Queued | Direct/Queued/Blocking/Auto/Unique/SingleShot | **Direct/Queued/Auto** |
| 并发 | 全局锁串行 | 按线程亲和性auto选择 | **per-scope并行** |
| 事件传播控制 | 无 | accept/ignore | **handler返回值控制（见2.5）** |

---

## 3. 核心API设计

### 3.1 ConnectionType

```python
from enum import Enum

class ConnectionType(Enum):
    DIRECT  = 'direct'    # handler在emit()调用栈内同步执行
    QUEUED  = 'queued'    # event入队到目标scope的事件循环，异步执行
    AUTO    = 'auto'      # 同scope → Direct，跨scope → Queued

class EmitPolicy(str, Enum):
    FAIL_FAST = 'fail_fast'            # 异常中断后续handler，propagate到调用方
    COLLECT_ERRORS = 'collect_errors'   # 异常记录到event_results，继续执行后续handler
```

**Direct模式语义：**
- handler在 `emit()` 的调用栈内同步执行，`emit()` 返回时handler已完成
- 适用于：安全检查（域名限制）、弹窗处理、崩溃响应——需要零延迟的场景
- handler抛出异常的行为由事件类的 `emit_policy` ClassVar 决定：
  - `FAIL_FAST`（默认）：异常记录到event_results且propagate到调用方，后续handler不执行
  - `COLLECT_ERRORS`：异常记录到event_results，后续handler继续执行
- handler返回值立即可用（不需要await）

**Queued模式语义：**
- event进入目标scope的asyncio.Queue，由scope的事件循环按序处理
- 适用于：DOM重建、截图、HAR记录——可以容忍排队延迟的场景
- handler在emit()返回后异步执行，调用方需要 `await event` 获取结果
- 保证同一scope内的handler执行顺序与event到达顺序一致

**Auto模式语义：**
- 发布者和handler在同一个scope → Direct
- 发布者和handler在不同scope → Queued
- 这是Qt `Qt::AutoConnection` 的对等设计

### 3.2 Connection（连接）

```python
class Connection:
    """发布者和订阅者之间的一条连接。"""

    id: str                                    # 连接唯一ID
    source_scope: 'EventScope'                 # 事件来源scope
    event_type: type[BaseEvent]                # 事件类型
    handler: Callable                          # 目标handler
    target_scope: 'EventScope | None'          # handler所属scope（None=无scope）
    mode: ConnectionType                       # 连接类型
    filter: Callable[[BaseEvent], bool] | None # 可选的事件过滤器
    priority: int                              # 执行优先级（高→低）

    def disconnect(self) -> None:
        """断开此连接。"""
        self.source_scope._remove_connection(self)
```

**多对多拓扑通过Connection集合自然实现：**

```python
# 一个source → 多个handler（扇出）
connect(tab1, NavigateToUrlEvent, security.check,  mode=DIRECT, priority=100)
connect(tab1, NavigateToUrlEvent, dom.rebuild,      mode=QUEUED, priority=0)
connect(tab1, NavigateToUrlEvent, har.record,       mode=QUEUED, priority=-10)

# 多个source → 一个handler（扇入）
connect(tab1, NavigateToUrlEvent, monitor.on_nav, mode=QUEUED)
connect(tab2, NavigateToUrlEvent, monitor.on_nav, mode=QUEUED)
connect(tab3, NavigateToUrlEvent, monitor.on_nav, mode=QUEUED)
# monitor.on_nav收到来自三个tab的导航事件

# 多个source → 多个handler（完全多对多）
for tab in [tab1, tab2, tab3]:
    connect(tab, CrashEvent, recovery.on_crash, mode=DIRECT)
    connect(tab, CrashEvent, logger.on_crash,   mode=QUEUED)
```

### 3.3 EventScope（事件作用域）

```python
class EventScope:
    """隔离的事件处理域。"""

    scope_id: str
    _connections_by_type: dict[type[BaseEvent], list[Connection]]  # MRO索引：按event type分桶
    _catch_all_connections: list[Connection]   # connect_all存这里，独立于_connections_by_type
    _incoming: list[Connection]               # 以本scope为target的所有连接
    _event_loop: ScopeEventLoop               # 该scope的Queued事件循环
    metadata: dict[str, Any]                  # 附加信息（如target_id）

    def connect(self, event_type, handler, *, mode=AUTO, ...):
        """以本scope为source创建连接。

        - event_type is BaseEvent → TypeError，用connect_all()代替
        - event_type须为__abstract__分组节点或EventRegistrar已注册的具体类
        """
        if event_type is BaseEvent:
            raise TypeError('Cannot connect to BaseEvent — use connect_all().')
        ...

    def connect_all(self, handler, *, mode=AUTO, priority=0, filter=None, ...):
        """显式catch-all：连接到所有事件类型。替代bubus的'*'。
        存入_catch_all_connections，BaseEvent在_connections_by_type中永远不出现。"""
        ...

    def _get_matching_connections(self, event: BaseEvent) -> list[Connection]:
        """MRO索引匹配 + catch_all。O(MRO_depth × conns_per_type)。"""
        matching = []
        for cls in type(event).__mro__:
            if cls in self._connections_by_type:
                matching.extend(c for c in self._connections_by_type[cls] if c.active)
        matching.extend(c for c in self._catch_all_connections if c.active)
        matching.sort(key=lambda c: c.priority, reverse=True)
        return matching

    def emit(self, event: BaseEvent) -> BaseEvent:
        """从本scope发布事件。sync函数——Direct handler同步完成，Queued handler入队。"""
        policy = type(event).emit_policy
        connections = self._get_matching_connections(event)

        for conn in connections:
            effective_mode = self._resolve_mode(conn)

            if effective_mode == ConnectionType.DIRECT:
                # 同步执行handler
                try:
                    result = conn.handler(event)
                    if isawaitable(result):
                        raise TypeError(
                            f'Direct handler {conn.handler} returned awaitable. '
                            f'Direct handlers must be synchronous.'
                        )
                    _record(event, conn, result=result)
                except Exception as e:
                    _record(event, conn, error=e)
                    if policy == EmitPolicy.FAIL_FAST:
                        raise                      # FAIL_FAST: propagate到调用方
                    # COLLECT_ERRORS: 继续下一个handler

                # 检查事件是否被消费（传播控制，与EmitPolicy正交）
                if event.consumed:
                    break

            elif effective_mode == ConnectionType.QUEUED:
                # 入队到目标scope的事件循环
                event._increment_pending()
                target_loop = conn.target_scope._event_loop
                target_loop.enqueue(event, conn)

        return event

    async def close(self) -> None:
        """关闭scope：停止事件循环，断开所有连接。"""
        await self._event_loop.stop()
        # 自动断开所有以本scope为source或target的连接
        for conns in self._connections_by_type.values():
            for conn in list(conns):
                conn.disconnect()
        for conn in list(self._catch_all_connections):
            conn.disconnect()
        for conn in list(self._incoming):
            conn.disconnect()
```

### 3.4 ScopeGroup（作用域组）

```python
class ScopeGroup:
    """管理一组EventScope，提供便捷的连接管理和广播。"""

    group_id: str
    scopes: dict[str, EventScope]

    def create_scope(self, scope_id: str, **metadata) -> EventScope:
        """创建新scope。"""
        scope = EventScope(scope_id=scope_id, metadata=metadata)
        self.scopes[scope_id] = scope
        return scope

    async def close_scope(self, scope_id: str) -> None:
        """关闭scope并自动断开所有相关连接。"""
        scope = self.scopes.pop(scope_id)
        await scope.close()                   # ← auto-disconnect

    def broadcast(self, event: BaseEvent, mode: ConnectionType = QUEUED) -> list[BaseEvent]:
        """向所有scope广播事件（每个scope收到独立副本）。"""
        results = []
        for scope in self.scopes.values():
            copy = event.model_copy(deep=True)
            scope.emit(copy)
            results.append(copy)
        return results

    def connect_all_scopes(
        self,
        event_type: type[BaseEvent],
        handler: Callable,
        mode: ConnectionType = AUTO,
        **kwargs,
    ) -> list[Connection]:
        """将handler连接到所有scope的指定事件类型。

        注意：这是ScopeGroup级别的"所有scope"，不同于EventScope.connect_all()的"所有事件类型"。
        """
        return [
            connect(scope, event_type, handler, mode=mode, **kwargs)
            for scope in self.scopes.values()
        ]
```

### 3.5 事件传播控制

借鉴Qt的 `accept()/ignore()`，但简化为单个标志：

```python
class BaseEvent(Generic[T]):
    consumed: bool = False

    def consume(self) -> None:
        """标记事件已被消费，阻止后续handler执行。"""
        self.consumed = True
```

**consume() 语义边界（设计评审后补充）：**

- `consume()` 仅影响当前 scope 上当前 `emit()` 的 dispatch chain
- broadcast 场景下，每个 scope 收到深拷贝——某个 scope 的 handler 调用 `consume()` 不影响其他 scope 的副本
- 同一 scope 的 Direct handler chain 内，`consume()` 是 in-place mutation，后续 handler 可见。这是有意设计：支持安全检查拦截（高优先级 handler 阻止低优先级 handler 执行）
- `consume()` 保持 in-place mutation 而非返回 sentinel 值，因为 `consume() + raise` 组合（安全检查拦截场景）无法用返回值表达

**传播控制与Direct模式配合使用：**

```python
def security_check(event: NavigateToUrlEvent) -> None:
    if not is_url_allowed(event.url):
        event.consume()                    # ← 标记消费
        raise NavigationBlocked(event.url)

# 连接：安全检查priority=100（最先执行），Direct模式
connect(tab1, NavigateToUrlEvent, security_check, mode=DIRECT, priority=100)
connect(tab1, NavigateToUrlEvent, do_navigate,    mode=DIRECT, priority=50)
connect(tab1, NavigateToUrlEvent, dom.rebuild,    mode=QUEUED, priority=0)
```

执行流程（`NavigateToUrlEvent` 默认 `emit_policy=FAIL_FAST`）：
1. `security_check` 先执行（priority=100），如果URL不允许 → `event.consume()` + 异常
2. 异常被记录到 `event_results`（方案Y），然后 propagate 到 `emit()` 调用方
3. `emit()` 检查 `event.consumed`，**跳过后续handler**（`do_navigate` 和 `dom.rebuild` 不执行）

**对比当前browser-use的做法：**
- 当前：SecurityWatchdog用 `raise ValueError` hack式"阻断"，但其他handler仍然执行
- 本设计：`event.consume()` + priority保证安全检查先执行且能真正阻止后续handler

**consume() 与 EmitPolicy 正交：**

| 机制 | 触发条件 | 效果 |
|------|---------|------|
| `consume()` | handler主动调用 | 后续handler不执行，无异常 |
| FAIL_FAST + 异常 | handler意外抛异常 | 后续handler不执行，异常propagate |
| COLLECT_ERRORS + 异常 | handler意外抛异常 | 后续handler**继续执行**，错误仅记录到event_results |

`consume()` 是有意的传播控制，`EmitPolicy` 是意外异常的处理策略——两个维度独立工作。

---

## 4. 并发模型

### 4.1 Per-Scope事件循环

每个EventScope有独立的 `ScopeEventLoop`（asyncio Task），处理该scope的Queued事件：

```python
class ScopeEventLoop:
    """一个scope的Queued事件处理循环。"""

    _queue: asyncio.Queue
    _task: asyncio.Task | None

    async def _run(self) -> None:
        while self._running:
            event, connection = await self._queue.get()
            await self._execute_handler(event, connection)
            self._queue.task_done()
```

**背压控制（设计评审后补充）：**

ScopeEventLoop 的 asyncio.Queue 默认 `maxsize=1024`（safe by default），防止高频事件（如 DOM mutation 从多个 tab 同时涌入）导致内存膨胀。满队列时采用 **drop-newest** 策略：

- 新事件被丢弃（不入队）
- 被丢弃事件的 `_pending_count` 立即 decrement（避免 `await event` 永久挂起）
- 输出 warning log，包含当前 queue size、maxsize、被 drop 的 event type，便于事后诊断
- 需要无界队列的场景，调用方显式设置 `maxsize=0`

```python
class ScopeEventLoop:
    def __init__(self, maxsize: int = 1024):  # safe by default
        self._queue = asyncio.Queue(maxsize=maxsize)

    def enqueue(self, event, connection):
        try:
            self._queue.put_nowait((event, connection))
        except asyncio.QueueFull:
            logger.warning(
                'Backpressure: dropping %s event (queue full, size=%d, maxsize=%d)',
                type(event).__name__, self._queue.qsize(), self._queue.maxsize,
            )
            event._decrement_pending()  # avoid deadlocking await
```

选择 drop-newest 而非 block 的原因：`emit()` 是 sync 函数，无法 `await queue.put()`。

**不同scope的ScopeEventLoop是独立的asyncio Task，天然并行。**

不需要全局锁，因为：
- 每个ScopeEventLoop只处理自己scope的Queued事件
- Direct handler在emit()调用栈内执行，不涉及跨scope共享状态
- 跨scope的Queued投递是 `queue.put_nowait()`——线程安全（asyncio.Queue是协程安全的）

### 4.2 Direct模式的并发安全

Direct handler在emit()的调用栈内同步执行。如果两个scope同时emit，各自的Direct handler在各自的asyncio Task中执行，互不干扰（asyncio是协作式并发，不会真正同时执行两个同步函数）。

**但跨scope的Direct连接需要注意：** 如果tab1_scope.emit()触发了一个Direct handler，该handler在tab1的上下文中执行。如果handler访问了tab2的状态，这是使用者的责任——框架不提供跨scope的状态保护。

```python
# 安全：handler只访问自己scope的状态
connect(tab1, NavEvent, tab1_security.check, mode=DIRECT)  # ✓

# 使用者责任：handler访问跨scope状态需自行加锁
connect(tab1, NavEvent, global_monitor.record, mode=DIRECT) # ⚠ 如果monitor有可变状态，需要自行保护
```

**建议：** 跨scope连接默认使用QUEUED或AUTO（Auto会自动选择QUEUED），确保handler在目标scope的事件循环中执行。只有确认handler是无状态的或只读的，才显式使用DIRECT跨scope连接。

---

## 5. 多对多连接拓扑

### 5.1 连接函数

```python
def connect(
    source: EventScope,
    event_type: type[BaseEvent],
    handler: Callable,
    *,
    mode: ConnectionType = AUTO,
    target_scope: EventScope | None = None,  # handler所属scope（用于Auto模式判断和lifecycle管理）
    priority: int = 0,                        # 高值先执行
    filter: Callable[[BaseEvent], bool] | None = None,
) -> Connection:
    """创建一条从source到handler的连接。"""
```

### 5.2 连接拓扑示例

**场景：多agent + 监控**

```
┌─────────────────────────────────────────────────────────┐
│ ScopeGroup (一个Chrome进程)                              │
│                                                         │
│  tab1_scope ──DIRECT──→ tab1_security.check             │
│      │       ──QUEUED──→ tab1_dom.rebuild                │
│      │       ──QUEUED──→ monitor.on_any_event (扇入)     │
│      │                                                   │
│  tab2_scope ──DIRECT──→ tab2_security.check             │
│      │       ──QUEUED──→ tab2_dom.rebuild                │
│      │       ──QUEUED──→ monitor.on_any_event (扇入)     │
│      │                                                   │
│  tab3_scope ──DIRECT──→ tab3_security.check             │
│      │       ──QUEUED──→ tab3_dom.rebuild                │
│      │       ──QUEUED──→ monitor.on_any_event (扇入)     │
│                                                         │
│  [broadcast] ──QUEUED──→ all scopes (CrashEvent)        │
└─────────────────────────────────────────────────────────┘
```

**扇入（fan-in）**：`monitor.on_any_event` 连接到3个scope的相同事件类型，接收来自所有tab的事件。monitor可以聚合跨tab信息（如"哪些tab正在加载"）。

**扇出（fan-out）**：每个tab的 `NavigateToUrlEvent` 扇出到安全检查（Direct）、DOM重建（Queued）、监控（Queued）。

**隔离**：tab1_dom只连接到tab1_scope。tab2的事件不会到达tab1_dom。

### 5.3 对比bubus的事件转发

| | bubus事件转发 | 本设计的多对多连接 |
|---|---|---|
| 机制 | `bus.on('*', other_bus.dispatch)` | `connect(source, EventType, handler, mode=...)` / `connect_all(handler)` |
| 事件对象 | 共享同一个引用 | 同scope内共享，跨scope广播深拷贝 |
| 结果聚合 | 所有bus的结果混在同一个dict | 每条连接的结果独立 |
| 粒度 | 全部事件类型（`'*'`） | 按事件类型精确连接 |
| 派发模式 | 仅Queued | Direct/Queued/Auto |
| 断开 | 无auto-disconnect | scope.close()自动断开所有相关连接 |

---

## 6. 解决的设计差距（Qt对比）

### 直接解决

**差距#1 — 仅Queued dispatch，无Direct模式**

`ConnectionType.DIRECT` 提供零延迟同步执行。handler在emit()调用栈内完成，不经过任何队列。

- CDP回调（弹窗、崩溃、下载开始）可以通过Direct连接直接触发handler，**不需要绕过事件系统直接注册CDP回调**
- 域名限制检查以Direct + priority=100连接，在导航handler执行前同步拦截
- 消除了browser-use中"双轨事件系统"的根源——所有事件都通过统一的连接机制处理

**差距#2 — 无事件传播控制（accept/ignore）**

`event.consume()` + handler priority实现了Qt accept/ignore的核心语义：
- priority高的handler先执行
- handler调用 `event.consume()` 后，后续handler被跳过
- 不需要Qt的ignore()——不调用consume()就是ignore

**差距#3 — 无事件过滤器链**

`connect()` 的 `filter` 参数提供per-connection过滤。`priority` 参数提供执行顺序控制。两者结合等价于Qt的事件过滤器链：

```python
# 断路器：高优先级catch-all + filter
tab1.connect_all(circuit_breaker,
        mode=DIRECT, priority=1000,
        filter=lambda e: e.event_type not in LIFECYCLE_EVENTS)

# exact match（不匹配子类）：用 lambda filter
tab1.connect(NavEvent, handler, filter=lambda e: type(e) is NavEvent)
```

**差距#4 — 无自动断连**

`scope.close()` 自动断开所有以该scope为source或target的Connection。生命周期管理从"手动清理"变为"scope级自动回收"：

```python
await group.close_scope('tab-A1B2')
# → scope的事件循环停止
# → 所有以tab-A1B2为source的连接断开（其他scope不再收到该tab的事件）
# → 所有以tab-A1B2为target的连接断开（该tab不再收到其他scope的事件）
# → handler引用释放，可被GC
```

**差距#5 — 无handler优先级**

`connect()` 的 `priority: int` 参数。同一个scope上的连接按priority降序排列后执行：

```python
connect(tab1, NavEvent, security_check, priority=100)  # 先执行
connect(tab1, NavEvent, do_navigate,    priority=50)   # 后执行
connect(tab1, NavEvent, dom_rebuild,    priority=0)    # 最后执行
```

### 间接解决

**差距#8 — 结果累积无短路**

`event.consume()` 实现了短路——先完成的handler可以consume事件，阻止后续handler执行。这比GObject的accumulator简单，但覆盖了最常见的短路场景（"第一个成功的handler消费事件"）。

### 不直接相关

| 差距 | 原因 | 备注 |
|------|------|------|
| #6 布尔标志vs状态机 | 属于上层应用的设计决策 | 可以用事件系统驱动状态转换，但不强制 |
| #7 属性变更手动dispatch | 属于上层应用的设计决策 | 可以用Python descriptor + emit()实现自动通知 |

---

## 7. 从bubus保留并增强的核心特性

bubus有6个Qt完全没有的domain-specific设计亮点。这些是agent浏览器场景下不可替代的能力，必须在新架构中完整保留。

### 7.1 BaseEvent[T] — 泛型事件结果的编译时类型安全

**bubus原设计：** 事件类通过泛型参数声明结果类型，`event_result()` 返回值的类型由泛型参数决定。

**本设计保留并适配：**

```python
def _make_set_event() -> asyncio.Event:
    e = asyncio.Event()
    e.set()
    return e

class BaseEvent(Generic[T_Result]):
    """泛型事件基类。T_Result声明handler应返回的结果类型。"""

    # --- 公共字段 ---
    event_id: str = Field(default_factory=uuid7str)
    event_timeout: float | None = 300.0
    consumed: bool = False                                 # ← 新增：传播控制
    event_parent_id: str | None = None                     # ← 保留：父子追踪
    event_results: dict[str, EventResult[T_Result]] = {}   # ← 保留：多handler结果

    # --- ClassVar（不序列化） ---
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST  # ← 新增：Direct handler异常策略

    # --- 内部状态（PrivateAttr，不序列化） ---
    _completion: asyncio.Event = PrivateAttr(default_factory=_make_set_event)  # 出生即set
    _pending_count: int = PrivateAttr(default=0)

    def consume(self) -> None:
        self.consumed = True

    @property
    def has_pending(self) -> bool:
        """是否有尚未完成的Queued handler。sync上下文调用方用此做知情决策。"""
        return self._pending_count > 0

    def record_result(
        self,
        *,                              # keyword-only，防止参数顺序错误
        connection_id: str,
        handler_name: str,
        result: T_Result | None = None,
        error: Exception | None = None,
    ) -> None:
        """记录一个handler的结果。由框架通过_record helper调用。"""
        ...

# 使用示例 — 类型安全 + EmitPolicy：
class ScreenshotEvent(BaseEvent[str]):              # 结果是base64 string
    full_page: bool = False

class BrowserStateRequestEvent(BaseEvent[BrowserStateSummary]):  # 结果是结构化对象
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.COLLECT_ERRORS  # 多handler各贡献一部分
    include_dom: bool = True

# pyright能检查：
event = ScreenshotEvent()
result: str = await event.event_result()                  # ✓ 类型正确
result: int = await event.event_result()                  # ✗ pyright报错
```

### 7.2 多handler结果聚合 — 6种聚合方式 + 冲突检测

**bubus原设计：** 这是bubus最核心的domain-specific创新。Qt的signal emit后无法收集slot返回值。bubus让多个handler各自返回结果，事件对象收集并提供6种聚合方式。

**设计评审后变更：聚合方法从 BaseEvent 方法提升为独立自由函数。**

原始设计将 6 种聚合方法作为 BaseEvent 的实例方法（`event.event_result()`）。设计评审指出 BaseEvent 承载了过多职责（God Object 风险）：同时是 Pydantic model、conscribe 注册节点、awaitable 对象、结果收集器、传播控制器、pending 状态机。

**变更后的架构：**
- BaseEvent 只保留数据载体 + 序列化 + pending 状态机 + awaitable + consume() 职责（~158 行）
- 6 种聚合方法提取到 `events/aggregation.py` 作为自由函数（~179 行）
- `HandlerError` 异常类移至 `aggregation.py`

```python
# 变更前（方法调用）：
result = await event.event_result()
merged = await event.event_results_flat_dict()

# 变更后（自由函数调用）：
from agent_cdp.events.aggregation import event_result, event_results_flat_dict
result = await event_result(event)
merged = await event_results_flat_dict(event)
```

Pending tracking（`_increment/_decrement_pending` + `_completion`）保留在 BaseEvent 上，因为 event 对象在多个 scope 之间流转（fan-in），pending 状态必须跟着 event 走。

**本设计保留，并适配Direct/Queued双模式：**

```python
class BaseEvent(Generic[T_Result]):

    def record_result(
        self,
        *,                              # keyword-only，防止参数顺序错误
        connection_id: str,
        handler_name: str,
        result: T_Result | None = None,
        error: Exception | None = None,
    ) -> None:
        """记录一个handler的结果。由框架通过_record helper调用。"""
        self.event_results[connection_id] = EventResult(
            handler_name=handler_name,
            connection_id=connection_id,
            result=result,
            error=error,
            status='failed' if error else 'completed',
        )

    # _record helper（住在scope包，集中Connection→primitives拆解）：
    # def _record(event, conn, *, result=None, error=None):
    #     event.record_result(
    #         connection_id=conn.id,
    #         handler_name=get_handler_name(conn.handler),
    #         result=result, error=error,
    #     )

    # ── 6种聚合方式（全部保留） ──

    async def event_result(
        self,
        timeout: float | None = None,
        raise_if_any: bool = True,
        raise_if_none: bool = True,
    ) -> T_Result | None:
        """取第一个非None结果。await确保所有handler（含Queued）完成。"""
        await self._wait_for_completion(timeout)
        ...

    async def event_results_list(self, ...) -> list[T_Result]:
        """所有结果组成list：[handler1_result, handler2_result, ...]"""
        ...

    async def event_results_by_handler_name(self, ...) -> dict[str, T_Result]:
        """按handler名分组：{'dom_watchdog': ..., 'screenshot_watchdog': ...}"""
        ...

    async def event_results_flat_dict(
        self,
        raise_if_conflicts: bool = True,      # ← 保留：冲突检测
        ...
    ) -> dict[str, Any]:
        """合并所有handler返回的dict：{**handler1_result, **handler2_result}
        如果两个handler返回了重叠的key，raise_if_conflicts=True时报错。"""
        ...

    async def event_results_flat_list(self, ...) -> list[Any]:
        """合并所有handler返回的list：[*handler1_list, *handler2_list]"""
        ...

    async def event_results_filtered(
        self,
        include: Callable[[EventResult], bool] = _is_truthy,
        ...
    ) -> dict[str, EventResult[T_Result]]:
        """自定义过滤后的结果集。"""
        ...
```

**在新架构中的使用场景（与bubus完全一致）：**

```python
# 多个handler各贡献一部分状态 → flat_dict合并
class BrowserStateRequestEvent(BaseEvent[BrowserStateSummary]):
    include_dom: bool = True
    include_screenshot: bool = True

# 连接：3个handler分别贡献dom、screenshot、downloads
connect(tab1, BrowserStateRequestEvent, dom_handler,        mode=QUEUED)
connect(tab1, BrowserStateRequestEvent, screenshot_handler,  mode=QUEUED)
connect(tab1, BrowserStateRequestEvent, downloads_handler,   mode=QUEUED)

# 使用：
event = tab1.emit(BrowserStateRequestEvent())
state = await event.event_results_flat_dict()
# state = {'dom_tree': ..., 'screenshot': ..., 'downloads': [...]}
#          ^^^^^^^^^^^^     ^^^^^^^^^^^^^^^     ^^^^^^^^^^^^^^^^
#          dom_handler贡献  screenshot贡献       downloads贡献
```

**Direct和Queued结果的统一等待：**

```python
# emit()返回时：Direct handler的结果已在event_results中
# Queued handler的结果还没有
event = tab1.emit(SomeEvent())

# has_pending 让sync上下文的调用方做知情决策
if event.has_pending:
    await event                          # 等待Queued handler完成
result = await event.event_result()      # 此时所有结果都可用
```

内部机制：event维护一个pending handler计数器（`_pending_count`，PrivateAttr）。
Queued handler入队时计数+1（`_increment_pending`，首次时 `_completion.clear()`），
执行完后计数-1（`_decrement_pending`，归零时 `_completion.set()`）。
Direct handler不参与计数——它在emit()调用栈内同步完成。
`_completion` 通过 `PrivateAttr(default_factory=_make_set_event)` 构造时默认set，
零handler/纯Direct场景 `await event` 立即返回。

### 7.3 Awaitable Event — 事件即Future

**bubus原设计：** `event = bus.dispatch(...)` 返回的event对象可以被await，等待所有handler完成后取结果。

**本设计保留，并增强Direct模式下的即时可用性：**

```python
# Queued模式：需要await
event = tab1.emit(NavigateToUrlEvent(url='...'))
await event                                     # 等待所有handler完成
result = await event.event_result()

# Direct模式：emit()返回即完成，has_pending为False
event = tab1.emit(SecurityCheckEvent(url='...'))
assert not event.has_pending                    # 纯Direct，无pending
assert event.event_results                      # 结果已可用

# 混合模式：Direct结果立即可用，has_pending指示需要await
event = tab1.emit(NavigateToUrlEvent(url='...'))
# Direct handler（如security check）已完成
assert event.has_pending                        # Queued handler在队列中
await event                                     # 等待Queued handler
```

**`has_pending` property：** 暴露 `_pending_count > 0`，让sync上下文的调用方做知情决策。
这是Qt `postEvent()` 的心智模型——投递后你知道它在队列里，但不在当前栈帧等。

**与bubus的差异：** bubus中 `dispatch()` 总是异步（入队后返回），必须 `await event` 才能确保handler执行。本设计中，如果所有连接都是Direct，`emit()` 返回时handler已完成——**不需要await即可取结果**。这是Direct模式带来的语义增强。

### 7.4 expect() — 声明式未来事件等待

**bubus原设计：** `await bus.expect(EventType, include=..., timeout=...)` 等待一个尚未发生的事件。

**本设计保留，提升为scope级别：**

```python
class EventScope:
    async def expect(
        self,
        event_type: type[T_Event],
        include: Callable[[T_Event], bool] = lambda _: True,
        exclude: Callable[[T_Event], bool] = lambda _: False,
        timeout: float | None = None,
    ) -> T_Event:
        """等待本scope中下一个匹配的事件。

        典型用法：dispatch一个action event后，等待对应的completion event。
        """
        ...
```

**使用场景：**

```python
# 导航后等待完成事件
tab1.emit(NavigateToUrlEvent(url='https://example.com'))

# 等待对应的NavigationCompleteEvent（可能由CDP回调触发）
complete = await tab1.expect(
    NavigationCompleteEvent,
    include=lambda e: e.url == 'https://example.com',
    timeout=30.0,
)
print(complete.status)  # 200

# 跨scope的expect：monitor scope等待任何tab的崩溃事件
crash = await monitor_scope.expect(CrashEvent, timeout=600.0)
print(f'Tab {crash.target_id} crashed!')
```

**expect + 扇入的组合：** 监控scope连接到多个tab的CrashEvent（扇入），然后 `expect()` 等待任意一个tab的崩溃。这在bubus中需要事件转发来实现，本设计通过多对多连接自然支持。

### 7.5 自动父子事件追踪 — 事件因果链

**bubus原设计：** handler内部dispatch的事件自动标记 `event_parent_id`，形成事件因果树。可通过 `event.event_children` 遍历。

**本设计保留，通过ContextVar实现跨Direct/Queued追踪：**

```python
# 上下文变量追踪当前正在处理的事件
_current_event: ContextVar[BaseEvent | None] = ContextVar('_current_event', default=None)
_current_connection: ContextVar[Connection | None] = ContextVar('_current_connection', default=None)
```

**追踪机制：**

```python
class EventScope:
    def emit(self, event: BaseEvent) -> BaseEvent:
        # 如果当前在一个handler内部（Direct或Queued），自动设置parent
        parent = _current_event.get()
        if parent is not None:
            event.event_parent_id = parent.event_id
            # 将child event记录到parent的当前handler结果中
            parent_conn = _current_connection.get()
            if parent_conn and parent_conn.id in parent.event_results:
                parent.event_results[parent_conn.id].event_children.append(event)

        # 设置当前事件上下文（Direct handler在此上下文中执行）
        token = _current_event.set(event)
        try:
            # ... 执行Direct handlers, 入队Queued handlers ...
            pass
        finally:
            _current_event.reset(token)

        return event
```

**可观测性价值（与bubus完全一致）：**

```python
# 追溯事件因果链
event = tab1.emit(NavigateToUrlEvent(url='...'))
await event

# 遍历事件树
for result in event.event_results.values():
    print(f'Handler: {result.handler_name}')
    for child in result.event_children:
        print(f'  → Child: {child.event_type} (status: {child.event_status})')
        for grandchild_result in child.event_results.values():
            for grandchild in grandchild_result.event_children:
                print(f'    → Grandchild: {grandchild.event_type}')

# 输出示例：
# Handler: security_check
#   → Child: SecurityCheckPassedEvent (status: completed)
# Handler: do_navigate
#   → Child: NavigationStartedEvent (status: completed)
#     → Grandchild: DOMContentLoadedEvent
#   → Child: NavigationCompleteEvent (status: completed)
```

**与bubus的差异：** bubus使用全局ContextVar（3个） + 全局锁来保证parent-child追踪的原子性。
本设计使用2个ContextVar（`_current_event` + `_current_connection`，不需要bubus的 `inside_handler_context`，
因为 `_current_event.get() is not None` 等价）。

- **Direct handler**：在emit()调用栈内执行，天然继承ContextVar。
- **Queued handler**：ScopeEventLoop在 `_execute_handler` 中直接 `_current_event.set(event)` 设置当前事件。
  不需要 `copy_context()`——`event_parent_id` 在emit()时已确定，Queued handler内部re-emit时的parent
  是它正在处理的event，不是最初enqueue时的context。
- **无全局锁**：不同scope的ContextVar在各自的asyncio Task中独立运行（协作式并发保证同一时刻只有一个协程执行）。

### 7.6 Per-Handler超时 + 死锁监测

**bubus原设计：** 每个handler执行有timeout控制。超过15秒发出死锁警告。超过 `event_timeout` 强制取消。

**本设计保留，按连接类型区分：**

```python
class ScopeEventLoop:
    async def _execute_handler(self, event: BaseEvent, connection: Connection) -> None:
        """在Queued事件循环中执行一个handler。"""

        timeout = event.event_timeout
        handler = connection.handler

        # 死锁监测task
        deadlock_monitor = asyncio.create_task(self._deadlock_warning(handler, delay=15.0))

        try:
            result = await asyncio.wait_for(
                handler(event),
                timeout=timeout,
            )
            _record(event, connection, result=result)
        except asyncio.TimeoutError:
            _record(event, connection, error=TimeoutError(
                f'Handler {get_handler_name(handler)} timed out after {timeout}s'
            ))
            # 取消该handler可能dispatch的所有pending child events
            self._cancel_pending_children(event, connection)
        except Exception as e:
            _record(event, connection, error=e)
        finally:
            deadlock_monitor.cancel()
            event._decrement_pending()  # 可能触发completion signal

    async def _deadlock_warning(self, handler: Callable, delay: float) -> None:
        await asyncio.sleep(delay)
        logger.warning(
            f'⚠️ Handler {get_handler_name(handler)} has been running for {delay}s — '
            f'possible deadlock or slow operation'
        )
```

**Direct模式的超时处理：**

Direct handler是同步执行的，不能被 `asyncio.wait_for` 控制。策略：

- Direct handler 不做框架级超时控制（同步函数无法被外部 cancel）
- Direct handler 应该是快速操作（安全检查、标志设置）；如果需要长时间运行，应使用 Queued 连接
- 文档明确约定：Direct handler 的执行时间应 < 100ms
- **运行时监测（设计评审后补充）：** `_dispatch_direct` 在 handler 执行前后记录 `time.monotonic()`，超过 100ms 阈值输出 warning log（包含 handler 名、scope ID、event 类型）。不阻断执行，仅暴露违规行为，便于开发阶段抓住第三方 handler 的性能问题
- **静态检查：** Direct handler 的类型签名为 `Callable[[BaseEvent], T_Result]`（非 `Awaitable`），pyright strict mode 下能在编译期拦截 `async def` handler

### 7.7 EventLog — Per-Scope 事件日志持久化

> **命名变更（设计评审后）：** 原名 WAL（Write-Ahead Logging）更正为 EventLog。当前实现是事件完成后追加写入（write-behind），不是 handler 执行前写入（write-ahead），因此是 Event Log 而非 WAL。如果未来需要真正的 crash recovery / deterministic replay debugging，可以在 EventLog 基础上增加 write-ahead 语义，但当前 browser agent 场景下 crash recovery 靠的是 CDP session reconnect 而非 event replay。

**bubus原设计：** 事件完成后以JSONL格式追加写入日志文件，用于审计追踪。

**本设计保留，per-scope独立配置：**

```python
class EventScope:
    def __init__(self, scope_id: str, event_log_path: Path | None = None, ...):
        self._event_log_path = event_log_path

    async def _event_log_write(self, event: BaseEvent) -> None:
        """事件完成后写入 EventLog。"""
        if not self._event_log_path:
            return
        event_json = event.model_dump_json()
        async with anyio.open_file(self._event_log_path, 'a') as f:
            await f.write(event_json + '\n')
```

每个scope可以有独立的日志文件（如 `tab1.jsonl`、`tab2.jsonl`），也可以共享一个文件（通过相同的 `event_log_path`）。

---

## 8. 完整概念模型总结

```
本设计 = Qt的连接拓扑 + bubus的事件结果模型 + 新的Scope隔离

来自Qt:
├── ConnectionType (Direct/Queued/Auto)
├── 多对多连接拓扑 (connect/disconnect)
├── 事件传播控制 (consume)
├── handler优先级 (priority)
├── 自动断连 (scope.close)
└── EmitPolicy (FAIL_FAST/COLLECT_ERRORS, 声明在事件类上)

来自bubus:
├── BaseEvent[T] 泛型结果类型安全
├── 6种结果聚合 — 自由函数 (event_result / flat_dict / flat_list / by_handler_name / list / filtered)
├── Awaitable事件 (await event)
├── expect() 声明式未来事件等待
├── 自动父子事件追踪 (event_parent_id / event_children)
├── Per-handler超时 + 死锁监测
└── EventLog 事件日志持久化

新增:
├── EventScope (隔离的事件处理域)
├── ScopeGroup (scope生命周期管理 + broadcast)
├── Per-scope事件循环 (无全局锁，真正并行)
├── broadcast() 深拷贝广播
├── MRO索引匹配 (isinstance子类匹配，O(MRO_depth))
├── connect_all() catch-all (替代bubus的'*'，独立存储路径)
├── conscribe验证 (connect时验证event_type合法性)
├── 背压控制 (ScopeEventLoop maxsize=1024, drop-newest)
└── Direct handler 执行时间监测 (>100ms warning)
```

---

## 9. 关键设计决策记录

### 为什么Connection是一等公民而不是Bus

bubus模型中，Bus是中心——publisher和subscriber都注册在bus上，bus是唯一的调度点。这在单scope场景下是简洁的，但在多scope场景下产生了问题：

- 跨scope通信需要事件转发（共享对象，混合结果）
- 一个handler想监听多个scope需要注册多次（或用wildcard转发）
- 无法对不同连接使用不同的派发模式

Qt证明了一个更好的模型：Connection是发布者和订阅者之间的显式链接，它携带了"如何派发"的语义（ConnectionType）。多个Connection自然形成多对多拓扑，不需要中心Bus的中介。

本设计保留了scope内的EventLoop（用于Queued处理的有序性保证），但将连接拓扑从"bus中介"提升为"显式Connection"。

### 为什么Direct handler必须同步

Qt的DirectConnection在信号emit的线程中同步执行slot。如果slot是异步的（async），在同步emit()调用栈内无法自然地await。

设计选择：
- Direct handler **必须是同步函数**（`def handler(event)`，不是 `async def`）
- 如果需要异步操作，使用Queued连接
- 如果Direct handler内部需要触发异步操作，它应该enqueue一个新事件而不是直接await

这保证了Direct模式的零开销承诺——emit()返回时所有Direct handler已经完成，调用方可以立即检查event结果。

### 为什么EmitPolicy声明在事件类上

Direct handler异常的处理有两种合理语义：
- FAIL_FAST：异常中断后续handler并propagate到调用方（Qt DirectConnection行为）
- COLLECT_ERRORS：异常记录到event_results，后续handler继续执行（bubus行为）

EmitPolicy作为ClassVar声明在事件类上（而非emit()参数或Connection属性），理由：
- **事件类型的作者最清楚语义**：SecurityCheckEvent天然FAIL_FAST，BrowserStateRequestEvent天然COLLECT_ERRORS
- **自文档化**：看事件定义就知道行为，不需要检查每个emit调用点
- **避免重复**：同一事件类型在不同emit调用点的行为应该一致

不放在Connection上，因为：
- 如果handler不够关键到不能crash emit chain，它大概率应该用Queued（Queued handler异常永远不propagate）
- Direct + 非关键的场景极少——Direct的存在意义就是零延迟的关键操作
- 如果未来需要connection-level的fine-grained控制，可以在Phase 4扩展，不影响现有API

### 为什么broadcast用深拷贝

broadcast的语义是"通知所有scope发生了一个全局事件"。每个scope独立处理这个事件，各自的处理结果不应互相可见：

- tab1的crash recovery结果不应出现在tab2的event对象中
- tab1的handler修改event字段不应影响tab2看到的event

深拷贝保证了scope间的完全隔离。成本（Pydantic model_copy）在全局事件（崩溃、断连）的频率下可忽略。

### 为什么聚合方法是自由函数而非 BaseEvent 方法

设计评审指出 BaseEvent 同时承载 Pydantic model、conscribe 注册节点、awaitable、结果收集器、传播控制器、pending 状态机六个职责，存在 God Object 风险。

将聚合方法提取为 `events/aggregation.py` 中的自由函数后：
- BaseEvent 从 ~300 行缩减到 ~158 行，职责清晰：数据载体 + 完成状态机
- 聚合逻辑可以独立修改（如更换策略、增加新聚合方式），不触及 BaseEvent 核心类
- pending tracking 保留在 BaseEvent 上——event 在多 scope 间流转（fan-in 场景），状态必须跟着 event 走

### 为什么 ScopeEventLoop 默认有界队列

基础设施代码应 safe by default。无界队列（`maxsize=0`）在高频事件场景下（DOM mutation 从 5 个 tab 同时涌入）可能导致 OOM。

- 默认 `maxsize=1024`，覆盖绝大多数正常场景（5 tabs × 200 pending events = 1000）
- 满队列时 drop-newest + warning log（包含 queue size 和 event type）
- 需要无界队列的场景由调用方显式 `maxsize=0`
- 选择 drop-newest 而非 block：`emit()` 是 sync 函数，无法 `await queue.put()`

---

## 10. 实施路径

### Phase 1：事件模型（bubus核心能力）

1. `BaseEvent[T]` — 泛型事件基类，含 `consumed` 字段、`event_parent_id`、`event_results` dict
2. `EventResult[T]` — handler结果容器，含 `event_children`、`status`、`error`
3. 6种结果聚合方法 — `event_result()`、`event_results_list()`、`event_results_by_handler_name()`、`event_results_flat_dict()`（含冲突检测）、`event_results_flat_list()`、`event_results_filtered()`
4. Awaitable event — `await event` 等待所有handler完成，pending计数器机制

验证：单handler返回结果、多handler结果聚合、flat_dict冲突检测、awaitable语义。

### Phase 2：连接与派发（Qt连接拓扑 + Direct模式）

1. `ConnectionType` — Direct/Queued/Auto枚举
2. `EmitPolicy` — FAIL_FAST/COLLECT_ERRORS枚举，声明在事件类ClassVar上
3. `Connection` — 连接对象（source, event_type, handler, mode, priority, filter）
4. `connect()` / `disconnect()` — 连接管理函数
5. `EventScope` — 事件作用域（emit, close, connection管理）
6. `ScopeEventLoop` — per-scope的Queued事件处理循环
7. `event.consume()` — 传播控制（与EmitPolicy正交）
8. `event.has_pending` — Queued handler状态查询
9. `_record` helper — Connection→primitives拆解，集中在scope包
10. 父子事件自动追踪 — 2个ContextVar（_current_event + _current_connection）

验证：Direct同步执行、Queued异步执行、Auto模式选择、priority排序、consume阻断传播、
FAIL_FAST异常propagate、COLLECT_ERRORS异常收集、has_pending查询、parent-child链。

### Phase 3：Scope生命周期与并发

1. `ScopeGroup` — scope生命周期管理
2. `broadcast()` — 深拷贝广播
3. `connect_all_scopes()` — 批量跨scope连接（ScopeGroup级别，区别于EventScope.connect_all的catch-all）
4. `scope.close()` — auto-disconnect
5. 多scope并行验证 — 无全局锁，真正并发

验证：广播到达所有scope、scope关闭后连接自动清理、并发scope互不干扰。

### Phase 4：高级能力

1. `expect()` — scope级别的声明式未来事件等待（含include/exclude/timeout）
2. per-handler超时 — Queued模式下 `asyncio.wait_for` + 死锁监测task
3. EventLog — per-scope JSONL 事件日志持久化（原名 WAL，设计评审后更正）
4. 循环检测 — Direct连接链深度限制
5. 事件历史 — per-scope `event_history` 用于调试和可观测性

验证：expect跨事件类型等待、handler超时取消child events、WAL写入恢复、循环检测阻止无限递归。
