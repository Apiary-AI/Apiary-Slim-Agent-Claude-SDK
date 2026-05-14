from src.recent_tasks import RecentTasksLog, TaskSummary


def test_empty_render_returns_none():
    log = RecentTasksLog()
    assert log.render("chat-1") is None


def test_record_then_render_contains_task_details():
    log = RecentTasksLog()
    log.record(
        "chat-1",
        TaskSummary(
            task_id="abc123",
            description="Fix login redirect bug",
            outcome="succeeded",
            detail="Opened PR #42 with CSRF token fix",
        ),
    )
    rendered = log.render("chat-1")
    assert rendered is not None
    assert "abc123" in rendered
    assert "succeeded" in rendered
    assert "Fix login redirect bug" in rendered
    assert "PR #42" in rendered
    assert "## Recent Background Tasks" in rendered


def test_ring_buffer_caps_at_max():
    log = RecentTasksLog(max_per_chat=3)
    for i in range(5):
        log.record(
            "chat-1",
            TaskSummary(
                task_id=f"task-{i}",
                description=f"desc-{i}",
                outcome="succeeded",
                detail=f"detail-{i}",
            ),
        )
    rendered = log.render("chat-1")
    # Oldest two should have been evicted
    assert "task-0" not in rendered
    assert "task-1" not in rendered
    assert "task-2" in rendered
    assert "task-3" in rendered
    assert "task-4" in rendered


def test_isolation_between_chats():
    log = RecentTasksLog()
    log.record("chat-a", TaskSummary("t-a", "desc-a", "succeeded", "detail-a"))
    log.record("chat-b", TaskSummary("t-b", "desc-b", "failed", "detail-b"))
    a = log.render("chat-a")
    b = log.render("chat-b")
    assert "t-a" in a and "t-b" not in a
    assert "t-b" in b and "t-a" not in b


def test_detail_truncated_when_long():
    log = RecentTasksLog()
    long_detail = "x" * 1000
    log.record("chat-1", TaskSummary("t1", "d", "succeeded", long_detail))
    rendered = log.render("chat-1")
    assert "…" in rendered
    # The full 1000 chars should not be present
    assert "x" * 1000 not in rendered


def test_chat_id_normalization():
    """Integer and string chat_ids referring to the same chat must coalesce —
    the SuperposPoller passes telegram_chat_id as a string from env, but
    Telegram itself passes update.effective_chat.id as an int."""
    log = RecentTasksLog()
    log.record(12345, TaskSummary("t1", "d", "succeeded", "detail"))
    assert log.render("12345") is not None
    assert log.render(12345) is not None


def test_clear_removes_chat_history():
    log = RecentTasksLog()
    log.record("chat-1", TaskSummary("t1", "d", "succeeded", "detail"))
    log.clear("chat-1")
    assert log.render("chat-1") is None
