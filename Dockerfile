FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip git curl tini && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Pin the CLI. The in-process superpos_ask MCP server + streaming control
# protocol in claude_executor.py speak the protocol of claude-code-sdk 0.0.25,
# which shipped alongside the CLI 2.0.x line. Leaving this unpinned pulled CLI
# `latest` (2.1.x) on rebuild, whose handshake the pinned SDK no longer
# survives → pre-init crash → ask/search permanently degraded (see issue #40).
# Keep this in lockstep with the claude-code-sdk pin in requirements.txt.
RUN npm install -g @anthropic-ai/claude-code@2.0.14

# uv / uvx — used to launch MiniMax's web-search MCP (uvx minimax-coding-plan-mcp)
# on shim backends. Harmless on native Anthropic (only invoked when configured).
RUN pip install --no-cache-dir --break-system-packages uv

WORKDIR /app

# slim-agent-core is pulled directly from GitHub via requirements.txt
# (the `git+https://…` line), so no parent-directory build context required.
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh
COPY workspace/ /workspace/

# Pre-populate modules-bin at build time with the workspace modules'
# scripts.  This is a safety net: at runtime, entrypoint.sh re-runs the
# symlinking via `module_setup --bin-dir` to layer in core-bundled
# modules (e.g. superpos-issues) on top.  If that runtime call fails for
# any reason, the build-time symlinks here keep workspace tools
# callable from PATH so the container is not totally broken.
RUN mkdir -p /workspace/.claude/modules-bin && \
    for dir in /workspace/.claude/modules/*/scripts; do \
      if [ -d "$dir" ]; then \
        for script in "$dir"/*; do \
          chmod +x "$script" && \
          ln -sf "$script" /workspace/.claude/modules-bin/$(basename "$script"); \
        done; \
      fi; \
    done
ENV PATH="/workspace/.claude/modules-bin:$PATH"

# Create non-root user (required for bypassPermissions mode)
RUN useradd -m -s /bin/bash -u 1001 agent && \
    mkdir -p /home/agent/.claude && \
    chown -R agent:agent /workspace /home/agent/.claude

ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV HOME="/home/agent"

VOLUME ["/home/agent/.claude"]

USER agent
WORKDIR /workspace

# tini runs as PID 1 and reaps orphaned grandchildren (esbuild/node
# subprocesses left behind when a Claude run dies) — without it they
# accumulate as zombies because Python doesn't reap reparented orphans.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python3", "-m", "superpos_agent_claude"]
