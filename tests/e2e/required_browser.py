import os

import pytest
from playwright.sync_api import Error, sync_playwright


def ensure_required_browser():
    if os.environ.get('ATR_REQUIRE_BROWSER') != '1':
        return
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None,
            )
            browser.close()
    except Error as error:
        pytest.fail(f'浏览器验收必须运行：{error}', pytrace=False)
