# agent-cdp 可行性报告：为什么 Agent 浏览器自动化需要一个新的事件系统

> 基于 browser-use、Skyvern、UI-TARS 的 CDP 接入架构对比调研，论证 agent-cdp 在当前行业格局中的技术定位与采用价值。

---

## 1. 行业现状：三种 CDP 接入架构及其天花板

通过对 browser-use (v0.12.2)、Skyvern、UI-TARS 的源码级调研（见 `cdp_usage_comparison_report.md`），我们发现当前 agent 浏览器自动化领域的 CDP 接入方案呈现三极分化：

| 路线 | 代表 | 核心优势 | 结构性瓶颈 |
|------|------|---------|-----------|
| **自建 CDP 层** | browser-use (cdp-use) | 完全控制、零抽象损耗 | 事件系统 (bubus) 无法统一同步/异步事件流，导致双轨事件架构 |
| **Playwright 复用** | Skyvern | 快速开发、生态复用 | 事件模型不足，多 Tab 支持退化为 hack，CDP 逃生口膨胀 |
| **协议无关（纯视觉）** | UI-TARS | 天然跨平台、反检测免疫 | 无 DOM/网络感知，无法做结构化数据提取和精确断言 |

**核心发现：无论选择哪条路线，事件协调层都是当前最薄弱的环节。**

- browser-use 选择了最激进的路线（自建 CDP），但其事件总线 bubus 的设计局限迫使关键 Watchdog 绕过事件系统直接注册 CDP 回调，形成了无法用现有架构修复的**双轨事件问题**。
- Skyvern 选择了最务实的路线（Playwright 复用），但 Playwright 的事件模型（`page.on()` fire-and-forget、无优先级、无传播控制）根本无法支撑 agent 级的事件协调——所有超出 Playwright 覆盖范围的需求都必须打开 CDP 逃生口，而逃生口代码（如 800 行的 `CDPDownloadInterceptor`）缺乏统一抽象。
- UI-TARS 选择了最极端的路线（不接触浏览器协议），但这意味着完全放弃了浏览器内部状态的感知能力。

**agent-cdp 正是为填补这个空白而设计的。** 它不替代 CDP 客户端（cdp-use 或 Playwright），而是提供 CDP 客户端之上、Agent 逻辑之下的**事件协调层**——解决"多个 Watchdog/Handler 如何高效、安全、可观测地响应浏览器事件"这个被现有方案集体忽视的问题。

---

## 2. agent-cdp 解决了什么：从已证实的行业痛点出发

以下每个痛点均来自 browser-use / Skyvern 源码中的实际代码模式，而非假设场景。

### 2.1 消除双轨事件系统（browser-use 的核心架构债务）

**问题**：browser-use 有 12+ 个 Watchdog，其中至少 4 个**绕过 bubus 事件总线**直接注册 CDP 回调：

| Watchdog | 绕过原因 | 直接注册的 CDP 事件 |
|----------|---------|-------------------|
| CrashWatchdog | 崩溃需要零延迟响应 | `Target.targetCrashed` |
| PopupsWatchdog | 对话框阻塞渲染进程，必须立即处理 | `Page.javascriptDialogOpening` |
| DownloadsWatchdog | 下载开始事件必须在第一时间捕获 | `Browser.downloadWillBegin`, `Browser.downloadProgress` |
| SessionManager | Target 生命周期事件不能排队 | `Target.attachedToTarget`, `Target.detachedFromTarget` |

根因：bubus 只有 Queued 模式。所有事件进入 `asyncio.Queue` 排队，不提供同步内联执行路径。

**agent-cdp 的解决**：`ConnectionType.DIRECT` 让 handler 在 `emit()` 调用栈内同步完成：

```python
# 弹窗处理：Direct 模式，emit() 返回前已处理完毕
connect(tab, DialogOpenedEvent, dismiss_dialog, mode=ConnectionType.DIRECT, priority=100)

# DOM 重建：Queued 模式，进入 scope 事件循环异步处理
connect(tab, DialogOpenedEvent, rebuild_dom, mode=ConnectionType.QUEUED, priority=0)
```

**所有 Watchdog 都通过统一的 `connect()` 机制接入**——不再有"重要事件走 CDP 直注册，不重要的事件走 bubus"的分裂。断路器、结果聚合、EventLog 等机制覆盖**全部**事件流。

