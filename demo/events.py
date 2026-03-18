"""Event definitions for the real-browser demo.

These are agent-cdp BaseEvent subclasses that mirror the browser-use events
but are designed around our scoped connection topology.
"""

from agent_cdp.events import BaseEvent, EmitPolicy


# ── Navigation ──


class NavigateToUrlEvent(BaseEvent[str]):
    """Request navigation to a URL. Result is the final URL after redirects."""

    emit_policy = EmitPolicy.FAIL_FAST  # security handler can raise to block

    url: str
    new_tab: bool = False


class NavigationCompleteEvent(BaseEvent[None]):
    """Fired after a page finishes loading."""

    target_id: str
    url: str


# ── Tab lifecycle ──


class TabCreatedEvent(BaseEvent[None]):
    """A new tab/target was created."""

    target_id: str
    url: str = 'about:blank'


class TabClosedEvent(BaseEvent[None]):
    """A tab/target was closed."""

    target_id: str


# ── Browser lifecycle ──


class BrowserConnectedEvent(BaseEvent[None]):
    """CDP connection established."""

    cdp_url: str


# ── Dialogs ──


class PopupDialogEvent(BaseEvent[None]):
    """JavaScript dialog detected (alert/confirm/prompt)."""

    dialog_type: str
    message: str
    target_id: str


# ── Screenshot ──


class ScreenshotEvent(BaseEvent[str]):
    """Request a screenshot. Result is base64-encoded PNG data."""

    full_page: bool = False


# ── Errors ──


class BrowserErrorEvent(BaseEvent[None]):
    """Browser-level error (crash, timeout, etc.)."""

    error_type: str
    message: str
    details: str = ''


# ── Captcha ──


class CaptchaDetectedEvent(BaseEvent[dict[str, object]]):
    """reCAPTCHA / hCaptcha element detected in DOM."""

    target_id: str
    vendor: str  # 'recaptcha', 'hcaptcha', 'cloudflare', etc.
    url: str
    sitekey: str = ''
    solved: bool = False
    challenge_visible: bool = False
    element_count: int = 0


class CaptchaStateChangedEvent(BaseEvent[None]):
    """Captcha state transition (unsolved→solving→solved/failed)."""

    target_id: str
    vendor: str
    state: str  # 'detected', 'challenge_visible', 'solved', 'expired', 'failed'
    url: str


# ── Global monitoring ──


class GlobalMonitorEvent(BaseEvent[None]):
    """Broadcast event for cross-tab monitoring."""

    source_scope_id: str
    event_name: str
    details: str = ''
