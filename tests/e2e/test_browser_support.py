from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from browser_support import launch_chromium
from playwright.sync_api import Error


@pytest.mark.parametrize("channel", [None, "", "chrome", "msedge"])
def test_launch_chromium_forwards_channel_and_returns_browser(monkeypatch, channel):
    if channel is None:
        monkeypatch.delenv("ATR_BROWSER_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("ATR_BROWSER_CHANNEL", channel)
    browser = object()
    launch = Mock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    assert launch_chromium(playwright) is browser
    launch.assert_called_once_with(headless=True, channel=channel or None)


@pytest.mark.parametrize("message", [
    "BrowserType.launch: Executable doesn't exist at /missing/chromium",
    "Please run the following command: playwright install",
])
def test_launch_chromium_skips_missing_browser(message):
    launch = Mock(side_effect=Error(message))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    with pytest.raises(pytest.skip.Exception) as caught:
        launch_chromium(playwright)

    assert str(caught.value) == (
        "浏览器未安装：请运行 python -m uv run playwright install chromium，"
        "或设置 ATR_BROWSER_CHANNEL=chrome 使用已安装的 Chrome"
    )


@pytest.mark.parametrize("error", [
    Error('Unsupported chromium channel "not-a-browser"'),
    Error("Browser process crashed"),
    RuntimeError("Executable doesn't exist"),
    RuntimeError("playwright install"),
])
def test_launch_chromium_preserves_other_errors(error):
    launch = Mock(side_effect=error)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    with pytest.raises(type(error)) as caught:
        launch_chromium(playwright)

    assert caught.value is error
