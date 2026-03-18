"""Demo: agent-cdp 作为 Agent Action 的事件分发层。

场景：Agent 发出 ClickAction，经过 agent-cdp 分发到：
1. 安全检查 handler（DIRECT, priority=100）— 拦截危险点击
2. 反检测执行器 handler（DIRECT, priority=50）— 模拟鼠标轨迹后点击
3. 审计日志 handler（QUEUED, priority=0）— 异步记录操作日志

运行: uv run python demo_action_dispatch.py
"""

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import BaseModel

from agent_cdp.connection import ConnectionType, connect
from agent_cdp.events import BaseEvent, EmitPolicy, event_result, event_results_list
from agent_cdp.scope import EventScope, ScopeGroup


# ╔══════════════════════════════════════════════════════════════════╗
# ║ 第一部分：定义 Action Events（Agent → Browser 方向）             ║
# ╚══════════════════════════════════════════════════════════════════╝


class ActionResult(BaseModel):
    """所有 action handler 的通用返回类型。"""

    success: bool
    detail: str
    coordinates: tuple[float, float] | None = None
    trajectory_length: int = 0


class BrowserAction(BaseEvent[ActionResult]):
    """所有浏览器动作的基类（abstract，不直接实例化）。"""

    __abstract__ = True
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST  # 安全检查失败 → 立即中断


class ClickAction(BrowserAction):
    """Agent 请求点击某个元素。"""

    selector: str = ''
    description: str = ''  # 人类可读描述，如 "点击提交按钮"


class TypeAction(BrowserAction):
    """Agent 请求在某个元素中输入文本。"""

    selector: str = ''
    text: str = ''
    clear_first: bool = True


class ScrollAction(BrowserAction):
    """Agent 请求滚动页面。"""

    direction: str = 'down'  # up / down / left / right
    amount: int = 300  # pixels


# ╔══════════════════════════════════════════════════════════════════╗
# ║ 第二部分：Handler 实现（你需要自己写的业务逻辑）                 ║
# ╚══════════════════════════════════════════════════════════════════╝


# ── 2.1 安全检查（DIRECT, priority=100）──

BLOCKED_SELECTORS = {'#delete-account', '#admin-panel', '.malicious-link'}


class SecurityViolation(Exception):
    pass


def security_check(event: BrowserAction) -> ActionResult:
    """拦截危险操作。优先级最高，consume() 后后续 handler 不执行。"""
    if isinstance(event, ClickAction) and event.selector in BLOCKED_SELECTORS:
        event.consume()
        raise SecurityViolation(f'Blocked click on forbidden selector: {event.selector}')

    if isinstance(event, TypeAction) and '<script>' in event.text.lower():
        event.consume()
        raise SecurityViolation(f'Blocked XSS injection attempt in TypeAction')

    return ActionResult(success=True, detail='security check passed')


# ── 2.2 反检测鼠标轨迹模拟器（DIRECT, priority=50）──


@dataclass
class MouseState:
    """模拟的鼠标当前位置。"""

    x: float = 0.0
    y: float = 0.0


def _generate_bezier_trajectory(
    start: tuple[float, float],
    end: tuple[float, float],
    num_points: int = 20,
) -> list[tuple[float, float]]:
    """生成贝塞尔曲线鼠标轨迹（简化版）。

    真实场景中这里会用更复杂的算法：
    - 多阶贝塞尔曲线 + 随机控制点
    - 加速/减速曲线模拟真实手部运动
    - 微小抖动模拟肌肉不稳定
    - 随机 overshoot（超过目标后回调）
    """
    sx, sy = start
    ex, ey = end

    # 随机控制点（模拟手不走直线）
    ctrl_x = (sx + ex) / 2 + random.uniform(-50, 50)
    ctrl_y = (sy + ey) / 2 + random.uniform(-50, 50)

    points: list[tuple[float, float]] = []
    for i in range(num_points + 1):
        t = i / num_points
        # 二阶贝塞尔
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * ctrl_x + t**2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * ctrl_y + t**2 * ey

        # 加入微小抖动
        x += random.gauss(0, 0.5)
        y += random.gauss(0, 0.5)
        points.append((round(x, 1), round(y, 1)))

    return points


