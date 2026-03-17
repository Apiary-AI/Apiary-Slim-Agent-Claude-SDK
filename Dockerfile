FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip git curl && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh
COPY workspace/ /workspace/

# Symlink module scripts onto PATH so Claude can call them by name
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

ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV HOME="/home/agent"

VOLUME ["/home/agent/.claude"]

USER agent
WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-m", "src.main"]
