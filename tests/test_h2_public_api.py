"""Tests for H2 — top-level public API completeness."""

from __future__ import annotations

import agent_cdp


class TestAllSymbolsImportable:
    """Every symbol in __all__ must be accessible on the package."""

    def test_all_symbols_importable(self) -> None:
        for name in agent_cdp.__all__:
            obj = getattr(agent_cdp, name, None)
            assert obj is not None, f'{name} listed in __all__ but not importable'

    def test_all_count(self) -> None:
        assert len(agent_cdp.__all__) == 26


class TestInternalNotInAll:
    """Internal symbols must not leak into __all__."""

    def test_max_direct_depth_not_exported(self) -> None:
        assert '_MAX_DIRECT_DEPTH' not in agent_cdp.__all__

    def test_scope_protocol_not_exported(self) -> None:
        assert 'ScopeProtocol' not in agent_cdp.__all__

    def test_scope_event_loop_not_exported(self) -> None:
        assert 'ScopeEventLoop' not in agent_cdp.__all__

    def test_event_registrar_not_exported(self) -> None:
        assert 'EventRegistrar' not in agent_cdp.__all__

    def test_event_bridge_not_exported(self) -> None:
        assert 'EventBridge' not in agent_cdp.__all__


class TestConvenientImport:
    """Common symbols importable directly from agent_cdp."""

    def test_convenient_import(self) -> None:
        from agent_cdp import (
            BaseEvent,
            CDPEventBridge,
            Connection,
            ConnectionType,
            EventScope,
            ScopeGroup,
            connect,
            event_result,
        )

        assert BaseEvent is not None
        assert EventScope is not None
        assert connect is not None
        assert ConnectionType is not None
        assert Connection is not None
        assert ScopeGroup is not None
        assert event_result is not None
        assert CDPEventBridge is not None
