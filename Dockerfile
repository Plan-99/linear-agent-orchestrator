# syntax=docker/dockerfile:1
FROM node:20-slim AS agent-cli

ARG CODEX_VERSION=0.144.1
ARG OMX_VERSION=0.20.0
ARG CLAUDE_CODE_VERSION=2.1.195

RUN npm install --global \
      "@openai/codex@${CODEX_VERSION}" \
      "oh-my-codex@${OMX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

FROM python:3.12-slim AS runtime

# Match the host user so bind-mounted files are never written as root.
ARG LAO_UID=1000
ARG LAO_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Baseline tools agent backends rely on (git, ps, curl, jq, rg).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       ca-certificates \
       curl \
       git \
       jq \
       procps \
       ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY docs ./docs
COPY scripts ./scripts

COPY --from=agent-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=agent-cli /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && ln -s /usr/local/lib/node_modules/oh-my-codex/dist/cli/omx.js /usr/local/bin/omx \
    && ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && install -m 0755 scripts/agent-runner.sh /usr/local/bin/lao-agent-runner

RUN pip install --upgrade pip \
    && pip install -e '.[test]'

RUN (getent group "${LAO_GID}" >/dev/null || groupadd --gid "${LAO_GID}" agent) \
    && useradd --create-home --home-dir /home/agent --uid "${LAO_UID}" --gid "${LAO_GID}" --non-unique agent \
    && mkdir -p /workspace/data /workspace/work /workspace/artifacts /workspace/project \
    && chown -R "${LAO_UID}:${LAO_GID}" /app /workspace /home/agent

ENV HOME=/home/agent \
    CODEX_HOME=/home/agent/.codex

USER agent

ENTRYPOINT ["linear-agent-orchestrator"]
CMD ["startup-check", "--allow-missing-secrets"]
