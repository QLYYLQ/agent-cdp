"""Watchdogs migrated from browser-use to agent-cdp scoped event system.

Each watchdog is a plain class with handler methods. Handlers are connected
to scopes explicitly via scope.connect() — no global bus, no magic method naming.

Key differences from browser-use:
- SecurityWatchdog uses Direct mode + consume() instead of raise-through-bus
- PopupsWatchdog registers CDP event bridge → agent-cdp event emission
- ScreenshotWatchdog uses Queued mode for async CDP operations
- No circuit breaker wrapper — Direct/Queued separation handles this cleanly
"""

import asyncio
import base64
import logging
from pathlib import Path
from urllib.parse import urlparse

from agent_cdp import CDPEventBridge, ConnectionType, EventScope

from .cdp_client import CDPClient
from .events import (
    BrowserErrorEvent,
    CaptchaDetectedEvent,
    CaptchaStateChangedEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    PopupDialogEvent,
    ScreenshotEvent,
)

logger = logging.getLogger(__name__)


# ── SecurityWatchdog ──────────────────────────────────────────────────────


class SecurityWatchdog:
    """Blocks navigation to disallowed domains.

    Uses Direct mode + high priority so it runs BEFORE any navigation handler.
    Calls event.consume() to prevent lower-priority handlers from executing.

    This replaces browser-use's pattern of raising ValueError through the bus.
    In agent-cdp, the Direct handler executes synchronously in emit() — the
    caller sees the exception immediately.
    """

    def __init__(self, allowed_domains: list[str] | None = None) -> None:
        self.allowed_domains = allowed_domains or []

    def attach(self, scope: EventScope) -> None:
        """Connect security check as Direct handler with high priority."""
        scope.connect(
            NavigateToUrlEvent,
            self.check_navigation,
            mode=ConnectionType.DIRECT,
            priority=100,  # runs before navigation handlers (priority=0)
        )

    def check_navigation(self, event: NavigateToUrlEvent) -> None:
        """Synchronous Direct handler — blocks disallowed URLs.

        This is the key difference from bubus: runs in emit() call stack,
        not in a queue. The caller sees the ValueError immediately.
        """
        if not self._is_url_allowed(event.url):
            event.consume()  # stop propagation to navigation handler
            msg = f'Navigation BLOCKED by SecurityWatchdog: {event.url}'
            raise ValueError(msg)

    def _is_url_allowed(self, url: str) -> bool:
        if not self.allowed_domains:
            return True  # no restrictions

        parsed = urlparse(url)
        host = parsed.hostname or ''

        # Special schemes always allowed
        if parsed.scheme in ('about', 'data', 'chrome'):
            return True

        for domain in self.allowed_domains:
            if host == domain or host.endswith('.' + domain):
                return True

        return False


# ── PopupsWatchdog ──────────────────────────────────────────────────────


class PopupsWatchdog:
    """Auto-dismisses JavaScript dialogs (alert/confirm/prompt).

    Bridges CDP events → agent-cdp events → handler processing.
    Uses dual-handler pattern:
    - Direct handler (p=100): synchronous recording only
    - Queued handler (p=50): async CDP dismiss

    This is the idiomatic Direct/Queued separation — synchronous work
    runs inline in emit(), async I/O goes through the event loop.
    """

    def __init__(self, cdp: CDPClient) -> None:
        self.cdp = cdp
        self._session_id: str | None = None
        self._bridge: CDPEventBridge | None = None
        self.dismissed_dialogs: list[dict[str, str]] = []

    def attach(self, scope: EventScope, session_id: str) -> None:
        """Connect dual handlers and register CDP event bridge."""
        self._session_id = session_id

        # Direct handler (p=100): synchronous recording only
        scope.connect(
            PopupDialogEvent,
            self._record_dialog,
            mode=ConnectionType.DIRECT,
            priority=100,
        )

        # Queued handler (p=50): async CDP dismiss
        scope.connect(
            PopupDialogEvent,
            self._dismiss_dialog_async,
            mode=ConnectionType.QUEUED,
            target_scope=scope,
            priority=50,
        )

        # CDP event bridge: Chrome → agent-cdp (replaces manual on_event wiring)
        self._bridge = CDPEventBridge(self.cdp, scope, session_id=session_id)
        self._bridge.bridge(
            'Page.javascriptDialogOpening',
            lambda params: PopupDialogEvent(
                dialog_type=params.get('type', 'alert'),
                message=params.get('message', ''),
                target_id=session_id,
            ),
        )

    def _record_dialog(self, event: PopupDialogEvent) -> None:
        """Direct handler (p=100) — synchronous recording only."""
        self.dismissed_dialogs.append({
            'type': event.dialog_type,
            'message': event.message,
        })
        logger.info(
            'PopupsWatchdog: recorded %s dialog: "%s"',
            event.dialog_type,
            event.message,
        )

    async def _dismiss_dialog_async(self, event: PopupDialogEvent) -> None:
        """Queued handler (p=50) — async CDP call to dismiss the dialog."""
        try:
            await self.cdp.send(
                'Page.handleJavaScriptDialog',
                {'accept': True},
                session_id=self._session_id,
            )
            logger.info('PopupsWatchdog: dismissed %s dialog', event.dialog_type)
        except Exception:
            logger.exception('Failed to dismiss dialog via CDP')


