"""Read-only Douban/Xiaohongshu/Amazon acceptance with real shared watchdogs.

Run: AGENT_CDP_LIVE_SITES=1 pytest tests/test_live_sites.py -q -s
Artifacts: tmp/acceptance/live-sites.json and screenshots (ignored by git).
External challenges and error pages are reported explicitly in the artifact.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any
from urllib.parse import urlparse

import pytest
from anyio import Path

from agent_cdp.advanced.expect import expect
from agent_cdp.bridge import CDPEventBridge
from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent, event_result
from agent_cdp.events.contracts import Participation, RequestContract, ResultContract
from agent_cdp.scope import ScopeGroup
from demo.events import NavigateToUrlEvent, ScreenshotEvent
from demo.watchdogs import PopupsWatchdog, ScreenshotWatchdog, SecurityWatchdog, make_navigation_handler
from tests.chrome_support import isolated_chrome

pytestmark = pytest.mark.skipif(
    os.environ.get('AGENT_CDP_LIVE_SITES') != '1', reason='set AGENT_CDP_LIVE_SITES=1 for live website acceptance'
)


class LivePageLoaded(BaseEvent[None]):
    route: str | None


class LiveDocumentResponse(BaseEvent[None]):
    route: str | None
    url: str
    status: int
    resource_type: str
    frame_id: str


class LiveResourceFailed(BaseEvent[None]):
    resource_type: str
    error_text: str
    blocked_reason: str


async def test_live_sites_shared_watchdogs_scopes_bridges_and_cleanup() -> None:
    output = Path('tmp/acceptance')
    await output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {'sites': [], 'shared_watchdog_survives_scope_close': False, 'cleanup_complete': False}
    async with isolated_chrome() as cdp:
        report['browser'] = await cdp.send('Browser.getVersion')
        group = ScopeGroup('live-acceptance')
        screenshots = ScreenshotWatchdog(cdp)
        security = SecurityWatchdog(['douban.com', 'xiaohongshu.com', 'amazon.com'])
        popups = PopupsWatchdog(cdp)
        scopes = []
        bridges = []
        targets: list[str] = []
        try:
            for name, url in [
                ('douban', 'https://www.douban.com/'),
                ('xiaohongshu', 'https://www.xiaohongshu.com/'),
                ('amazon', 'https://www.amazon.com/'),
            ]:
                target, session = await cdp.create_tab()
                targets.append(target)
                frame_tree = await cdp.send('Page.getFrameTree', session_id=session)
                main_frame = frame_tree['frameTree']['frame']['id']
                scope = await group.create_scope(
                    name,
                    queue_maxsize=65536,
                    max_history_size=0,
                    metadata={'target_id': target, 'session_id': session},
                )
                scopes.append(scope)
                screenshots.attach(scope, session)
                security.attach(scope, session)
                popups.attach(scope, session)
                navigation = scope.connect(
                    NavigateToUrlEvent,
                    make_navigation_handler(cdp, session, scope),
                    mode=ConnectionType.QUEUED,
                    receiver_key='navigate',
                    result_contract=ResultContract(
                        lambda value: isinstance(value, str) and value.startswith('https://')
                    ),
                )
                bridge = CDPEventBridge(cdp, scope, session_id=session)
                bridges.append(bridge)
                bridge.bridge(
                    'Page.loadEventFired',
                    lambda params, route: LivePageLoaded(route=route),
                    with_session=True,
                )
                bridge.bridge(
                    'Network.responseReceived',
                    lambda params, route: LiveDocumentResponse(
                        route=route,
                        url=params.get('response', {}).get('url', ''),
                        status=params.get('response', {}).get('status', 0),
                        resource_type=params.get('type', ''),
                        frame_id=params.get('frameId', ''),
                    ),
                    with_session=True,
                )
                bridge.bridge(
                    'Network.loadingFailed',
                    lambda params: LiveResourceFailed(
                        resource_type=params.get('type', ''),
                        error_text=params.get('errorText', ''),
                        blocked_reason=params.get('blockedReason', ''),
                    ),
                )
                failures: list[dict[str, str]] = []

                def failure_observer(event: LiveResourceFailed) -> None:
                    failures.append(
                        {
                            'type': event.resource_type,
                            'error': event.error_text,
                            'blocked_reason': event.blocked_reason,
                        }
                    )

                failure_connection = scope.connect(
                    LiveResourceFailed,
                    failure_observer,
                    mode=ConnectionType.DIRECT,
                    participation=Participation.OBSERVER,
                )
                responses: list[dict[str, Any]] = []
                routes: set[str | None] = set()

                def response_observer(event: LiveDocumentResponse) -> None:
                    routes.add(event.route)
                    if event.resource_type == 'Document':
                        responses.append({'url': event.url, 'status': event.status, 'frame_id': event.frame_id})

                observer = scope.connect(
                    LiveDocumentResponse,
                    response_observer,
                    mode=ConnectionType.DIRECT,
                    participation=Participation.OBSERVER,
                )
                site: dict[str, Any] = {'name': name, 'requested_url': url, 'session': session}
                report['sites'].append(site)
                with expect(scope, LivePageLoaded, timeout=25) as loaded:
                    action = scope.emit(
                        NavigateToUrlEvent(
                            url=url,
                            execution_timeout=40,
                            request_contract=RequestContract(required_receivers=('navigate',), wait_for='required'),
                        )
                    )
                    await action.wait(timeout=45)
                    action.check_success()
                    assert action.event_results[navigation.id].result == url
                    try:
                        page_event = await loaded
                        assert page_event.route == session
                        site['load_event'] = True
                    except TimeoutError:
                        site['load_event'] = False
                site['dom'] = await cdp.evaluate(
                    '({url: location.href, title: document.title, readyState: document.readyState, '
                    "text: (document.body?.innerText || '').slice(0, 1800), "
                    "elements: document.querySelectorAll('*').length})",
                    session_id=session,
                )
                # Some sites deliver an initial script-only HTTP 202 document.
                # Allow ordinary rendering to settle; do not bypass challenges.
                if not site['dom']['text'].strip():
                    await asyncio.sleep(5)
                    site['dom'] = await cdp.evaluate(
                        '({url: location.href, title: document.title, readyState: document.readyState, '
                        "text: (document.body?.innerText || '').slice(0, 1800), "
                        "elements: document.querySelectorAll('*').length})",
                        session_id=session,
                    )
                site['assets'] = await cdp.evaluate(
                    '({stylesheet_count: document.styleSheets.length, image_count: document.images.length, '
                    'loaded_images: [...document.images].filter(i => i.complete && i.naturalWidth > 0).length, '
                    'failed_images: [...document.images].filter(i => i.complete && i.naturalWidth === 0).length})',
                    session_id=session,
                )
                site['resource_failures'] = list(failures)
                site['partial_assets'] = bool(site['assets']['failed_images'] or failures)
                shot = scope.emit(ScreenshotEvent(request_contract=RequestContract(min_receivers=1)))
                image = await event_result(shot, timeout=15)
                shot.check_success()
                assert isinstance(image, str) and image
                image_bytes = base64.b64decode(image)
                assert image_bytes.startswith(b'\x89PNG\r\n\x1a\n')
                path = output / f'{name}.png'
                await path.write_bytes(image_bytes)
                site['screenshot'] = str(path)
                site['screenshot_bytes'] = len(image_bytes)
                site['document_responses'] = list(responses)
                site['bridge_hits'] = bridge.hit_counts
                site['routes_match'] = routes == {session}
                site['flush_failures'] = len((await scope.flush(timeout=5)).failures)
                assert site['routes_match'] and site['flush_failures'] == 0
                assert urlparse(site['dom']['url']).hostname is not None
                assert site['dom']['elements'] > 0
                text = (site['dom']['title'] + ' ' + site['dom']['text']).lower()
                site['challenge_or_error_indicators'] = [
                    word
                    for word in [
                        'captcha',
                        'robot',
                        '安全验证',
                        '安全限制',
                        'ip存在风险',
                        'access denied',
                        'sorry',
                        '不可用',
                    ]
                    if word in text
                ]
                if '/website-login/error' in site['dom']['url']:
                    site['challenge_or_error_indicators'].append('/website-login/error')
                site['content_present'] = bool(site['dom']['text'].strip())
                main_responses = [response for response in responses if response['frame_id'] == main_frame]
                site['main_document_status'] = main_responses[-1]['status'] if main_responses else None
                expected_host = name + '.com'
                host = urlparse(site['dom']['url']).hostname or ''
                same_site = host == expected_host or host.endswith('.' + expected_host)
                site['framework_passed'] = True
                site['access_ok'] = bool(
                    site['main_document_status'] is not None
                    and 200 <= site['main_document_status'] < 400
                    and same_site
                    and not site['challenge_or_error_indicators']
                    and site['content_present']
                )
                if site['challenge_or_error_indicators']:
                    site['access_status'] = 'blocked'
                elif not site['content_present']:
                    site['access_status'] = 'incomplete_blank_document'
                else:
                    site['access_status'] = 'accessible' if site['access_ok'] else 'unavailable'
                if site['access_status'] == 'accessible' and site['partial_assets']:
                    site['access_status'] = 'content_accessible_partial_assets'
                observer.disconnect()
                failure_connection.disconnect()
                print(
                    json.dumps(
                        {
                            key: site[key]
                            for key in (
                                'name',
                                'access_status',
                                'main_document_status',
                                'framework_passed',
                                'routes_match',
                                'flush_failures',
                                'screenshot',
                            )
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            # One shared ScreenshotWatchdog remains useful after another owning
            # scope closes. Its closure-bound session routes to the surviving tab.
            await scopes[0].close()
            assert bridges[0].closed
            surviving_shot = scopes[-1].emit(ScreenshotEvent(request_contract=RequestContract(min_receivers=1)))
            assert await event_result(surviving_shot, timeout=15)
            surviving_shot.check_success()
            report['shared_watchdog_survives_scope_close'] = True
        finally:
            await group.close_all()
            report['cleanup_complete'] = all(scope.is_closed for scope in scopes)
            report['bridges_closed'] = all(bridge.closed for bridge in bridges)
            report['popup_events'] = popups.dismissed_dialogs
            await output.joinpath('live-sites.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            for target in targets:
                await cdp.send('Target.closeTarget', {'targetId': target})
        assert report['cleanup_complete'] and report['bridges_closed']
