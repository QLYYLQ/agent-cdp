# agent-cdp 架构缺陷与技术债分析

> 审查日期: 2026-03-19
> 审查范围: `src/agent_cdp/` 全部源码、12 个 demo 文件（`demo/` + 根目录 `demo_*.py`）、`other_framework/` 参考实现
> 审查方法: 静态架构分析，聚焦类型安全、并发模型、API 表面、内存管理、错误处理、测试覆盖

---

## 总览

agent-cdp 的核心设计（Direct/Queued/Auto 三模式、per-scope event loop、Qt 风格 N:M 连接拓扑）在架构层面是合理的。主要技术债集中在三个方向：

1. **泛型类型安全被 `Any` 全面侵蚀** — `BaseEvent[T_Result]` 的泛型参数在聚合层完全擦除
2. **Demo 层大量重复代码暴露了核心库缺少的抽象** — CDP 桥接、tab 创建、结果提取均无标准化方案
3. **关键防御路径缺乏测试覆盖** — 背压、filter 异常、并发 emit 等

以下按严重程度分级。

---

## HIGH — 阻碍生产使用的结构性问题

### H1. 泛型 `T_Result` 全面擦除

核心设计用 `BaseEvent[T_Result]` 定义了泛型结果类型，但在聚合层全部退化为 `Any`：

| 位置 | 问题 |
|------|------|
| `aggregation.py` 全部 7 个函数 | 签名 `event: Any -> Any`，无类型提示 |
| `base.py:59` `event_results` | `dict[str, Any]`（注释承认运行时应为 `dict[str, EventResult[T_Result]]`） |
| `connection.py:28` `handler` | `Callable[..., Any]`，无参数/返回类型约束 |
| `connection.py:26,29` scopes | `weakref.ref[Any]`，应为 `weakref.ref[EventScope]` |
| `connect()` 函数 `source` 参数 | `Any`，任何对象均可通过 |

**全源码约 55 处 `Any`**，多数为结构性而非装饰性。根源是为避免循环导入而选择 `Any` 作为逃生舱。

**影响**: 用户从 emit 到取结果的全链路无类型提示，pyright strict 模式形同虚设。

**建议**: 引入 `_protocols.py` 定义 `ScopeProtocol`（含 `_add_connection`、`_remove_connection` 等方法签名）；aggregation 函数改用 `TypeVar` + `overload`。

### H2. 无顶层公开 API

`src/agent_cdp/__init__.py` 只有一行 docstring，无 `__all__`、无 re-export。现状：

- 用户必须写 `from agent_cdp.scope import EventScope` 等深路径导入
- `_MAX_DIRECT_DEPTH`（带下划线前缀）却被 `advanced/__init__.py` re-export
- `connect()` 函数接受 `source: Any`，无任何运行时类型检查
- `ScopeEventLoop` 无公开导出，但通过 `scope._event_loop` 可访问（测试中大量使用）
- `EventRegistrar`/`EventBridge` 等 conscribe 内部实现被暴露，无公开/内部区分

**建议**: 在顶层 `__init__.py` 定义明确的 `__all__`，re-export 核心公开类型。

### H3. 缺少 CDP 事件桥接抽象

每个 watchdog 手动重复相同的桥接模式：

```python
# PopupsWatchdog._on_cdp_dialog (watchdogs.py:137-158)
cdp.on_event('Page.javascriptDialogOpening', callback)
# callback 中:
event = PopupDialogEvent(...)
scope.emit(event)
```

5 个 watchdog 各自独立实现，无 `CDPBridge` 或 `cdp_to_scope()` 工具函数。这是 demo 层最大的重复代码来源，也是最容易出错的地方（如 `advanced.py` 的 `Page.loadEventFired` 监听未按 `session_id` 过滤，导致跨 tab 竞态条件）。

**建议**: 提供标准桥接工具函数 `cdp_to_scope(cdp, method, event_factory, scope, session_id=None)`。

### H4. `event_timeout` 字段是装饰品

`BaseEvent.event_timeout: float | None = 300.0` 从未被强制执行：

- `aggregation.py` 的 `_wait_for_completion` 直接 `await event._completion.wait()`，无超时
- Demo 中 `await event` 无超时保护
- 如果 queued handler 静默失败或死锁，调用方永远挂起

