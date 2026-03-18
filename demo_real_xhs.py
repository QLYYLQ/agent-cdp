"""真实场景 Demo: agent-cdp 驱动 Chrome 访问小红书。

架构:
  Agent 决策循环
    → agent-cdp EventScope.emit(action)
      → [DIRECT p=90]  SecurityCheck: 拦截危险操作
      → [QUEUED p=80]  DOMExtractor: 通过 CDP 提取/过滤页面信息
      → [QUEUED p=50]  StealthExecutor: 贝塞尔曲线鼠标轨迹 + 真实 CDP Input
      → [QUEUED p=0]   AuditLogger: 异步审计日志

运行: uv run python demo_real_xhs.py
前提: Chrome --remote-debugging-port=9222 已启动并打开 xiaohongshu.com
"""

import asyncio
import json
import random
import time
from typing import Any, ClassVar

from pydantic import BaseModel

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent, EmitPolicy, event_results_list
from agent_cdp.scope import ScopeGroup
from demo.cdp_client import CDPClient

cdp: CDPClient | None = None


# ════════════════════════════════════════════════════════════════
# 事件定义
# ════════════════════════════════════════════════════════════════


class ActionResult(BaseModel):
    success: bool
    detail: str
    data: Any = None


class BrowserAction(BaseEvent[ActionResult]):
    __abstract__ = True
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST


class ClickAction(BrowserAction):
    selector: str = ''
    description: str = ''


class ScrollAction(BrowserAction):
    direction: str = 'down'
    amount: int = 500


class ExtractDOMAction(BrowserAction):
    js_expression: str = ''  # 直接传 JS 代码，最灵活
    description: str = ''


class NavigateAction(BrowserAction):
    url: str = ''


# ════════════════════════════════════════════════════════════════
# Handler 实现
# ════════════════════════════════════════════════════════════════


# ── 安全检查 (DIRECT, priority=90) ──

BLOCKED_DOMAINS = {'malicious-site.com', 'phishing.example.com'}


class SecurityViolation(Exception):
    pass


def security_check(event: BrowserAction) -> ActionResult:
    if isinstance(event, NavigateAction):
        from urllib.parse import urlparse
        domain = urlparse(event.url).netloc
        if domain in BLOCKED_DOMAINS:
            event.consume()
            raise SecurityViolation(f'Blocked navigation to {domain}')
    return ActionResult(success=True, detail='security: pass')


# ── DOM 提取器 (QUEUED, priority=80) ──

async def dom_extractor(event: ExtractDOMAction) -> ActionResult:
    """通过 CDP Runtime.evaluate 执行 JS 提取页面信息。"""
    assert cdp is not None
    raw = await cdp.evaluate(event.js_expression)
    if raw is None:
        return ActionResult(success=False, detail='JS returned null')
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        data = raw
    count = len(data) if isinstance(data, list) else 1
    return ActionResult(success=True, detail=f'extracted {count} items', data=data)


# ── 反检测鼠标轨迹点击 (QUEUED, priority=50) ──

async def stealth_click(event: ClickAction) -> ActionResult:
    """贝塞尔曲线鼠标轨迹 → 真实 CDP Input.dispatchMouseEvent。"""
    assert cdp is not None

    # 1. 获取元素坐标
    js = f"""
    (() => {{
        const el = document.querySelector('{event.selector}');
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        return JSON.stringify({{
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2,
            text: el.textContent.trim().substring(0, 40)
        }});
    }})()
    """
    raw = await cdp.evaluate(js)
    if not raw:
        return ActionResult(success=False, detail=f'element not found: {event.selector}')
    target = json.loads(raw)
    tx, ty = target['x'], target['y']

    # 2. 贝塞尔曲线轨迹
    sx, sy = random.uniform(200, 600), random.uniform(100, 400)
    trajectory = _bezier_trajectory((sx, sy), (tx, ty), steps=random.randint(18, 35))

    # 3. 逐点发送 mouseMoved
    for px, py in trajectory:
        await cdp.send('Input.dispatchMouseEvent', {
            'type': 'mouseMoved', 'x': px, 'y': py,
            'button': 'none', 'timestamp': time.time(),
        })
        await asyncio.sleep(random.uniform(0.004, 0.02))

    # 4. 点击 (mousePressed + mouseReleased)
    await asyncio.sleep(random.uniform(0.05, 0.12))
    for etype in ('mousePressed', 'mouseReleased'):
        await cdp.send('Input.dispatchMouseEvent', {
            'type': etype, 'x': tx, 'y': ty,
            'button': 'left', 'clickCount': 1, 'timestamp': time.time(),
        })
        if etype == 'mousePressed':
            await asyncio.sleep(random.uniform(0.04, 0.10))

    return ActionResult(
        success=True,
        detail=f'stealth click ({tx:.0f},{ty:.0f}) trajectory={len(trajectory)}pts target="{target["text"]}"',
        data=target,
    )


# ── 反检测滚动 (QUEUED, priority=50) ──

