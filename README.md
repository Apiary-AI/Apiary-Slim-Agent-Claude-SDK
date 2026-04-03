# slim-apiary-agent-cc

Docker agent that bridges **Apiary** and **Telegram** with **Claude Code** as the brain.

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in your `.env`:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Yes | Your Telegram user ID (comma-separated for multiple) |
| `TELEGRAM_CHAT_ID` | No | Default chat for Apiary task notifications |
| `APIARY_BASE_URL` | No | Your Apiary instance URL |
| `APIARY_HIVE_ID` | No | Hive ID from Apiary UI |
| `APIARY_AGENT_ID` | No | Agent ID from agent creation dialog |
| `APIARY_API_TOKEN` | No | API Token from agent creation dialog |
| `APIARY_REFRESH_TOKEN` | No | Refresh Token from agent creation dialog |
| `APIARY_CAPABILITIES` | No | Comma-separated capabilities |
| `APIARY_POLL_INTERVAL` | No | Poll interval in seconds (default: 5) |
| `ANTHROPIC_API_KEY` | No | Only if not using OAuth |
| `CLAUDE_MODEL` | No | Default: claude-opus-4-6 |
| `CLAUDE_EFFORT` | No | Effort level: low, medium, high, max (default: high) |
| `CLAUDE_MAX_TURNS` | No | Default: 30 |
| `CLAUDE_WORKING_DIR` | No | Default: /workspace |

Apiary variables are optional — if omitted, only the Telegram bot runs.

### 2. Build

```bash
docker build -t slim-apiary-agent .
```

### 3. Authenticate Claude (OAuth)

One-time step. This lets you use your Claude Pro/Max subscription instead of paying per API call.

```bash
docker run -it -v claude_auth:/home/agent/.claude --entrypoint claude slim-apiary-agent
```

The CLI will print a URL like:

```
To authenticate, visit: https://claude.ai/oauth/authorize?...
```

Open that URL in your browser, log in with your Claude account, and the CLI will confirm authentication. Then `Ctrl+C` to exit.

### 4. Run

```bash
docker run --env-file .env -v claude_auth:/home/agent/.claude slim-apiary-agent
```

The `claude_auth` volume persists your OAuth session across container restarts.

To prevent your Mac from sleeping while the agent runs, wrap the command with `caffeinate`:

```bash
caffeinate -is docker run --env-file .env -v claude_auth:/home/agent/.claude slim-apiary-agent
```

`-i` prevents idle sleep, `-s` prevents system sleep (keeps the machine awake even with the lid closed on AC power). `caffeinate` exits automatically when the Docker container stops.

### Alternative: API key auth

If you prefer API key auth, skip step 3, set `ANTHROPIC_API_KEY` in `.env`, and run without the volume:

```bash
docker run --env-file .env slim-apiary-agent
```

## Multi-agent setup (Docker Compose)

Run multiple independent agents, each with its own Telegram bot and Apiary registration.

### 1. Create env files

Each agent gets its own `.env.agentN`:

```bash
cp .env.example .env.agent1
cp .env.example .env.agent2
# ... etc
```

Fill in unique values per agent:
- `APIARY_AGENT_ID` + `APIARY_API_TOKEN` (register each agent in Apiary dashboard)
- `TELEGRAM_BOT_TOKEN` (create separate bots via @BotFather)

Shared values (Git, GitHub, Apiary URL/Hive) can be the same across all agents.

### 2. Build

```bash
docker compose build
```

### 3. Authenticate each agent (OAuth)

Each agent needs its own Claude OAuth session, stored in a separate volume:

```bash
docker compose run --rm agent1 claude
docker compose run --rm agent2 claude
# ... etc
```

Open the printed URL for each, log in, then `Ctrl+C`.

### 4. Run

Start all agents:

```bash
docker compose up -d
```

Start a specific agent:

```bash
docker compose up -d agent1
```

View logs:

```bash
docker compose logs -f           # all agents
docker compose logs -f agent1    # single agent
```

Stop all:

```bash
docker compose down
```

### Re-authenticate

If OAuth expires for an agent, stop it and re-auth:

```bash
docker compose stop agent1
docker compose run --rm agent1 claude
docker compose up -d agent1
```

## Testing

Tests cover the concurrency-critical paths: task dedup, claim-expiry abort, and poller enqueue logic.

### Run tests

```bash
docker build -f Dockerfile.test -t slim-agent-test .
docker run --rm slim-agent-test
```

No credentials or environment variables needed — everything is mocked.

### Test layout

```
tests/
  conftest.py          # shared fixtures (executor, mock_apiary, mock_config)
  test_executor.py     # dedup methods, _report_progress 409/500, claim-expiry cleanup
  test_poller.py       # skip in-flight tasks, claim+enqueue new tasks, skip malformed tasks
```

## Usage

- Send any text message to your Telegram bot — Claude processes it and streams the response back
- `/status` — check queue depth
- Apiary tasks are automatically polled, claimed, executed, and completed