**建议**: 在 `_wait_for_completion` 中用 `asyncio.wait_for(event._completion.wait(), timeout=event.event_timeout)`。

---

## MEDIUM — 影响可维护性和可靠性

### M1. Direct dispatch 的 TypeError 捕获过宽

`scope.py:237-243`:

```python
except TypeError:
    # Re-raise TypeErrors from async detection
    raise
```

意图是检测"async handler 被 Direct 调用"，但会误捕用户 handler 自身抛出的 `TypeError`。在 `COLLECT_ERRORS` 策略下，用户异常应被收集而非传播。

**建议**: 用特定的 sentinel 异常类（如 `AsyncHandlerError`）替代通用 `TypeError` 检测。

### M2. Queued handler 异常完全静默

`ScopeEventLoop._execute_handler`（event_loop.py:152-166）捕获所有异常后只写入 `event_results`，无日志输出。用户唯一发现方式是主动检查结果字典。Demo 中从无人检查 queued 结果中的错误。

**建议**: 添加 `logger.warning()` 或可配置的错误回调。

### M3. `Connection.filter` 异常会炸掉整个 emit 链

`scope.py:170`:

```python
if conn.filter is not None and not conn.filter(event):
    continue
```

无 try/except 包裹。filter 函数抛异常时，整个 dispatch 链崩溃，后续所有 handler 均不执行。

**建议**: 包裹 try/except，记录错误后 skip 该 connection。

### M4. Demo 层大量重复代码

| 重复项 | 出现次数 | 说明 |
|--------|---------|------|
| Chrome tab 创建 boilerplate | 5+ | `Target.createTarget` → attach → enable → create_scope |
| ANSI 输出辅助函数 | 6 | `banner`/`phase`/`ok`/`fail`/`info` 完全拷贝 |
| `OpTiming` + `bench_op` | 2 | `bench_cdp_vs_pw.py` 与 `bench_agentcdp_vs_pw.py` 完全重复 |
| `PWBench` 类 | 2 | 同上两个 benchmark 文件 |
| wait-for-load 逻辑 | 3+ | `bench.py`/`advanced.py`/`multi_tab.py` 各有不同实现 |

**建议**: 提取 `demo/_utils.py`（输出辅助）、`demo/_bench_utils.py`（OpTiming/PWBench/bench_op）、`demo/_chrome_utils.py`（tab 创建、wait-for-load）。

### M5. Watchdog 无统一协议

5 个 watchdog 都有 `attach()` 方法但签名不一致：

| Watchdog | `attach()` 签名 |
|----------|-----------------|
| `SecurityWatchdog` | `attach(scope)` |
| `PopupsWatchdog` | `attach(scope, session_id)` |
| `ScreenshotWatchdog` | `attach(scope, session_id)` |
| `CrashWatchdog` | `attach(scope)` |
| `CaptchaWatchdog` | `attach(scope, session_id)` |

无 `Watchdog` Protocol 或基类。每个 demo 重复手动 attach 循环。

**建议**: 定义 `Watchdog` Protocol，统一 `attach(scope, session_id=None)` 签名。

### M6. 跨 tab 竞态条件（advanced.py）

`advanced.py:121-133` 的 `Page.loadEventFired` 监听未按 `session_id` 过滤 — 任意 tab 的加载完成都可能错误触发另一个 tab 的导航等待 Future。对比 `multi_tab.py:138` 正确做了 `session_id` 校验。

**建议**: 所有 CDP 事件回调必须校验 `session_id`，或通过前面提出的 `cdp_to_scope()` 桥接自动过滤。

### M7. 缺少 `emit_and_wait()` 便利方法

每个 demo 重复相同 3 行模式：

```python
event = scope.emit(action)
await event
results = await event_results_list(event)
```

`demo_real_xhs.py` 自行提取了 `run_action()` 辅助函数。

**建议**: 在 `EventScope` 上提供 `async def emit_and_wait(event, timeout=None) -> list[EventResult]` 方法。

---

## MEDIUM-LOW — 测试与内存

### ML1. 关键路径缺测试覆盖

