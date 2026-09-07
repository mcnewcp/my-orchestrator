# Phase 2 — the unattended host

Design §13. One host, one image per target repo, one wrapper, one timer per repo. Nothing is
installed natively on the host except Docker, `factory-host`, the env file, and the two units.
Everything here assumes `auth = "api"`: `poll` refuses `subscription`, and no credential directory
is ever mounted.

Files in this directory:

| file | installs as | what it is |
|---|---|---|
| `Dockerfile` | — | the factory base image: Python 3.12, uv, git, gh, make, Node LTS, both harness CLIs at pinned versions, the factory wheel, a non-root user |
| `factory-host` | `/usr/local/bin/factory-host` | the only place the `docker run` invocation lives; the timer and the operator both use it |
| `factory-poll@.service` | `/etc/systemd/system/factory-poll@.service` | `Type=oneshot`, `ExecStart=/usr/local/bin/factory-host %i poll` |
| `factory-poll@.timer` | `/etc/systemd/system/factory-poll@.timer` | every 15 minutes, `Persistent=true` |
| `../.github/workflows/image.yml` | — | builds and pushes `ghcr.io/<owner>/factory:<tag>` on every `v*` git tag |

`<repo>` below is the instance name — the timer instance, the env file name, and the directory
under `/srv/factory`. Use the target repository's short name (`my-service`).

## 1. Publish the image

Tag the factory repo and push the tag; `image.yml` builds `deploy/Dockerfile` with the repo root as
context and pushes `ghcr.io/<owner>/factory:<tag>` (plus `:latest`).

```
git tag v0.1.0 && git push origin v0.1.0
```

The harness pins live in the Dockerfile's build args and default to the versions in
`docs/design/harness-smoke-2026-09-06.md` (`CLAUDE_CODE_VERSION`, `CODEX_VERSION`; also `GH_VERSION`,
`NODE_MAJOR`, `UV_VERSION`). Bumping a harness means changing the default, retagging, and re-running
`doctor` on the host — that is what makes the pin real. Set the matching
`[harness.<name>] pinned_version` in the target repo's `factory.toml` so `doctor` warns on drift.

To build locally instead (from the repo root, not from `deploy/`):

```
docker build -f deploy/Dockerfile -t ghcr.io/<owner>/factory:dev .
```

There is no `.dockerignore`; a local build sends the whole checkout, `.venv/` included. Build from a
clean clone if that matters.

## 2. Target image

In the target repo, `.devcontainer/Dockerfile` is `FROM ghcr.io/<owner>/factory:<tag>` plus whatever
`make test` and `make lint` need. It is a protected path (§9) — the factory's own branches can never
change the image it runs in. Build it on the host and point `FACTORY_IMAGE` at it; publishing it is
optional.

```
docker build -f /srv/factory/<repo>/repo/.devcontainer/Dockerfile -t factory-<repo>:latest /srv/factory/<repo>/repo
```

## 3. Host setup

Docker, one unprivileged user with no production access, one directory per repo.

```
sudo adduser --system --group --home /srv/factory --shell /usr/sbin/nologin factory
sudo usermod -aG docker factory                       # to run docker run; see the note below
sudo install -d -o factory -g factory -m 0755 /srv/factory /srv/factory/<repo>
sudo -u factory git clone https://github.com/<owner>/<repo> /srv/factory/<repo>/repo
```

`/srv/factory/<repo>` is bind-mounted at `/work`; `/srv/factory/<repo>/repo` is the clone and the
container's working directory. `.factory/` (worktrees, transcripts, locks, `poll.json`,
`doctor.json`) lives inside the clone and is host-local and expendable. Everything under
`/srv/factory/<repo>` must be owned by the `factory` user, because the container runs as that uid.

Docker group membership is root-equivalent on the host. That is the trade the design accepts (§16:
the container is blast-radius reduction, not isolation); keep this host dedicated and free of
production access. Rootless Docker works too — then drop the `usermod` and run the timer as the
user that owns the rootless daemon.

## 4. The env file

`/etc/factory/<repo>.env`, owner root, mode 600. It is a `docker --env-file`: plain `KEY=value`,
one per line, no quoting, no `export`, no interpolation.

```
sudo install -d -m 0755 /etc/factory
sudo install -m 600 /dev/null /etc/factory/<repo>.env
sudoedit /etc/factory/<repo>.env
```