### 2.2 提供真正的事件传播控制（browser-use 和 Skyvern 都没有）

**问题**：browser-use 的 SecurityWatchdog 用 `raise ValueError` 来"阻断"导航——但这只是让自己的 handler 抛异常，事件本身不停止传播。实际执行路径是：

```
1. BrowserSession.on_NavigateToUrlEvent 执行导航（先于安全检查！）
2. SecurityWatchdog.on_NavigateToUrlEvent 发现域名不允许，raise ValueError
3. bubus 捕获异常，记录到 EventResult
4. SecurityWatchdog.on_NavigationCompleteEvent 补救：重定向到 about:blank
```

**页面已经加载了，JavaScript 已经执行了，然后才被重定向。** 这是"先导航后补救"模式。

Skyvern 更糟——Playwright 的 `page.on()` 没有任何传播控制机制，所有监听器平等执行。

**agent-cdp 的解决**：`event.consume()` + handler 优先级，实现"先检查后执行"：

```python
def security_check(event: NavigateToUrlEvent) -> None:
    if not is_url_allowed(event.url):
        event.consume()                     # 停止传播
        raise NavigationBlocked(event.url)  # 异常 propagate 到 emit() 调用方

# priority=100 保证安全检查先于导航执行
connect(tab, NavigateToUrlEvent, security_check, mode=DIRECT, priority=100)
connect(tab, NavigateToUrlEvent, do_navigate,    mode=DIRECT, priority=50)
```

**导航在安全检查通过后才执行。** 如果安全检查失败，`event.consume()` 阻止后续 handler（包括导航 handler）执行——不是"先做后补救"，而是"先检查后放行"。

### 2.3 Per-Tab 隔离与并发（browser-use 和 Skyvern 都缺失）

**问题**：

- **browser-use**：bubus 使用单一全局 `asyncio.Queue`。5 个 Tab 的事件串行排队，一个 Tab 的慢 handler 阻塞所有 Tab 的事件处理。
- **Skyvern**：`get_working_page()` hack（取 `context.pages` 最后一个元素），本质上是单 Tab 模型。源码注释承认："Need to refactor this logic when we want to manipulate multi pages together"。

**agent-cdp 的解决**：`EventScope` 提供 per-tab 隔离，每个 scope 有独立的 `ScopeEventLoop`（asyncio Task）：

```python
group = ScopeGroup('browser')
tab1 = await group.create_scope('tab-1', target_id='AAA')
tab2 = await group.create_scope('tab-2', target_id='BBB')

# tab1 和 tab2 的事件循环并行运行，互不阻塞
tab1.emit(NavigateToUrlEvent(url='https://site-a.com'))
tab2.emit(NavigateToUrlEvent(url='https://site-b.com'))
```

**无全局锁。** 不同 scope 的 ScopeEventLoop 是独立的 asyncio Task，天然并行。跨 scope 的 Queued 投递是 `queue.put_nowait()`（asyncio.Queue 是协程安全的）。

### 2.4 N:M 连接拓扑（当前方案均为 N:1:M 或更弱）

**问题**：

- **bubus**：N:1:M 拓扑（N 个 publisher → 1 个 bus → M 个 handler）。跨 bus 通信需要事件转发 `bus.on('*', other_bus.dispatch)`，共享事件对象引用，结果混合。
- **Playwright**：1:N 拓扑（`page.on(event, handler)` 只支持一个 page 到多个 handler）。无法 fan-in（多个 page 的事件汇聚到一个 handler）。
- **cdp-use EventRegistry**：1:1 拓扑。每个 CDP 事件方法只允许一个 handler——`register()` 静默覆盖前一个。

**agent-cdp 的解决**：通过 `Connection` 一等公民实现完全 N:M：

```python
# 扇出：一个 tab 的事件 → 多个 handler
connect(tab1, NavEvent, security.check,   mode=DIRECT)
connect(tab1, NavEvent, dom.rebuild,      mode=QUEUED)
connect(tab1, NavEvent, monitor.record,   mode=QUEUED)

# 扇入：多个 tab 的事件 → 一个 handler
connect(tab1, CrashEvent, recovery.handle, mode=DIRECT)
connect(tab2, CrashEvent, recovery.handle, mode=DIRECT)
connect(tab3, CrashEvent, recovery.handle, mode=DIRECT)

# 广播：全局事件 → 所有 scope（深拷贝隔离）
group.broadcast(BrowserDisconnectedEvent())
```

