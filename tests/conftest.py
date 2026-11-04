import pytest

TEST_SERVICE_API_KEY = "test_service_key_0123456789ABCDEF_abcd"
AUTH_HEADERS = {"X-API-Key": TEST_SERVICE_API_KEY}


@pytest.fixture(autouse=True)
def fake_service_api_key(monkeypatch):
    """Keep Settings and child processes independent of a developer's service key."""
    monkeypatch.setenv("SERVICE_API_KEY", TEST_SERVICE_API_KEY)
