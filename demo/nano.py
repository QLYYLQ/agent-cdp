"""Nano example: agent-cdp 作为 Action 分发层的最小可运行示例。

只保留核心骨架，展示事件流是怎么走的。

运行: uv run python -m demo.nano
"""

import asyncio
from typing import ClassVar

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent, EmitPolicy, event_results_list
from agent_cdp.scope import ScopeGroup

# ── 1. 定义事件 ──


class ClickAction(BaseEvent[str]):
    """Agent 想点击某个东西。T=str 表示 handler 返回 str 类型结果。"""

    selector: str = ''
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST


# ── 2. 定义 handler ──


class Blocked(Exception):
    pass


def security_check(event: ClickAction) -> str:
    """DIRECT handler: emit() 调用栈内同步执行。"""
    if 'danger' in event.selector:
        event.consume()  # 阻止后续 handler
        raise Blocked(event.selector)
    return 'safe'


async def stealth_executor(event: ClickAction) -> str:
    """QUEUED handler: 进入 scope 事件循环，异步执行。"""
    # 这里放真实的 CDP Input.dispatchMouseEvent 调用
    await asyncio.sleep(0.01)  # 模拟鼠标轨迹耗时
    return f'clicked {event.selector}'


async def audit_log(event: ClickAction) -> str:
    """QUEUED handler: 异步记录日志。"""
    return f'logged {event.selector}'


# ── 3. 组装并运行 ──


async def main() -> None:
    group = ScopeGroup('browser')
    scope = await group.create_scope('tab-1')

    # 注册 handler — priority 决定执行顺序
    scope.connect(ClickAction, security_check, mode=ConnectionType.DIRECT, priority=100)
    scope.connect(ClickAction, stealth_executor, mode=ConnectionType.QUEUED, priority=50)
    scope.connect(ClickAction, audit_log, mode=ConnectionType.QUEUED, priority=0)

    # ── 正常点击 ──
    event = scope.emit(ClickAction(selector='#submit'))
    # ↑ emit() 是同步的
    # ↑ security_check 已经在 emit() 内执行完毕（DIRECT）
    # ↑ stealth_executor 和 audit_log 被丢进 scope 事件循环队列（QUEUED）

    await event  # 等待所有 QUEUED handler 完成

    results = await event_results_list(event)
    print('正常点击结果:', [r for r in results])
    # → ['safe', 'clicked #submit', 'logged #submit']

    # ── 被拦截的点击 ──
    try:
        scope.emit(ClickAction(selector='#danger-btn'))
    except Blocked as e:
        print(f'拦截成功: {e}')
        # security_check 调用了 event.consume() + raise
        # stealth_executor 和 audit_log 根本没执行

    await group.close_all()


if __name__ == '__main__':
    asyncio.run(main())
