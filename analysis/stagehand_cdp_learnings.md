# Stagehand V3 CDP 架构调研：可借鉴特性

> 调研对象：Stagehand V3.0.0（`other_framework/stagehand/`）
> 调研日期：2026-03-20
> 目的：识别 Stagehand 从 Playwright 迁移到 CDP 直连过程中的关键设计，评估哪些可以融入我们的 Scoped Event System

---

## 背景

Stagehand V3.0.0 彻底移除了 Playwright 依赖，自建 CDP 传输层。核心代码在 `packages/core/lib/v3/understudy/` 目录。迁移后实现了 20-40% 的性能提升。

Stagehand 的定位是**单 agent AI 浏览器自动化 SDK**，事件处理停留在原始回调层面（`session.on(event, handler)`），没有事件总线、优先级、传播控制等抽象。但其 CDP 传输层和可观测性设计有多处值得借鉴。

---

## 特性 1：FlowLogger — CDP 调用因果追踪

### 来源

`cdp.ts:148-161`（发送时捕获 context），`cdp.ts:390-400`（unsolicited 消息关联）

### 设计

Stagehand 在每次 CDP `send()` 时捕获一个 `FlowLoggerContext` 快照，并发出一个 `CdpCallEvent`。后续的 response 和 unsolicited 事件（如 `Network.requestWillBeSent`）自动关联到最近的 CDP 调用，形成因果树：

```
CdpCallEvent: Page.navigate({url: "..."})
  ├── CdpResponseEvent: {frameId: "..."}
  ├── CdpMessageEvent: Page.frameNavigated
  ├── CdpMessageEvent: Network.requestWillBeSent
  └── CdpMessageEvent: Page.loadEventFired
```

关键实现：
- `latestCdpCallEvent: Map<sessionId | null, { flowLoggerContext, cdpCallEvent }>` — 每个 session 维护最近一次 CDP 调用的身份
- 收到 unsolicited 消息时，查找该 session 的 `latestCdpCallEvent` 作为 parent anchor
- 使用 `FlowLogger.withContext()` 在回调中恢复 flow context（类似 AsyncLocalStorage）

### 我们可以如何借鉴

我们的 EventLog 目前是 per-scope JSONL 追加写入（事件完成后记录），但缺少 **CDP 调用 → 响应 → 后续事件** 的因果链。

**建议：** 在 EventScope 的 emit 路径中增加可选的 flow correlation：

```python
class EventScope:
    def emit(self, event: BaseEvent, *, flow_id: str | None = None) -> BaseEvent:
        # flow_id 允许将多个相关事件归入同一个 flow
        # Direct handler 内部的 re-emit 自动继承 flow_id
        ...
```

或者在 EventLog 层面，利用已有的 `event_parent_id` 因果链，增加 `flow_id` 字段将跨 scope 的相关事件串联起来。这对调试 "导航触发了哪些 watchdog 反应" 这类问题非常有用。

### 优先级

中。当前 parent-child 追踪已覆盖基本因果链，flow correlation 是增强可观测性。

---

## 特性 2：Flattened Target Session 复用

### 来源

`cdp.ts:135-142`（`enableAutoAttach`），`cdp.ts:238-251`（`attachToTarget`），`context.ts:660-663`（递归 auto-attach）

### 设计

Stagehand 使用 `Target.attachToTarget({ flatten: true })` 将所有 CDP session 扁平化到一条 WebSocket 连接上：

```
WebSocket (CdpConnection)
  ├── root 消息（无 sessionId）
  ├── session-A（tab 1 的消息，payload 带 sessionId 字段）
  ├── session-B（tab 1 的 OOPIF iframe）
  ├── session-C（tab 2）
  └── session-D（tab 2 的 worker）
```

相比非扁平模式（消息嵌套为 JSON 字符串），扁平模式：
- 无二次 JSON 序列化/反序列化
- 所有消息格式统一，只多一个 `sessionId` 字段
- 路由逻辑简单——按 `sessionId` 分发

配合三个关键参数：
- `autoAttach: true` — 新 target 自动 attach
- `flatten: true` — 扁平化 session
- `waitForDebuggerOnStart: true` — 新 target 暂停执行，给初始化预留时间窗口

