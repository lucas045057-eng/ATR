from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error
from required_browser import ensure_required_browser


@pytest.mark.parametrize('message', [
    "Executable doesn't exist", 'Please run playwright install',
])
def test_required_browser_fails_instead_of_skipping(monkeypatch, message):
    monkeypatch.setenv('ATR_REQUIRE_BROWSER', '1')
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=Mock(side_effect=Error(message))))
    manager = Mock()
    manager.__enter__ = Mock(return_value=playwright)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('required_browser.sync_playwright', lambda: manager)
    with pytest.raises(pytest.fail.Exception, match='浏览器验收必须运行'):
        ensure_required_browser()


def test_optional_browser_preflight_does_not_require_installation(monkeypatch):
    monkeypatch.delenv('ATR_REQUIRE_BROWSER', raising=False)
    start = Mock(side_effect=AssertionError('optional preflight must not start a browser'))
    monkeypatch.setattr('required_browser.sync_playwright', start)
    ensure_required_browser()
    start.assert_not_called()


def test_required_browser_preflight_launches_selected_channel_and_closes(monkeypatch):
    monkeypatch.setenv('ATR_REQUIRE_BROWSER', '1')
    monkeypatch.setenv('ATR_BROWSER_CHANNEL', 'chrome')
    browser = Mock()
    launch = Mock(return_value=browser)
    manager = Mock()
    manager.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('required_browser.sync_playwright', lambda: manager)
    ensure_required_browser()
    launch.assert_called_once_with(headless=True, channel='chrome')
    browser.close.assert_called_once_with()
