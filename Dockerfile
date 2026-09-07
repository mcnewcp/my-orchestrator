# Extend the apt toolchain here for the one configured trusted target repository.
FROM node:24-bookworm-slim AS agents
ARG CODEX_VERSION=0.153.4
ARG CLAUDE_VERSION=2.1.263
RUN npm install --global --omit=dev \
    @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} \
    && npm cache clean --force

FROM python:3.12-slim-bookworm
ARG FACTORY_UID=1000
ARG FACTORY_GID=1000
LABEL org.opencontainers.image.source="https://github.com/mcnewcp/my-orchestrator"
RUN apt-get update && apt-get install --no-install-recommends -y \
    bash build-essential ca-certificates curl gh git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${FACTORY_GID} factory \
    && useradd --uid ${FACTORY_UID} --gid factory --create-home --shell /bin/bash factory
COPY --from=agents /usr/local/bin/node /usr/local/bin/node
COPY --from=agents /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && node -e 'const root = "/usr/local/lib/node_modules/@anthropic-ai/claude-code/"; \
        const entry = require(root + "package.json").bin.claude; \
        require("node:fs").symlinkSync(root + entry, "/usr/local/bin/claude");' \
    && pip install --no-cache-dir uv==0.12.10
WORKDIR /opt/factory
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-editable \
    && mkdir -p /workspace /auth/codex /auth/claude /auth/gh \
    && chown -R factory:factory /workspace /auth
ENV PATH="/opt/factory/.venv/bin:${PATH}" \
    CODEX_HOME=/auth/codex \
    CLAUDE_CONFIG_DIR=/auth/claude \
    GH_CONFIG_DIR=/auth/gh \
    FACTORY_CONFIG=/etc/factory/factory.toml \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1
USER factory
WORKDIR /workspace
# Exercise the final PATH and runtime as the same user that launches agent sessions.
RUN python - <<'PY'
import re
import subprocess

from factory.agents import PINNED_VERSIONS

for engine, expected in PINNED_VERSIONS.items():
    result = subprocess.run(
        [engine, "--version"], check=True, capture_output=True, text=True, timeout=30
    )
    actual = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
    if actual is None or actual.group() != expected:
        raise SystemExit(f"{engine}: expected {expected}, got {result.stdout!r}")
    print(result.stdout.strip(), flush=True)
PY
CMD ["factory", "worker"]
