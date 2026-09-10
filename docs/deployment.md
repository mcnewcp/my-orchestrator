# Dedicated-host deployment

Phase 2 packaging is included. Image publication, container `doctor`, and timer
dogfood are acceptance steps to run after the attended prototype converges.
These instructions target a dedicated Linux host with Docker and systemd.

The base image includes Python 3.12, git, gh, make, ripgrep, uv 0.12.10, Node 24,
Claude Code 2.1.263, and Codex 0.153.4. Node 24 is an LTS release; uv is copied
from its official image and gh uses its official signed Debian repository.
See the [Node release schedule](https://nodejs.org/en/about/previous-releases),
[uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/), and
[gh installation instructions](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).

1. Build the factory image locally with
   `docker build -f deploy/Dockerfile -t factory:v0.1.0 .`, or publish a Git tag.
   The tag workflow publishes `ghcr.io/<owner>/factory:<tag>` for Linux amd64.
   Make the package public or arrange registry access for the host's factory
   account before pulling it. Harness versions are build arguments; changes
   require another `doctor` run and matching pins in the target `factory.toml`.

2. Have the host administrator create the account and install the supplied
   wrapper and units. Run these from the factory source checkout:

   ```sh
   sudo useradd --system --create-home --user-group --shell /bin/bash factory
   sudo usermod -aG docker factory
   sudo install -d -m 0750 -o root -g factory /etc/factory
   sudo install -d -m 0750 -o factory -g factory /srv/factory/orch-sandbox
   sudo install -m 0755 deploy/factory-host /usr/local/bin/factory-host
   sudo install -m 0644 deploy/factory-poll@.service deploy/factory-poll@.timer /etc/systemd/system/
   sudo install -m 0600 -o factory -g factory examples/factory.env.example /etc/factory/orch-sandbox.env
   sudoedit /etc/factory/orch-sandbox.env
   ```

   Replace every placeholder. `GH_TOKEN` needs repository contents, issues,
   and pull requests read/write, plus metadata read. Include the API key for
   the selected harness. Set `auth = "api"` in the target `factory.toml`;
   subscription authentication is refused by `poll`. The env file must remain
   mode 600 **owned by factory**: the wrapper reads it as that user, while
   systemd reads it through `EnvironmentFile`. Use literal `KEY=value` lines
   with no quoting, substitutions, or `export`. The wrapper never sources it.

3. Clone the target through the base image, which supplies git and gh. Replace
   the image and repository URL below. No native harness or host login is needed:

   ```sh
   sudo -u factory docker run --rm --init \
     --user "$(id -u factory):$(id -g factory)" \
     --env-file /etc/factory/orch-sandbox.env \
     --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
     --mount type=bind,src=/srv/factory/orch-sandbox,dst=/work \
     --workdir /work ghcr.io/OWNER/factory:FACTORY_TAG \
     git clone https://github.com/mcnewcp/orch-sandbox.git repo
   ```

   Prepare and commit the target's `factory.toml`, `AGENTS.md`, `CLAUDE.md`,
   `REVIEW.md`, checks, and intake template during attended setup with
   `factory init`. Copy `examples/target.Dockerfile` into the target as
   `.devcontainer/Dockerfile`, replace its base image, and install the exact
   toolchain and test dependencies there. Checks need to work without fetching
   dependencies from a harness sandbox.

   ```sh
   sudo -u factory docker build \
     -f /srv/factory/orch-sandbox/repo/.devcontainer/Dockerfile \
     -t factory-target:v0.1.0 /srv/factory/orch-sandbox/repo
   ```

   Set `FACTORY_IMAGE` in the env file to that target image. All wrapper commands
   run as the factory account, with its UID/GID, a writable `/tmp` and `HOME`, and
   `/srv/factory/orch-sandbox` mounted at `/work`. Credential directories are not
   mounted. Commits use the configured identity or `Software Factory
   <factory@localhost>`; repository-local git configuration takes precedence.

4. Verify both installed harnesses with their selected API keys, then run one
   labeled issue manually before enabling the timer:

   ```sh
   sudo -u factory factory-host orch-sandbox doctor --harness claude --auth api
   sudo -u factory factory-host orch-sandbox doctor --harness codex --auth api
   sudo -u factory factory-host orch-sandbox poll
   sudo systemctl daemon-reload
   sudo systemctl enable --now factory-poll@orch-sandbox.timer
   ```

   Both read and write probes must pass inside the target image. If a sandbox
   cannot start under the host's Docker/kernel settings, fix those settings and
   repeat `doctor`; the wrapper adds no bypass or automatic fallback.
   The service has no start timeout because one poll can process several issues.
   Calendar ticks occur every 15 minutes and `Persistent=true` catches a missed
   tick after downtime. The local poll lock prevents overlapping manual runs.

Use `journalctl -u factory-poll@orch-sandbox.service` for logs and
`sudo -u factory factory-host orch-sandbox status 42` for branch state.
Clear a gate with the same wrapper (`accept`, `dismiss`, or a hand commit),
then let the next tick resume it. Verify one ready PR and one parked issue that
resumes after operator action before calling phase 2 validated.

To rebuild a host, restore the env file, wrapper, units, and target image; clone
the repository; run `doctor`; and enable the timer. Issue branches recover from
`origin/factory/<issue>`. `.factory/` transcripts and retry counters are
expendable. The factory never merges PRs; retain human approval on the base branch.
