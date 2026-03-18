# CDP 使用方式技术调研报告：browser-use / Skyvern / UI-TARS

## 摘要

本报告对三个 agent 浏览器自动化框架如何接入 Chrome DevTools Protocol (CDP) 进行了深度技术调研。结论：三者代表了 CDP 接入光谱上的三个极端位置——browser-use 完全自建 CDP 层、Skyvern 以 Playwright 为主+CDP 逃生口、UI-TARS 根本不接触浏览器协议。

---

## 1. 三者的 CDP 接入架构总览

| 维度 | browser-use | Skyvern | UI-TARS |
|------|-------------|---------|---------|
| 核心浏览器通信层 | **自建 CDP 客户端 (cdp-use)** | **Playwright async API** | **无**（纯视觉 VLM + pyautogui） |
| Playwright 依赖 | 仅用于发现/安装浏览器二进制文件 | 运行时核心依赖 | 无 |
| 原始 WebSocket 连接 | ✅ `websockets.connect()` 直连 | ❌ 通过 Playwright CDPSession 间接访问 | ❌ |
| CDP 域覆盖 | ~15 个域、完整控制 | 3-4 个域、补丁式使用 | 0 |
| 事件订阅模型 | cdp-use EventRegistry（单处理器/事件） | Playwright `cdp_session.on()` | 无 |
| 多 Tab 管理 | 自建 SessionManager + Target 域 | Playwright `context.pages`（hack: 取最后一个） | 无 |

---

## 2. 详细分析

### 2.1 browser-use：完全自建 CDP 层

#### 架构

browser-use **运行时完全不使用 Playwright**。它通过自研的 `cdp-use` 库直接以 WebSocket 方式连接 Chrome：

```
Chrome (--remote-debugging-port)
  ↕ WebSocket (ws://...)
cdp-use CDPClient
  ├── CDPLibrary      (45+ CDP 域的类型化方法: Page.navigate, Target.createTarget, ...)
  ├── RegistrationLib  (CDP 事件注册: Target.targetCrashed, Page.javascriptDialogOpening, ...)
  └── EventRegistry   (事件分发：单处理器/事件方法)
      ↓
BrowserSession (session.py)
  ├── SessionManager   (Target/Session 生命周期)
  └── 12+ Watchdog     (崩溃检测、弹窗处理、下载管理、DOM 提取、截图等)
      ↓
bubus EventBus (应用层事件: TabCreatedEvent, NavigateToUrlEvent, ...)
```

#### 连接流程

1. `LocalBrowserWatchdog` 以 `--remote-debugging-port=PORT` 启动 Chrome 子进程
2. 轮询 `http://127.0.0.1:PORT/json/version` 获取 `webSocketDebuggerUrl`
3. `CDPClient(ws_url)` 建立 WebSocket 连接
4. 通过 `Target.setAutoAttach` 自动附加到所有 target
5. `SessionManager` 开始监控 target 生命周期

#### 直接使用的 CDP 域

| CDP 域 | 用途 | 调用位置 |
|--------|------|----------|
| **Target** | Tab 创建/关闭/切换/自动附加 | SessionManager, BrowserSession |
| **Page** | 导航、截图、生命周期事件、对话框处理 | 多个 Watchdog |
| **Network** | 启用监控、注入 HTTP 头、清除 Cookie | BrowserSession |
| **Runtime** | JS 执行 (`evaluate`)、调试器恢复 | CrashWatchdog, DomWatchdog |
| **Browser** | 下载行为配置、权限授予 | DownloadsWatchdog, PermissionsWatchdog |
| **Fetch** | 代理认证拦截 | BrowserSession |
| **Storage** | Cookie 读取 | BrowserSession |
| **DOMSnapshot** | DOM 树提取 | DomService |
| **Emulation** | 视口模拟 | BrowserSession |
| **Input** | 输入模拟 | 通过 JS 或直接 CDP |

#### Playwright 仅作为工具链

- **二进制发现**：`LocalBrowserWatchdog._find_installed_browser_path()` 扫描 `~/.cache/ms-playwright/chromium-*/` 目录
- **安装降级**：找不到浏览器时执行 `uvx playwright install chrome`（CLI 调用，非库依赖）
- **Cookie 格式兼容**：序列化时兼容 Playwright `storage_state` 格式

#### 双轨事件问题

这是 browser-use 架构的核心矛盾。Watchdog 存在两条事件路径：

