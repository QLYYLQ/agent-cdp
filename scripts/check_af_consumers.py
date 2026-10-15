"""Exercise actual AF watchdog consumers through this checkout's public API.

Run with AF's dependency environment, without changing AF's files or environment::

    /path/to/af/.venv/bin/python scripts/check_af_consumers.py --af-root /path/to/af

The CDP transport is deterministic. This does not claim that unchanged AF's
BrowserCapability setup has completed its 0.5 migration or replace real Chrome QA.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / 'src'


async def check_consumers(af_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(af_root))
    sys.path.insert(0, str(_SOURCE_ROOT))

    from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
    from spectragent.core.capabilities.browser.watchdogs.browser_meta_watchdog import BrowserMetaWatchdog
    from spectragent.core.capabilities.browser.watchdogs.events import (
        BrowserStateRequestEvent,
        BrowserStateSummary,
        ExecuteJavaScriptEvent,
        NavigateToUrlEvent,
    )
    from spectragent.core.capabilities.browser.watchdogs.js_execution import JavaScriptExecutionWatchdog
    from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
    from spectragent.core.capabilities.browser.watchdogs.security_watchdog import SecurityWatchdog

    from agent_cdp import (
        BaseEvent,
        ConnectionType,
        EventExecutionError,
        Participation,
        RequestContract,
        ResultContract,
        ScopeGroup,
        event_result,
        event_results_list,
    )

    png = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6E1sAAAAASUVORK5CYII='
    )

    class CDP:
        """Only the browser transport is replaced; all watchdog handlers are real AF."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any], str | None]] = []
            self.callbacks: dict[str, list[Any]] = {}
            self.navigation_error = False

        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            session_id: str | None = None,
            *,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            params = params or {}
            self.calls.append((method, params, session_id))
            if method == 'Page.captureScreenshot':
                return {'data': base64.b64encode(png).decode()}
            if method == 'Page.getNavigationHistory':
                return {'currentIndex': 0, 'entries': [{'url': 'https://example.com/', 'title': 'AF consumer'}]}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'frame', 'loaderId': 'loader', 'url': 'https://example.com/'}}}
            if method == 'Runtime.evaluate':
                return {'result': {'type': 'string', 'value': f'{session_id}:{params["expression"]}'}}
            if method == 'Page.navigate':
                return {'errorText': 'navigation refused'} if self.navigation_error else {'frameId': 'frame'}
            return {}

        def on_event(self, method: str, callback: Any) -> None:
            self.callbacks.setdefault(method, []).append(callback)

        def off_event(self, method: str, callback: Any) -> None:
            self.callbacks[method].remove(callback)

    class ObserveJsEvent(BaseEvent[dict[str, Any]]):
        __registry_key__ = 'agent_cdp_af_consumer_probe'

    cdp = CDP()
    group = ScopeGroup('actual-af-consumers')
    observation = await group.create_scope('tab-a', metadata={'target_id': 'tab-a'}, max_history_size=0)
    js_scope = await group.create_scope('tab-a-js', max_history_size=0)
    other = await group.create_scope('tab-b', max_history_size=0)
    other_js = await group.create_scope('tab-b-js', max_history_size=0)
    shared_js = JavaScriptExecutionWatchdog(cdp)
    shared_js.attach(observation)
    shared_js.bind_tab_scopes(observation, js_scope, session_id='session-a')
    shared_js.bind_tab_scopes(other, other_js, session_id='session-b')
    ScreenshotWatchdog(cdp).attach(observation, session_id='session-a')
    BrowserMetaWatchdog(cdp).attach(observation, session_id='session-a')
    SecurityWatchdog(allowed_domains=['example.com']).attach(observation, session_id='session-a')
    ActionWatchdog(cdp, input_platform='macos').attach(observation, session_id='session-a')
    observation.connect_all(lambda event: None, participation=Participation.OBSERVER)

    async def resolve_js(event: ObserveJsEvent) -> dict[str, Any]:
        request = observation.emit(
            ExecuteJavaScriptEvent(
                method='Runtime.evaluate',
                params={'expression': 'document.title'},
                session_id='session-a',
                request_contract=RequestContract(min_receivers=1),
            )
        )
        result = await event_result(request)
        assert isinstance(result, dict)
        return result

    observation.connect(ObserveJsEvent, resolve_js, mode=ConnectionType.QUEUED)
    try:
        request = observation.emit(
            BrowserStateRequestEvent(
                fragments=frozenset({'browser.screenshot', 'browser.browser_meta'}),
                request_contract=RequestContract(
                    min_receivers=2,
                    result_contract=ResultContract(lambda value: isinstance(value, BrowserStateSummary)),
                ),
            )
        )
        summaries = await event_results_list(request, timeout=1)
        assert len(summaries) == 2
        fragments = {key: value for summary in summaries for key, value in summary.fragments.items()}
        assert fragments['browser.screenshot'].image_bytes == png
        assert fragments['browser.browser_meta'].url == 'https://example.com/'
        assert all(summary.document.loader_id == 'loader' for summary in summaries)

        navigation_rule = ResultContract(lambda value: value is None or 'error' not in value)
        allowed = observation.emit(
            NavigateToUrlEvent(
                url='https://example.com/',
                wait_until='',
                request_contract=RequestContract(min_receivers=2, result_contract=navigation_rule),
            )
        )
        await allowed.wait(timeout=1)
        allowed.check_success()
        before = len([call for call in cdp.calls if call[0] == 'Page.navigate'])
        try:
            observation.emit(NavigateToUrlEvent(url='https://blocked.example/', wait_until=''))
        except ValueError:
            pass
        else:
            raise AssertionError('Actual SecurityWatchdog did not block the navigation')
        assert len([call for call in cdp.calls if call[0] == 'Page.navigate']) == before

        cdp.navigation_error = True
        refused = observation.emit(
            NavigateToUrlEvent(
                url='https://example.com/',
                wait_until='',
                request_contract=RequestContract(min_receivers=2, result_contract=navigation_rule),
            )
        )
        await refused.wait(timeout=1)
        try:
            refused.check_success()
        except EventExecutionError as error:
            assert len(error.failures) == 1
            assert 'navigation refused' in error.failures[0].result['error']
        else:
            raise AssertionError('Actual ActionWatchdog business failure was mistaken for success')

        reply = await event_result(observation.emit(ObserveJsEvent()), timeout=1)
        assert reply['result']['value'] == 'session-a:document.title'
        await observation.close()
        await js_scope.close()
        request_b = other.emit(
            ExecuteJavaScriptEvent(
                method='Runtime.evaluate',
                params={'expression': 'document.title'},
                session_id='session-b',
                request_contract=RequestContract(min_receivers=1),
            )
        )
        reply_b = await event_result(request_b, timeout=1)
        assert reply_b['result']['value'] == 'session-b:document.title'
        assert not observation.event_history
        return {
            'status': 'passed',
            'af_root': str(af_root),
            'watchdogs': [
                'ScreenshotWatchdog',
                'BrowserMetaWatchdog',
                'SecurityWatchdog',
                'ActionWatchdog',
                'JavaScriptExecutionWatchdog',
            ],
            'checks': [
                'combined screenshot/meta contributions',
                'security prevents navigation',
                'allowed navigation succeeds',
                'explicit AF business-error rule',
                'queued observation awaits independent JS scope',
                'shared JS survives closing another tab',
            ],
            'transport': 'deterministic fake CDP; actual imported AF watchdog implementations',
        }
    finally:
        shared_js.detach()
        await group.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--af-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check_consumers(args.af_root.resolve())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