每个子 target 的 session 上也递归设置 `Target.setAutoAttach({ flatten: true })`，确保 OOPIF 等深层 target 也被扁平化。

### 我们可以如何借鉴

我们已经通过 `CDPClientProtocol`（结构类型协议，`src/agent_cdp/bridge.py`）抽象了 CDP 客户端接口。当实际 CDP 传输层需要自建时（而非依赖第三方客户端），应采用扁平化模式。自然的映射：

```
CdpConnection (WebSocket)
  ├── session-A  →  EventScope("tab-1")
  ├── session-B  →  EventScope("tab-1") 内的 OOPIF（事件转发到同 scope）
  ├── session-C  →  EventScope("tab-2")
  └── session-D  →  EventScope("tab-2") 内的 worker
```

**建议：** 实现一个满足 `CDPClientProtocol` 的扁平化传输层：

```python
class CdpTransport:
    """WebSocket 连接 + 扁平化 session 复用。满足 CDPClientProtocol。"""
    _ws: websockets.WebSocketClientProtocol
    _sessions: dict[str, CdpSession]         # sessionId → session
    _session_to_scope: dict[str, EventScope]  # sessionId → 所属 scope

    # 满足 CDPClientProtocol 接口
    def on_event(self, method: str, callback: Callable) -> None: ...
    def off_event(self, method: str, callback: Callable) -> None: ...

    async def connect(cls, ws_url: str) -> 'CdpTransport': ...
    async def enable_auto_attach(self) -> None: ...
    def route_message(self, msg: dict) -> None:
        # 按 sessionId 分发到对应 CdpSession
        # CdpSession 再通过 CDPEventBridge 将 CDP 事件转化为 BaseEvent，emit 到对应 EventScope
        ...
```

这样 `CDPEventBridge` 可以无缝对接自建传输层，不需要修改上层代码。

### 优先级

低（当前阶段）→ 高（自建传输层阶段）。当前可以用任何满足 `CDPClientProtocol` 的第三方客户端。但这是完全自主控制 CDP 连接的参考架构。

---

## 特性 3：Inflight 请求生命周期管理

### 来源

`cdp.ts:200-209`（`rejectAllInflight`），`cdp.ts:334-358`（session detach 时的清理）

### 设计

Stagehand 维护 `inflight: Map<id, { resolve, reject, sessionId, method, stack, ts }>` 追踪所有未完成的 CDP 调用。在两种情况下自动清理：

**1. WebSocket 关闭/错误时 — reject 所有 inflight：**
```typescript
private rejectAllInflight(why: string): void {
    for (const [id, entry] of this.inflight.entries()) {
        entry.reject(new CdpConnectionClosedError(why));
        this.inflight.delete(id);
    }
}
```

**2. Session detach 时 — 只 reject 该 session 的 inflight：**
```typescript
// Target.detachedFromTarget 事件处理
for (const [id, entry] of this.inflight.entries()) {
    if (entry.sessionId === p.sessionId) {
        entry.reject(new PageNotFoundError(
            `target closed before CDP response (sessionId=${p.sessionId})`
        ));
        this.inflight.delete(id);
    }
}
```

每个 inflight 记录还保存了 `stack`（调用栈快照）和 `ts`（时间戳），便于调试超时/泄漏问题。

### 我们可以如何借鉴

我们的 `ScopeEventLoop` 中 Queued 事件入队后，如果 scope 被关闭，队列中未处理的事件的 `_pending_count` 需要正确清理，否则 `await event` 会永久挂起。

**建议：** 在 `EventScope.close()` 中增加 drain 逻辑：

```python
class EventScope:
    async def close(self) -> None:
        await self._event_loop.stop()
        # 清理队列中未处理的事件的 pending 状态
        while not self._event_loop._queue.empty():
            event, conn = self._event_loop._queue.get_nowait()
            event._decrement_pending()  # 避免 await event 永久挂起
            event.record_result(
                connection_id=conn.id,
                handler_name=get_handler_name(conn.handler),
                error=ScopeClosedError(f'Scope {self.scope_id} closed before handler executed'),
            )
        # ... 断开连接 ...
```

