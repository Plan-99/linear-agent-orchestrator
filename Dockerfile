# syntax=docker/dockerfile:1

# Exact, pinned top-level defaults for direct and first-time builds. The update
# launcher may override these with equally exact, validated versions.
ARG CODEX_VERSION=0.144.1
ARG OMX_VERSION=0.20.0
ARG CLAUDE_CODE_VERSION=2.1.195
ARG LAO_SOURCE_REVISION=unknown

FROM node:22-slim AS agent-cli

ARG CODEX_VERSION
ARG OMX_VERSION
ARG CLAUDE_CODE_VERSION

RUN npm install --global --engine-strict \
      "@openai/codex@${CODEX_VERSION}" \
      "oh-my-codex@${OMX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # The container never has remote git credentials. Without this, git (e.g.
    # git-lfs downloads during worktree checkout) prompts on the container TTY
    # and hangs the run until the 120s git timeout kills it.
    GIT_TERMINAL_PROMPT=0

# Baseline tools agent backends rely on (git, ps, curl, jq, rg). tmux hosts the
# optional per-run interactive console (AGENT_CONSOLE); it is inert when the
# feature is disabled. git-lfs is required because target repositories may use
# LFS: their post-checkout hook aborts `git worktree add` when git-lfs is
# missing, and without the smudge filter LFS files check out as pointer text.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       ca-certificates \
       curl \
       git \
       git-lfs \
       jq \
       procps \
       ripgrep \
       tmux \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install --system --skip-repo \
    # The container has no LFS remote credentials; keep pointer files instead
    # of failing checkout when objects are absent from the local LFS store.
    && git config --system lfs.skipdownloaderrors true

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY docs ./docs
COPY scripts ./scripts
RUN pip install --upgrade pip \
    && pip install -e '.[test]'


COPY --from=agent-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=agent-cli /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && ln -s /usr/local/lib/node_modules/oh-my-codex/dist/cli/omx.js /usr/local/bin/omx \
    && ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && install -m 0755 scripts/agent-runner.sh /usr/local/bin/lao-agent-runner \
    && install -m 0755 scripts/lao-pty-host.sh /usr/local/bin/lao-pty-host \
    && install -m 0755 scripts/lao-pty-entry.sh /usr/local/bin/lao-pty-entry

# Match the host user so bind-mounted files are never written as root. Keep
# these build args close to their first use so host-ID changes do not invalidate
# the expensive OS and Python dependency layers above.
ARG LAO_UID=1000
ARG LAO_GID=1000

RUN (getent group "${LAO_GID}" >/dev/null || groupadd --gid "${LAO_GID}" agent) \
    && useradd --create-home --home-dir /home/agent --uid "${LAO_UID}" --gid "${LAO_GID}" --non-unique agent \
    && mkdir -p /workspace/data /workspace/work /workspace/artifacts /workspace/project /workspace/logs \
    && chown -R "${LAO_UID}:${LAO_GID}" /app /workspace /home/agent

ENV HOME=/home/agent \
    CODEX_HOME=/home/agent/.codex

# Labels are deliberately last: changing a CLI version or source revision must
# not invalidate the reusable apt/pip/runtime setup layers.
ARG CODEX_VERSION
ARG OMX_VERSION
ARG CLAUDE_CODE_VERSION
ARG LAO_SOURCE_REVISION

LABEL org.opencontainers.image.revision="${LAO_SOURCE_REVISION}" \
      io.linear-agent-orchestrator.codex.version="${CODEX_VERSION}" \
      io.linear-agent-orchestrator.omx.version="${OMX_VERSION}" \
      io.linear-agent-orchestrator.claude-code.version="${CLAUDE_CODE_VERSION}"

USER agent

ENTRYPOINT ["linear-agent-orchestrator"]
CMD ["startup-check", "--allow-missing-secrets"]