async def stealth_scroll(event: ScrollAction) -> ActionResult:
    """多次小幅滚动模拟人类行为。"""
    assert cdp is not None
    total = event.amount
    scrolled = 0
    n_steps = random.randint(5, 10)
    sign = 1 if event.direction == 'down' else -1

    for _ in range(n_steps):
        chunk = total // n_steps + random.randint(-15, 15)
        chunk = max(10, min(chunk, total - scrolled))
        if scrolled >= total:
            break
        await cdp.send('Input.dispatchMouseEvent', {
            'type': 'mouseWheel',
            'x': random.randint(400, 800), 'y': random.randint(300, 500),
            'deltaX': 0, 'deltaY': chunk * sign,
            'timestamp': time.time(),
        })
        scrolled += chunk
        await asyncio.sleep(random.uniform(0.03, 0.08))

    return ActionResult(success=True, detail=f'scroll {event.direction} {scrolled}px in {n_steps} steps')


# ── 导航 (QUEUED, priority=50) ──

async def navigate_executor(event: NavigateAction) -> ActionResult:
    assert cdp is not None
    await cdp.send('Page.navigate', {'url': event.url})
    await asyncio.sleep(2)
    return ActionResult(success=True, detail=f'navigated to {event.url}')


# ── 审计日志 (QUEUED, priority=0) ──

audit_log: list[str] = []


async def audit_logger(event: BrowserAction) -> ActionResult:
    ts = time.strftime('%H:%M:%S')
    name = type(event).__name__
    eid = event.event_id[:8]
    audit_log.append(f'{ts} | {name:20s} | {eid}')
    return ActionResult(success=True, detail='logged')


# ════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════


def _bezier_trajectory(start: tuple[float, float], end: tuple[float, float], steps: int = 20) -> list[tuple[float, float]]:
    sx, sy = start
    ex, ey = end
    cx = (sx + ex) / 2 + random.uniform(-80, 80)
    cy = (sy + ey) / 2 + random.uniform(-60, 60)
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep easing
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey
        pts.append((round(x + random.gauss(0, 0.6), 2), round(y + random.gauss(0, 0.6), 2)))
    return pts


def _get_executor_result(results: list[ActionResult]) -> ActionResult | None:
    """从结果列表中找到执行器的结果（跳过 security: pass 和 logged）。"""
    for r in results:
        if r.detail not in ('security: pass', 'logged', 'dom_filter: pass'):
            return r
    return results[0] if results else None


# ════════════════════════════════════════════════════════════════
# Agent 主循环
# ════════════════════════════════════════════════════════════════


async def run_action(scope: Any, action: BrowserAction, label: str) -> ActionResult | None:
    """统一的 action 执行 + 结果提取。"""
    print(f'\n  [{label}] {action.__class__.__name__}: ', end='')
    if isinstance(action, ClickAction):
        print(action.description or action.selector)
    elif isinstance(action, ScrollAction):
        print(f'{action.direction} {action.amount}px')
    elif isinstance(action, ExtractDOMAction):
        print(action.description)
    elif isinstance(action, NavigateAction):
        print(action.url)
    else:
        print()

    try:
        event = scope.emit(action)
        await event
        results = await event_results_list(event)
        r = _get_executor_result(results)
        if r:
            print(f'       → {r.detail}')
        return r
    except SecurityViolation as e:
        print(f'       → BLOCKED: {e}')
        return None


