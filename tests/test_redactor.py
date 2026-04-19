from src.redactor import redact


def test_redacts_ghp_token():
    token = "ghp_" + "A" * 36
    out = redact(f"url is https://x:{token}@github.com/foo")
    assert token not in out
    assert "[REDACTED]" in out


def test_redacts_github_fine_grained_pat():
    token = "github_pat_" + "A" * 82
    out = redact(f"secret={token} bye")
    assert token not in out
    assert "[REDACTED]" in out


def test_redacts_anthropic_api_key():
    token = "sk-ant-api03-" + "A" * 95
    out = redact(f"ANTHROPIC_API_KEY={token}")
    assert token not in out
    assert "[REDACTED]" in out


def test_redacts_anthropic_oauth_token():
    token = "sk-ant-oat01-" + "A" * 40
    out = redact(f"Authorization: Bearer {token}")
    assert token not in out
    assert "[REDACTED]" in out


def test_preserves_clean_text():
    text = "this is a normal sentence with no secrets, just words and numbers 42"
    assert redact(text) == text


def test_empty_input():
    assert redact("") == ""


def test_keeps_short_prefix_for_debuggability():
    token = "ghp_" + "B" * 36
    out = redact(token)
    # first 8 chars preserved for "which key leaked?" attribution
    assert out.startswith("ghp_B")
    assert "[REDACTED]" in out


def test_multiple_tokens_on_one_line():
    t1 = "ghp_" + "A" * 36
    t2 = "sk-ant-api03-" + "B" * 95
    out = redact(f"gh={t1} claude={t2}")
    assert t1 not in out
    assert t2 not in out
