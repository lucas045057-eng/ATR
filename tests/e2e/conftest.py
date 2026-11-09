import pytest
from required_browser import ensure_required_browser


@pytest.fixture(scope='session', autouse=True)
def required_browser_acceptance():
    ensure_required_browser()
