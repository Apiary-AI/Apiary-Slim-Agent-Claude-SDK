import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from src.superpos_client import SuperposClient
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
def superpos_client():
    config = Config(
        superpos_base_url="https://api.example.com",
        superpos_hive_id="hive-1",
        superpos_agent_id="agent-1",
        superpos_api_token="token-abc",
        superpos_refresh_token="refresh-xyz",
    )
    c = SuperposClient(config)
    c._client = MagicMock()
    c._client.request = AsyncMock()
    c._client.post = AsyncMock()
    return c


# --- _request: 401 auto-refresh ---

async def test_request_retries_after_401_refresh(superpos_client):
    resp_401 = _mock_resp(401)
    resp_200 = _mock_resp(200)
    superpos_client._client.request.side_effect = [resp_401, resp_200]
    superpos_client.refresh_auth = AsyncMock(return_value=True)

    result = await superpos_client._request("GET", "/test")

    assert result is resp_200
    superpos_client.refresh_auth.assert_called_once()
    assert superpos_client._client.request.call_count == 2


async def test_request_no_retry_on_500(superpos_client):
    err_resp = _mock_resp(
        500,
        raise_for_status_exc=httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=MagicMock()
        ),
    )
    superpos_client._client.request.return_value = err_resp
    superpos_client.refresh_auth = AsyncMock(return_value=True)

    with pytest.raises(httpx.HTTPStatusError):
        await superpos_client._request("GET", "/test")

    superpos_client.refresh_auth.assert_not_called()
    assert superpos_client._client.request.call_count == 1


# --- refresh_auth ---

async def test_refresh_auth_succeeds_on_first_endpoint(superpos_client):
    resp = _mock_resp(200, json_data={"token": "new-token"})
    superpos_client._client.post = AsyncMock(return_value=resp)

    result = await superpos_client.refresh_auth()

    assert result is True
    assert superpos_client._token == "new-token"
    assert superpos_client._client.post.call_count == 1


async def test_refresh_auth_skips_404_and_tries_next(superpos_client):
    resp_404 = _mock_resp(404)
    resp_200 = _mock_resp(200, json_data={"token": "refreshed"})
    superpos_client._client.post = AsyncMock(side_effect=[resp_404, resp_200])

    result = await superpos_client.refresh_auth()

    assert result is True
    assert superpos_client._client.post.call_count == 2


async def test_refresh_auth_returns_false_when_all_endpoints_fail(superpos_client):
    bad_resp = _mock_resp(
        400,
        raise_for_status_exc=httpx.HTTPStatusError(
            "bad request", request=MagicMock(), response=MagicMock()
        ),
    )
    superpos_client._client.post = AsyncMock(return_value=bad_resp)

    result = await superpos_client.refresh_auth()

    assert result is False
    assert superpos_client._client.post.call_count == 3  # tried all 3 endpoints


async def test_refresh_auth_also_updates_refresh_token(superpos_client):
    resp = _mock_resp(
        200,
        json_data={"token": "new-token", "refresh_token": "new-refresh"},
    )
    superpos_client._client.post = AsyncMock(return_value=resp)

    await superpos_client.refresh_auth()

    assert superpos_client._token == "new-token"
    assert superpos_client._refresh_token == "new-refresh"


# --- poll_tasks ---

async def test_poll_tasks_unwraps_data_key(superpos_client):
    resp = _mock_resp(200, json_data={"data": [{"id": "task-1"}]})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.poll_tasks()

    assert result == [{"id": "task-1"}]


async def test_poll_tasks_handles_flat_list_response(superpos_client):
    resp = _mock_resp(200, json_data=[{"id": "task-1"}])
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.poll_tasks()

    assert result == [{"id": "task-1"}]


# --- get_persona_assembled ---

async def test_get_persona_assembled_returns_prompt(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"version": 7, "prompt": "You are a senior code reviewer.", "document_count": 2}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.get_persona_assembled()

    assert result == "You are a senior code reviewer."


