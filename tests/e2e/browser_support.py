import os

import pytest
from playwright.sync_api import Error


def launch_chromium(playwright):
    try:
        return playwright.chromium.launch(
            headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None,
        )
    except Error as error:
        if "Executable doesn't exist" in str(error) or "playwright install" in str(error):
            pytest.skip(
                "浏览器未安装：请运行 python -m uv run playwright install chromium，"
                "或设置 ATR_BROWSER_CHANNEL=chrome 使用已安装的 Chrome"
            )
        raise
