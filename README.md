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
| `TELEGRAM_TOPIC_ID` | No | Forum topic (`message_thread_id`) this agent is bound to — see [Telegram topics](#telegram-topics) |
| `SUPERPOS_BASE_URL` | No | Your Superpos instance URL |
| `SUPERPOS_HIVE_ID` | No | Hive ID from Superpos UI |
| `SUPERPOS_AGENT_ID` | No | Agent ID from agent creation dialog |
| `SUPERPOS_API_TOKEN` | No | API Token from agent creation dialog |
| `SUPERPOS_REFRESH_TOKEN` | No | Refresh Token from agent creation dialog |
| `SUPERPOS_CAPABILITIES` | No | Comma-separated capabilities |
| `SUPERPOS_POLL_INTERVAL` | No | Poll interval in seconds (default: 5) |
| `ANTHROPIC_API_KEY` | No | Only if not using OAuth |
| `CLAUDE_MODEL` | No | Default: claude-opus-4-8 |
| `CLAUDE_EFFORT` | No | Effort level: low, medium, high, max (default: high) |
| `CLAUDE_MAX_TURNS` | No | Default: 30 |
| `CLAUDE_WORKING_DIR` | No | Default: /workspace |
| `ANTHROPIC_BASE_URL` | No | Route through an Anthropic-compatible backend (MiniMax, Kimi, …) |
| `WEB_SEARCH_MCP` | No | JSON MCP server config replacing web search on non-Anthropic backends |

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
docker build -t superpos-agent-claude-php        -f Dockerfile.php        .
docker build -t superpos-agent-claude-node       -f Dockerfile.node       .
docker build -t superpos-agent-claude-playwright -f Dockerfile.playwright .
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

If you need two stacks together (e.g. JS + browser), the cleanest path is to create a chained `Dockerfile.node-playwright` that does `FROM superpos-agent-claude-node` and then adds the Playwright layers — there's no inherent conflict.

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

# Optional — MiniMax's own web-search MCP (see "Web search on alternative
# backends" below). Needs MiniMax Token-Plan credits; kept separate from
# ANTHROPIC_AUTH_TOKEN on purpose.
MINIMAX_API_KEY=your-minimax-token-plan-key
# MINIMAX_API_HOST=https://api.minimax.io   # default
```

Then run as normal:

```bash
docker run --env-file .env superpos-claude-agent
```

The `claude` CLI honors `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` and the model-override vars. The agent additionally reads `ANTHROPIC_BASE_URL` to detect the shim and adjust tooling. To switch back to Anthropic, clear those vars and restore your normal Anthropic auth — the hosted tools come back automatically.

### Alternative: Kimi Code (Anthropic-compatible endpoint)

[Kimi Code](https://www.kimi.com/code/docs/en/third-party-tools/other-coding-agents.html) (Moonshot AI) also exposes an Anthropic-compatible API. It requires a Kimi membership with Code benefits — create an API key in the Kimi Code Console. The model id `kimi-for-coding` is a stable alias that auto-maps to Kimi's latest coding model, so it never needs updating.

Skip OAuth and put these in your `.env`:

```bash
ANTHROPIC_BASE_URL=https://api.kimi.com/coding/
ANTHROPIC_API_KEY=your-kimi-code-key
CLAUDE_MODEL=kimi-for-coding
CLAUDE_CODE_AUTO_COMPACT_WINDOW=262144   # context window hint from Kimi's docs
ANTHROPIC_DEFAULT_SONNET_MODEL=kimi-for-coding
ANTHROPIC_DEFAULT_OPUS_MODEL=kimi-for-coding
ANTHROPIC_DEFAULT_HAIKU_MODEL=kimi-for-coding
```

Then run as normal:

```bash
docker run --env-file .env superpos-claude-agent
```

Kimi ships no web-search MCP of its own, so a Kimi-backed agent has no web access out of the box — coding tasks work fine, but for web lookups set `WEB_SEARCH_MCP` (next section).

### Web search on alternative backends

Anthropic's hosted WebSearch/WebFetch tools exist only on Anthropic's own API — on any other `ANTHROPIC_BASE_URL` they fail with HTTP 400. The agent detects the shim from the base URL, disables the dead hosted tools, and wires a replacement web-search MCP chosen by precedence:

1. **`WEB_SEARCH_MCP` set** — that server is mounted (as `web_search`) and the model is pointed at it. Works on any shim backend. The value is a JSON stdio-server config; any MCP search provider works, e.g. [Tavily](https://github.com/tavily-ai/tavily-mcp) (free tier ~1k queries/month):

   ```bash
   WEB_SEARCH_MCP={"command":"npx","args":["-y","tavily-mcp"],"env":{"TAVILY_API_KEY":"tvly-your-key"}}
   ```

2. **else `MINIMAX_API_KEY` set** — MiniMax's own web-search MCP (`uvx minimax-coding-plan-mcp`) is mounted. The natural default for a MiniMax backend.

3. **else** — no web access; a warning is logged at startup.

On a native Anthropic backend none of this applies: the hosted tools are used and `WEB_SEARCH_MCP` is ignored.

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

## Telegram topics

The agent understands [forum topics](https://telegram.org/blog/topics-in-groups-collectible-usernames)
in supergroups (enable "Topics" in the group settings):

- **Each topic is its own conversation.** Messages posted in a topic get a
  session keyed by `chat:topic`, the reply streams into the same topic, and
  `/new` / `/stop` only affect the topic they're issued in. DMs and plain
  groups behave exactly as before.
- **One topic per agent (optional).** Set `TELEGRAM_TOPIC_ID` to a topic's
  `message_thread_id` to bind the agent to it: the agent then ignores group
  messages outside its topic (DMs still work) and sends its proactive
  output — Superpos task streams, disk alerts, permission warnings — into
  that topic. Run several agents (Claude, Codex, …) in one forum group by
  giving each bot its own topic.

  The easiest way to find a topic's id: open the topic in Telegram Web and
  copy the number after the last `/` in the URL, or forward a message from
  the topic to @userinfobot-style tools that echo `message_thread_id`.