def _resolve_selector_to_coords(selector: str) -> tuple[float, float]:
    """模拟：将 CSS selector 解析为页面坐标。

    真实场景中这里会调用 CDP Runtime.evaluate 获取元素 bounding box。
    """
    fake_elements = {
        '#submit-btn': (450.0, 320.0),
        '#search-input': (300.0, 80.0),
        '#login-btn': (500.0, 400.0),
        '.product-card': (250.0, 550.0),
        '#delete-account': (600.0, 700.0),
    }
    return fake_elements.get(selector, (random.uniform(100, 800), random.uniform(100, 600)))


# 全局鼠标状态
_mouse = MouseState()


def stealth_click_executor(event: ClickAction) -> ActionResult:
    """反检测点击执行器：模拟鼠标轨迹 → 移动 → 点击。

    这是 DIRECT handler（同步），因为 Agent 需要等点击完成后才能继续。
    真实场景中，鼠标移动会通过 CDP Input.dispatchMouseEvent 发送。
    """
    target = _resolve_selector_to_coords(event.selector)
    trajectory = _generate_bezier_trajectory((_mouse.x, _mouse.y), target)

    # 模拟移动（真实场景：循环发送 Input.dispatchMouseEvent type=mouseMoved）
    for point in trajectory:
        _mouse.x, _mouse.y = point
        # 这里会有 time.sleep(random.uniform(0.005, 0.02)) 模拟人类速度
        # Demo 中跳过真实延迟

    # 模拟点击（真实场景：Input.dispatchMouseEvent type=mousePressed + mouseReleased）
    _mouse.x, _mouse.y = target

    return ActionResult(
        success=True,
        detail=f'stealth click at ({target[0]}, {target[1]}) via {len(trajectory)}-point trajectory',
        coordinates=target,
        trajectory_length=len(trajectory),
    )


def stealth_type_executor(event: TypeAction) -> ActionResult:
    """反检测输入执行器：模拟人类打字节奏。"""
    target = _resolve_selector_to_coords(event.selector)

    # 模拟打字（真实场景：循环发送 Input.dispatchKeyEvent，每个字符间隔随机）
    char_delays = [random.uniform(0.03, 0.15) for _ in event.text]
    total_delay = sum(char_delays)

    return ActionResult(
        success=True,
        detail=f'typed {len(event.text)} chars into {event.selector} (simulated {total_delay:.2f}s)',
        coordinates=target,
    )


def scroll_executor(event: ScrollAction) -> ActionResult:
    """滚动执行器。"""
    return ActionResult(
        success=True,
        detail=f'scrolled {event.direction} by {event.amount}px',
    )


# ── 2.3 审计日志（QUEUED, priority=0）──

audit_log: list[str] = []


async def audit_logger(event: BrowserAction) -> ActionResult:
    """异步审计日志 handler。QUEUED 模式 — 不阻塞 Agent 主循环。"""
    entry = f'[AUDIT] {type(event).__name__}(id={event.event_id[:8]}...)'
    if isinstance(event, ClickAction):
        entry += f' selector={event.selector}'
    elif isinstance(event, TypeAction):
        entry += f' selector={event.selector} text_len={len(event.text)}'
    elif isinstance(event, ScrollAction):
        entry += f' direction={event.direction} amount={event.amount}'

    audit_log.append(entry)
    return ActionResult(success=True, detail='logged')


# ╔══════════════════════════════════════════════════════════════════╗
# ║ 第三部分：组装 — 把 handler 连接到 scope                        ║
# ╚══════════════════════════════════════════════════════════════════╝