### 2.5 Scope 关闭自动断连（消除 handler 泄漏）

**问题**：

- **browser-use**：`BaseWatchdog.__del__()` 不调用 `detach_handler_from_session()`。多次重连后 handler 可能累积。当前通过 `event_bus.stop(clear=True)` 全量清理缓解，但不支持单个 Watchdog 的动态卸载。
- **Skyvern**：Playwright 在 page close 时移除该 page 的监听器，但 CDP session 级别的监听器（`cdp_session.on()`) 无自动清理。

**agent-cdp 的解决**：`scope.close()` 自动断开所有相关 Connection：

```python
await group.close_scope('tab-1')
# → 事件循环停止
# → 所有以 tab-1 为 source 的连接断开（其他 scope 不再收到该 tab 的事件）
# → 所有以 tab-1 为 target 的连接断开（该 tab 不再收到其他 scope 的事件）
# → handler 引用释放，可被 GC
```

---

## 3. 为什么现在可行：实现成熟度评估

agent-cdp 不是 proposal——它是一个**已完成、已测试、可发布的实现**。

### 3.1 代码规模与覆盖

| 指标 | 数值 |
|------|------|
| 源码文件 | 20 个，~1,564 行 |
| 测试文件 | 10 个，~2,902 行 |
| 测试函数 | 185 个 |
| 测试/源码比 | 1.86:1 |
| Proposal 覆盖率 | ~95%（4 个 Phase 全部实现） |

### 3.2 四阶段实施状态

| Phase | 内容 | 状态 | 关键验证 |
|-------|------|------|---------|
| **Phase 1** | 事件模型（BaseEvent、EventResult、6 种聚合） | ✅ 完成 | 28+28+18 = 74 个测试 |
| **Phase 2** | 连接与派发（Connection、Direct/Queued/Auto、consume、MRO） | ✅ 完成 | 28+36+9 = 73 个测试 |
| **Phase 3** | Scope 生命周期与并发（ScopeGroup、broadcast、auto-disconnect） | ✅ 完成 | 11+8 = 19 个测试 |
| **Phase 4** | 高级能力（expect、timeout、EventLog、cycle detection） | ✅ 完成 | 10+9 = 19 个测试 |

### 3.3 生产就绪度指标

| 指标 | 状态 | 说明 |
|------|------|------|
| **类型安全** | ✅ pyright strict | 全量类型标注，Generic 正确使用 |
| **不可变性** | ✅ | EventResult/Connection 为 frozen dataclass |
| **并发安全** | ✅ | per-scope 事件循环，ContextVar 隔离，无全局锁 |
| **背压控制** | ✅ | 有界队列 maxsize=1024，drop-newest + warning log |
| **边界处理** | ✅ | WeakRef GC 安全、幂等 disconnect、pending 计数下限 clamping |
| **可观测性** | ✅ | Direct handler >100ms 告警、死锁监测、EventLog JSONL |
| **序列化** | ✅ | Pydantic model + conscribe 注册，支持多态反序列化 |

### 3.4 API 表面积控制

公开 API 仅包含 **16 个符号**，分布在 4 个子包中：

```
agent_cdp.events:      BaseEvent, EmitPolicy, EventResult, ResultStatus,
                        HandlerError, event_result, event_results_list,
                        event_results_by_handler_name, event_results_flat_dict,
                        event_results_flat_list, event_results_filtered
agent_cdp.connection:   Connection, ConnectionType, connect
agent_cdp.scope:        EventScope, ScopeGroup
agent_cdp.advanced:     expect, EventLogWriter
```

API 紧凑、正交、无冗余。学习曲线低：理解 `EventScope.emit()` + `connect()` + `ConnectionType` 三个概念即可开始使用。

---

## 4. 与现有方案的集成可行性

agent-cdp 是一个**独立的事件协调层**，不绑定任何特定的 CDP 客户端或浏览器自动化框架。它可以与现有方案组合使用：

### 4.1 替换 browser-use 的 bubus

browser-use 当前的事件流：

