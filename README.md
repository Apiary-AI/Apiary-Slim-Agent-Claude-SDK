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
| `CLAUDE_MODEL` | No | Default: claude-sonnet-4-20250514 |
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

### Alternative: API key auth

If you prefer API key auth, skip step 3, set `ANTHROPIC_API_KEY` in `.env`, and run without the volume:

```bash
docker run --env-file .env slim-apiary-agent
```

## Usage

- Send any text message to your Telegram bot — Claude processes it and streams the response back
- `/status` — check queue depth
- Apiary tasks are automatically polled, claimed, executed, and completed