1. **bubus 路径**（应用层）：通过 `event_bus.on()` 注册，所有事件排队异步分发
2. **CDP 直注册路径**（底层）：通过 `cdp_client.register.*` 直接绑定 CDP 事件回调

需要零延迟响应的 Watchdog（崩溃检测、弹窗关闭、下载监控）**被迫绕过 bubus 直接注册 CDP 事件**，因为 bubus 只支持 Queued 模式，无法提供同步内联处理。

### 2.2 Skyvern：Playwright 为主 + CDP 逃生口

#### 架构

Skyvern 以 Playwright 为运行时核心，所有浏览器交互默认走 Playwright API：

```
Playwright async API
  ├── chromium.launch_persistent_context()   (本地模式)
  ├── chromium.connect_over_cdp()            (远程 CDP 模式)
  └── page.*/locator.*/mouse.*/keyboard.*   (全量 Playwright API)
      ↓
  少数 CDP 逃生口 (通过 Playwright CDPSession)
  ├── Browser.setDownloadBehavior           (下载路径配置)
  ├── Fetch.*                               (CDPDownloadInterceptor, 800+ 行)
  ├── Target.*/Page.*                       (exfiltration 流式通道)
  └── Runtime/DOM/Page.enable               (事件监听)
```

#### 三种浏览器启动模式

| 模式 | 方法 | 场景 |
|------|------|------|
| `chromium-headless` | `launch_persistent_context()` | 本地无头，支持 user_data_dir |
| `chromium-headful` | `launch_persistent_context(headless=False)` | 本地有头 |
| `cdp-connect` | `connect_over_cdp(cdp_url)` | 远程浏览器 / 云浏览器 |

#### CDP 逃生口：为什么需要绕过 Playwright

**逃生口 1：下载路径（CDPDownloadInterceptor，800+ 行）**

这是 Skyvern 中最大的 CDP 逃生口，存在原因是 **Playwright bug #38805**——远程 Windows Chrome 忽略 Linux 下载路径。整个 `cdp_download_interceptor.py` 通过：
- `Fetch.enable` 拦截所有网络响应
- `Fetch.getResponseBody` / `Fetch.takeResponseBodyAsStream` 提取响应体
- `IO.read` / `IO.close` 流式读取
- `Browser.downloadWillBegin` 捕获 blob URL 下载

实质上在 Playwright 之上重新实现了一套下载管理。

**逃生口 2：用户行为流式追踪（exfiltration.py）**

通过 CDP 订阅导航和 target 事件实现实时行为流推送：
```python
cdp_session.on('Target.targetCreated', ...)
cdp_session.on('Page.frameNavigated', ...)
cdp_session.on('Page.navigatedWithinDocument', ...)
```

Playwright 的 `page.on()` 事件粒度不够，无法提供所需的导航细节。

**逃生口 3：下载行为配置**

```python
cdp_session = await browser.new_browser_cdp_session()
await cdp_session.send('Browser.setDownloadBehavior', {...})
```

Playwright 没有直接暴露此 CDP 方法的高层 API。

#### 多 Tab 管理的 hack

```python
# real_browser_state.py:171-173
# HACK: currently, assuming the last page is always the working page.
# Need to refactor this logic when we want to manipulate multi pages together
pages = await self.list_valid_pages()
last_page = pages[-1]
```

这表明 Skyvern 的多 Tab 支持本质上是单 Tab 模型的 workaround。

#### 其他 Playwright 限制的 workaround

| 问题 | workaround | 位置 |
|------|-----------|------|
| JS 执行上下文在导航时销毁 | 捕获 `Execution context was destroyed`，重新注入 `domUtils.js` | `utils/page.py:252-275` |
| 截图动画超时 | 先 `animations="disabled"`，超时后 `animations="allow"` | `utils/page.py:57-66` |
| MSEdge CDP 下载面板出现在 pages 列表 | 过滤 `about:blank` 和非 http(s) 页面 | `real_browser_state.py:191-204` |
| 文件上传时序 | 使用 `page.expect_file_chooser()` + hardcoded sleep (TODO: 应该用 `wait_for_event`) | `handler.py:2494` |
| 自动化检测 | `--disable-blink-features=AutomationControlled`, 移除 `--enable-automation` | `browser_factory.py:272-293` |

### 2.3 UI-TARS：纯视觉模型，无浏览器协议

#### 架构

UI-TARS **不是浏览器自动化框架**，而是一个 VLM（Vision-Language Model）的动作解析库：

