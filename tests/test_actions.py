"""Tests for smart_fetch actions (page interaction)."""

import asyncio

import pytest

from master_fetch.actions import build_page_action, _validate_actions, MAX_ACTIONS


# ─── validation ─────────────────────────────────────────────────────────

def test_validate_click():
    out = _validate_actions([{"click": "button.load-more"}])
    assert out == [{"click": "button.load-more"}]


def test_validate_fill():
    out = _validate_actions([{"fill": {"selector": "#q", "text": "hound"}}])
    assert out == [{"fill": {"selector": "#q", "text": "hound"}}]


def test_validate_press_string_and_dict():
    a = _validate_actions([{"press": "Enter"}, {"press": {"selector": "#q", "key": "Enter"}}])
    assert a[0] == {"press": {"selector": None, "key": "Enter"}}
    assert a[1] == {"press": {"selector": "#q", "key": "Enter"}}


def test_validate_wait_scroll():
    out = _validate_actions([{"wait": 500}, {"scroll": 3}])
    assert out == [{"wait": 500}, {"scroll": 3}]


def test_validate_wait_selector():
    out = _validate_actions([{"wait_selector": ".item"}])
    assert out == [{"wait_selector": ".item"}]


def test_validate_empty_raises():
    with pytest.raises(ValueError):
        _validate_actions([])
    with pytest.raises(ValueError):
        _validate_actions(None)  # type: ignore


def test_validate_non_list_raises():
    with pytest.raises(ValueError):
        _validate_actions({"click": "x"})  # type: ignore


def test_validate_multi_key_dict_raises():
    with pytest.raises(ValueError):
        _validate_actions([{"click": "x", "wait": 1}])


def test_validate_unknown_key_raises():
    with pytest.raises(ValueError):
        _validate_actions([{"screenshot": "x"}])


def test_validate_bad_selector_raises():
    with pytest.raises(Exception):
        _validate_actions([{"click": "<script>x</script>"}])  # injection -> SecurityError


def test_validate_wait_capped():
    out = _validate_actions([{"wait": 999_999_999}])
    assert out[0]["wait"] <= 30_000


def test_validate_scroll_capped_nonneg():
    out = _validate_actions([{"scroll": -5}, {"scroll": 999}])
    assert out[0]["scroll"] == 0
    assert out[1]["scroll"] <= 50


def test_validate_too_many_actions_raises():
    with pytest.raises(ValueError):
        _validate_actions([{"wait": 1}] * (MAX_ACTIONS + 1))


def test_validate_fill_missing_text_raises():
    with pytest.raises(ValueError):
        _validate_actions([{"fill": {"selector": "#q"}}])


def test_validate_fill_text_too_long_raises():
    with pytest.raises(ValueError):
        _validate_actions([{"fill": {"selector": "#q", "text": "x" * 6000}}])


# ─── build_page_action ─────────────────────────────────────────────────

def test_build_page_action_none_for_empty():
    assert build_page_action(None) is None
    assert build_page_action([]) is None


def test_build_page_action_returns_callable():
    pa = build_page_action([{"click": "button"}])
    assert callable(pa)


# ─── page_action execution (mock Playwright page) ───────────────────────

class _FakeLocator:
    def __init__(self, name): self.name = name; self.first = self
        # first is self so .first.click() works
    async def click(self, timeout=None): self._clicked = True
    async def fill(self, text, timeout=None): self._filled = text
    async def press(self, key, timeout=None): self._pressed = key
    async def wait_for(self, state="attached", timeout=None): self._waited = state


class _FakeKeyboard:
    def __init__(self): self.pressed = []
    async def press(self, key): self.pressed.append(key)


class _FakePage:
    def __init__(self):
        self.locators = {}
        self.keyboard = _FakeKeyboard()
        self.waited_ms = []
        self.evals = 0
    def locator(self, sel):
        self.locators.setdefault(sel, _FakeLocator(sel))
        return self.locators[sel]
    async def wait_for_timeout(self, ms): self.waited_ms.append(ms)
    async def evaluate(self, expr): self.evals += 1
    async def mouse_wheel(self, x, y): self.evals += 0


def test_page_action_click_fill_press_wait():
    pa = build_page_action([
        {"click": "button.go"},
        {"fill": {"selector": "#q", "text": "hound"}},
        {"press": "Enter"},
        {"wait": 250},
    ])
    page = _FakePage()
    asyncio.run(pa(page))
    assert getattr(page.locators["button.go"], "_clicked", False) is True
    assert page.locators["#q"]._filled == "hound"
    assert page.keyboard.pressed == ["Enter"]
    assert page.waited_ms == [250]


def test_page_action_scroll_runs_eval_per_step():
    pa = build_page_action([{"scroll": 3}])
    page = _FakePage()
    asyncio.run(pa(page))
    assert page.evals == 3


def test_page_action_press_with_selector():
    pa = build_page_action([{"press": {"selector": "#q", "key": "Enter"}}])
    page = _FakePage()
    asyncio.run(pa(page))
    assert page.locators["#q"]._pressed == "Enter"
    assert page.keyboard.pressed == []


def test_page_action_one_failure_doesnt_abort_rest():
    pa = build_page_action([
        {"click": "missing"},  # _FakeLocator.click sets _clicked but doesn't fail
        {"wait": 100},
    ])
    page = _FakePage()
    asyncio.run(pa(page))  # should not raise
    assert page.waited_ms == [100]