# ── ScreenshotWatchdog ────────────────────────────────────────────────


class ScreenshotWatchdog:
    """Captures page screenshots via CDP.

    Uses Queued mode because Page.captureScreenshot is an async CDP operation.
    This is the correct pattern: heavy I/O handlers go through the event loop,
    not the Direct path.
    """

    def __init__(self, cdp: CDPClient) -> None:
        self.cdp = cdp
        self._session_id: str | None = None

    def attach(self, scope: EventScope, session_id: str) -> None:
        """Connect screenshot handler as Queued handler."""
        self._session_id = session_id
        scope.connect(
            ScreenshotEvent,
            self.capture,
            mode=ConnectionType.QUEUED,
            target_scope=scope,  # target_scope = same scope → Queued via explicit mode
        )

    async def capture(self, event: ScreenshotEvent) -> str:
        """Queued async handler — captures screenshot via CDP."""
        params: dict[str, bool] = {}
        if event.full_page:
            params['captureBeyondViewport'] = True

        result = await self.cdp.send(
            'Page.captureScreenshot',
            {'format': 'png', **params},
            session_id=self._session_id,
        )
        data: str = result.get('data', '')
        logger.info('ScreenshotWatchdog: captured %d bytes of PNG data', len(data))
        return data


# ── CrashWatchdog ─────────────────────────────────────────────────────


class CrashWatchdog:
    """Monitors browser/tab health and emits BrowserErrorEvent on crash.

    Demonstrates:
    - connect_all for catch-all monitoring
    - CDP event bridge for Target.targetCrashed
    - Per-scope isolation (each tab's crash is independent)
    """

    def __init__(self, cdp: CDPClient) -> None:
        self.cdp = cdp
        self._bridge: CDPEventBridge | None = None
        self.crash_events: list[str] = []

    def attach(self, scope: EventScope) -> None:
        """Connect crash handler and register CDP crash event bridge."""
        self._bridge = CDPEventBridge(self.cdp, scope)
        self._bridge.bridge(
            'Target.targetCrashed',
            lambda params: self._make_crash_event(params),
        )

    def _make_crash_event(self, params: dict[str, str]) -> BrowserErrorEvent:
        """Build a BrowserErrorEvent from CDP crash params."""
        target_id = params.get('targetId', 'unknown')
        error_text = params.get('errorCode', 'unknown crash')
        self.crash_events.append(target_id)
        return BrowserErrorEvent(
            error_type='target_crashed',
            message=f'Tab {target_id} crashed: {error_text}',
            details=str(params),
        )


# ── NavigationHandler (not a watchdog, just a handler for the demo) ──


def make_navigation_handler(cdp: CDPClient, session_id: str, scope: EventScope):
    """Create a Queued navigation handler (closure over cdp + session_id).

    This replaces the direct CDP call pattern in browser-use where navigation
    was a bus event handler. In agent-cdp, this is a Queued handler that
    runs asynchronously after security checks pass.
    """

    async def navigate(event: NavigateToUrlEvent) -> str:
        """Execute the actual navigation via CDP."""
        logger.info('NavigationHandler: navigating to %s', event.url)

        await cdp.send(
            'Page.navigate',
            {'url': event.url},
            session_id=session_id,
        )

        # Wait for page load
        await asyncio.sleep(1.5)

        # Emit NavigationCompleteEvent
        complete = NavigationCompleteEvent(
            target_id=session_id,
            url=event.url,
        )
        scope.emit(complete)
        return event.url

    return navigate


def save_screenshot(data_b64: str, path: str | Path) -> Path:
    """Decode base64 PNG and save to file."""
    path = Path(path)
    path.write_bytes(base64.b64decode(data_b64))
    return path


# ── CaptchaWatchdog ──────────────────────────────────────────────────

