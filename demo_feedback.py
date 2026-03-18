"""Nano example: Action 如何拿到反馈。

核心机制: handler 的 return 值自动写入 event.event_results，
Agent 通过 await event + 聚合函数取回。

运行: uv run python demo_feedback.py
"""

import asyncio
from typing import ClassVar

from pydantic import BaseModel

from agent_cdp.connection import ConnectionType
from agent_cdp.events import (
    BaseEvent,
    EmitPolicy,
    event_result,
    event_results_by_handler_name,
    event_results_list,
)
from agent_cdp.scope import ScopeGroup


# ── 定义结果类型 ──


class ClickResult(BaseModel):
    coords: tuple[float, float]
    trajectory_points: int
    element_text: str


class ClickAction(BaseEvent[ClickResult]):
    """BaseEvent[ClickResult] — 声明 handler 应返回 ClickResult。"""

    selector: str = ''
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST


# ── Handler: 每个 handler 的 return 就是反馈 ──


def security_check(event: ClickAction) -> ClickResult:
    # 安全检查不产生真正的 ClickResult，返回一个标记
    return ClickResult(coords=(0, 0), trajectory_points=0, element_text='security:pass')


async def stealth_executor(event: ClickAction) -> ClickResult:
    await asyncio.sleep(0.01)
    # 真实场景：CDP 获取坐标 + 模拟鼠标轨迹 + 点击
    return ClickResult(
        coords=(450.0, 320.0),
        trajectory_points=25,
        element_text='提交按钮',
    )


async def screenshot_recorder(event: ClickAction) -> ClickResult:
    await asyncio.sleep(0.01)
    return ClickResult(coords=(0, 0), trajectory_points=0, element_text='screenshot:saved')


# ── 展示 4 种取回反馈的方式 ──


async def main() -> None:
    group = ScopeGroup('demo')
    scope = await group.create_scope('tab')

    c1 = scope.connect(ClickAction, security_check, mode=ConnectionType.DIRECT, priority=100)
    c2 = scope.connect(ClickAction, stealth_executor, mode=ConnectionType.QUEUED, priority=50)
    c3 = scope.connect(ClickAction, screenshot_recorder, mode=ConnectionType.QUEUED, priority=0)

    event = scope.emit(ClickAction(selector='#submit'))
    await event  # 等所有 QUEUED handler 完成

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方式 1: event_result() — 取第一个成功结果
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    first = await event_result(event)
    print(f'方式1 event_result():        {first}')
    # → security_check 的结果（第一个完成的）

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方式 2: event_results_list() — 所有结果的列表
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    all_results = await event_results_list(event)
    print(f'方式2 event_results_list():  {all_results}')
    # → [security的, executor的, recorder的]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方式 3: event_results_by_handler_name() — 按 handler 名查
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    by_name = await event_results_by_handler_name(event)
    print(f'方式3 by_handler_name():     {by_name}')
    # → {'security_check': result, 'stealth_executor': result, ...}

    # 精确取 executor 的反馈:
    executor_feedback = by_name['stealth_executor']
    print(f'  → executor 反馈: coords={executor_feedback.coords}, trajectory={executor_feedback.trajectory_points}pts')

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方式 4: event.event_results[conn.id] — 用 connection id 精确取
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    er = event.event_results[c2.id]  # c2 = stealth_executor 的连接
    print(f'方式4 event_results[conn.id]:')
    print(f'  → result:       {er.result}')
    print(f'  → status:       {er.status}')
    print(f'  → handler_name: {er.handler_name}')
    print(f'  → error:        {er.error}')

    await group.close_all()


if __name__ == '__main__':
    asyncio.run(main())