```
外部系统截图 → UI-TARS VLM（远程推理） → 文本输出
  ↓                                          ↓
action_parser.py                        "click(start_box='(100,200)')"
  ↓
pyautogui 代码字符串
  ↓
外部系统执行 pyautogui.click(x, y)
```

#### 关键特征

- **零 CDP 依赖**：不导入 playwright、cdp、websocket、selenium 中的任何一个
- **零浏览器生命周期管理**：不启动、不连接、不关闭浏览器
- **纯视觉坐标**：基于截图像素坐标操作，通过 pyautogui 模拟 OS 级鼠标/键盘事件
- **无状态**：`action_parser.py` 是纯函数，接收文本输入，返回结构化数据或代码字符串

#### 动作空间

```python
# 所有动作都是基于像素坐标的 OS 级操作
pyautogui.click(x, y)           # 点击
pyautogui.doubleClick(x, y)     # 双击
pyautogui.moveTo(x, y)          # 悬停
pyautogui.dragTo(ex, ey)        # 拖拽
pyautogui.scroll(amount, x, y)  # 滚动
pyautogui.hotkey('ctrl', 'c')   # 快捷键
pyautogui.write(content)        # 打字
```

---

## 3. Playwright 复用在 Agent Browser 场景下的缺陷分析

基于 Skyvern 的实际代码和 browser-use 选择自建的原因，复用 Playwright 存在以下结构性缺陷：

### 3.1 事件模型不足

| 缺陷 | 表现 | 影响 |
|------|------|------|
| **无同步内联事件处理** | Playwright `page.on()` 是异步 fire-and-forget，无法在事件回调中阻止默认行为 | 安全 Watchdog 无法在导航发生前拦截恶意 URL |
| **无事件优先级** | 所有 `page.on()` 监听器平等执行，无优先级排序 | 安全处理器与业务处理器无法保证执行顺序 |
| **无 `event.consume()` 传播控制** | 一个监听器无法阻止后续监听器执行 | 当安全检查失败时，后续处理器仍会执行 |
| **CDP 事件覆盖不完整** | Playwright 只暴露部分 CDP 事件为高层 API，很多事件（如 `Target.targetCrashed`、`Fetch.authRequired`）需要通过 CDPSession 逃生口访问 | 需要维护两套事件监听代码 |

### 3.2 多 Tab / 多 Agent 并发限制

| 缺陷 | 表现 |
|------|------|
| **单一 context.pages 列表** | Playwright 将所有 page 放在一个 flat list 中，无法按 agent scope 隔离 |
| **无 per-tab 事件隔离** | `context.on('page')` 是全局的，无法为不同 tab 建立独立事件循环 |
| **Tab 身份跟踪困难** | Playwright 的 `Page` 对象在 crash/navigation 后可能变为无效引用，无自动恢复机制 |

Skyvern 的 hack（`get_working_page()` 取最后一个 page）正是这一限制的体现。

### 3.3 生命周期管理间隙

| 缺陷 | 表现 |
|------|------|
| **JS 上下文随导航销毁** | Skyvern 必须捕获 `Execution context was destroyed` 错误并重新注入 JS |
| **无自动重连** | CDP WebSocket 断开时 Playwright 不提供自动重连机制 |
| **下载管理在远程场景失效** | Playwright bug #38805 迫使 Skyvern 编写 800+ 行 CDP Fetch 拦截器 |

### 3.4 反检测 / 指纹管理限制

Playwright 默认注入 automation flags（`navigator.webdriver=true`），需要手动禁用。Skyvern 通过 `--disable-blink-features=AutomationControlled` 和移除 `--enable-automation` 来绕过，但这只是部分解决方案——高级反爬系统仍能通过 Playwright 的 JS 运行时特征检测到自动化。

### 3.5 各框架如何绕过这些缺陷

**browser-use 的策略：完全抛弃 Playwright**

- 自建 `cdp-use` 作为类型化 CDP 客户端
- 自建 `SessionManager` 管理 Target 和 Session
- 自建 12+ Watchdog 系统处理各类浏览器事件
- 代价：维护成本高，cdp-use 的 EventRegistry 单处理器/事件限制引入新问题

**Skyvern 的策略：Playwright 为主 + CDP 逃生口**

- 95% 的交互通过 Playwright API
- 在 Playwright 不足的地方通过 `page.context.new_cdp_session(page)` 获取 CDP 通道
- 代价：两套代码路径并存，CDP 逃生口缺乏统一抽象

**UI-TARS 的策略：完全回避浏览器协议**