# JavaScript for reCAPTCHA / hCaptcha / Cloudflare detection via DOM inspection.
# browser-use relies on custom CDP events from their cloud proxy (BrowserUse.captchaSolverStarted).
# For local Chrome, we detect captcha elements directly in the DOM.
CAPTCHA_DETECT_JS = """
(function() {
    var result = {detected: false, vendor: 'none', solved: false, challengeVisible: false,
                  sitekey: '', elementCount: 0, response: '', details: ''};

    // reCAPTCHA v2/v3
    var recapFrames = document.querySelectorAll('iframe[src*="recaptcha"]');
    var grecapDiv = document.querySelector('.g-recaptcha');
    if (recapFrames.length > 0 || grecapDiv) {
        result.detected = true;
        result.vendor = 'recaptcha';
        result.elementCount = recapFrames.length + (grecapDiv ? 1 : 0);
        if (grecapDiv) result.sitekey = grecapDiv.getAttribute('data-sitekey') || '';
        try { result.response = grecaptcha.getResponse() || ''; } catch(e) {}
        result.solved = result.response.length > 0;
        // Challenge popup detection
        var cf = document.querySelector('iframe[src*="recaptcha"][title*="challenge"]');
        result.challengeVisible = cf ? cf.getBoundingClientRect().height > 0 : false;
        // Check for anchor checkbox state
        try {
            var anchor = document.querySelector('iframe[src*="recaptcha/api2/anchor"]');
            if (anchor) {
                var inner = anchor.contentDocument;
                if (inner) {
                    var checkbox = inner.querySelector('.recaptcha-checkbox');
                    if (checkbox) result.details = 'checkbox:' + checkbox.getAttribute('aria-checked');
                }
            }
        } catch(e) { result.details = 'cross-origin (cannot inspect checkbox)'; }
    }

    // hCaptcha
    var hcapFrames = document.querySelectorAll('iframe[src*="hcaptcha"]');
    var hcapDiv = document.querySelector('.h-captcha');
    if (!result.detected && (hcapFrames.length > 0 || hcapDiv)) {
        result.detected = true;
        result.vendor = 'hcaptcha';
        result.elementCount = hcapFrames.length + (hcapDiv ? 1 : 0);
        if (hcapDiv) result.sitekey = hcapDiv.getAttribute('data-sitekey') || '';
        try { result.response = hcaptcha.getResponse() || ''; } catch(e) {}
        result.solved = result.response.length > 0;
    }

    // Cloudflare Turnstile
    var cfDiv = document.querySelector('.cf-turnstile');
    var cfFrame = document.querySelector('iframe[src*="challenges.cloudflare"]');
    if (!result.detected && (cfDiv || cfFrame)) {
        result.detected = true;
        result.vendor = 'cloudflare';
        result.elementCount = (cfDiv ? 1 : 0) + (cfFrame ? 1 : 0);
        if (cfDiv) result.sitekey = cfDiv.getAttribute('data-sitekey') || '';
    }

    return result;
})()
"""


class CaptchaWatchdog:
    """Detects captcha elements in the DOM and emits CaptchaDetectedEvent.

    Unlike browser-use which relies on custom CDP events from a cloud proxy,
    this watchdog does direct DOM inspection via Runtime.evaluate. This works
    with any Chrome instance, not just browser-use cloud browsers.

    Uses Queued mode (async CDP operation). Attach to a NavigationCompleteEvent
    to auto-scan after each page load, or call scan() manually.
    """

    def __init__(self, cdp: CDPClient) -> None:
        self.cdp = cdp
        self._session_id: str | None = None
        self._scope: EventScope | None = None
        self.last_detection: dict[str, object] | None = None

    def attach(self, scope: EventScope, session_id: str) -> None:
        """Connect captcha scan as Queued handler on NavigationCompleteEvent."""
        self._scope = scope
        self._session_id = session_id

        # Auto-scan after each navigation
        scope.connect(
            NavigationCompleteEvent,
            self._on_navigation_complete,
            mode=ConnectionType.QUEUED,
            target_scope=scope,
            priority=10,
        )

    async def _on_navigation_complete(self, event: NavigationCompleteEvent) -> None:
        """Queued handler — scan for captcha after page load."""
        await self.scan(event.url)

    async def scan(self, url: str = '') -> dict[str, object] | None:
        """Run DOM-based captcha detection. Returns detection result or None."""
        if not self._session_id or not self._scope:
            return None

        try:
            result = await self.cdp.send(
                'Runtime.evaluate',
                {
                    'expression': CAPTCHA_DETECT_JS,
                    'returnByValue': True,
                    'awaitPromise': False,
                },
                session_id=self._session_id,
            )
        except Exception:
            logger.exception('CaptchaWatchdog: Runtime.evaluate failed')
            return None

        value = result.get('result', {}).get('value', {})
        if not isinstance(value, dict):
            return None

        self.last_detection = value

        if value.get('detected'):
            det_event = CaptchaDetectedEvent(
                target_id=self._session_id or '',
                vendor=str(value.get('vendor', 'unknown')),
                url=url,
                sitekey=str(value.get('sitekey', '')),
                solved=bool(value.get('solved', False)),
                challenge_visible=bool(value.get('challengeVisible', False)),
                element_count=int(value.get('elementCount', 0)),
            )
            self._scope.emit(det_event)

            # Determine state
            if value.get('solved'):
                state = 'solved'
            elif value.get('challengeVisible'):
                state = 'challenge_visible'
            else:
                state = 'detected'

            state_event = CaptchaStateChangedEvent(
                target_id=self._session_id or '',
                vendor=str(value.get('vendor', 'unknown')),
                state=state,
                url=url,
            )
            self._scope.emit(state_event)

            logger.info(
                'CaptchaWatchdog: %s captcha %s on %s (sitekey=%s, elements=%d)',
                value.get('vendor'),
                state,
                url,
                value.get('sitekey', '')[:20],
                value.get('elementCount', 0),
            )

        return value