### 优先级

高。这是正确性问题——scope 关闭时必须保证没有 Promise/Future 永远 pending。

---

## 特性 4：waitForDebuggerOnStart — 初始化时间窗口

### 来源

`cdp.ts:139`（参数设置），`context.ts:616-700`（pre-resume 命令编排）

### 设计

当 `waitForDebuggerOnStart: true` 时，新 target 在创建后暂停执行。Stagehand 利用这个窗口在页面加载前完成所有初始化：

```
Target 创建 → 暂停
  │
  ├── Page.enable                             (启用 Page 域事件)
  ├── Runtime.enable                          (启用 Runtime 域事件)
  ├── Target.setAutoAttach({ flatten: true }) (子 target 递归 auto-attach)
  ├── Network.enable + setExtraHTTPHeaders    (网络拦截)
  ├── Page.addScriptToEvaluateOnNewDocument   (注入 init scripts)
  │
  └── Runtime.runIfWaitingForDebugger         (恢复执行)
       → 页面开始加载，所有监听已就绪
```

Stagehand 还处理了一个微妙的时序问题：某些 CDP 后端在 `Runtime.runIfWaitingForDebugger` 之后才回复 `*.enable()` 的 response。所以它用 `waitForSessionDispatch` 等待命令**被发送**（而非等待响应），然后立即 resume，之后再 await 响应。

### 我们可以如何借鉴

在多 agent 场景中，新 tab 打开后需要在页面加载前注册 watchdog。如果没有 `waitForDebuggerOnStart`，存在竞态——页面可能在 watchdog 注册完成前就触发了导航/弹窗等事件。

我们已经有 `CDPEventBridge`（`src/agent_cdp/bridge.py`）通过 `CDPClientProtocol`（结构类型协议）桥接 CDP 事件到 EventScope。这里需要的是在 bridge 建立流程中集成 `waitForDebuggerOnStart` 的时序保证。

**建议：** 在 `CDPEventBridge` 上增加 pre-resume 协调能力：

```python
class CDPEventBridge:
    async def attach_with_pause(
        self,
        cdp: CDPClientProtocol,
        scope: EventScope,
        *,
        setup: Callable[[CDPEventBridge], None],
    ) -> None:
        """在 target 暂停期间完成所有 bridge 注册，然后恢复执行。

        流程：
        1. Target 已通过 waitForDebuggerOnStart 暂停
        2. 调用 setup() — 用户在此注册所有 bridge 和 watchdog 连接
        3. 所有 Direct handler 就绪后，调用 Runtime.runIfWaitingForDebugger
        4. 页面开始加载，所有监听已就绪，不存在竞态
        """
        setup(self)  # 注册所有 bridge：security check, dialog handler, etc.
        await cdp.send('Runtime.runIfWaitingForDebugger')
```

使用示例：
```python
bridge = CDPEventBridge(cdp_client, tab1_scope)
await bridge.attach_with_pause(cdp_client, tab1_scope, setup=lambda b: (
    b.bridge('Page.javascriptDialogOpening', lambda p: DialogOpenedEvent(**p)),
    b.bridge('Page.frameNavigated', lambda p: NavigateToUrlEvent(**p)),
    tab1_scope.connect(NavigateToUrlEvent, security_check, mode=DIRECT, priority=100),
    tab1_scope.connect(DialogOpenedEvent, dialog_handler, mode=DIRECT, priority=90),
))
# 此时页面才开始加载，所有 watchdog 已就绪
```

### 优先级

中。这是确保 watchdog 零遗漏的关键机制，尤其在多 tab 并发打开的场景下。

---

## 特性 5：Session Dispatch Waiter — 等待命令发送

### 来源

`cdp.ts:215-236`（`waitForSessionDispatch`），`context.ts:633-646`（使用场景）

### 设计

```typescript
waitForSessionDispatch(
    sessionId: string,
    method: string,
    match?: (params?: object) => boolean,
): Promise<void>
```

这个 API 不等待 CDP **响应**，而是等待某个 CDP 命令**被发送到 WebSocket**。用途：协调命令时序。

