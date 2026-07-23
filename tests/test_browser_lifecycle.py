"""Browser lifecycle regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import master_fetch.browser as browser_module
from master_fetch.browser import DynamicBrowser


@pytest.mark.asyncio
async def test_start_failure_removes_partial_profile_and_stops_playwright(
    monkeypatch, tmp_path
):
    profile = tmp_path / "profile"
    profile.mkdir()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(
                side_effect=RuntimeError("browser launch failed")
            )
        ),
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))

    monkeypatch.setattr("patchright.async_api.async_playwright", lambda: manager)
    monkeypatch.setattr(browser_module, "_detect_chrome_channel", lambda: None)
    monkeypatch.setattr(
        browser_module.tempfile, "mkdtemp", lambda **_kwargs: str(profile)
    )

    session = DynamicBrowser(real_chrome=False)
    with pytest.raises(RuntimeError, match="browser launch failed"):
        await session.start()

    playwright.stop.assert_awaited_once()
    assert not profile.exists()
    assert session._playwright is None
    assert session._user_data_dir is None
    assert session._is_alive is False


@pytest.mark.asyncio
async def test_start_failure_after_launch_closes_context(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    context = SimpleNamespace(
        add_cookies=AsyncMock(side_effect=RuntimeError("cookie setup failed")),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context)
        ),
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))

    monkeypatch.setattr("patchright.async_api.async_playwright", lambda: manager)
    monkeypatch.setattr(browser_module, "_detect_chrome_channel", lambda: None)
    monkeypatch.setattr(
        browser_module.tempfile, "mkdtemp", lambda **_kwargs: str(profile)
    )

    session = DynamicBrowser(
        real_chrome=False,
        cookies=[{"name": "session", "value": "x", "domain": "example.com"}],
    )
    with pytest.raises(RuntimeError, match="cookie setup failed"):
        await session.start()

    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert not profile.exists()
    assert session._context is None
    assert session._playwright is None