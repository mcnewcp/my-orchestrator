"""`factory doctor` (design §6, §8, §13): binaries, versions vs pins, gh auth, per-harness auth probe + write-mode probe.

Each probe runs in a throwaway WORKTREE — `git init` a scratch repo under .factory/tmp/doctor/<harness>/, commit one
file, `git worktree add wt` — so it reproduces what every stage sees: .git is a FILE pointing at a git dir outside the
sandbox's writable root, and the path has never been trusted by the harness before. Probes use schemas/probe.json
({"ok": bool, "note": str}) and a prompt written by doctor. The read probe exercises every flag the adapter depends on
(--max-turns, --setting-sources, --strict-mcp-config, --json-schema ...) because `claude --version` exits 0 for unknown
flags and cannot detect a removal. The write probe must exit 0, leave probe.txt in place, and then one configured check
command (config.checks[0]) must run in that worktree via checks.run_checks with the checks env (catches a sandbox that
cannot write its cache, or a missing `make`).

.factory/doctor.json = {"<harness>:<auth>": {"factory_version", "cli_version", "at", "checks": [...]}}; records for
different combinations coexist. cli.py runs doctor once for the configured (harness, auth) pair, or for the pair given by
--harness/--auth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .repo import Repo


@dataclass
class DoctorReport:
    ok: bool
    lines: list[str] = field(default_factory=list)  # "ok: ..." / "warn: ..." / "FAIL: ..."
    harness: str = ""
    cli_version: str = ""
    auth: str = ""

    def render(self) -> str:
        return "\n".join(self.lines)


def doctor(repo: Repo, config: Config, *, harness: str, auth: str, model: str | None,
           parent_env: dict, write_probe: bool = True) -> DoctorReport:
    """Order (stop at the first FAIL that makes later steps meaningless):
    python >= 3.12; git present, `git status` in the checkout works (a "dubious ownership" error FAILs with the
    safe.directory hint), identity: `git var GIT_AUTHOR_IDENT` or "ok: no git identity; commits use factory <factory@localhost>";
    every distinct config.checks[i][0] resolves on PATH (else FAIL naming it); gh present + gh.auth_ok; harness binary
    present + version (claude >= CLAUDE_MIN_VERSION; warn on pinned_version mismatch); build_env (api key presence;
    reports which NETWORK_ALLOWLIST vars were forwarded); read probe; write probe (+ one check command).
    For codex, best effort: `codex login status` under the built env, WARN if it mentions multiple auth env vars.
    Writes the doctor record on ok."""
    raise NotImplementedError


def doctor_record_is_current(records: dict, *, harness: str, auth: str, cli_version: str, factory_version: str) -> bool:
    raise NotImplementedError


_ = Path
