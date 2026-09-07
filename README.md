# Software Factory v0

A Python/Postgres prototype that turns one bounded GitHub issue into a checked,
agent-reviewed PR. You approve the specification and plan before implementation,
then review and merge the PR yourself. One worker serves one trusted repository,
with at most three implementation attempts per run.

This deployment uses native **Codex and Claude Code subscriptions**. Keep
`auth = "subscription"` in the configuration and use that mode for every run and
smoke test. The examples and smoke workflow do not configure API credentials.

## Set up the server

Use a Linux host with Docker Compose, AppArmor, Tailscale/SSH, tmux, and an editor.
Install the host profile following [the sandbox setup](deploy/security/README.md).
Keep this checkout, including `deploy/security/`, at its deployment path.

```bash
cp .env.example .env
chmod 600 .env
cp examples/factory.toml factory.toml
mkdir -p .auth/codex .auth/claude .auth/gh
chmod 700 .auth/codex .auth/claude .auth/gh
```

Edit `.env`: set an absolute workspace directory, a generated Postgres password,
and a released GHCR image digest. For a local prototype build, run
`docker build -t software-factory:v0-prototype .` and use the immutable ID printed
by `docker image inspect software-factory:v0-prototype --format '{{.Id}}'` as
`FACTORY_IMAGE`. Keep that image available for the lifetime of unfinished runs.
Workspace and authentication directories must be writable by the image's
`factory` user (default UID/GID `1000:1000`).

Clone the target using its HTTPS URL into `<workspace>/target`. Review
`factory.toml`: repository, base branch, host workspace path, non-mutating local
checks, exact required CI job names, and protected verification files. Review and
commit the target's `AGENTS.md`, `CLAUDE.md`, and `REVIEW.md`; starters are in
[examples/target](examples/target). Install its build/test dependencies in the
Dockerfile before building the image.

For the prepared `mcnewcp/orch-sandbox` trial, use base `prototype/v0-cdx`, required
CI `factory-v0-tests`, and local check `['sh', 'scripts/check-factory-v0.sh']`.

Start Postgres, migrate, and log in through the native tools. Choose the
subscription account in each agent's login flow. Codex's device login is designed
for headless hosts; see [official OpenAI documentation](https://learn.chatgpt.com/docs/auth).

```bash
docker compose up -d postgres
docker compose run --rm factory factory migrate
docker compose run --rm factory gh auth login --hostname github.com --git-protocol https
docker compose run --rm factory git -C /workspace/target config credential.https://github.com.helper '!gh auth git-credential'
docker compose run --rm factory codex login --device-auth
docker compose run --rm factory claude auth login
docker compose run --rm factory factory doctor
```

Run the subscription smokes sequentially. They consume subscription usage and
save native transcripts and validated responses under the workspace's `smoke/`.

```bash
docker compose run --rm factory factory smoke --agent codex --auth subscription --output /workspace/smoke
docker compose run --rm factory factory smoke --agent claude --auth subscription --output /workspace/smoke
docker compose up -d factory
export PATH="$PWD/scripts:$PATH"
factory doctor
```

## Try one issue

Connect over SSH, attach with `tmux new-session -A -s factory`, and submit a bounded
issue in the configured target. Replace `42` and `RUN_ID` below with its issue
number and the returned run ID.

```bash
factory prepare 42 --agent codex --auth subscription  # Or --agent claude
factory status RUN_ID
# Wait for awaiting_approval, then read/edit the printed spec.md and plan.md paths.
factory run RUN_ID                                   # Approves those exact contents
factory status RUN_ID
factory logs RUN_ID | less
factory resume RUN_ID                                # After resolving a blocked run
```

Submission returning zero means queued. `ready` means the candidate passed local
checks, agent review, and required CI and its PR is ready for human review.
Resume retains approval and the attempt budget. Changing scope requires a new
preparation with `--supersede RUN_ID`, after closing any open PR. The worker keeps
running when tmux or SSH disconnects. Use `FACTORY_DEPLOYMENT_DIR` with the host
wrapper if you move deployment configuration to a different directory.

## Verify the implementation

With Python 3.12+ and uv, use a **disposable** Postgres database for the complete
suite. Without its URL, database and workflow tests are skipped.

```bash
uv sync --frozen
uv run ruff check .
FACTORY_TEST_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE uv run pytest -q
bash scripts/check-codex-sandbox software-factory:v0-prototype
```

The offline sandbox probe needs the installed host profile and makes no model
requests. Tests use real Git/Postgres with simulated agents and GitHub; native
smokes and actual issue-to-PR trials provide separate evidence.

Tagged `v*` releases run tests and publish an image to GHCR. The manual
**Agent smoke** workflow accepts that digest, loads its matching source profiles,
and runs both subscription smokes sequentially. It needs repository secrets
`CODEX_AUTH_JSON_B64` and `CLAUDE_CREDENTIALS_JSON_B64` containing base64-encoded
native credential files. Its evidence artifacts expire after seven days; native
credential refresh in CI does not update those secrets.

Before upgrading, complete or supersede unfinished runs, stop the worker, back up
Postgres and the workspace, select the new digest, migrate, and recreate the
service. Preserve authentication mounts and history. The target's own deployment
pipeline remains responsible for releasing merged application changes.
