# superpos-claude-agent

Docker agent that bridges **Superpos** and **Telegram** with **Claude Code** as the brain.

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
| `TELEGRAM_CHAT_ID` | No | Default chat for Superpos task notifications |
| `SUPERPOS_BASE_URL` | No | Your Superpos instance URL |
| `SUPERPOS_HIVE_ID` | No | Hive ID from Superpos UI |
| `SUPERPOS_AGENT_ID` | No | Agent ID from agent creation dialog |
| `SUPERPOS_API_TOKEN` | No | API Token from agent creation dialog |
| `SUPERPOS_REFRESH_TOKEN` | No | Refresh Token from agent creation dialog |
| `SUPERPOS_CAPABILITIES` | No | Comma-separated capabilities |
| `SUPERPOS_POLL_INTERVAL` | No | Poll interval in seconds (default: 5) |
| `ANTHROPIC_API_KEY` | No | Only if not using OAuth |
| `CLAUDE_MODEL` | No | Default: claude-opus-4-6 |
| `CLAUDE_EFFORT` | No | Effort level: low, medium, high, max (default: high) |
| `CLAUDE_MAX_TURNS` | No | Default: 30 |
| `CLAUDE_WORKING_DIR` | No | Default: /workspace |

Superpos variables are optional — if omitted, only the Telegram bot runs.

### 2. Build

```bash
docker build -t superpos-claude-agent .
```

### 3. Authenticate Claude (OAuth)

One-time step. This lets you use your Claude Pro/Max subscription instead of paying per API call.

```bash
docker run -it -v claude_auth:/home/agent/.claude --entrypoint claude superpos-claude-agent
```

The CLI will print a URL like:

```
To authenticate, visit: https://claude.ai/oauth/authorize?...
```

Open that URL in your browser, log in with your Claude account, and the CLI will confirm authentication. Then `Ctrl+C` to exit.

### 4. Run

```bash
docker run --env-file .env -v claude_auth:/home/agent/.claude superpos-claude-agent
```

The `claude_auth` volume persists your OAuth session across container restarts.

To prevent your Mac from sleeping while the agent runs, wrap the command with `caffeinate`:

```bash
caffeinate -is docker run --env-file .env -v claude_auth:/home/agent/.claude superpos-claude-agent
```

`-i` prevents idle sleep, `-s` prevents system sleep (keeps the machine awake even with the lid closed on AC power). `caffeinate` exits automatically when the Docker container stops.

### Stack variants

The base `Dockerfile` ships Node 22, Python 3, git, `gh`, and the `claude` CLI — enough for most code-review and writing tasks. For tasks that need to actually *run* a project's tests or drive a browser, three pre-built sibling Dockerfiles extend the base with extra tooling:

| File | Adds | When to use |
|---|---|---|
| `Dockerfile.php` | PHP 8.4 (via sury repo) + Composer + Laravel-relevant extensions (mbstring, xml, curl, sqlite/mysql/pgsql, bcmath, gd, zip, intl) | Running Pint, PHPUnit, Pest, Artisan, PHPStan inside the container |
| `Dockerfile.node` | corepack-enabled pnpm + yarn, plus build-essential / pkg-config / python3 for native modules (better-sqlite3, sharp, node-gyp targets) | Tasks that need pnpm/yarn or that compile native npm modules during `npm install` |
| `Dockerfile.playwright` | Playwright + Chromium with headless system deps, browsers installed to `/opt/playwright-browsers` so the non-root `agent` user can read them | End-to-end browser tests, screenshots, scraping behind JS. Playwright exposes the full Chrome DevTools Protocol via `CDPSession` when you need to go lower-level. |

Each variant does `FROM slim-apiary-agent-base`, so first tag the base accordingly, then build the variant you want:

```bash
# 1. Build (or rebuild) the base with the tag the variants expect
docker build -t slim-apiary-agent-base -f Dockerfile .

# 2. Build the variant
docker build -t slim-agent-claude-php        -f Dockerfile.php        .
docker build -t slim-agent-claude-node       -f Dockerfile.node       .
docker build -t slim-agent-claude-playwright -f Dockerfile.playwright .
```

To use one in `docker-compose.yml`, point the `dockerfile:` field at the variant:

```yaml
services:
  agent1:
    build:
      context: .
      dockerfile: Dockerfile.playwright   # or .php / .node
    container_name: agent1
    restart: unless-stopped
    env_file: .env.agent1
    volumes:
      - claude_auth_1:/home/agent/.claude
```

If you need two stacks together (e.g. JS + browser), the cleanest path is to create a chained `Dockerfile.node-playwright` that does `FROM slim-agent-claude-node` and then adds the Playwright layers — there's no inherent conflict.

### Alternative: API key auth

If you prefer API key auth, skip step 3, set `ANTHROPIC_API_KEY` in `.env`, and run without the volume:

```bash
docker run --env-file .env superpos-claude-agent
```

### Alternative: MiniMax (Anthropic-compatible endpoint)

[MiniMax](https://platform.minimax.io/docs/guides/text-ai-coding-tools) exposes an Anthropic-compatible API, so the bundled `claude` CLI can route through it natively — no code changes, no fork. Useful as a cheaper backend.

Skip OAuth, leave `ANTHROPIC_API_KEY` blank, and put these in your `.env` instead:

```bash
ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic   # or .minimaxi.com for CN
ANTHROPIC_AUTH_TOKEN=your-minimax-key
CLAUDE_MODEL=MiniMax-M2.7
ANTHROPIC_DEFAULT_SONNET_MODEL=MiniMax-M2.7
ANTHROPIC_DEFAULT_OPUS_MODEL=MiniMax-M2.7
ANTHROPIC_DEFAULT_HAIKU_MODEL=MiniMax-M2.7
```

Then run as normal:

```bash
docker run --env-file .env superpos-claude-agent
```

The `claude` CLI honors `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` and the model-override vars. To switch back to Anthropic, clear those four vars and restore your normal Anthropic auth.

## Multi-agent setup (Docker Compose)

Run multiple independent agents, each with its own Telegram bot and Superpos registration.

### 1. Create compose and env files

```bash
cp docker-compose.example.yml docker-compose.yml
```

Edit `docker-compose.yml` to add/remove agents as needed. Then create env files:

```bash
cp .env.example .env.agent1
cp .env.example .env.agent2
# ... etc
```

Fill in unique values per agent:
- `SUPERPOS_AGENT_ID` + `SUPERPOS_API_TOKEN` (register each agent in Superpos dashboard)
- `TELEGRAM_BOT_TOKEN` (create separate bots via @BotFather)

Shared values (Git, GitHub, Superpos URL/Hive) can be the same across all agents.

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
  conftest.py          # shared fixtures (executor, mock_superpos, mock_config)
  test_executor.py     # dedup methods, _report_progress 409/500, claim-expiry cleanup
  test_poller.py       # skip in-flight tasks, claim+enqueue new tasks, skip malformed tasks
```

## Usage

- Send any text message to your Telegram bot — Claude processes it and streams the response back
- `/status` — check queue depth
- `/model [<id>|list]` — show or change the model. Any provider model id is accepted (e.g. `claude-opus-4-6`, `MiniMax-M2.7`); `/model list` prints known ids. Persists across restarts.
- `/effort [low|medium|high|max]` — show or change reasoning effort. Persists across restarts.
- Superpos tasks are automatically polled, claimed, executed, and completed
