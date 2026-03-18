import asyncio

import pytest

from src.apiary_poller import run_apiary_poller


# --- Already in-flight task is not re-claimed ---

async def test_poller_skips_in_flight_task(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-1", "invoke": {"instructions": "do something"}}
    ]
    executor.add_apiary_task("task-1")  # simulate already claimed

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    mock_apiary.claim_task.assert_not_called()
    assert executor.queue.empty()


# --- New task is claimed, registered in in-flight, and enqueued ---

async def test_poller_claims_and_enqueues_new_task(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-2", "invoke": {"instructions": "do something"}}
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    mock_apiary.claim_task.assert_called_once_with("task-2")
    assert executor.has_apiary_task("task-2")
    assert not executor.queue.empty()
    req = executor.queue.get_nowait()
    assert req.apiary_task_id == "task-2"
    assert req.source == "apiary"


# --- Task with missing id/prompt is skipped without claiming ---

async def test_poller_skips_task_without_prompt(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [{"id": "task-3"}]  # no prompt

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    mock_apiary.claim_task.assert_not_called()


# --- Prompt extraction from payload.prompt ---

async def test_poller_extracts_prompt_from_payload_prompt(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-p1", "payload": {"prompt": "from payload"}}
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    req = executor.queue.get_nowait()
    assert req.prompt.startswith("from payload")


# --- Prompt extraction from payload.input ---

async def test_poller_extracts_prompt_from_payload_input(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-p2", "payload": {"input": "from input"}}
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    req = executor.queue.get_nowait()
    assert req.prompt.startswith("from input")


# --- Prompt extraction from top-level field ---

async def test_poller_extracts_prompt_from_top_level_description(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-p3", "description": "top-level description"}
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    req = executor.queue.get_nowait()
    assert req.prompt == "top-level description"


# --- Claim failure: task not enqueued, loop continues ---

async def test_poller_skips_task_when_claim_fails(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-cf", "invoke": {"instructions": "do something"}}
    ]
    mock_apiary.claim_task.side_effect = Exception("already claimed")

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.queue.empty()
    assert not executor.has_apiary_task("task-cf")


# --- Heartbeat failure: polling still happens ---

async def test_poller_continues_after_heartbeat_failure(mock_apiary, executor, mock_config):
    mock_apiary.heartbeat.side_effect = Exception("connection refused")
    mock_apiary.poll_tasks.return_value = [
        {"id": "task-hb", "invoke": {"instructions": "do something"}}
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    mock_apiary.claim_task.assert_called_once_with("task-hb")
    assert not executor.queue.empty()


# --- Context data appended to prompt ---

async def test_poller_appends_event_payload_to_prompt(mock_apiary, executor, mock_config):
    mock_apiary.poll_tasks.return_value = [
        {
            "id": "task-ctx",
            "invoke": {"instructions": "process this"},
            "event_payload": {"foo": "bar"},
        }
    ]

    task = asyncio.create_task(run_apiary_poller(mock_apiary, executor, mock_config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    req = executor.queue.get_nowait()
    assert req.prompt.startswith("process this")
    assert "Task payload data:" in req.prompt
    assert "foo" in req.prompt