async def setup_tab_scope(group: ScopeGroup, tab_id: str) -> EventScope:
    """为一个浏览器 Tab 创建 scope 并注册所有 action handler。"""
    scope = await group.create_scope(tab_id)

    # 安全检查：对所有 BrowserAction 子类生效（MRO 匹配）
    # priority=100 → 最先执行
    scope.connect(BrowserAction, security_check, mode=ConnectionType.DIRECT, priority=100)

    # 反检测执行器：按 action 类型分别注册
    # priority=50 → 安全检查通过后执行
    scope.connect(ClickAction, stealth_click_executor, mode=ConnectionType.DIRECT, priority=50)
    scope.connect(TypeAction, stealth_type_executor, mode=ConnectionType.DIRECT, priority=50)
    scope.connect(ScrollAction, scroll_executor, mode=ConnectionType.DIRECT, priority=50)

    # 审计日志：对所有 BrowserAction 子类生效，QUEUED 异步执行
    # priority=0 → 最后执行，不阻塞 Agent
    scope.connect(BrowserAction, audit_logger, mode=ConnectionType.QUEUED, priority=0)

    return scope


# ╔══════════════════════════════════════════════════════════════════╗
# ║ 第四部分：Agent 循环 — 模拟 LLM 发出一系列动作                  ║
# ╚══════════════════════════════════════════════════════════════════╝


async def agent_loop(scope: EventScope) -> None:
    """模拟 Agent 的决策-执行循环。"""

    # Agent 的任务序列（模拟 LLM 输出的 action list）
    actions: list[BrowserAction] = [
        ClickAction(selector='#search-input', description='点击搜索框'),
        TypeAction(selector='#search-input', text='agent-cdp event system'),
        ClickAction(selector='#submit-btn', description='点击搜索按钮'),
        ScrollAction(direction='down', amount=500),
        ClickAction(selector='.product-card', description='点击第一个商品'),
        # 这个会被安全检查拦截：
        ClickAction(selector='#delete-account', description='尝试删除账户（应被拦截）'),
    ]

    print('=' * 70)
    print('Agent Action Dispatch Demo — agent-cdp as middleware')
    print('=' * 70)

    for i, action in enumerate(actions, 1):
        print(f'\n--- Action {i}: {type(action).__name__} ---')

        if isinstance(action, ClickAction):
            print(f'  selector: {action.selector}')
            print(f'  description: {action.description}')
        elif isinstance(action, TypeAction):
            print(f'  selector: {action.selector}')
            print(f'  text: {action.text!r}')
        elif isinstance(action, ScrollAction):
            print(f'  direction: {action.direction}, amount: {action.amount}px')

        try:
            # Agent emit action → agent-cdp 分发到所有 handler
            event = scope.emit(action)

            # Direct handler 的结果已经在 event 里了（同步执行完毕）
            # 但 Queued handler（audit_logger）还在异步执行
            # await event 等待所有 handler 完成
            await event

            # 获取执行结果
            results = await event_results_list(event)
            for r in results:
                if isinstance(r, ActionResult) and r.detail != 'logged' and r.detail != 'security check passed':
                    print(f'  ✓ Result: {r.detail}')
                    if r.coordinates:
                        print(f'    coords: {r.coordinates}')
                    if r.trajectory_length:
                        print(f'    trajectory: {r.trajectory_length} points')

        except SecurityViolation as e:
            print(f'  ✗ BLOCKED: {e}')

        except Exception as e:
            print(f'  ✗ ERROR: {type(e).__name__}: {e}')


# ╔══════════════════════════════════════════════════════════════════╗
# ║ 第五部分：主入口                                                ║
# ╚══════════════════════════════════════════════════════════════════╝


async def main() -> None:
    group = ScopeGroup('browser')

    # 为 Tab 创建 scope 并注册 handler
    tab = await setup_tab_scope(group, 'tab-1')

    # 运行 Agent 循环
    await agent_loop(tab)

    # 打印审计日志
    print('\n' + '=' * 70)
    print(f'Audit Log ({len(audit_log)} entries):')
    print('=' * 70)
    for entry in audit_log:
        print(f'  {entry}')

    # 清理
    await group.close_all()
    print('\nAll scopes closed. Done.')


if __name__ == '__main__':
    asyncio.run(main())
