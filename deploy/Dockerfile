# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM python:3.12-slim-bookworm AS wheel
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.12-slim-bookworm
ARG CLAUDE_VERSION=2.1.263
ARG CODEX_VERSION=0.153.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git make ripgrep libatomic1 libstdc++6 \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl --fail --silent --show-error --location \
       https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       --output /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
       "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global \
       "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
       "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

COPY --from=wheel /dist /tmp/factory-wheels
RUN uv pip install --system --no-deps /tmp/factory-wheels/*.whl \
    && rm -rf /tmp/factory-wheels \
    && useradd --create-home --uid 1000 --user-group factory
COPY --chmod=0755 deploy/entrypoint /usr/local/bin/factory-entrypoint

ENV HOME=/tmp/factory-home \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER factory
WORKDIR /work/repo
ENTRYPOINT ["/usr/local/bin/factory-entrypoint"]
CMD ["factory", "--help"]
