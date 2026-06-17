---
name: superpos-agent-fluency
description: End-to-end Superpos workflow — file a proposal, open a track, file issues, read back, and update, with copy-pasteable CLI commands
---

Walk the full Superpos delivery loop: **proposal → track → issues → read-back → update**. Every command below is real CLI surface (`superpos-knowledge`, `superpos-tracks`, `superpos-issues` — all on `PATH`). Run `<cmd> --help` to see all flags.

## 1. File a proposal (a typed `topic` knowledge page)

A proposal is a `topic` page with slug `proposal-<x>`. Capture the returned `id`/`slug` from the JSON.

```bash
superpos-knowledge create --type topic --slug proposal-foo \
  --title "Proposal: Foo" \
  --body-file /tmp/proposal-foo.md \
  --frontmatter '{"summary":"One-line summary shown in search"}' \
  --tags proposal,architecture
```

## 2. Create a track

The track is the delivery container. Put the proposal in the spec body as a `[[proposal-foo]]` wikilink — wikilinks render as clickable links in knowledge/track bodies.

```bash
# spec body can reference [[proposal-foo]] directly
superpos-tracks create --slug foo --title "Foo" --spec-file /tmp/foo-spec.md
# optional: --description "..."  --status planning|active|paused|done|archived
```

## 3. File issues, then link them to the track

`--issue-type-id` is a row id from the type catalogue — list it first. Reference the proposal as a **raw** `[[proposal-foo]]` wikilink in the description; it renders clickable. The old backtick workaround is **not** needed.

```bash
superpos-issues types                              # find the issue-type id
superpos-issues create --title "TASK-001: do the thing" \
  --issue-type-id <task-type-id> \
  --description 'Implements [[proposal-foo]]. Details...'
# capture the returned issue ULID, then:
superpos-tracks link-issue foo <issue-ULID>
# Shortcut: superpos-issues create ... --track-slug foo  (creates then links in one flow)
```

## 4. Read back

```bash
superpos-knowledge get-by-slug proposal-foo        # the proposal page
superpos-knowledge list-by-type topic              # all topic pages
# backlinks takes a ULID, not a slug — resolve the slug first:
superpos-knowledge backlinks "$(superpos-knowledge search proposal-foo --limit 1 | jq -r '.[0].id')"
superpos-tracks list                               # all tracks
superpos-tracks get foo                            # track + spec
superpos-tracks list-issues foo                    # issues linked to the track
superpos-issues show <issue-ULID>                  # one issue with relations
```

Link health: there are **no** `lint-state` / `broken-links` subcommands on `superpos-knowledge`. Check the page's own frontmatter fields (`lint_state`, `broken_links`) in the JSON returned by `get-by-slug` instead.

## 5. Update

```bash
# Track spec/title/description:
superpos-tracks patch foo --spec-file /tmp/foo-spec-v2.md
superpos-tracks patch foo --title "Foo (revised)"

# Issue (issue_id is positional; patch only the fields you pass):
superpos-issues update <issue-ULID> --description 'Updated [[proposal-foo]] reference'
superpos-issues transition <issue-ULID> --to <state>  # drive the state machine
superpos-issues close <issue-ULID>                 # policy-aware close

# Knowledge page (resolve by --type+--slug, or pass exact --id for older pages):
superpos-knowledge update --type topic --slug proposal-foo --body-file /tmp/proposal-foo-v2.md
```

## Registration

This skill is registered automatically: any flat `<slug>.md` in `.claude/skills/` with `name`/`description` frontmatter is auto-discovered, and the filename stem becomes the command (`superpos-agent-fluency.md` → `/superpos-agent-fluency`). No separate index edit is needed.
