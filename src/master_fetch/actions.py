"""Page interaction for smart_fetch (the `actions` param).

When the agent passes `actions=[...]`, smart_fetch forces the stealthy browser
tier and runs the actions on the page after navigation, before content
extraction. This reaches content behind a click, a search form, a "load more"
button, or infinite scroll — cases a plain fetch can't.

Implementation: patchright's stealthy fetch accepts a `page_action` callable that
receives the Playwright AsyncPage after goto and is awaited. We build that
callable from a validated list of action dicts and thread it through
smart_fetch -> _force_fetch -> stealthy_fetch -> session.fetch(page_action=...).

Action schema (one key per dict):
  {"click": "css-selector"}                 click the first match
  {"fill": {"selector": "css", "text": "x"}}  clear + fill an input
  {"press": "Enter"}                        press a keyboard key on the page
  {"press": {"selector": "css", "key": "Enter"}}  press on a specific element
  {"wait": 500}                             wait milliseconds
  {"scroll": 3}                             scroll down N viewport-heights
  {"wait_selector": "css"}                  wait for a selector to appear

Validation is strict (CSS selectors validated, counts/ms capped) so a bad action
fails fast instead of hanging the browser. Per-action errors are caught so one
failing step doesn't abort the rest; the agent gets the page in whatever state
it reached.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("master-fetch.actions")

MAX_ACTIONS = 20
MAX_WAIT_MS = 30_000
MAX_SCROLL = 50
_VALID_KEYS = {"click", "fill", "press", "wait", "scroll", "wait_selector"}


def _validate_actions(actions) -> list[dict]:
    """Validate + normalize the actions list. Raises ValueError on bad input."""
    from master_fetch.security import validate_css_selector

    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty list of action dicts")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"Too many actions ({len(actions)}). Maximum is {MAX_ACTIONS}.")
    out: list[dict] = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict) or len(a) != 1:
            raise ValueError(f"action {i} must be a dict with exactly one key {sorted(_VALID_KEYS)}")
        (key, val), = a.items()
        if key not in _VALID_KEYS:
            raise ValueError(f"action {i} has unknown key {key!r}; valid: {sorted(_VALID_KEYS)}")
        if key in ("click", "wait_selector"):
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"action {i} {key!r} must be a non-empty CSS selector string")
            validate_css_selector(val)  # raises SecurityError on bad selector
            out.append({key: val})
        elif key == "fill":
            if not isinstance(val, dict) or "selector" not in val or "text" not in val:
                raise ValueError(f"action {i} 'fill' must be {{selector, text}}")
            sel = val["selector"]
            if not isinstance(sel, str) or not sel.strip():
                raise ValueError(f"action {i} 'fill.selector' must be a non-empty CSS selector")
            validate_css_selector(sel)
            text = val["text"]
            if not isinstance(text, str):
                raise ValueError(f"action {i} 'fill.text' must be a string")
            if len(text) > 5000:
                raise ValueError(f"action {i} 'fill.text' too long (max 5000 chars)")
            out.append({key: {"selector": sel, "text": text}})
        elif key == "press":
            if isinstance(val, str):
                k = val.strip()
                if not k:
                    raise ValueError(f"action {i} 'press' key is empty")
                out.append({key: {"selector": None, "key": k[:50]}})
            elif isinstance(val, dict) and "key" in val:
                sel = val.get("selector")
                k = str(val.get("key", "")).strip()
                if not k:
                    raise ValueError(f"action {i} 'press.key' is empty")
                if sel is not None and (not isinstance(sel, str) or not sel.strip()):
                    raise ValueError(f"action {i} 'press.selector' must be a non-empty CSS selector")
                if sel:
                    validate_css_selector(sel)
                out.append({key: {"selector": sel, "key": k[:50]}})
            else:
                raise ValueError(f"action {i} 'press' must be a key string or {{selector, key}}")
        elif key == "wait":
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(f"action {i} 'wait' must be an int (ms)")
            out.append({key: max(0, min(val, MAX_WAIT_MS))})
        elif key == "scroll":
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(f"action {i} 'scroll' must be an int (viewport steps)")
            out.append({key: max(0, min(val, MAX_SCROLL))})
    return out


def build_page_action(actions) -> Optional[Callable]:
    """Validate `actions` and return an async page_action(page) callable, or None
    if `actions` is None/empty."""
    if not actions:
        return None
    validated = _validate_actions(actions)

    async def page_action(page) -> None:
        for a in validated:
            try:
                if "click" in a:
                    await page.locator(a["click"]).first.click(timeout=10_000)
                elif "fill" in a:
                    f = a["fill"]
                    await page.locator(f["selector"]).first.fill(f["text"], timeout=10_000)
                elif "press" in a:
                    p = a["press"]
                    if p["selector"]:
                        await page.locator(p["selector"]).first.press(p["key"], timeout=10_000)
                    else:
                        await page.keyboard.press(p["key"])
                elif "wait" in a:
                    await page.wait_for_timeout(a["wait"])
                elif "scroll" in a:
                    for _ in range(a["scroll"]):
                        try:
                            await page.evaluate(
                                "() => window.scrollBy(0, window.innerHeight || 800)"
                            )
                        except Exception:
                            await page.mouse.wheel(0, 800)
                        await page.wait_for_timeout(700)
                elif "wait_selector" in a:
                    await page.locator(a["wait_selector"]).first.wait_for(
                        state="attached", timeout=10_000
                    )
            except Exception as e:
                logger.warning("page action %a failed: %s", a, str(e)[:160])

    return page_action