async def test_get_persona_assembled_returns_none_on_error(superpos_client):
    superpos_client._client.request = AsyncMock(side_effect=Exception("network error"))

    result = await superpos_client.get_persona_assembled()

    assert result is None


async def test_get_persona_assembled_returns_none_when_prompt_empty(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"version": 1, "prompt": "", "document_count": 0}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.get_persona_assembled()

    assert result is None


# --- update_persona_memory ---

async def test_update_persona_memory_append_default(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"name": "MEMORY", "content": "new content"}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.update_persona_memory("new content")

    superpos_client._client.request.assert_called_once()
    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.args[0] == "PATCH"
    assert call_kwargs.args[1] == "/api/v1/persona/memory"
    assert call_kwargs.kwargs["json"] == {"content": "new content", "mode": "append"}
    assert result == {"data": {"name": "MEMORY", "content": "new content"}}


async def test_update_persona_memory_replace_mode(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"name": "MEMORY"}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    await superpos_client.update_persona_memory("full content", mode="replace")

    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.kwargs["json"] == {"content": "full content", "mode": "replace"}


async def test_update_persona_memory_prepend_mode(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"name": "MEMORY"}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    await superpos_client.update_persona_memory("important note", mode="prepend")

    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.kwargs["json"] == {"content": "important note", "mode": "prepend"}


async def test_update_persona_memory_with_message(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"name": "MEMORY"}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    await superpos_client.update_persona_memory("content here", message="added python version")

    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.kwargs["json"] == {
        "content": "content here",
        "message": "added python version",
        "mode": "append",
    }


async def test_update_persona_memory_no_message(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"name": "MEMORY"}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    await superpos_client.update_persona_memory("content here", message=None)

    call_kwargs = superpos_client._client.request.call_args
    assert "message" not in call_kwargs.kwargs["json"]


# --- get_persona_version ---

async def test_get_persona_version_without_known_version(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"version": 5, "changed": False}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.get_persona_version()

    superpos_client._client.request.assert_called_once()
    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.args[0] == "GET"
    assert call_kwargs.args[1] == "/api/v1/persona/version"
    assert call_kwargs.kwargs["params"] is None
    assert result == {"data": {"version": 5, "changed": False}}


async def test_get_persona_version_with_known_version(superpos_client):
    resp = _mock_resp(200, json_data={"data": {"version": 7, "changed": True}})
    superpos_client._client.request = AsyncMock(return_value=resp)

    result = await superpos_client.get_persona_version(known_version=5)

    call_kwargs = superpos_client._client.request.call_args
    assert call_kwargs.kwargs["params"] == {"known_version": 5}
    assert result == {"data": {"version": 7, "changed": True}}


async def test_get_persona_version_returns_empty_on_404(superpos_client):
    err_resp = MagicMock()
    err_resp.status_code = 404
    exc = httpx.HTTPStatusError("not found", request=MagicMock(), response=err_resp)
    superpos_client._client.request = AsyncMock(side_effect=exc)

    result = await superpos_client.get_persona_version()

    assert result == {}


async def test_get_persona_version_returns_empty_on_exception(superpos_client):
    superpos_client._client.request = AsyncMock(side_effect=Exception("connection error"))

    result = await superpos_client.get_persona_version()

    assert result == {}


async def test_update_status_sends_patch(superpos_client):
    resp = _mock_resp(200)
    superpos_client._client.request = AsyncMock(return_value=resp)

    await superpos_client.update_status("online")

    superpos_client._client.request.assert_called_once()
    call_args = superpos_client._client.request.call_args
    assert call_args.args[0] == "PATCH"
    assert call_args.args[1] == "/api/v1/agents/status"
    assert call_args.kwargs["json"] == {"status": "online"}


async def test_update_persona_memory_locked_raises(superpos_client):
    err_resp = MagicMock()
    err_resp.status_code = 403
    exc = httpx.HTTPStatusError("locked", request=MagicMock(), response=err_resp)
    err_resp.raise_for_status.side_effect = exc
    superpos_client._client.request = AsyncMock(return_value=err_resp)

    with pytest.raises(httpx.HTTPStatusError):
        await superpos_client.update_persona_memory("content")
