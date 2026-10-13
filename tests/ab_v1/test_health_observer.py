import pytest
from app.connectors.models import RuntimeTargetCreate
from app.connectors.policy import check
from app.connectors.remote_reads import NoRedirect, execute, validate_url
from pydantic import ValidationError


def test_endpoint_parameters_cannot_choose_a_url_or_local_observer():
    config = RuntimeTargetCreate(name='server', host='47.98.183.77', health_endpoints=[
        {'endpoint_id': 'entry', 'url': 'http://127.0.0.1:18000/health', 'service': 'api'}]).model_dump()
    assert check(config, 'health.get', {'endpoint_id': 'entry'}) == {'endpoint_id': 'entry'}
    with pytest.raises(ValidationError):
        check(config, 'health.get', {'endpoint_id': 'entry', 'url': 'http://127.0.0.1:28000'})
    with pytest.raises(PermissionError):
        check(config, 'health.get', {'endpoint_id': 'unapproved'})
    with pytest.raises(PermissionError):
        execute({}, 'health.get', {'endpoint_id': 'entry'})


@pytest.mark.parametrize('url', ['http://u:secret@localhost/health', 'http://localhost/health?token=x',
                              'http://localhost/health#x', 'file:///etc/passwd', 'http://localhost:bad/health'])
def test_urls_reject_credentials_and_ambiguous_observers(url):
    with pytest.raises(ValueError):
        validate_url(url)
    with pytest.raises(ValidationError):
        RuntimeTargetCreate(name='server', health_endpoints=[{'endpoint_id': 'entry', 'url': url, 'service': 'api'}])


def test_cross_host_endpoint_is_rejected_and_redirects_are_disabled():
    with pytest.raises(ValidationError):
        RuntimeTargetCreate(name='server', host='47.98.183.77', health_endpoints=[
            {'endpoint_id': 'entry', 'url': 'https://different.example/health', 'service': 'api'}])
    assert NoRedirect().redirect_request(None, None, None, None, None, None) is None