| 未覆盖路径 | 风险等级 | 说明 |
|-----------|---------|------|
| 背压（queue full → drop-newest） | 高 | `ScopeEventLoop.enqueue` QueueFull 分支未测试 |
| filter 函数抛异常 | 高 | 前面 M3 提到会炸掉 emit |
| 同 scope 并发 emit | 中 | 多 coroutine 同时 emit 到同一 scope |
| Auto 模式 target scope 被 GC | 低 | 防御分支 `_resolve_mode` 返回 DIRECT |
| `expect()` + `consume()` 交互 | 中 | 高优先级消费后低优先级 expect handler 仍触发 |
| 3+ 层 MRO 继承匹配 | 低 | 菱形继承场景 |
| `EventLogWriter` 并发写入 | 低 | 多 coroutine 同时追加可能交错 JSON 行 |

### ML2. 内存管理隐患

| 隐患 | 说明 |
|------|------|
| `_event_history` 保留大对象 | 默认 1000 事件，每个携带全部 `event_results`（可能含 DOM 快照等大对象），截断用 O(N) 切片 |
| `EventResult.error` 持有异常 | 异常对象保留完整 traceback 帧链，可能拖住整个调用栈 |
| `Connection` 断开后的引用 | disconnect 后如外部仍持有 `conn` 变量，handler callable 不会被 GC |
| 未启动的 event loop | `ScopeEventLoop` 未 start 时 enqueue 的事件永不处理，对象堆积 |

---

## LOW — 风格与小问题

### L1. CDPClient 非生产质量

Demo 中的 `CDPClient`（`demo/cdp_client.py`）会被用户直接复制使用：

- 无重连逻辑，WebSocket 断开后所有 pending future 挂死（30s 超时后才失败）
- `asyncio.create_task` fire-and-forget，CDP handler 异常静默丢失
- `_msg_id` 自增非原子操作（单线程 asyncio 下安全，但非保证）

### L2. 基准测试方法论不一致

| 问题 | 文件 |
|------|------|
| "Key Insight" 是硬编码字符串非计算值 | `bench_agentcdp_vs_pw.py:694-698` |
| 无 warmup 阶段 | `bench.py` |
| GC 禁用策略不一致 | `bench_cdp_vs_pw.py` 禁用，`bench.py` 未禁用 |
| 并发调度优势未在正式 benchmark 量化 | 仅在 `advanced.py` Phase 6 非正式展示 |

### L3. Demo 中的反模式

- `demo_real_xhs.py` 使用模块级全局 `cdp: CDPClient | None = None`，handler 通过 `assert cdp is not None` 访问，绕过 scope 隔离
- `demo_action_dispatch.py` 使用模块级 `_mouse = MouseState()` 和 `audit_log: list[str] = []` 可变全局状态
- 结果过滤靠字符串匹配（`'security: pass'`/`'logged'`），无 handler 角色标注机制

### L4. 循环依赖 workaround 脆弱

- `result.py:11` 的 `TYPE_CHECKING` guard 注释 "C3 not yet implemented" 已过时
- `connection.py` 用 `hasattr` duck typing 替代 import `EventScope`
- 多个模块的 type annotation 仅在 `TYPE_CHECKING` 下存在，运行时不可内省

---

## 建议优先级

| 优先级 | 项目 | 工作量估计 |
|--------|------|-----------|
| P0 | 引入 `ScopeProtocol`，消除核心 `Any`，恢复泛型类型安全 | 中 |
| P0 | 强制 `event_timeout`（在 `_wait_for_completion` 中加 `wait_for`） | 小 |
| P1 | 实现 `CDPBridge` 抽象 + `cdp_to_scope()` 标准桥接 | 中 |
| P1 | 顶层 `__init__.py` re-export + `emit_and_wait()` 便利方法 | 小 |
| P1 | 补齐关键路径测试（背压、filter 异常、并发 emit） | 中 |
| P2 | 统一 Watchdog Protocol | 小 |
| P2 | 提取 demo 共享代码（输出辅助、bench 工具、chrome 工具） | 小 |
| P2 | filter 异常保护 + queued 错误日志 | 小 |
| P3 | TypeError 捕获精确化 | 小 |
| P3 | 内存管理优化（history 大对象、exception traceback） | 中 |