```
CDP WebSocket → cdp-use EventRegistry → Watchdog 回调
                                            ├── 直接处理（双轨之一）
                                            └── bubus EventBus.dispatch() → 排队 → handler（双轨之二）
```

替换为 agent-cdp 后：

```
CDP WebSocket → cdp-use EventRegistry → agent-cdp EventScope.emit()
                                            ├── Direct handler（零延迟，替代 CDP 直注册）
                                            └── Queued handler（排队，替代 bubus 队列）
```

**双轨合一。** cdp-use 的 EventRegistry（1:1 单处理器）作为 CDP 事件的入口，将事件转发到 agent-cdp 的 scope。agent-cdp 的 N:M 连接拓扑自然解决了 EventRegistry 的单处理器限制。

**迁移路径：**
1. 为每个 Tab 创建 `EventScope`
2. 将 Watchdog 的 `event_bus.on()` 调用改为 `connect(scope, EventType, handler, mode=...)`
3. 将 CDP 直注册调用改为 `connect(scope, EventType, handler, mode=DIRECT)`
4. 删除 bubus 依赖

### 4.2 增强 Skyvern 的事件能力

Skyvern 当前没有结构化的事件系统——它的核心循环是"截图→LLM→操作→截图"轮询模型。agent-cdp 可以作为 Skyvern 的**事件驱动增强层**：

```python
# 在 Skyvern 的 BrowserState 上增加 scope
scope = await group.create_scope('skyvern-task', target_id=page_id)

# 将 CDP 逃生口统一到 scope
connect(scope, DownloadStartedEvent, handle_download, mode=DIRECT)
connect(scope, NavigationEvent, track_navigation, mode=QUEUED)

# 替代 Skyvern 的 "hardcoded sleep" 等待模式
complete = await expect(scope, PageLoadedEvent, timeout=30.0)
```

这不需要替换 Skyvern 的 Playwright 层——agent-cdp 运行在 Playwright 之上，通过 `CDPSession.on()` 接收事件，通过 `EventScope` 分发。

### 4.3 为 UI-TARS 类框架提供浏览器状态感知

UI-TARS 的纯视觉模型无法感知浏览器内部状态（DOM、网络、Cookie）。agent-cdp 可以作为编排层的事件骨架：

```python
# 编排层：连接 CDP 事件到 VLM 决策循环
connect(scope, PageLoadedEvent, take_screenshot_and_send_to_vlm, mode=QUEUED)
connect(scope, DialogOpenedEvent, auto_dismiss, mode=DIRECT, priority=100)
connect(scope, NetworkIdleEvent, signal_ready, mode=DIRECT)
```

VLM 不需要感知事件系统——它只看截图。但编排层可以用 agent-cdp 精确控制**何时截图、何时安全操作、何时等待**。

---

## 5. 技术优势的结构性分析

### 5.1 来自 Qt 的经验：经过 30 年验证的事件架构

agent-cdp 的核心设计（ConnectionType、Connection 一等公民、事件传播控制、handler 优先级、auto-disconnect）直接借鉴 Qt 的 signal/slot 机制。Qt 的事件系统经过 30 年的生产验证（KDE、嵌入式系统、实时应用），其设计选择的可靠性已被充分证明。

agent-cdp 没有照搬 Qt 的全部复杂性（如 BlockingQueuedConnection、UniqueConnection、线程亲和性），而是精选了 agent 浏览器场景下必需的子集。

### 5.2 来自 bubus 的遗产：Agent 浏览器领域独有的能力

agent-cdp 完整保留了 bubus 在 agent 浏览器场景下不可替代的 6 项创新：

| 能力 | 价值 | Qt/Playwright 有吗？ |
|------|------|-------------------|
| `BaseEvent[T]` 泛型结果 | 编译期类型安全的 handler 结果 | ❌ Qt emit 无返回值，Playwright 回调无类型 |
| 6 种结果聚合 | 多 Watchdog 各贡献状态片段，聚合为完整视图 | ❌ |
| Awaitable 事件 | `await event` 等待所有 handler 完成 | ❌ |
| `expect()` | 声明式等待未来事件 | ❌ Playwright 有 `wait_for_event` 但无 include/exclude |
| 父子事件追踪 | 自动建立事件因果链，支持可观测性 | ❌ |
| Per-handler 超时 | 死锁检测 + 超时取消 | ❌ |

