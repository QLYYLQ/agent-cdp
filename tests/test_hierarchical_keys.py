"""Tests for Phase 8a — Hierarchical event keys (conscribe 0.5.3).

Verifies that:
1. EventRegistrar.children("navigation") returns 2 events
2. EventRegistrar.children("lifecycle") returns 4 events
3. EventRegistrar.tree() structure is correct
4. EventRegistrar.get("navigation.to_url") returns NavigateToUrlEvent
5. Auto-derivation: abstract parent + short-named child
"""

from __future__ import annotations

import pytest

from agent_cdp._registry import EventRegistrar
from agent_cdp.events import BaseEvent


class TestHierarchicalKeys:
    def test_navigation_children(self) -> None:
        # Import demo events to trigger registration
        from demo.events import NavigateToUrlEvent, NavigationCompleteEvent  # noqa: F401

        children = EventRegistrar.children('navigation')  # type: ignore[reportUnknownMemberType]
        assert len(children) == 2, f'Expected 2 navigation children, got {len(children)}: {list(children.keys())}'
        assert 'navigation.to_url' in children
        assert 'navigation.complete' in children

    def test_lifecycle_children(self) -> None:
        from demo.events import (  # noqa: F401
            BrowserConnectedEvent,
            BrowserErrorEvent,
            TabClosedEvent,
            TabCreatedEvent,
        )

        children = EventRegistrar.children('lifecycle')  # type: ignore[reportUnknownMemberType]
        assert len(children) == 4, f'Expected 4 lifecycle children, got {len(children)}: {list(children.keys())}'
        assert 'lifecycle.tab_created' in children
        assert 'lifecycle.tab_closed' in children
        assert 'lifecycle.browser_connected' in children
        assert 'lifecycle.browser_error' in children

    def test_tree_structure(self) -> None:
        from demo.events import NavigateToUrlEvent  # noqa: F401

        tree = EventRegistrar.tree()  # type: ignore[reportUnknownMemberType]
        assert 'navigation' in tree
        assert 'to_url' in tree['navigation']
        assert 'complete' in tree['navigation']

    def test_get_by_hierarchical_key(self) -> None:
        from demo.events import NavigateToUrlEvent

        cls = EventRegistrar.get('navigation.to_url')  # type: ignore[reportUnknownMemberType]
        assert cls is NavigateToUrlEvent

    def test_auto_derivation_from_abstract_parent(self) -> None:
        """Abstract parent with __registry_key__ → child auto-derives hierarchical key."""

        class TestCategoryEvent(BaseEvent[str]):
            __abstract__ = True
            __registry_key__ = 'test_hierarchy'

        class FooEvent(TestCategoryEvent):
            pass

        # The child key should be "test_hierarchy.foo" (parent key + separator + snake_case(name - suffix))
        cls = EventRegistrar.get('test_hierarchy.foo')  # type: ignore[reportUnknownMemberType]
        assert cls is FooEvent

    def test_captcha_children(self) -> None:
        from demo.events import CaptchaDetectedEvent, CaptchaStateChangedEvent  # noqa: F401

        children = EventRegistrar.children('captcha')  # type: ignore[reportUnknownMemberType]
        assert len(children) == 2
        assert 'captcha.detected' in children
        assert 'captcha.state_changed' in children

    def test_bench_events_hierarchical(self) -> None:
        pytest.importorskip('playwright')
        from demo.bench_agentcdp_vs_pw import JSEvalEvent  # noqa: F401

        children = EventRegistrar.children('bench')  # type: ignore[reportUnknownMemberType]
        assert len(children) >= 7, f'Expected at least 7 bench children, got {len(children)}: {list(children.keys())}'
        assert 'bench.js_eval' in children