async def main() -> None:
    global cdp

    print('=' * 70)
    print('  agent-cdp 真实浏览器 Demo: 小红书 (xiaohongshu.com)')
    print('=' * 70)

    # ── 连接 ──
    import urllib.request
    data = json.loads(urllib.request.urlopen('http://127.0.0.1:9222/json').read())
    ws_url = next(p['webSocketDebuggerUrl'] for p in data if 'xiaohongshu' in p.get('url', ''))
    print(f'\n[CDP] Connecting to {ws_url[:50]}...')
    cdp = CDPClient(ws_url)
    await cdp.connect()
    # Enable CDP domains (the demo CDPClient doesn't auto-enable)
    await cdp.send('DOM.enable')
    await cdp.send('Runtime.enable')
    await cdp.send('Page.enable')
    print('[CDP] Connected!')

    # ── 关掉登录弹窗 ──
    await cdp.evaluate("""
        document.querySelectorAll('[class*="mask"], [class*="login-container"]').forEach(el => el.remove());
    """)
    # 滚回顶部
    await cdp.evaluate('window.scrollTo(0, 0)')
    await asyncio.sleep(0.5)

    # ── 创建 scope ──
    group = ScopeGroup('xhs')
    scope = await group.create_scope('tab-1')

    scope.connect(BrowserAction, security_check, mode=ConnectionType.DIRECT, priority=90)
    scope.connect(ExtractDOMAction, dom_extractor, mode=ConnectionType.QUEUED, priority=80)
    scope.connect(ClickAction, stealth_click, mode=ConnectionType.QUEUED, priority=50)
    scope.connect(ScrollAction, stealth_scroll, mode=ConnectionType.QUEUED, priority=50)
    scope.connect(NavigateAction, navigate_executor, mode=ConnectionType.QUEUED, priority=50)
    scope.connect(BrowserAction, audit_logger, mode=ConnectionType.QUEUED, priority=0)

    print('\n[agent-cdp] Scope + handlers ready')
    print('  DIRECT: security_check (p=90)')
    print('  QUEUED: dom_extractor (p=80), stealth_click/scroll (p=50), audit (p=0)')

    # ════════════════════════════════════════════════════════
    # Agent 任务序列
    # ════════════════════════════════════════════════════════
    print('\n' + '=' * 70)
    print('  Agent Task: 浏览小红书、提取笔记、切换分类、点击内容')
    print('=' * 70)

    # ── A: 提取首页笔记列表 ──
    r = await run_action(scope, ExtractDOMAction(
        description='提取首页推荐笔记',
        js_expression="""
        (() => {
            const notes = document.querySelectorAll('section.note-item');
            const items = [];
            notes.forEach((n, i) => {
                if (i >= 8) return;
                const text = n.textContent.trim();
                // 小红书 note-item 结构: 标题 + 作者 + 点赞数
                const parts = text.split('\\n').filter(s => s.trim());
                items.push({
                    title: parts[0] || '',
                    author: parts[1] || '',
                    likes: parts[2] || '',
                });
            });
            return JSON.stringify(items);
        })()
        """,
    ), 'A')

    if r and r.data:
        print('       笔记列表:')
        for i, note in enumerate(r.data[:6], 1):
            title = note.get('title', '')[:35]
            author = note.get('author', '')[:12]
            likes = note.get('likes', '')
            print(f'         {i}. {title}  —{author}  ♡{likes}')

    # ── B: 模拟鼠标滚动，加载更多 ──
    await run_action(scope, ScrollAction(direction='down', amount=500), 'B')
    await asyncio.sleep(1.5)  # 等待懒加载

    # ── C: 滚回顶部，准备点击分类 ──
    await cdp.evaluate('window.scrollTo({top: 0, behavior: "smooth"})')
    await asyncio.sleep(0.8)

    # ── D: 点击"美食"分类（贝塞尔曲线鼠标轨迹） ──
    await run_action(scope, ClickAction(
        selector='div.channel',  # XHS 分类标签的真实 class
        description='点击第一个分类标签（stealth trajectory）',
    ), 'D')
    await asyncio.sleep(1.5)

    # ── E: 精确点击"美食"标签 — mark via DOM, then click via event system ──
    # Step 1: Use ExtractDOMAction to find and mark the target element
    await run_action(scope, ExtractDOMAction(
        description='标记"美食"标签 (data-target)',
        js_expression="""
        (() => {
            const divs = document.querySelectorAll('div.channel');
            for (const d of divs) {
                if (d.textContent.trim() === '美食') {
                    d.setAttribute('data-target', 'meishi');
                    return JSON.stringify({found: true, text: d.textContent.trim()});
                }
            }
            return JSON.stringify({found: false});
        })()
        """,
    ), 'E-mark')
    # Step 2: Click via event system — stealth_click handler applies bezier trajectory
    await run_action(scope, ClickAction(
        selector='[data-target="meishi"]',
        description='stealth-click "美食" 标签',
    ), 'E')
    await asyncio.sleep(2)

    # ── F: 提取美食分类下的笔记 ──
    r2 = await run_action(scope, ExtractDOMAction(
        description='提取"美食"分类的笔记',
        js_expression="""
        (() => {
            const notes = document.querySelectorAll('section.note-item');
            const items = [];
            notes.forEach((n, i) => {
                if (i >= 6) return;
                const text = n.textContent.trim();
                const parts = text.split('\\n').filter(s => s.trim());
                items.push({title: parts[0] || '', author: parts[1] || '', likes: parts[2] || ''});
            });
            return JSON.stringify(items);
        })()
        """,
    ), 'F')

    if r2 and r2.data:
        print('       美食笔记:')
        for i, note in enumerate(r2.data[:5], 1):
            title = note.get('title', '')[:35]
            author = note.get('author', '')[:12]
            likes = note.get('likes', '')
            print(f'         {i}. {title}  —{author}  ♡{likes}')

    # ── G: 滚动查看更多美食内容 ──
    await run_action(scope, ScrollAction(direction='down', amount=600), 'G')
    await asyncio.sleep(1)

    # ── H: 安全拦截测试 ──
    await run_action(scope, NavigateAction(url='https://malicious-site.com/steal'), 'H')

    # ════════════════════════════════════════════════════════
    # 结果汇总
    # ════════════════════════════════════════════════════════
    print('\n' + '=' * 70)
    print('  Summary')
    print('=' * 70)
    print(f'  Total events dispatched: {len(scope.event_history)}')
    print(f'  Audit log ({len(audit_log)} entries):')
    for entry in audit_log:
        print(f'    {entry}')

    # 截图
    screenshot = await cdp.send('Page.captureScreenshot', {'format': 'png'})
    import base64
    with open('/tmp/xhs_final.png', 'wb') as f:
        f.write(base64.b64decode(screenshot['data']))
    print('\n  Final screenshot saved: /tmp/xhs_final.png')

    await group.close_all()
    await cdp.close()
    print('  Done.\n')


if __name__ == '__main__':
    asyncio.run(main())