| key | value |
|---|---|
| `FACTORY_IMAGE` | the image to run, e.g. `factory-<repo>:latest` or `ghcr.io/<owner>/factory:v0.1.0` |
| `GH_TOKEN` | fine-grained PAT scoped to the one target repo: contents, issues, pull requests (write); metadata (read) |
| `ANTHROPIC_API_KEY` | when `harness = "claude"` |
| `CODEX_API_KEY` | when `harness = "codex"` |

Set only the provider key for the configured harness. The factory forwards exactly that one key to
harness subprocesses and strips `GH_TOKEN`, `GITHUB_TOKEN`, and every other provider key (§8); check
commands get no provider key at all. `GH_TOKEN` is used by the container's entrypoint
(`gh auth setup-git`) and by the factory's own `gh` and `git push` calls, nothing else.

## 5. Install the wrapper and the units

```
sudo install -m 0755 deploy/factory-host /usr/local/bin/factory-host
sudo install -m 0644 deploy/factory-poll@.service deploy/factory-poll@.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Verify before enabling anything:

```
sudo -u factory factory-host <repo> doctor        # binaries, versions vs pins, gh auth, per-harness probes
```

`doctor` runs each harness in read *and* write mode in a throwaway worktree. The write probe is the
one that matters in a container: both harnesses' sandboxes depend on kernel features Docker's
default seccomp and user-namespace settings can restrict (§13). If it fails, fix the container
(seccomp profile, namespaces) — never the harness flags, and never by disabling the sandbox at
runtime. `doctor.json` caches the passing result per harness, auth, factory version and CLI version;
`poll` re-runs `doctor` by itself whenever that record is missing or stale.

Then enable the timer:

```
sudo systemctl enable --now factory-poll@<repo>.timer
systemctl list-timers 'factory-poll@*'
journalctl -u factory-poll@<repo>.service -f
```

The service is `oneshot` with `TimeoutStartSec=infinity` (a tick legitimately runs for hours: each
stage is bounded by `stage_timeout_min`, and a tick runs every eligible issue in turn),
`KillMode=mixed` and `TimeoutStopSec=120` so a stop signals the container first and gives the stage
time to die. systemd will not start a oneshot that is still running, and `poll` takes
`.factory/run/poll.lock` regardless. The timer's `Persistent=true` catches up a tick missed while
the host was down; `AccuracySec=1min` keeps the quarter-hour honest.

## 6. Operating

Everything is the same CLI through the wrapper:

```
ssh host
sudo -u factory factory-host <repo> status 42
sudo -u factory factory-host <repo> accept 42
sudo -u factory factory-host <repo> dismiss 42 F3 "not a real bug: X"
sudo -u factory factory-host <repo> run 42
sudo -u factory factory-host <repo> abandon 42
sudo -u factory factory-host <repo> poll          # a tick by hand
```

`factory-host` adds `-it` only when it has a terminal on both ends, so the same command works under
the timer and at a prompt. Close runs with `abandon`, never by closing the PR in the GitHub UI: a
hand-closed PR leaves an open, labeled issue whose `finalize` fails until the retry cap parks it
(§12).

Gates arrive as PR comments; the journal (`journalctl -u factory-poll@<repo>.service`) holds the log.
An issue enters the loop only by carrying the `[poll] label` from `factory.toml`.

Overrides `factory-host` reads from its own environment, for a non-standard host layout:
`FACTORY_ENV_DIR` (default `/etc/factory`), `FACTORY_STATE_DIR` (default `/srv/factory`),
`FACTORY_IMAGE` (default: read out of the env file), `FACTORY_USER` / `FACTORY_UID` / `FACTORY_GID`
(default: the `factory` user's ids).

## 7. Rebuild

The host is disposable (rule 6): nothing needed to continue a run exists only on it. To rebuild,
repeat sections 3–5 — restore the env file, the wrapper and the units, clone, pull the image, enable
the timer. Every run resumes from `origin/factory/<issue>`: a command for an issue with no local
branch recreates the branch and worktree from the remote and continues. Transcripts from before the
rebuild are gone; nothing else is.

To move to a new image tag, edit `FACTORY_IMAGE` in the env file (rebuild the target image on top of
the new base first) and re-run `doctor`. No service to restart: the next tick picks it up.
