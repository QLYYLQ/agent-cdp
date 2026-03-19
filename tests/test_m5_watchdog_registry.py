"""Tests for M5 — WatchdogRegistrar (conscribe 0.5.0).

Verifies that:
1. All 5 watchdogs auto-registered
2. Key lookup works
3. Protocol compliance
"""

from __future__ import annotations

from agent_cdp import WatchdogProtocol, WatchdogRegistrar


class TestWatchdogRegistrar:
    def test_all_5_watchdogs_registered(self) -> None:
        # Import demo watchdogs to trigger registration
        from demo.watchdogs import (  # noqa: F401
            CaptchaWatchdog,
            CrashWatchdog,
            PopupsWatchdog,
            ScreenshotWatchdog,
            SecurityWatchdog,
        )

        all_watchdogs = WatchdogRegistrar.get_all()  # type: ignore[reportUnknownMemberType]
        keys = list(all_watchdogs.keys())
        assert len(all_watchdogs) >= 5, f'Expected >=5 watchdogs, got {len(all_watchdogs)}: {keys}'

    def test_key_lookup_security(self) -> None:
        from demo.watchdogs import SecurityWatchdog  # noqa: F401

        cls = WatchdogRegistrar.get('security')  # type: ignore[reportUnknownMemberType]
        assert cls.__name__ == 'SecurityWatchdog'

    def test_key_lookup_crash(self) -> None:
        from demo.watchdogs import CrashWatchdog  # noqa: F401

        cls = WatchdogRegistrar.get('crash')  # type: ignore[reportUnknownMemberType]
        assert cls.__name__ == 'CrashWatchdog'

    def test_key_lookup_popups(self) -> None:
        from demo.watchdogs import PopupsWatchdog  # noqa: F401

        cls = WatchdogRegistrar.get('popups')  # type: ignore[reportUnknownMemberType]
        assert cls.__name__ == 'PopupsWatchdog'

    def test_protocol_compliance(self) -> None:
        from demo.watchdogs import SecurityWatchdog

        wd = SecurityWatchdog(allowed_domains=['example.com'])
        assert isinstance(wd, WatchdogProtocol)