这些不是"nice to have"——它们是 browser-use 在 agent 场景中验证过的核心能力。没有结果聚合，多个 Watchdog 无法协作构建 `BrowserStateSummary`。没有 awaitable 事件，agent 循环无法等待所有 handler 完成后再取结果。

### 5.3 agent-cdp 的独有创新

在 Qt 和 bubus 基础上，agent-cdp 新增了两者都没有的能力：

| 能力 | 解决的问题 |
|------|-----------|
| **EventScope 隔离** | bubus 全局队列 → per-tab 独立事件循环 |
| **per-scope 并发** | 5 个 Tab 真正并行处理事件，无全局锁 |
| **深拷贝广播** | broadcast 时每个 scope 收到独立副本，结果互不污染 |
| **MRO 事件匹配** | `connect(scope, BaseNavigationEvent, handler)` 自动匹配所有子类 |
| **connect_all() catch-all** | 替代 bubus 的 `'*'` 字符串匹配，类型安全 |
| **背压控制** | 有界队列 + drop-newest，防止高频事件 OOM |
| **Direct handler 执行时间监测** | >100ms 输出 warning，暴露违规 handler |
| **EmitPolicy 声明在事件类上** | 事件作者决定异常语义，自文档化 |

---

## 6. 面向未来的扩展性评估

### 6.1 多 Agent 协作（已就绪）

多 Agent 共享一个浏览器、各操作自己的 Tab，是 agent 浏览器自动化的明确演进方向。agent-cdp 的 Scope 模型天然支持：

```python
# 每个 Agent 有自己的 scope
agent_a_scope = await group.create_scope('agent-a-tab')
agent_b_scope = await group.create_scope('agent-b-tab')

# 各自的事件独立处理
connect(agent_a_scope, NavEvent, agent_a.navigate, mode=QUEUED)
connect(agent_b_scope, NavEvent, agent_b.navigate, mode=QUEUED)

# 监控 scope 扇入所有 agent 的事件
connect(agent_a_scope, AnyEvent, monitor.record, mode=QUEUED)
connect(agent_b_scope, AnyEvent, monitor.record, mode=QUEUED)

# 全局事件广播到所有 agent
group.broadcast(BrowserCrashedEvent())
```

bubus 和 Playwright 都**无法**原生支持这个模式——bubus 是单队列全局串行，Playwright 的 `page.on()` 无跨 page 路由能力。

### 6.2 动态 Watchdog 加载/卸载（已就绪）

当前 browser-use 的 Watchdog 是固定集合，整体 attach/整体 reset。未来按任务类型动态启用不同 Watchdog 组合时：

```python
# 启用：连接 handler 到 scope
conn = connect(scope, DownloadEvent, download_handler, mode=QUEUED)

# 卸载：断开连接，无副作用
conn.disconnect()

# 或者：关闭整个 scope，自动断开所有相关连接
await scope.close()
```

不需要 `event_bus.stop(clear=True)` 这种全量清理。

### 6.3 安全拦截升级（已就绪）

从"域名限制"升级到"真正的安全拦截"（如防御 prompt injection 通过恶意页面内容）时，handler 优先级 + `event.consume()` + Direct 模式提供了必要的基础设施：

```python
# 多层安全检查，按优先级依次执行
connect(scope, NavEvent, waf_check,       mode=DIRECT, priority=200)  # WAF 拦截
connect(scope, NavEvent, domain_check,    mode=DIRECT, priority=100)  # 域名限制
connect(scope, NavEvent, do_navigate,     mode=DIRECT, priority=50)   # 实际导航

# 任何一层调用 event.consume()，后续层不执行
```

### 6.4 跨浏览器支持（架构不阻碍）

agent-cdp 不绑定 CDP 协议。`EventScope` 和 `Connection` 是纯 Python 抽象，不依赖任何浏览器协议。如果未来需要支持 Firefox（WebDriver BiDi）或 Safari，只需为新协议编写事件桥接层，复用整个事件协调架构。

相比之下，browser-use 的 cdp-use 是 CDP 协议的类型化绑定，切换协议需要替换整个通信层。

### 6.5 云浏览器 / 远程 CDP 场景（架构不阻碍）