- 基于视觉感知 + OS 级操作，绕过所有浏览器 API 限制
- 代价：无法访问 DOM、网络、Cookie 等任何浏览器内部状态

---

## 4. 各架构对未来扩展的天然限制

### 4.1 browser-use 自建 CDP 层的限制

| 限制 | 说明 | 影响场景 |
|------|------|----------|
| **CDP 协议版本耦合** | cdp-use 的 `CDPLibrary` 是自动生成的类型化代码，每次 Chrome 更新 CDP 协议都需重新生成 | Chrome 版本升级 |
| **仅 Chromium 家族** | CDP 是 Chromium 特有协议，无法支持 Firefox（需 WebDriver BiDi）或 Safari | 跨浏览器测试、企业合规 |
| **EventRegistry 单处理器限制** | `register()` 静默覆盖前一个处理器，两个 Watchdog 不能监听同一个 CDP 事件 | 多 Watchdog 协作、插件系统 |
| **无高层 API 安全网** | 直接操作 CDP 需要处理所有边缘情况（session stale、target crash、WS 断连），Playwright 已替用户处理了大部分 | 稳定性、开发效率 |
| **单连接瓶颈** | 一个 `CDPClient` 对应一个 WebSocket，所有 Tab 的通信共享同一连接 | 高并发多 Tab 场景 |
| **重连复杂度** | 自建重连逻辑（指数退避、session 恢复）远比依赖 Playwright 维护成本高 | 长时运行任务、不稳定网络 |

**业务扩展影响**：
- ❌ 无法在不大幅重构的情况下支持 Firefox / Safari
- ❌ 插件/扩展系统受限于 EventRegistry 单处理器模型
- ⚠️ 每个 Chrome 大版本升级都是技术风险
- ✅ 对 CDP 的完全控制使得深度定制（如自定义协议扩展）成为可能

### 4.2 Skyvern Playwright 依赖的限制

| 限制 | 说明 | 影响场景 |
|------|------|----------|
| **Playwright 版本锁定** | Playwright 与其附带的浏览器版本强绑定，升级 Playwright 可能破坏 CDP 逃生口 | 依赖升级 |
| **CDPSession 能力受限** | `new_cdp_session()` 是 Playwright 内部创建的 CDP 通道，受 Playwright 的连接管理约束 | 需要原始 WebSocket 级控制的场景 |
| **多 Tab 模型根本缺陷** | `get_working_page()` hack 表明架构上不支持真正的多 Tab agent 协作 | 多 Agent 并发操作同一浏览器 |
| **轮询式感知模型** | 核心循环是"截图→LLM→操作→截图"，无事件驱动的页面变化感知 | 需要实时响应页面变化的场景（如实时监控、流式数据） |
| **JS 上下文不稳定** | 导航销毁执行上下文的 workaround 增加了延迟和失败点 | 频繁导航的 SPA/MPA 场景 |
| **下载能力碎片化** | Playwright 下载 + CDP Fetch 拦截 + blob URL 不支持 = 三层逻辑 | 文件下载密集型任务 |

**业务扩展影响**：
- ❌ 多 Tab agent 协作需要重写 browser state 管理
- ❌ 实时事件响应（如价格监控、通知侦测）受轮询模型限制
- ⚠️ Playwright 版本升级可能破坏 CDP 逃生口
- ✅ Playwright 的跨浏览器支持（Chromium/Firefox/WebKit）理论上可复用（但 CDP 逃生口仅限 Chromium）
- ✅ 快速开发：大部分交互只需调用 Playwright API，开发效率高

### 4.3 UI-TARS 纯视觉模型的限制

| 限制 | 说明 | 影响场景 |
|------|------|----------|
| **无 DOM 访问** | 无法读取/验证表单值、提取结构化数据、操作不可见元素 | 数据抓取、表单验证、自动化测试 |
| **无网络感知** | 不知道页面是否加载完成、是否有 pending 请求 | 等待时机判断全靠 VLM 视觉推断 |
| **坐标系脆弱** | 像素坐标依赖截图分辨率和 VLM 预处理参数的精确匹配 | 不同分辨率/DPI 的设备 |
| **pyautogui OS 级限制** | 无法操作浏览器安全对话框、跨域 iframe、被覆盖的元素 | 需要浏览器级操作的场景 |
| **无状态管理** | 每次调用都是独立的，上下文由外部系统维护 | 长流程多步骤任务 |
| **速度受 VLM 推理限制** | 每个动作需要一次 VLM 推理，延迟 1-5 秒 | 对速度敏感的批量操作 |

