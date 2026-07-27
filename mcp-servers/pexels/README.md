# Pexels MCP server

> **Vendored** from `Superpos-AI/superpos-app` `mcp-servers/pexels/` (PR #901).
> The source of truth is superpos-app; this copy is baked into the
> superpos-claude-agent image so the Claude runtime can launch it. Switch to
> `npx @superpos/mcp-pexels` once the package is published to npm and delete
> this vendored copy.

A stdio [MCP](https://modelcontextprotocol.io/) server that exposes the
[Pexels API](https://www.pexels.com/api/) so any agent can search stock video
and photos (first consumer: the Instagram Reels agent, for b-roll).

## Tools

| Tool | Arguments | Returns |
|------|-----------|---------|
| `search_videos` | `query: string`, `per_page?: 1-80`, `orientation?: portrait\|landscape\|square`, `size?: large\|medium\|small` | `[{ id, url, duration, width, height, photographer, photographer_url }]` |
| `search_photos` | `query: string`, `per_page?: 1-80`, `orientation?`, `size?` | `[{ id, url, photographer, photographer_url }]` |
| `get_video` | `id: number` | `{ url, photographer, photographer_url, download_links: [{ link, quality, width, height, file_type }] }` |

Output is deterministic JSON. Download URLs are returned to the caller — the
agent downloads media itself (e.g. `curl`/`requests` into its workspace).

Errors surface as an `isError` result with a JSON body `{ error: { code, message, retry_after? } }`:
- **missing key** → `code: "missing_key"` (`PEXELS_API_KEY not set in env`)
- **401** → `code: "unauthorized"` (`check PEXELS_API_KEY`)
- **429** → `code: "rate_limited"` with `retry_after` from the rate-limit headers

## Auth

Reads `PEXELS_API_KEY` from the process env — **never** baked into the image.
The key is a bare API key sent in the `Authorization` header.

## Cost

Free tier: **200 requests/hour, 20,000/month**. The tools are read-only
searches; a single b-roll session is a handful of calls, well inside the free
tier.

## Attribution

Using the Pexels API **requires attribution** (per the
[API Guidelines](https://www.pexels.com/api/documentation/#guidelines)):

- Show a prominent link back to Pexels wherever API results are surfaced.
- Credit the photographer/videographer when possible
  (e.g. *"Video by Jane Doe on Pexels"*, linking to the media page).

Every tool result carries the attribution data needed to render this credit:

- `url` — the Pexels media page; use it for the "on Pexels" link.
- `photographer` — the creator's name to display.
- `photographer_url` — the creator's Pexels profile; link the name to it.

`get_video` (the download path) returns the same `photographer` /
`photographer_url` / `url` fields, so a consumer never has to correlate a
download back to its search result to attribute it. Consumers of these tools
are responsible for actually rendering the attribution.

## Wiring the credential (MCP-3 broker)

The credential flows to the server env via a **`ServiceConnection`**, not the
LLM `Provider` table (see "Decision points" below):

1. Create a `ServiceConnection` (type `custom`, auth type `api_key`) in the org
   whose `auth_config.mcp_env` carries the key:
   ```json
   { "mcp_env": { "PEXELS_API_KEY": "<your-pexels-key>" } }
   ```
2. Grant the agent `services:{connection_id}` permission.
3. At boot the agent is *intended* to resolve the key via
   `POST /api/v1/mcp/credentials`
   (`{ service_connection_id, keys: ["PEXELS_API_KEY"] }`) and inject the
   resolved value into the server's child-process env.

   > **Status — not yet wired in the runtime executors.** Today each executor
   > passes the inline `mcp:` block through verbatim (e.g. the Codex executor's
   > `_write_mcp_config()` writes `collect_mcp_servers()` output straight to
   > `~/.codex/config.json`), so the server receives the literal
   > `${PEXELS_API_KEY}` placeholder and Pexels returns `401` unless a real key
   > is already present in the process env. Resolving the placeholder against
   > the authorized `ServiceConnection` before launch — plus an integration
   > test asserting the spawned server sees the resolved value — is tracked in
   > `superpos-codex-agent` / `superpos-agent-core` (MCP-3 boot credential
   > injection), not in this package: this repo ships only the server and its
   > docs.

## Wiring the MCP entry (inline module `mcp:` block, MCP-2 path)

Until MCP-5 (registry MCP) lands, publish via an inline `mcp:` block on a
module. The block carries **NAMES only** — the value is a `${...}` placeholder,
never a literal secret (enforced by `App\Registry\ModuleMcpValidator`):

```json
{
  "pexels": {
    "command": "npx",
    "args": ["-y", "@superpos/mcp-pexels"],
    "env": { "PEXELS_API_KEY": "${PEXELS_API_KEY}" }
  }
}
```

> **Status — not launchable from an agent runtime yet.** This PR adds the
> server's source under `superpos-app`; it does **not** publish the package or
> bake its source and dependencies into any agent image. The `npx` +
> published-package form above (the tavily/exa prior art) is what an agent image
> should use once the package is published/baked. A raw `node
> mcp-servers/pexels/src/index.js` command resolves **only** from a checkout of
> this repo (local development) — that path does not exist in an agent's runtime
> filesystem. Bundling the server + an end-to-end launch test is tracked as
> cross-repo follow-up in `superpos-claude-agent` / `superpos-agent-core` (see
> "Decision points" in the PR). Until then this entry is documentation only.

## Running the tests

```bash
cd mcp-servers/pexels
node --test          # unit tests (mocked fetch) — no install needed
npm install && npm start   # to actually launch the stdio server
```

The live smoke test (`test/smoke.test.mjs`) runs only when `PEXELS_API_KEY` is
set in the env, and is skipped otherwise (including in CI without a key).