agent-cdp 的 Scope 模型与浏览器运行位置无关。无论 CDP WebSocket 连接的是本地 Chrome、远程 Docker 容器、还是云浏览器服务（如 browser-use 的 cloud mode），事件流入 `EventScope.emit()` 后的处理逻辑完全相同。

Skyvern 在远程场景下遇到的问题（Playwright bug #38805 导致下载路径失效）不影响 agent-cdp——agent-cdp 不管理下载路径，它管理的是事件流。

---

## 7. 采用成本与风险评估

### 7.1 采用成本

| 成本项 | 评估 |
|--------|------|
| **学习曲线** | 低。核心概念 3 个：`EventScope.emit()` + `connect()` + `ConnectionType`。熟悉 Qt signal/slot 或 bubus 的开发者可在 30 分钟内上手 |
| **迁移成本** | 中。从 bubus 迁移需要将 `event_bus.on()` 改为 `connect()`，将 CDP 直注册改为 Direct 连接。API 形态相似，但多了 mode/priority 参数 |
| **依赖体积** | 极低。仅依赖 `pydantic>=2.0`、`conscribe>=0.4.0`、`uuid-utils>=0.9`。无 C 扩展，纯 Python |
| **运行时开销** | 可忽略。Direct handler 在 `emit()` 调用栈内同步执行，无额外调度开销。Queued handler 使用标准 `asyncio.Queue`，性能与 bubus 相当 |

### 7.2 风险

| 风险 | 缓解措施 |
|------|---------|
| **新项目，无生产环境验证** | 185 个测试覆盖所有核心路径；设计基于 Qt（30 年验证）和 bubus（browser-use 生产使用）的成熟模式 |
| **API 可能变化** | 公开 API 仅 16 个符号，表面积小，变化风险低。核心概念（Scope、Connection、emit）已稳定 |
| **conscribe 依赖** | conscribe 是同一作者维护的包，提供事件类型注册和多态反序列化。如果需要去依赖，可替换为手动的 type registry |
| **单维护者风险** | 代码量小（1,564 行），设计文档完整。任何有 asyncio 经验的 Python 开发者可在 1-2 天内理解全部代码 |

---

## 8. 结论

### 行业需要什么

当前 agent 浏览器自动化框架在**CDP 接入层**（cdp-use、Playwright）和**Agent 逻辑层**（LLM 决策循环）上投入了大量工程，但**事件协调层**——多个 Watchdog/Handler 如何响应浏览器事件——仍然依赖 ad-hoc 方案：bubus 的单队列全局串行、Playwright 的 fire-and-forget 回调、或直接跳过事件系统用 CDP 硬编码。

这些方案在单 Agent 单 Tab 场景下可以工作，但在多 Agent 多 Tab、安全拦截、动态 Watchdog、实时事件响应等演进方向上存在**结构性限制**——不是可以通过打补丁解决的问题，而是架构层面的天花板。

### agent-cdp 提供什么

一个**独立的、已实现的、经过充分测试的**事件协调层，它：

1. **统一了同步和异步事件处理**——Direct/Queued/Auto 三种模式，消除双轨事件架构
2. **提供了真正的事件传播控制**——`consume()` + 优先级，实现"先检查后放行"
3. **实现了 per-tab 隔离和并发**——EventScope 独立事件循环，无全局锁
4. **支持 N:M 连接拓扑**——扇出、扇入、广播，一个 `connect()` 函数统一表达
5. **保留了 agent 领域的核心能力**——泛型事件结果、6 种聚合、awaitable 事件、expect、父子追踪
6. **为未来扩展留出了空间**——多 Agent 协作、动态 Watchdog、安全升级、跨浏览器支持

### 建议

对于正在构建或维护 agent 浏览器自动化框架的团队：

- **如果你在用 bubus**：agent-cdp 是它的直接替代，保留全部优点、解决全部已知缺陷。迁移路径清晰。
- **如果你在用 Playwright 事件**：agent-cdp 提供 Playwright `page.on()` 无法提供的优先级、传播控制、结果聚合和跨 tab 路由。它运行在 Playwright 之上，不替代 Playwright。
- **如果你在自建事件系统**：agent-cdp 已经实现了你需要的大部分功能（185 个测试验证），且 API 紧凑（16 个公开符号）。采用它比从头实现更高效、更可靠。