**业务扩展影响**：
- ❌ 无法做结构化数据提取（需要 DOM）
- ❌ 无法做网络级监控（请求拦截、响应修改）
- ❌ 无法做精确的自动化测试（缺乏断言能力）
- ✅ 天然跨平台：不仅限于浏览器，可操作任何 GUI 应用
- ✅ 对浏览器反爬完全免疫：不注入任何 JS，不修改 DOM
- ✅ 模型升级即能力升级：不需要修改代码

---

## 5. 核心发现与对 Scoped Event System 的启示

### 5.1 三者架构对比的核心 insight

```
控制粒度     browser-use ████████████  (完全控制)
             Skyvern     ██████░░░░░░  (Playwright + 逃生口)
             UI-TARS     ░░░░░░░░░░░░  (零控制)

开发效率     browser-use ██████░░░░░░  (高维护成本)
             Skyvern     ████████████  (Playwright 开箱即用)
             UI-TARS     ████████████  (无需实现浏览器层)

多 Tab 支持  browser-use ████████████  (SessionManager 完整实现)
             Skyvern     ████░░░░░░░░  (hack: 取最后一个 page)
             UI-TARS     ░░░░░░░░░░░░  (不涉及)

事件驱动能力 browser-use ████████░░░░  (有 bubus，但受限于 Queued-only)
             Skyvern     ████░░░░░░░░  (仅 Playwright fire-and-forget)
             UI-TARS     ░░░░░░░░░░░░  (无)
```

### 5.2 对 Scoped Event System 的设计验证

browser-use 的"双轨事件"问题和 Skyvern 的"CDP 逃生口"模式共同验证了 Scoped Event System 的核心设计决策：

1. **Direct 模式**：browser-use 的 Watchdog 绕过 bubus 直接注册 CDP 事件 → 证明需要同步内联处理
2. **事件优先级 + `consume()`**：Skyvern/browser-use 都无法让安全检查优先于业务处理 → 证明需要优先级和传播控制
3. **Per-scope 事件循环**：Skyvern 的单 Tab hack + browser-use 的全局 bubus 队列 → 证明需要 per-tab 隔离
4. **Auto-disconnect**：browser-use 的 SessionManager 手动清理 + Skyvern 的 page close 后残留监听 → 证明需要 scope 关闭时自动断连
5. **N:M 连接拓扑**：cdp-use EventRegistry 的单处理器限制 → 证明需要多对多连接

### 5.3 Playwright 复用的建议

对于新的 agent 浏览器自动化项目，**不建议纯依赖 Playwright**，但也**不建议完全抛弃**：

- **推荐模式**：以自建 CDP 事件层为核心（类似 browser-use），但保留 Playwright 作为可选的高层 API 代理（用于快速开发非关键路径的交互）
- **核心事件通路**必须走自建 CDP 层，确保 Direct 模式、优先级、传播控制的可用性
- **Playwright 可作为"语法糖"**用于 `page.click()`、`page.fill()` 等不需要事件控制的简单操作
- **避免 Skyvern 模式**——Playwright 为主 + CDP 逃生口会导致两套代码路径的维护负担递增

---

## 附录 A：关键代码位置

### browser-use
- `bu_ref/cdp-use/cdp_use/client.py` — CDPClient WebSocket 实现
- `bu_ref/cdp-use/cdp_use/cdp/registry.py` — EventRegistry 单处理器限制
- `bu_ref/browser-use/browser_use/browser/session.py` — BrowserSession 核心
- `bu_ref/browser-use/browser_use/browser/session_manager.py` — Target/Session 生命周期
- `bu_ref/browser-use/browser_use/browser/watchdog_base.py` — BaseWatchdog 断路器

### Skyvern
- `other_framework/skyvern/skyvern/webeye/browser_factory.py` — 浏览器启动 + CDP 逃生口
- `other_framework/skyvern/skyvern/webeye/cdp_download_interceptor.py` — 800 行 CDP Fetch 拦截器
- `other_framework/skyvern/skyvern/webeye/utils/page.py` — JS 上下文重注入 workaround
- `other_framework/skyvern/skyvern/webeye/real_browser_state.py` — 多 Tab hack

### UI-TARS
- `other_framework/UI-TARS/codes/ui_tars/action_parser.py` — 动作解析 + pyautogui 代码生成
- `other_framework/UI-TARS/codes/ui_tars/prompt.py` — VLM 提示模板
