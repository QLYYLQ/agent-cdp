"""Event definitions for the real-browser demo.

These are agent-cdp BaseEvent subclasses that mirror the browser-use events
but are designed around our scoped connection topology.

Keys follow hierarchical dot-separated naming via conscribe 0.5.3:
  navigation.*, lifecycle.*, security.*, content.*, captcha.*, monitor.*
"""

from agent_cdp.events import BaseEvent, EmitPolicy

# ── Navigation ──


class NavigateToUrlEvent(BaseEvent[str]):
    """Request navigation to a URL. Result is the final URL after redirects."""

    __registry_key__ = 'navigation.to_url'

    emit_policy = EmitPolicy.FAIL_FAST  # security handler can raise to block

    url: str
    new_tab: bool = False


class NavigationCompleteEvent(BaseEvent[None]):
    """Fired after a page finishes loading."""

    __registry_key__ = 'navigation.complete'

    target_id: str
    url: str


# ── Tab lifecycle ──


class TabCreatedEvent(BaseEvent[None]):
    """A new tab/target was created."""

    __registry_key__ = 'lifecycle.tab_created'

    target_id: str
    url: str = 'about:blank'


class TabClosedEvent(BaseEvent[None]):
    """A tab/target was closed."""

    __registry_key__ = 'lifecycle.tab_closed'

    target_id: str


# ── Browser lifecycle ──


class BrowserConnectedEvent(BaseEvent[None]):
    """CDP connection established."""

    __registry_key__ = 'lifecycle.browser_connected'

    cdp_url: str


# ── Dialogs ──


class PopupDialogEvent(BaseEvent[None]):
    """JavaScript dialog detected (alert/confirm/prompt)."""

    __registry_key__ = 'security.popup_dialog'

    dialog_type: str
    message: str
    target_id: str


# ── Screenshot ──


class ScreenshotEvent(BaseEvent[str]):
    """Request a screenshot. Result is base64-encoded PNG data."""

    __registry_key__ = 'content.screenshot'

    full_page: bool = False


# ── Errors ──


class BrowserErrorEvent(BaseEvent[None]):
    """Browser-level error (crash, timeout, etc.)."""

    __registry_key__ = 'lifecycle.browser_error'

    error_type: str
    message: str
    details: str = ''


# ── Captcha ──


class CaptchaDetectedEvent(BaseEvent[dict[str, object]]):
    """reCAPTCHA / hCaptcha element detected in DOM."""

    __registry_key__ = 'captcha.detected'

    target_id: str
    vendor: str  # 'recaptcha', 'hcaptcha', 'cloudflare', etc.
    url: str
    sitekey: str = ''
    solved: bool = False
    challenge_visible: bool = False
    element_count: int = 0


class CaptchaStateChangedEvent(BaseEvent[None]):
    """Captcha state transition (unsolved→solving→solved/failed)."""

    __registry_key__ = 'captcha.state_changed'

    target_id: str
    vendor: str
    state: str  # 'detected', 'challenge_visible', 'solved', 'expired', 'failed'
    url: str


# ── Global monitoring ──


class GlobalMonitorEvent(BaseEvent[None]):
    """Broadcast event for cross-tab monitoring."""

    __registry_key__ = 'monitor.global'

    source_scope_id: str
    event_name: str
    details: str = ''
