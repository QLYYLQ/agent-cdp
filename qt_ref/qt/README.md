# Qt 参考资料索引 — 对标 bubus/browser_use 事件架构

> 本目录收集 Qt 框架的核心设计文档，用于系统性对比 browser_use 的事件驱动架构，识别设计缺口并探索改进方案。

## 对比维度与文件清单

### 1. Signal/Slot 核心机制（对标 bubus EventBus.on() + dispatch）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `01_signals_and_slots.md` | `doc.qt.io/qt-6/signalsandslots.html` | 核心：连接类型（Direct/Queued/BlockingQueued/Auto/UniqueConnection）— bubus 目前只有 Queued 一种模式 |
| `02_connection_type_enum.md` | `doc.qt.io/qt-6/qt.html#ConnectionType-enum` | 5 种连接类型的精确语义 — 这是 bubus 最大的设计缺口之一 |
| `03_qobject_connect.md` | `doc.qt.io/qt-6/qobject.html#connect` | 连接的生命周期管理、自动断开（对象销毁时）、sender/receiver 关系 — bubus 缺少自动断开 |

### 2. Event System（对标 BaseEvent + 事件分发链）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `04_event_system.md` | `doc.qt.io/qt-6/eventsandfilters.html` | 事件传播链（accept/ignore）、事件过滤器（installEventFilter）— bubus 没有 accept/ignore 机制 |
| `05_qevent.md` | `doc.qt.io/qt-6/qevent.html` | 事件优先级、spontaneous vs posted vs sent 三种事件投递方式 — bubus 只有 posted（queue）|
| `06_send_post_event.md` | `doc.qt.io/qt-6/qcoreapplication.html#sendEvent` | sendEvent（同步直投）vs postEvent（队列投递）的区别 — 这正是 bubus 缺失的 Direct connection |

### 3. Event Loop 与线程模型（对标 bubus _run_loop + ReentrantLock）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `07_threads_and_qobjects.md` | `doc.qt.io/qt-6/threads-qobject.html` | 线程亲和性（thread affinity）、跨线程信号自动切换为 Queued — bubus 是纯单线程 asyncio |
| `08_qeventloop.md` | `doc.qt.io/qt-6/qeventloop.html` | 嵌套事件循环、processEvents — 对标 bubus 的 inside_handler_context 轮询机制 |
| `09_qthread.md` | `doc.qt.io/qt-6/qthread.html` | moveToThread()、事件循环与线程的关系 |

### 4. Event Filter 与事件拦截（对标 watchdog 的 circuit breaker）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `10_event_filter.md` | `doc.qt.io/qt-6/qobject.html#installEventFilter` | 事件过滤器链 — 比 bubus 的 _would_create_loop + circuit breaker 更通用的拦截模式 |

### 5. Property System 与状态通知（对标 watchdog 的状态管理）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `11_property_system.md` | `doc.qt.io/qt-6/properties.html` | Q_PROPERTY + NOTIFY signal — 属性变更自动触发信号 |

### 6. State Machine（对标 BrowserSession 生命周期管理）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `12_state_machine.md` | `doc.qt.io/qt-6/statemachine-api.html` | QStateMachine — BrowserSession 目前用 flags 管理状态，不是正式的状态机 |

### 7. Meta-Object System（对标 bubus 的反射式 handler 注册）

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `13_meta_object_system.md` | `doc.qt.io/qt-6/metaobjects.html` | MOC 如何实现编译时类型安全的 signal/slot |
| `14_moc.md` | `doc.qt.io/qt-6/moc.html` | 代码生成 vs 运行时反射的权衡 |

### 8. 深度实现参考

| 文件 | 来源 URL | 为什么需要 |
|------|----------|-----------|
| `15_woboq_signal_internals.md` | `woboq.com/blog/how-qt-signals-slots-work.html` 系列 | 深入 connection list、sender/receiver 元数据、自动清理机制的底层实现 |
| `16_gobject_signals.md` | `docs.gtk.org/gobject/concepts.html#signals` | 第二参照系：emission hooks、detail strings、accumulator — 对比 bubus 的 event_results 聚合 |

## 优先级排序（Top 5）

1. **01 + 02** Signals & Slots + ConnectionType — bubus 只有一种连接模式
2. **04** Event System（accept/ignore + filter）— bubus 没有事件传播控制
3. **07** Threads and QObjects（thread affinity）— bubus 纯单线程
4. **10** installEventFilter — bubus 的事件拦截是 ad-hoc 的
5. **15** woboq 内部实现 — 理解自动清理机制

## bubus 当前架构速览（用于对比）

- **dispatch 模式**: 仅 Queued（asyncio.Queue），无 Direct/Blocking-Queued
- **handler 注册**: `bus.on(EventClass, handler)` 显式 + `on_{EventName}` 命名约定自动注册
- **事件传播控制**: 无 accept/ignore，无 event filter 链
- **handler 优先级**: 无，按注册顺序 FIFO
- **结果聚合**: EventResult[T] 存储在 event.event_results dict，支持 flat_dict/list/by_handler 多种聚合
- **生命周期**: 无自动断开（对象销毁时不自动取消注册）
- **线程模型**: 纯单线程 asyncio + ReentrantLock
- **超时**: 每事件可配（env var 覆盖），handler 级 asyncio.wait_for
- **错误处理**: 错误存储在 EventResult.error，不中断其他 handler