Stagehand 用它解决的问题：必须在 `Runtime.runIfWaitingForDebugger` 之前确保 `Page.enable` 等命令已经发出（但不需要等到响应回来）。因为某些 CDP 后端在 target 暂停时不会回复 `*.enable()` 的响应。

### 我们可以如何借鉴

我们的 `expect()` 等待**收到的事件**，但没有等待**发出的命令/事件**的机制。在某些编排场景中（"确保 security handler 已注册后再开始导航"），需要类似的协调原语。

**建议：** 可以在 `EventScope` 上增加：

```python
class EventScope:
    async def wait_for_emit(
        self,
        event_type: type[BaseEvent],
        match: Callable[[BaseEvent], bool] | None = None,
        timeout: float | None = None,
    ) -> None:
        """等待本 scope 上的指定事件类型被 emit（不等待 handler 完成）。"""
        ...
```

### 优先级

低。属于高级编排能力，大部分场景可以通过 `expect()` + 事件因果链解决。

---

## 特性 6：Per-Session 事件清理与 Owner 追踪

### 来源

`cdp.ts:327-369`（Target lifecycle 事件处理），`context.ts:115-121`（ownership maps），`networkManager.ts:161-177`（`untrackSession`）

### 设计

Stagehand 维护多个 ownership 映射，确保资源不泄漏：

```typescript
// CdpConnection 层
sessions: Map<sessionId, CdpSession>
sessionToTarget: Map<sessionId, targetId>

// V3Context 层
pagesByTarget: Map<targetId, Page>
sessionOwnerPage: Map<sessionId, Page>
frameOwnerPage: Map<frameId, Page>

// NetworkManager 层
sessions: Map<sessionId, { session, detach: () => void }>
requests: Map<requestKey, NetworkRequestInfo>
```

当 `Target.detachedFromTarget` 发生时，逐层清理：
1. CdpConnection: reject inflight, 删除 session/target 映射
2. V3Context: 从 Page 移除 session, 清理 frame ownership
3. NetworkManager: `untrackSession()` 移除事件监听, 清理 inflight 请求记录

`untrackSession` 的 `detach()` 闭包模式值得注意——注册时保存 off 所需的 handler 引用，卸载时精确移除：

```typescript
this.sessions.set(sid, {
    session,
    detach: () => {
        session.off("Network.requestWillBeSent", onRequest);
        session.off("Network.loadingFinished", onFinished);
        // ...
    },
});
```

### 我们可以如何借鉴

我们的 `EventScope.close()` 已经自动断开所有 Connection，但如果未来有类似 NetworkManager 的 per-scope 状态追踪器，需要同样的分层清理机制。

**建议：** 在 `EventScope` 上增加 close hook，允许外部组件注册清理回调：

```python
class EventScope:
    _close_hooks: list[Callable[[], Awaitable[None]]]

    def on_close(self, hook: Callable[[], Awaitable[None]]) -> None:
        """注册 scope 关闭时的清理回调。"""
        self._close_hooks.append(hook)

    async def close(self) -> None:
        for hook in reversed(self._close_hooks):
            await hook()
        # ... 原有的 connection 断开逻辑 ...
```

### 优先级

中。当前 scope.close() 的 auto-disconnect 已覆盖核心场景，close hook 是扩展性增强。

---

## 总结：优先级排序

| 优先级 | 特性 | 理由 |
|--------|------|------|
| **高** | Inflight/pending 生命周期管理 | 正确性问题，scope 关闭时必须清理 pending 状态 |
| **中** | FlowLogger 因果追踪 | 增强可观测性，对调试多 scope 场景有价值 |
| **中** | waitForDebuggerOnStart 初始化窗口 | 确保 watchdog 零遗漏的关键，但依赖是否自建 CDP 层 |
| **中** | Close hook 机制 | 扩展性增强，支持外部组件注册清理逻辑 |
| **低→高** | Flattened target session | 当前不需要，直连 Chrome 阶段必须采用 |
| **低** | Session dispatch waiter | 高级编排，大部分场景有替代方案 |
