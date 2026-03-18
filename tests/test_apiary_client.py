import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from src.apiary_client import ApiaryClient
from src.config import Config


def _mock_resp(status_code, json_data=None, raise_for_status_exc=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if raise_for_status_exc:
        resp.raise_for_status.side_effect = raise_for_status_exc
    else:
        resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def apiary_client():
    config = Config(
        apiary_base_url="https://api.example.com",
        apiary_hive_id="hive-1",
        apiary_agent_id="agent-1",
        apiary_api_token="token-abc",
        apiary_refresh_token="refresh-xyz",
    )
    c = ApiaryClient(config)
    c._client = MagicMock()
    c._client.request = AsyncMock()
    c._client.post = AsyncMock()
    return c


# --- _request: 401 auto-refresh ---

async def test_request_retries_after_401_refresh(apiary_client):
    resp_401 = _mock_resp(401)
    resp_200 = _mock_resp(200)
    apiary_client._client.request.side_effect = [resp_401, resp_200]
    apiary_client.refresh_auth = AsyncMock(return_value=True)

    result = await apiary_client._request("GET", "/test")

    assert result is resp_200
    apiary_client.refresh_auth.assert_called_once()
    assert apiary_client._client.request.call_count == 2


async def test_request_no_retry_on_500(apiary_client):
    err_resp = _mock_resp(
        500,
        raise_for_status_exc=httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=MagicMock()
        ),
    )
    apiary_client._client.request.return_value = err_resp
    apiary_client.refresh_auth = AsyncMock(return_value=True)

    with pytest.raises(httpx.HTTPStatusError):
        await apiary_client._request("GET", "/test")

    apiary_client.refresh_auth.assert_not_called()
    assert apiary_client._client.request.call_count == 1


# --- refresh_auth ---

async def test_refresh_auth_succeeds_on_first_endpoint(apiary_client):
    resp = _mock_resp(200, json_data={"token": "new-token"})
    apiary_client._client.post = AsyncMock(return_value=resp)

    result = await apiary_client.refresh_auth()

    assert result is True
    assert apiary_client._token == "new-token"
    assert apiary_client._client.post.call_count == 1


async def test_refresh_auth_skips_404_and_tries_next(apiary_client):
    resp_404 = _mock_resp(404)
    resp_200 = _mock_resp(200, json_data={"token": "refreshed"})
    apiary_client._client.post = AsyncMock(side_effect=[resp_404, resp_200])

    result = await apiary_client.refresh_auth()

    assert result is True
    assert apiary_client._client.post.call_count == 2


async def test_refresh_auth_returns_false_when_all_endpoints_fail(apiary_client):
    bad_resp = _mock_resp(
        400,
        raise_for_status_exc=httpx.HTTPStatusError(
            "bad request", request=MagicMock(), response=MagicMock()
        ),
    )
    apiary_client._client.post = AsyncMock(return_value=bad_resp)

    result = await apiary_client.refresh_auth()

    assert result is False
    assert apiary_client._client.post.call_count == 3  # tried all 3 endpoints


async def test_refresh_auth_also_updates_refresh_token(apiary_client):
    resp = _mock_resp(
        200,
        json_data={"token": "new-token", "refresh_token": "new-refresh"},
    )
    apiary_client._client.post = AsyncMock(return_value=resp)

    await apiary_client.refresh_auth()

    assert apiary_client._token == "new-token"
    assert apiary_client._refresh_token == "new-refresh"


# --- poll_tasks ---

async def test_poll_tasks_unwraps_data_key(apiary_client):
    resp = _mock_resp(200, json_data={"data": [{"id": "task-1"}]})
    apiary_client._client.request = AsyncMock(return_value=resp)

    result = await apiary_client.poll_tasks()

    assert result == [{"id": "task-1"}]


async def test_poll_tasks_handles_flat_list_response(apiary_client):
    resp = _mock_resp(200, json_data=[{"id": "task-1"}])
    apiary_client._client.request = AsyncMock(return_value=resp)

    result = await apiary_client.poll_tasks()

    assert result == [{"id": "task-1"}]
