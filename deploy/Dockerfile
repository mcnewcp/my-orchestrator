# syntax=docker/dockerfile:1
#
# Factory base image (design §13). Debian slim + Python 3.12, uv, git, gh, make, Node LTS, the two
# harness CLIs at pinned versions, and the factory wheel built from this repo. A non-root user; an
# entrypoint that wires the git credential helper to GH_TOKEN and then execs the command.
#
# The pins default to the versions smoke-tested in docs/design/harness-smoke-2026-09-06.md; that is
# what makes them real (§13). Override at build time, e.g.
#   docker build -f deploy/Dockerfile --build-arg CLAUDE_CODE_VERSION=2.1.270 -t factory:dev .
# The build context is the repo root (the wheel is built from pyproject.toml + src/).
#
# This image is packaging and blast-radius reduction, not a security boundary (§16).

ARG UV_VERSION=0.5.11

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- build the factory wheel from this repo (hatchling, per pyproject.toml) --------------------
FROM python:3.12-slim-bookworm AS wheel
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

# --- runtime ------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# Harness CLI pins: docs/design/harness-smoke-2026-09-06.md. doctor warns when factory.toml's
# pinned_version disagrees with what the image actually ships.
ARG CLAUDE_CODE_VERSION=2.1.263
ARG CODEX_VERSION=0.153.4
ARG GH_VERSION=2.65.0
ARG NODE_MAJOR=22
ARG UID=1000
ARG GID=1000

LABEL org.opencontainers.image.title="factory" \
      org.opencontainers.image.description="Factory v0: one GitHub issue in, one PR ready for review out."

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    HOME=/home/factory

# ca-certificates + curl: fetching gh and the NodeSource setup script, and the harnesses' own egress.
# make: the target repo's checks are `make test` / `make lint` (§4).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git make \
 && rm -rf /var/lib/apt/lists/*

# gh, pinned. The apt repository carries only the newest build, so the release tarball is what pins.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    gh --version

# Node LTS, then both harness CLIs at their pinned versions.
RUN set -eux; \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*; \
    npm install -g \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}"; \
    npm cache clean --force; \
    claude --version; \
    codex --version

# uv: the target image's toolchain layer and the harnesses' allowlisted `Bash(uv *)` both expect it.
COPY --from=uv /uv /uvx /usr/local/bin/

COPY --from=wheel /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl \
 && python -c "import factory.cli" && command -v factory >/dev/null

# Non-root. The host wrapper passes --user <host factory uid:gid>, which need not equal UID above, so
# HOME must be writable by any uid: claude and codex write their config dirs there.
RUN set -eux; \
    groupadd -g "${GID}" factory; \
    useradd -m -u "${UID}" -g "${GID}" -s /bin/bash factory; \
    chmod 0777 /home/factory; \
    mkdir -p /work; \
    chown "${UID}:${GID}" /work

# gh auth setup-git installs the credential helper that reads GH_TOKEN, so git push over HTTPS works.
# Harness subprocesses never receive GH_TOKEN (§8); only the factory's own git and gh calls use it.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -e' \
      'if [ -n "${GH_TOKEN:-}" ]; then' \
      '    gh auth setup-git' \
      'fi' \
      'exec "$@"' \
      > /usr/local/bin/factory-entrypoint \
 && chmod 0755 /usr/local/bin/factory-entrypoint

USER factory
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/factory-entrypoint"]
CMD ["factory", "--help"]
