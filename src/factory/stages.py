"""Stage contracts (design §9), termination rule (§11), operator commands (§6).

Every stage: validate state -> render prompt -> run harness -> validate output -> deterministic gate ->
commit artifact + state.json -> push. ANY failure after the harness launched (timeout, non-zero exit, bad output,
gate, allowed-edit violation) resets the worktree to HEAD (repo.reset_hard) BEFORE raising, keeps the transcript,
and exits 1 — so "re-run the same command" (§15) always works. Every exit 2 posts one PR comment naming the gate and
what clears it, idempotent per gate and outcome_sha.

Invariants
  * cli.py calls prepare(ctx) exactly once per process, before dispatch; stage functions assume a prepared ctx and
    never call it (prepare() returns immediately if ctx.prepared). spec and run are dispatched with need_state=False.
    status() and init/doctor/poll never call prepare. abandon calls prepare(need_state=False,
    commit_operator_edits=False, fetch=False, strict_clean=False, check_protected=False): it tears the branch down,
    so a dirty worktree, a diverged remote and a protected-path change on the branch must not stop it.
  * The RunLock is taken by prepare() for the whole command (ctx.holds_lock records that THIS process wrote it) and
    cleared with RunLock.clear_if_owned by cli.py / run_issue in a finally — never after a KeyboardInterrupt, whose
    dead pid is what makes the next command take the interrupted-stage path. Each stage updates the lock's `stage`
    field (RunLock.write) before doing work. run_harness_stage assumes the lock is held.
  * Why an attempt failed reaches the next attempt's prompt through TWO channels (state.py): the RunLock's own
    `last_error` for an interrupted (killed) stage, and `.factory/run/<issue>.last-error` for an ordinary exit 1,
    which the lock cannot carry because the lock is cleared at the end of every command. prepare() reads both into
    ctx.last_error (the interrupted one wins, being the more recent), _gate_failure and run_harness_stage write the
    file, and commit_and_push clears it: a stage that committed has nothing to apologise for.
  * NeedsHuman is constructed in exactly two places: park() (commits the outcome, posts the gate comment) and
    check_parked() (re-raises an already-recorded, already-commented gate). No other function raises it.
  * Round index: review and fix use their round number; spec, plan and build always use 1 (a --force re-run overwrites
    work/<n>/prompts/<stage>-1.md and checks/<stage>-1*.log rather than accumulating attempts).
  * Context.harness_name / auth / model are read from ctx.config (validate_overrides applied CLI flags): one source.

Adaptations to the leaf modules as implemented (see the module notes in the driver's report):
  * prepare() probes repo.local_branch_exists / remote_branch_exists before calling ensure_worktree, so the
    "no branch yet and need_state=False" case returns worktree=None instead of hitting ensure_worktree's FactoryError.
  * The read-stage "harness wrote files" gate compares repo.changed_paths_in_worktree (exact NUL-parsed paths) rather
    than status_porcelain lines, which are `XY path` records a rename would make unparseable.
  * park() commits EVERY pending work/<n>/ artifact in its evidence commit, so its state-only commit really does
    change only work/<n>/state.json and the issue stays parked (state.is_parked's second clause).
  * prepare() extends .git/info/exclude with config.transient_paths (Repo.ensure_worktree only knows the built-ins).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import checks as checks_module
from . import gh as gh_module
from . import prompts
from . import schema as schema_module
from . import state as state_module
from .config import Config, HarnessConfig
from .errors import FactoryError, GateViolation, NeedsHuman
from .gh import GitHub, PullRequest
from .harness import Harness, HarnessResult, build_env, checks_env, get_harness
from .repo import Repo
from .state import Ledger, ReviewRecord, RunLock, StageRecord, State

STAGE_ORDER = ("spec", "plan", "build")  # committed once each; review/fix are rounds

GATES = ("open_questions", "baseline_failing", "no_progress", "rounds_exhausted")

#: The gates raised by the review/fix loop itself. They are recorded on the review that raised them
#: (ReviewRecord.gated), so `run` reviews the operator's answer instead of re-parking on findings that were
#: adjudicated by hand — an `accept`/`dismiss` moves HEAD without changing a line of code.
REVIEW_LOOP_GATES = ("no_progress", "rounds_exhausted")

_CHECK_TAIL_LINES = 200  # of the latest check log, into a {checks} placeholder
_SPEC_BODY_LINES = 40  # of spec.md, into the draft PR body
_TRUNCATION_RESERVE = (
    4096  # bytes of the diff budget kept for the truncation banner and the diffstat
)
_INTENT_TITLE_PREFIX = "# Intent: "


@dataclass
class Context:
    repo: Repo
    gh: GitHub
    config: Config  # already has CLI overrides applied (harness, auth, model)
    issue: int
    force: bool = False  # --force was passed (only spec/plan/build accept it; run() calls stages with force=False)
    parent_env: dict | None = None  # os.environ by default; tests inject
    out: Callable[[str], None] | None = (
        None  # progress lines; None -> sys.stderr. stdout is for command output.
    )

    # populated by prepare()
    prepared: bool = False
    worktree: Path | None = None
    state: State | None = None
    ledger: Ledger | None = None
    harness: Harness | None = None
    harness_env: dict | None = None
    force_push: bool = (
        False  # set by rewind_for_force; commit_and_push then uses --force-with-lease
    )
    last_error: str | None = None  # why the previous attempt was discarded (stage_note input)
    holds_lock: bool = False  # this process wrote .factory/run/<issue>.json and must clear it

    @property
    def branch(self) -> str:
        return f"factory/{self.issue}"

    @property
    def harness_name(self) -> str:
        return self.config.harness

    @property
    def auth(self) -> str:
        return self.config.auth

    @property
    def model(self) -> str | None:
        return self.config.model

    def log(self, msg: str) -> None:
        """f"[factory] {msg}" via self.out, or to sys.stderr when out is None."""
        line = f"[factory] {msg}"
        if self.out is None:
            print(line, file=sys.stderr)
        else:
            self.out(line)


# ---------------------------------------------------------------- small accessors


def _work_rel(issue: int) -> str:
    return f"work/{issue}"


def _require_worktree(ctx: Context, what: str) -> Path:
    if ctx.worktree is None:
        raise FactoryError(
            f"{what} needs a worktree for issue {ctx.issue}",
            hint=f"run `factory spec {ctx.issue}` first",
        )
    return ctx.worktree


def _require_state(ctx: Context, what: str) -> State:
    if ctx.state is None:
        raise FactoryError(
            f"{what} needs work/{ctx.issue}/state.json",
            hint=f"run `factory spec {ctx.issue}` first",
        )
    return ctx.state


def _require_stage(ctx: Context, stage: str, what: str) -> None:
    state = _require_state(ctx, what)
    if not state.stage_done(stage):
        raise FactoryError(
            f"{what} requires the {stage} stage, which has not run for issue {ctx.issue}",
            hint=f"run `factory {stage} {ctx.issue}` (or `factory run {ctx.issue}`) first",
        )


def _read_work_file(ctx: Context, name: str) -> str:
    """A committed artifact under work/<issue>/; FactoryError names the path when it is missing."""
    path = state_module.work_dir(_require_worktree(ctx, f"reading {name}"), ctx.issue) / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryError(f"cannot read {path}: {exc}") from exc


def _write_work_file(ctx: Context, name: str, text: str) -> Path:
    path = state_module.work_dir(_require_worktree(ctx, f"writing {name}"), ctx.issue) / name
    state_module.atomic_write_text(path, text)
    return path


def _checks_text(ctx: Context) -> str:
    """The {checks} placeholder: the tail of the newest committed check log."""
    _, text = checks_module.latest_check_log(_require_worktree(ctx, "checks"), ctx.issue)
    return checks_module.tail(text, _CHECK_TAIL_LINES)


def _stage_note(ctx: Context, *, notes: list[str]) -> str:
    """The {stage_note} placeholder: what the factory knows about THIS attempt. "" renders as "(none)"."""
    parts = [note.strip() for note in notes if note and note.strip()]
    if ctx.force:
        parts.insert(
            0,
            "This is a `--force` re-run: the branch was rewound to the start of this stage and the "
            "previous attempt's output was discarded. The inputs above are the current ones.",
        )
    if ctx.last_error:
        parts.append(f"The previous attempt was discarded because: {ctx.last_error}")
    return "\n\n".join(parts)


def _run_lock(ctx: Context, *, stage: str | None = None, last_error: str | None = None) -> None:
    """Read-modify-write `.factory/run/<issue>.json`; prepare() created it, each stage updates it.

    A lock that has to be recreated here is built with RunLock.create, which stamps the pid identity
    (boot_id, pid_start) that is_mine() and pid_alive() check; a hand-built lock would silently degrade
    both to the bare pid, which repeats across containers."""
    factory_dir = ctx.repo.factory_dir
    lock = RunLock.read(factory_dir, ctx.issue)
    if lock is None:
        lock = RunLock.create(ctx.issue, stage or "", ctx.worktree or "")
    if last_error is not None:
        lock.last_error = last_error
    if ctx.worktree is not None:
        lock.worktree = str(ctx.worktree)
    lock.write(factory_dir, stage=stage)
    ctx.holds_lock = True


# ---------------------------------------------------------------- shared plumbing


def prepare(
    ctx: Context,
    *,
    need_state: bool = True,
    fetch: bool = True,
    commit_operator_edits: bool = True,
    strict_clean: bool = True,
    check_protected: bool = True,
) -> Context:
    """Start-of-command validation (design §7). Returns ctx (prepared=True); returns immediately if already prepared.
    1. ctx.harness = get_harness(name); ctx.harness_env = build_env(name, auth, parent_env, passthrough=...) — this is
       where `api` with no key fails, before any GitHub or git write. (finalize needs harness_env too, for the checks.)
    2. if fetch: repo.fetch(). worktree = repo.ensure_worktree(issue) (recovery from origin/factory/<n>); when neither a
       local nor a remote branch exists: need_state=False -> return with worktree=None (spec creates it; run starts at
       spec), else FactoryError("no branch for issue N; run `factory spec N`").
    3. ctx.last_error = the exit-1 message left by the previous attempt (state.read_last_error), then the RunLock:
       a live lock this process does not own -> FactoryError("issue N: <stage> in progress (pid P)"); a dead writer ->
       repo.reset_hard(HEAD), its lock's last_error wins (more recent), log "interrupted <stage> discarded; transcript
       kept". Then take the lock for this command (ctx.holds_lock = True) and clear the last-error FILE — the lock
       now carries the message, so nothing is lost if this command is killed too. Step 3's reset runs BEFORE step 5's
       operator-edit commit, always.
    4. if fetch: repo.sync_with_remote(worktree, branch) (fast-forward if strictly ahead; diverged -> exit 1), then
       repo.push_if_ahead ONCE state.json exists (a local-only accept/dismiss commit must not die with the host;
       failure logged, not fatal). With no state there is nothing to publish and an empty factory/<n> branch would
       only have to be cleaned up later.
    5. Dirty paths: under work/<n>/ and commit_operator_edits -> commit "factory(N): operator edits"; is_transient()
       paths ignored; anything else dirty -> FactoryError naming the paths, unless strict_clean is False (abandon
       discards the worktree, so refusing to start would only strand it).
    6. Load State + Ledger; check every stages[*].start_commit and reviews[*].sha is an ancestor of HEAD
       (else FactoryError "state.json records commit X not reachable from HEAD"). Then ensure_pr(ctx) (best effort),
       and unless check_protected is False the whole-branch protected-path check (design §19, deviation 13): the
       commands that run no session — build's baseline checks, finalize's checks — would otherwise execute a
       Makefile that a pushed or hand-written commit rewrote.
    """
    if ctx.prepared:
        return ctx
    config = ctx.config
    ctx.harness = get_harness(config.harness)
    ctx.harness_env = build_env(
        config.harness, config.auth, ctx.parent_env, passthrough=config.env_passthrough
    )

    if fetch:
        ctx.repo.fetch()
    ctx.worktree = _resolve_worktree(ctx, need_state=need_state)
    ctx.last_error = state_module.read_last_error(ctx.repo.factory_dir, ctx.issue)
    _take_lock(ctx)
    state_module.clear_last_error(ctx.repo.factory_dir, ctx.issue)

    if ctx.worktree is not None:
        wt = ctx.worktree
        if fetch:
            ctx.repo.sync_with_remote(wt, ctx.branch)
            if State.path(wt, ctx.issue).is_file():
                _push_if_ahead(ctx)
        _commit_operator_edits(
            ctx, commit_operator_edits=commit_operator_edits, strict_clean=strict_clean
        )
        ctx.state = State.load(wt, ctx.issue) if need_state else State.load_or_none(wt, ctx.issue)
        ctx.ledger = Ledger.load(wt, ctx.issue)
        if ctx.state is not None:
            _check_recorded_commits(ctx)
            ensure_pr(ctx)
            if check_protected:
                _refuse_protected_paths(ctx)
    ctx.prepared = True
    return ctx


def _resolve_worktree(ctx: Context, *, need_state: bool) -> Path | None:
    """Step 2. The branch predicates are probed first: ensure_worktree raises for "no branch anywhere", which is a
    legitimate state for `spec` and for a `run` that starts at spec."""
    repo = ctx.repo
    known = repo.local_branch_exists(ctx.branch) or repo.remote_branch_exists(ctx.branch)
    if not known and not need_state:
        return None
    worktree = repo.ensure_worktree(ctx.issue)  # raises the §7 message when no branch exists
    repo.ensure_excludes(
        [*checks_module.TRANSIENT_PATHS, *ctx.config.transient_paths]
    )  # config.transient_paths never reaches ensure_worktree's own call
    return worktree


def _take_lock(ctx: Context) -> None:
    """Step 3. The lock spans the whole command (deviation 5), so a kill during the gate/commit window is still
    detected as an interrupted stage.

    Ownership is `is_mine()`, not `pid == getpid()`: on a disposable host the recorded pid can be ours without the
    process being us (a fresh container, the same pid namespace). A live lock somebody else holds raises with
    ctx.holds_lock still False, so this command's finally leaves that lock exactly where it is."""
    factory_dir = ctx.repo.factory_dir
    existing = RunLock.read(factory_dir, ctx.issue)
    if existing is not None and not existing.is_mine():
        if existing.pid_alive():
            raise FactoryError(
                f"issue {ctx.issue}: {existing.stage or 'a stage'} in progress (pid {existing.pid})",
                hint=f"wait for it, or remove {RunLock.path(factory_dir, ctx.issue)} if that process is gone",
            )
        if existing.last_error:  # the interrupted attempt is the more recent failure
            ctx.last_error = existing.last_error  # into this attempt's stage_note (prompts.py)
        if ctx.worktree is not None:
            ctx.repo.reset_hard(ctx.worktree)
        ctx.log(
            f"interrupted {existing.stage or 'stage'} discarded; transcript kept "
            f"(worktree reset to HEAD)"
        )
    RunLock.create(ctx.issue, "", ctx.worktree or "", ctx.last_error).write(factory_dir)
    ctx.holds_lock = True


def _push_if_ahead(ctx: Context) -> None:
    """Step 4's second half: keeping a local-only commit is best effort — an offline host must still work."""
    try:
        if ctx.repo.push_if_ahead(ctx.worktree, ctx.branch):
            ctx.log(f"pushed pending commits on {ctx.branch}")
    except FactoryError as exc:
        ctx.log(f"could not push {ctx.branch} yet: {exc.message}")


def _commit_operator_edits(
    ctx: Context, *, commit_operator_edits: bool, strict_clean: bool = True
) -> None:
    """Step 5. Everything dirty is either the operator's work under work/<n>/, a transient, or a reason to stop —
    unless strict_clean is False, when a dirty worktree is simply not this command's problem (abandon)."""
    wt = ctx.worktree
    dirty = [
        path
        for path in ctx.repo.changed_paths_in_worktree(wt)
        if not checks_module.is_transient(path, ctx.config)
    ]
    work = _work_rel(ctx.issue)
    outside = [path for path in dirty if not checks_module.is_under(path, [work])]
    if outside and strict_clean:
        raise FactoryError(
            f"uncommitted changes outside {work}/ in {wt}: {', '.join(sorted(outside))}",
            hint="commit them on the branch or discard them, then re-run the same command",
        )
    under_work = [path for path in dirty if checks_module.is_under(path, [work])]
    if not under_work or not commit_operator_edits:
        return
    ctx.repo.commit_all(wt, f"factory({ctx.issue}): operator edits", paths=[work])
    ctx.log(f"committed operator edits under {work}/: {', '.join(sorted(under_work))}")


def _check_recorded_commits(ctx: Context) -> None:
    """Step 6. Every commit state.json anchors on must still be reachable, or the branch was rewritten under us."""
    wt, state = ctx.worktree, ctx.state
    recorded = [
        (f"stages.{name}.start_commit", rec.start_commit) for name, rec in state.stages.items()
    ]
    recorded += [(f"reviews[{rec.round}].sha", rec.sha) for rec in state.reviews]
    for what, sha in recorded:
        if not ctx.repo.is_ancestor(sha, "HEAD", wt):
            raise FactoryError(
                f"work/{ctx.issue}/state.json records commit {sha[:12]} ({what}) "
                f"not reachable from HEAD in {wt}",
                hint=f"the branch was rewritten; `factory abandon {ctx.issue}` and start again, "
                "or restore the commit",
            )


def ensure_pr(ctx: Context, *, required: bool = False) -> PullRequest | None:
    """If state.pr is set, return it. Else if origin/<branch> exists: gh.find_pr_for_branch(branch) -> record in state
    (in memory; the next commit persists it); if none and stages['spec'] is recorded: create_draft_pr and record it.
    Idempotent. A gh failure is logged and returns None unless required=True (finalize) -> FactoryError."""
    state = ctx.state
    if state is None:
        if required:
            raise FactoryError(f"no state for issue {ctx.issue}; cannot find its pull request")
        return None
    if state.pr:
        return PullRequest(
            number=int(state.pr["number"]),
            url=str(state.pr.get("url") or ""),
            is_draft=True,
            state="OPEN",
            head=ctx.branch,
        )
    try:
        pr = (
            ctx.gh.find_pr_for_branch(ctx.branch)
            if ctx.repo.remote_branch_exists(ctx.branch)
            else None
        )
        if pr is None and state.stage_done("spec"):
            pr = ctx.gh.create_draft_pr(
                branch=ctx.branch,
                base=ctx.config.base_branch,
                title=_pr_title(ctx),
                body=_pr_body(ctx),
            )
    except FactoryError as exc:
        if required:
            raise
        ctx.log(f"could not reach the pull request for {ctx.branch}: {exc.message}")
        return None
    if pr is None:
        if required:
            raise FactoryError(
                f"no pull request for {ctx.branch}",
                hint=f"run `factory spec {ctx.issue}` to create the draft PR",
            )
        return None
    state.pr = {"number": pr.number, "url": pr.url}
    ctx.log(f"pull request #{pr.number} {pr.url}")
    return pr


def _pr_title(ctx: Context) -> str:
    """The issue title, read back from the committed intent.md so a PR can be created long after `spec` ran."""
    try:
        first = _read_work_file(ctx, "intent.md").splitlines()[0]
    except (FactoryError, IndexError):
        return f"factory: issue {ctx.issue}"
    title = (
        first[len(_INTENT_TITLE_PREFIX) :].strip() if first.startswith(_INTENT_TITLE_PREFIX) else ""
    )
    return title or f"factory: issue {ctx.issue}"


def _pr_body(ctx: Context) -> str:
    """ "Closes #N" plus the first lines of spec.md (design §9 spec row)."""
    try:
        spec_md = _read_work_file(ctx, "spec.md")
    except FactoryError:
        spec_md = ""
    head = "\n".join(spec_md.splitlines()[:_SPEC_BODY_LINES]).strip()
    lines = [
        f"Closes #{ctx.issue}",
        "",
        f"Draft opened by the factory from `work/{ctx.issue}/spec.md`.",
    ]
    if head:
        lines += ["", head, "", f"_(the full spec is `work/{ctx.issue}/spec.md` on this branch)_"]
    return "\n".join(lines) + "\n"


def branch_protected_path_violations(ctx: Context) -> list[str]:
    """checks.is_protected over repo.changed_paths_between(worktree, state.base['sha'], HEAD). Called at the top of
    run_harness_stage before EVERY launch (design §19: the check protects the session about to start, including from
    operator commits and fast-forwarded remote commits). Any hit -> FactoryError telling the operator to revert them."""
    state = _require_state(ctx, "the protected-path check")
    wt = _require_worktree(ctx, "the protected-path check")
    changed = ctx.repo.changed_paths_between(wt, state.base["sha"], "HEAD")
    return [path for path in changed if checks_module.is_protected(path, ctx.config)]


def _refuse_protected_paths(ctx: Context) -> None:
    """branch_protected_path_violations -> FactoryError. Called from prepare() (before any check command runs)
    and again from run_harness_stage (before every session), because both execute what those files configure."""
    protected = branch_protected_path_violations(ctx)
    if protected:
        raise FactoryError(
            f"protected paths changed on {ctx.branch}: {', '.join(protected)}",
            hint="revert them on the branch before another session or check runs (design §9); "
            "the factory would otherwise run whatever they configure",
        )


def run_harness_stage(
    ctx: Context, *, stage: str, round: int, mode: str, prompt_text: str, schema_name: str
) -> tuple[dict, HarnessResult, Path]:
    """1. branch_protected_path_violations -> FactoryError.
    2. Write the prompt to work/<n>/prompts/<stage>-<round>.md (prompt_path) and copy it next to the transcript.
    3. RunLock.write(stage=stage). Snapshot the worktree's changed paths (minus transients).
    4. harness.run(cwd=worktree, prompt_file, schema_file=schema.schema_path(schema_name), mode, model=ctx.model,
       auth, env=harness_env, timeout_s=config.stage_timeout_s, transcript_path=
       .factory/transcripts/<n>/<stage>-<round>-<UTC ts>.json, max_turns=<read|write from harness config>,
       max_budget_usd, agents_md=<worktree>/AGENTS.md if exists, writable_dirs=<codex config>).
    5. schema.validate_or_raise(output). Log permission_denials (never a gate) and "hit the turn bound".
    6. mode=read: the changed paths after (minus transients) must equal the snapshot (the prompt file is in both) else
       GateViolation("read stage wrote files", paths).
    On ANY exception in 4-6: repo.reset_hard(worktree) first, record str(exc) into the RunLock's last_error, re-raise.
    Returns (validated output, HarnessResult, prompt_path rel to worktree) — the caller records metadata and, for write
    stages, passes the prompt path as `ignore` to allowed_edit_violations."""
    wt = _require_worktree(ctx, f"the {stage} stage")
    _refuse_protected_paths(ctx)

    prompt_path = prompts.write_prompt(wt, ctx.issue, stage, round, prompt_text)
    prompt_rel = Path(os.path.relpath(prompt_path, wt))
    transcript_path = _transcript_path(ctx, stage, round)
    state_module.atomic_write_text(
        transcript_path.with_name(f"{transcript_path.stem}.prompt.md"), prompt_text
    )

    _run_lock(ctx, stage=stage)
    before = _worktree_fingerprint(ctx)
    harness_config = ctx.config.harness_config(ctx.harness_name)
    agents_md = wt / "AGENTS.md"
    try:
        result = ctx.harness.run(
            cwd=wt,
            prompt_file=prompt_path,
            schema_file=schema_module.schema_path(schema_name),
            mode=mode,
            model=ctx.model,
            auth=ctx.auth,
            env=ctx.harness_env,
            timeout_s=ctx.config.stage_timeout_s,
            transcript_path=transcript_path,
            max_turns=(
                harness_config.max_turns_read if mode == "read" else harness_config.max_turns_write
            ),
            max_budget_usd=harness_config.max_budget_usd,
            agents_md=agents_md if agents_md.exists() else None,
            writable_dirs=list(harness_config.writable_dirs),
        )
        schema_module.validate_or_raise(
            result.output, schema_module.load_schema(schema_name), f"{stage} round {round}"
        )
        _log_harness_result(ctx, stage, result, harness_config, mode)
        if mode == "read":
            wrote = sorted(_worktree_fingerprint(ctx) - before)
            if wrote:
                raise GateViolation(
                    f"{stage} is a read-only stage but the harness changed files",
                    wrote,
                    hint="read stages produce their artifact through the output schema only (design §8)",
                )
    except BaseException as exc:  # any post-launch failure: reset first, re-raise unchanged
        ctx.repo.reset_hard(wt)
        _remember_failure(ctx, _error_text(exc))
        raise
    return result.output, result, prompt_rel


def _remember_failure(ctx: Context, message: str) -> str:
    """Record why this attempt was discarded in BOTH channels (see the module docstring): the RunLock, which
    survives a kill, and `.factory/run/<issue>.last-error`, which survives the clean exit 1 that clears the lock.
    prepare() reads them back into ctx.last_error, and _stage_note puts it in the next attempt's prompt."""
    _run_lock(ctx, last_error=message)
    state_module.write_last_error(ctx.repo.factory_dir, ctx.issue, message)
    return message


def _transcript_path(ctx: Context, stage: str, round: int) -> Path:
    """`.factory/transcripts/<issue>/<stage>-<round>-<UTC stamp>.json`. The name carries exactly one dot before the
    extension: Codex derives its `-o` file with Path.with_suffix('.last.json')."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = ctx.repo.factory_dir / "transcripts" / str(ctx.issue) / f"{stage}-{round}-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _worktree_fingerprint(ctx: Context) -> set[str]:
    return {
        path
        for path in ctx.repo.changed_paths_in_worktree(ctx.worktree)
        if not checks_module.is_transient(path, ctx.config)
    }


def _log_harness_result(
    ctx: Context, stage: str, result: HarnessResult, harness_config: HarnessConfig, mode: str
) -> None:
    # Codex reports no turn count, so "0 turns" would be a fact the transcript contradicts: say nothing.
    turns = f"{result.num_turns} turns, " if result.num_turns else ""
    ctx.log(
        f"{stage}: {ctx.harness_name} {result.cli_version} finished in {result.duration_s:.0f}s "
        f"({turns}transcript {result.transcript_path})"
    )
    if result.permission_denials:
        tools = ", ".join(
            sorted(
                {
                    str(denial.get("tool_name") or denial.get("tool") or "?")
                    for denial in result.permission_denials
                }
            )
        )
        ctx.log(
            f"{stage}: {len(result.permission_denials)} permission denial(s) [{tools}] — not a gate (deviation 16)"
        )
    bound = harness_config.max_turns_read if mode == "read" else harness_config.max_turns_write
    if result.num_turns and bound and result.num_turns >= bound:
        ctx.log(
            f"{stage}: hit the turn bound ({bound}); raise [harness.{ctx.harness_name}] max_turns_* if this recurs"
        )


def _error_text(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    text = message if isinstance(message, str) and message else str(exc)
    return text or exc.__class__.__name__


def record_stage(ctx: Context, stage: str, result: HarnessResult, start_commit: str) -> None:
    """state.stages[stage] = StageRecord(start_commit, at=now, harness=ctx.harness_name, model=result.model or ctx.model
    or "(cli default)", cli_version=result.cli_version, auth=ctx.auth, permission_denials=len(result.permission_denials))."""
    state = _require_state(ctx, f"recording the {stage} stage")
    state.stages[stage] = StageRecord(
        start_commit=start_commit,
        at=state_module.now_iso(),
        harness=ctx.harness_name,
        model=result.model or ctx.model or "(cli default)",
        cli_version=result.cli_version,
        auth=ctx.auth,
        permission_denials=len(result.permission_denials),
    )


def commit_and_push(
    ctx: Context,
    message: str,
    *,
    paths: list[str] | None = None,
    force_with_lease: bool | None = None,
) -> str:
    """state.save + ledger.save; if repo.has_changes(paths) commit_all(paths) else skip the commit; then
    repo.push(force_with_lease = ctx.force_push when None). Returns HEAD. A push failure after the commit leaves the commit
    local (design §15) and raises FactoryError saying re-running pushes it.

    A successful push also retires the previous attempt's failure (state.clear_last_error and ctx.last_error): work
    that reached the branch is not something the next prompt should apologise for."""
    wt = _require_worktree(ctx, "committing")
    state = _require_state(ctx, "committing")
    state.save(wt)
    if ctx.ledger is not None:
        ctx.ledger.save(wt, ctx.issue)
    if ctx.repo.has_changes(wt, paths):
        head = ctx.repo.commit_all(wt, message, paths=paths)
        ctx.log(f"{message} [{head[:12]}]")
    else:
        head = ctx.repo.head(wt)
        ctx.log(f'nothing to commit for "{message}"')
    force = ctx.force_push if force_with_lease is None else force_with_lease
    try:
        ctx.repo.push(wt, ctx.branch, force_with_lease=force)
    except FactoryError as exc:
        raise FactoryError(
            f"pushed nothing: {exc.message}",
            hint=f"the commit is local; re-running the same command pushes {ctx.branch}",
        ) from exc
    ctx.force_push = False  # the rewind's single force-with-lease push is spent
    state_module.clear_last_error(ctx.repo.factory_dir, ctx.issue)
    ctx.last_error = None
    return head


def park(
    ctx: Context, gate: str, what_clears_it: str, *, evidence_paths: list[str] | None = None
) -> None:
    """Design §9/§11 gate exit. No-op re-park: if state.outcome == needs_human:<gate> and is_parked(ctx) -> reset the
    worktree and raise NeedsHuman directly (the recorded gate already describes this HEAD; the reset keeps whatever the
    stage wrote before the gate — a fresh baseline log, say — from becoming an operator edit that un-parks the issue on
    the next command). Otherwise:
    1. commit evidence_paths first in their own commit (e.g. checks/build-1-baseline.log: "factory(N): build baseline (red)")
    2. outcome_sha = HEAD (after 1); state.set_outcome(f"needs_human:{gate}", outcome_sha); for a REVIEW_LOOP_GATES
       gate also state.mark_last_review_gated(), so the state-only commit carries the flag `run` reads back: the
       operator's answer to those gates (a dismiss, a hand fix) has to be REVIEWED, not fixed
    3. commit ONLY work/<n>/state.json ("factory(N): park <gate>"); push
    4. ensure_pr; gh.pr_comment(render_gate_comment(...), marker=GATE_MARKER(gate, outcome_sha)) when a PR exists
    5. raise NeedsHuman(gate, what_clears_it)."""
    state = _require_state(ctx, "parking")
    wt = _require_worktree(ctx, "parking")
    if state.outcome == f"needs_human:{gate}" and is_parked(ctx):
        ctx.log(f"still parked on {gate}")
        ctx.repo.reset_hard(wt)
        raise NeedsHuman(gate, what_clears_it)

    work = _work_rel(ctx.issue)
    # 1. every pending work/<n>/ artifact, not just `evidence_paths`: the state-only commit below is what keeps the
    #    issue parked, and it is state-only only if nothing else under work/ is still uncommitted.
    state.save(wt)
    if ctx.ledger is not None:
        ctx.ledger.save(wt, ctx.issue)
    if ctx.repo.has_changes(wt, [work]):
        evidence = ", ".join(evidence_paths) if evidence_paths else work
        ctx.repo.commit_all(wt, f"factory({ctx.issue}): {gate} evidence ({evidence})", paths=[work])

    # 2. outcome_sha is HEAD *before* the state-only commit (state.is_parked's second clause). The gated flag is
    #    set here, after the evidence commit, so it travels in that same state-only commit.
    outcome_sha = ctx.repo.head(wt)
    state.set_outcome(f"needs_human:{gate}", outcome_sha)
    if gate in REVIEW_LOOP_GATES:
        state.mark_last_review_gated()
    state.save(wt)
    _assert_only_state_dirty(ctx)
    commit_and_push(
        ctx, f"factory({ctx.issue}): park {gate}", paths=state_module.state_only_paths(ctx.issue)
    )

    pr = ensure_pr(ctx)
    if pr is not None:
        _post_comment(
            ctx,
            pr,
            gh_module.render_gate_comment(gate, what_clears_it, outcome_sha, ctx.issue),
            marker=gh_module.GATE_MARKER.format(gate=gate, sha=outcome_sha),
            what=f"gate comment for {gate}",
        )
    raise NeedsHuman(gate, what_clears_it)


def _assert_only_state_dirty(ctx: Context) -> None:
    """park()'s commit must change exactly work/<n>/state.json, or the issue would not read as parked."""
    work = _work_rel(ctx.issue)
    leftovers = sorted(
        path
        for path in ctx.repo.changed_paths_in_worktree(ctx.worktree)
        if checks_module.is_under(path, [work])
        and path not in state_module.state_only_paths(ctx.issue)
        and not checks_module.is_transient(path, ctx.config)
    )
    if leftovers:
        raise FactoryError(
            f"cannot park issue {ctx.issue}: {', '.join(leftovers)} is still uncommitted under {work}/",
            hint="park commits only state.json; commit or discard these first",
        )


def _post_comment(ctx: Context, pr: PullRequest, body: str, *, marker: str, what: str) -> None:
    """PR comments are idempotent by marker and never fail a stage that has already committed its work."""
    try:
        if ctx.gh.pr_comment(pr.number, body, marker=marker):
            ctx.log(f"posted the {what} on PR #{pr.number}")
    except FactoryError as exc:
        ctx.log(f"could not post the {what} on PR #{pr.number}: {exc.message}")


def is_parked(ctx: Context) -> bool:
    """state.is_parked(state, head, repo.parent(head), repo.changed_paths_of_commit(head))."""
    if ctx.state is None or ctx.worktree is None:
        return False
    head = ctx.repo.head(ctx.worktree)
    return state_module.is_parked(
        ctx.state,
        head,
        ctx.repo.parent(head, ctx.worktree),
        ctx.repo.changed_paths_of_commit(ctx.worktree, head),
    )


def check_parked(ctx: Context) -> None:
    """Parked stays parked (design §11): if is_parked(ctx) raise NeedsHuman(state.gate(), <what clears it for that gate>)
    with no harness call and no new commit or comment."""
    if not is_parked(ctx):
        return
    gate = ctx.state.gate() or "needs_human"
    ctx.log(f"issue {ctx.issue} is parked on {gate} and HEAD has not moved")
    raise NeedsHuman(gate, what_clears(gate, ctx))


def what_clears(gate: str, ctx: Context) -> str:
    """Operator-facing sentence per gate (used by park and check_parked so both say the same thing)."""
    issue = ctx.issue
    if gate == "open_questions":
        questions = ctx.state.spec_open_questions if ctx.state else []
        listed = "\n".join(f"  - {q}" for q in questions) or "  (none recorded)"
        return (
            f"The spec asked questions that block implementation:\n{listed}\n"
            f"Answer them in work/{issue}/spec.md on branch {ctx.branch} (or fix the issue and re-run "
            f"`factory spec {issue} --force`), then `factory accept {issue}` and `factory run {issue}`."
        )
    if gate == "baseline_failing":
        return (
            f"The configured checks were already red before the build started, so nothing a builder does could be "
            f"trusted. The output is committed as work/{issue}/checks/build-1-baseline.log. Fix the checks on "
            f"{ctx.branch} and commit, then `factory run {issue}`."
        )
    if gate == "no_progress":
        return (
            f"The last review after a fix resolved no Important finding, so the loop stopped instead of spending "
            f"another round on a fixer that is not converging.\n{_open_important_list(ctx)}\n"
            f'Fix them by hand and commit, `factory dismiss {issue} <id> "reason"` the ones you disagree with, or '
            f"edit work/{issue}/plan.md and `factory build {issue} --force`. Then `factory run {issue}`."
        )
    if gate == "rounds_exhausted":
        rounds = ctx.config.max_fix_rounds
        return (
            f"All {rounds} fix round(s) are spent and Important findings are still open.\n"
            f"{_open_important_list(ctx)}\n"
            f'Fix them by hand and commit, `factory dismiss {issue} <id> "reason"` the ones you disagree with, or '
            f"edit work/{issue}/plan.md and `factory build {issue} --force`. Then `factory run {issue}`."
        )
    return (
        f"Act on branch {ctx.branch} — a commit of your own, `factory accept {issue}`, "
        f'`factory dismiss {issue} <id> "reason"`, or a `--force` re-run — then `factory run {issue}`.'
    )


def _open_important_list(ctx: Context) -> str:
    findings = ctx.ledger.open_important() if ctx.ledger else []
    if not findings:
        return "  (no Important findings are open)"
    return "\n".join(
        f"  - {f.id} {f.file}:{f.line} {f.title}"
        if f.line is not None
        else f"  - {f.id} {f.file} {f.title}"
        for f in findings
    )


def rewind_for_force(ctx: Context, stage: str) -> None:
    """--force on spec|plan|build (design §6), called by the stage itself when ctx.force and stages[stage] is recorded
    (--force with the stage unrecorded is a no-op and the stage simply runs):
    1. old_head = HEAD; target = state.stages[stage].start_commit (spec: state.base['sha']); repo.reset_hard(wt, target)
    2. re-apply the operator's inputs from old_head with repo.checkout_paths: plan keeps work/<n>/spec.md and intent.md;
       build keeps spec.md, intent.md and plan.md; spec keeps nothing
    3. rebuild state.json IN MEMORY (never inherit the one the reset restored): carry issue, base, branch, pr,
       spec_open_questions, spec_accepted (spec --force drops these two) and stages BEFORE `stage`; drop stages[stage] and
       later, reviews, fix_rounds, outcome, outcome_sha; ledger = empty (findings.json removed)
    4. state.save; commit "factory(N): operator edits before <stage> --force" if anything changed
    5. push the rewound branch immediately with --force-with-lease, and leave ctx.force_push = True for the stage's
       own final push. The rewind is what the operator asked for; publishing it at once means the draft PR shows the
       branch that is actually being rebuilt, and a stage that then fails a gate leaves origin == the local tip
       instead of a remote that is ahead of us and would need a second --force to reconcile."""
    if stage not in STAGE_ORDER:
        raise FactoryError(
            f"--force is not supported for {stage}; only {', '.join(STAGE_ORDER)} rewind"
        )
    wt = _require_worktree(ctx, f"{stage} --force")
    old = _require_state(ctx, f"{stage} --force")
    old_head = ctx.repo.head(wt)
    target = old.base["sha"] if stage == "spec" else old.stages[stage].start_commit

    ctx.repo.reset_hard(wt, target)
    work = _work_rel(ctx.issue)
    keep = {
        "spec": [],
        "plan": [f"{work}/intent.md", f"{work}/spec.md"],
        "build": [f"{work}/intent.md", f"{work}/spec.md", f"{work}/plan.md"],
    }[stage]
    ctx.repo.checkout_paths(wt, old_head, keep)

    rebuilt = State(
        issue=dict(old.issue),
        base=dict(old.base),
        branch=old.branch,
        pr=dict(old.pr) if old.pr else None,
    )
    for earlier in STAGE_ORDER[: STAGE_ORDER.index(stage)]:
        if earlier in old.stages:
            rebuilt.stages[earlier] = old.stages[earlier]
    if stage != "spec":
        rebuilt.spec_open_questions = list(old.spec_open_questions)
        rebuilt.spec_accepted = dict(old.spec_accepted) if old.spec_accepted else None
    ctx.state = rebuilt
    ctx.ledger = Ledger()
    Ledger.path(wt, ctx.issue).unlink(missing_ok=True)

    rebuilt.save(wt)
    if ctx.repo.has_changes(wt):
        ctx.repo.commit_all(wt, f"factory({ctx.issue}): operator edits before {stage} --force")
    ctx.force_push = True
    ctx.log(f"{stage} --force: rewound {ctx.branch} to {target[:12]} (was {old_head[:12]})")
    if ctx.repo.remote_branch_exists(ctx.branch):
        ctx.repo.push(wt, ctx.branch, force_with_lease=True)
        ctx.log(f"{stage} --force: pushed the rewound {ctx.branch} (force-with-lease)")


def _gate_failure(
    ctx: Context, message: str, *, paths: list[str] | None = None, hint: str | None = None
) -> None:
    """A deterministic gate rejected the stage: reset the worktree (design §15), remember why, exit 1."""
    ctx.repo.reset_hard(_require_worktree(ctx, "the gate"))
    _remember_failure(ctx, message)
    raise GateViolation(message, paths, hint)


@contextlib.contextmanager
def _after_the_session(ctx: Context) -> Iterator[None]:
    """Everything a write or review stage does AFTER run_harness_stage returned, so that no failure can leave the
    session's edits in the worktree (design §15 / deviation 6: "re-run the same command" must never meet "worktree
    dirty"). The gates call _gate_failure themselves; this catches the OTHER FactoryErrors of that window — a
    ledger merge that rejects the reviewer's output, an unwritable artifact, a check command factory.toml spells
    wrong, a push that fails — and routes them through the same reset-and-remember path.

    GateViolation passes through untouched: _gate_failure and run_harness_stage have already reset for it, and
    resetting twice would only widen the window in which the worktree is being rewritten."""
    try:
        yield
    except GateViolation:
        raise
    except FactoryError as exc:
        _gate_failure(ctx, exc.message, paths=getattr(exc, "paths", None) or None, hint=exc.hint)


# ---------------------------------------------------------------- stages


def spec(ctx: Context) -> None:
    """Design §9 row `spec` (mode read):
    - if stages.spec recorded: not force -> log "spec already done", return; force -> rewind_for_force(ctx, "spec")
    - base_sha = _spec_base_sha(ctx, rewound); snapshot issue via gh.issue (the stage's one GitHub read);
      intent_md, sha = state.render_intent(...)
    - worktree = repo.ensure_worktree(issue, start_point=base_sha) if ctx.worktree is None; ctx.worktree = it
    - initial State(issue={number, snapshot_sha256, snapshot_at}, base={branch, sha}, branch); write intent.md
    - prompt = render_role("spec", {issue, intent, stage_note}); run_harness_stage(mode read, schema "spec")
    - gate: markdown.strip() non-empty (Python check) else reset + GateViolation
    - spec.md = render_spec_md(markdown, open_questions); state.spec_open_questions; record_stage(start_commit=base_sha)
    - commit_and_push("factory(N): spec"); ensure_pr (creates the draft PR: title = issue title, body = "Closes #N" +
      first 40 lines of spec.md); if pr newly recorded: commit_and_push("factory(N): record PR")
    - if state.spec_needs_acceptance(): park("open_questions", what_clears("open_questions")).
    """
    rewound = False
    if ctx.state is not None and ctx.state.stage_done("spec"):
        if not ctx.force:
            ctx.log("spec already done")
            return
        rewind_for_force(ctx, "spec")
        rewound = True

    base_sha = _spec_base_sha(ctx, rewound=rewound)
    snapshot_at = state_module.now_iso()
    issue = ctx.gh.issue(ctx.issue)
    intent_md, digest = state_module.render_intent(issue.to_dict(), snapshot_at)

    if ctx.worktree is None:
        ctx.worktree = ctx.repo.ensure_worktree(ctx.issue, start_point=base_sha)
        ctx.repo.ensure_excludes([*checks_module.TRANSIENT_PATHS, *ctx.config.transient_paths])
    ctx.state = State(
        issue={"number": ctx.issue, "snapshot_sha256": digest, "snapshot_at": snapshot_at},
        base={"branch": ctx.config.base_branch, "sha": base_sha},
        branch=ctx.branch,
        pr=dict(ctx.state.pr) if ctx.state is not None and ctx.state.pr else None,
    )
    ctx.ledger = Ledger()
    _write_work_file(ctx, "intent.md", intent_md)

    prompt = prompts.render_role(
        "spec",
        {
            "issue": str(ctx.issue),
            "intent": intent_md,
            "stage_note": _stage_note(ctx, notes=[f"Issue snapshot taken at {snapshot_at}."]),
        },
    )
    out, result, _ = run_harness_stage(
        ctx, stage="spec", round=1, mode="read", prompt_text=prompt, schema_name="spec"
    )
    markdown = str(out.get("markdown") or "")
    if not markdown.strip():
        _gate_failure(
            ctx,
            "spec produced no markdown",
            hint="the spec role must return a non-empty `markdown`",
        )
    questions = [str(q).strip() for q in (out.get("open_questions") or []) if str(q).strip()]

    _write_work_file(ctx, "spec.md", state_module.render_spec_md(markdown, questions))
    ctx.state.spec_open_questions = questions
    record_stage(ctx, "spec", result, base_sha)
    commit_and_push(ctx, f"factory({ctx.issue}): spec")

    had_pr = bool(ctx.state.pr)
    ensure_pr(ctx)
    if ctx.state.pr and not had_pr:
        commit_and_push(ctx, f"factory({ctx.issue}): record PR")

    if ctx.state.spec_needs_acceptance():
        park(ctx, "open_questions", what_clears("open_questions", ctx))


def _spec_base_sha(ctx: Context, *, rewound: bool) -> str:
    """`state.base['sha']` — the commit the whole run diffs against, and the only thing that keeps the
    protected-path check and the review diff talking about this branch.

    Only the path that CREATES the branch may read `origin/<base>`: if the branch already exists (a first `spec`
    that was interrupted after `git worktree add` and before its commit, then re-run) origin may have moved on,
    and a base that is not an ancestor of the branch would make `git diff base HEAD` report every commit that
    landed on the base branch meanwhile as a change of this run's. So an existing branch reports what it was cut
    from: `origin/<base>` when it is still an ancestor of the tip, else the tip itself."""
    if rewound:
        return str(_require_state(ctx, "spec --force").base["sha"])
    if ctx.worktree is None:
        return ctx.repo.base_sha(ctx.config.base_branch)  # spec is about to create the branch
    if ctx.state is not None and ctx.state.base.get("sha"):
        return str(ctx.state.base["sha"])
    tip = ctx.repo.head(ctx.worktree)
    base_branch = ctx.config.base_branch
    if ctx.repo.remote_branch_exists(base_branch) and ctx.repo.is_ancestor(
        f"origin/{base_branch}", tip, ctx.worktree
    ):
        return ctx.repo.rev_parse(f"origin/{base_branch}", ctx.worktree)
    ctx.log(
        f"{ctx.branch} already exists with no state.json; using its tip {tip[:12]} as the base "
        f"(origin/{base_branch} is not an ancestor of it)"
    )
    return tip


def accept(ctx: Context) -> None:
    """Requires stages.spec. spec_accepted={"by":"operator","at":now}; re-read open questions from spec.md
    (parse_open_questions — the operator may have edited them); state.set_outcome(None, None);
    commit_and_push("factory(N): accept spec"). Idempotent (no changes -> log and return)."""
    _require_stage(ctx, "spec", "accept")
    state = ctx.state
    questions = state_module.parse_open_questions(_read_work_file(ctx, "spec.md"))
    unchanged = (
        state.spec_accepted is not None
        and questions == state.spec_open_questions
        and state.outcome is None
    )
    if unchanged:
        ctx.log("spec is already accepted")
        return
    state.spec_open_questions = questions
    state.spec_accepted = {"by": "operator", "at": state_module.now_iso()}
    state.set_outcome(None, None)
    commit_and_push(ctx, f"factory({ctx.issue}): accept spec")
    ctx.log(
        f"spec accepted with {len(questions)} open question(s) recorded; run `factory run {ctx.issue}`"
    )


def plan(ctx: Context) -> None:
    """Requires stages.spec else FactoryError. spec_needs_acceptance() -> check_parked() then park(open_questions).
    stages.plan recorded: not force -> "plan already done"; force -> rewind_for_force(ctx, "plan").
    start_commit = HEAD; prompt = render_role("plan", {issue, spec, stage_note}); mode read; schema "plan";
    gate: plan_has_required_sections(markdown) empty else reset + GateViolation(hint naming the missing headings);
    write plan.md; record_stage; commit_and_push("factory(N): plan")."""
    _require_stage(ctx, "spec", "plan")
    state = ctx.state
    if state.spec_needs_acceptance():
        check_parked(ctx)
        park(ctx, "open_questions", what_clears("open_questions", ctx))
    if state.stage_done("plan"):
        if not ctx.force:
            ctx.log("plan already done")
            return
        rewind_for_force(ctx, "plan")

    start_commit = ctx.repo.head(ctx.worktree)
    prompt = prompts.render_role(
        "plan",
        {
            "issue": str(ctx.issue),
            "spec": _read_work_file(ctx, "spec.md"),
            "stage_note": _stage_note(ctx, notes=[]),
        },
    )
    out, result, _ = run_harness_stage(
        ctx, stage="plan", round=1, mode="read", prompt_text=prompt, schema_name="plan"
    )
    markdown = str(out.get("markdown") or "")
    missing = checks_module.plan_has_required_sections(markdown)
    if missing:
        _gate_failure(
            ctx,
            f"plan is missing required heading(s): {', '.join(missing)}",
            hint="design §9: the plan gate requires '## Files that change' and '## Proof'",
        )
    _write_work_file(ctx, "plan.md", markdown if markdown.endswith("\n") else markdown + "\n")
    record_stage(ctx, "plan", result, start_commit)
    commit_and_push(ctx, f"factory({ctx.issue}): plan")


def build(ctx: Context) -> None:
    """Requires stages.plan. Parked and not forced -> re-raise the recorded gate (§11) before the baseline runs
    (--force is an operator action that re-opens the loop, so it runs the stage).
    stages.build recorded: not force -> "build already done"; force -> rewind_for_force.
    start_commit = HEAD. Baseline: run_checks(worktree, config, checks_env(harness_env), stage_timeout_s) BEFORE the
    session; red -> write_check_log(stage="build", round=1, suffix="-baseline") and park("baseline_failing", ...,
    evidence_paths=[that log]) (§11). Green: prompt = render_role("build", {issue, plan, spec, checks, stage_note});
    run_harness_stage(mode write, schema "build") -> (out, result, prompt_rel).
    Gates, each: reset_hard + GateViolation (message recorded in RunLock.last_error for the next attempt's stage_note):
      allowed_edit_violations(changed_paths_in_worktree, stage="build", ignore=[prompt_rel]);
      paths_missing_from_plan(changed non-work paths, plan.md as now in the worktree) (hint: list them in plan.md);
      run_checks red (log kept next to the transcript).
    Green: write_check_log("build", 1, log); work/<n>/build-1.json = out; record_stage; commit_and_push("factory(N): build")."""
    _require_stage(ctx, "plan", "build")
    if not ctx.force:  # §11 lists --force among the operator actions that re-open the loop
        check_parked(ctx)
    state = ctx.state
    if state.stage_done("build"):
        if not ctx.force:
            ctx.log("build already done")
            return
        rewind_for_force(ctx, "build")

    wt = ctx.worktree
    start_commit = ctx.repo.head(wt)
    ctx.log("running the baseline checks before the build session")
    baseline = _run_checks(ctx)
    if not baseline.ok:
        log_path = checks_module.write_check_log(
            wt, ctx.issue, "build", 1, baseline.log, suffix="-baseline"
        )
        park(
            ctx,
            "baseline_failing",
            what_clears("baseline_failing", ctx),
            evidence_paths=[os.path.relpath(log_path, wt)],
        )

    prompt = prompts.render_role(
        "build",
        {
            "issue": str(ctx.issue),
            "plan": _read_work_file(ctx, "plan.md"),
            "spec": _read_work_file(ctx, "spec.md"),
            "checks": checks_module.tail(baseline.log, _CHECK_TAIL_LINES),
            "stage_note": _stage_note(
                ctx, notes=["The baseline checks were green before this session started."]
            ),
        },
    )
    out, result, prompt_rel = run_harness_stage(
        ctx, stage="build", round=1, mode="write", prompt_text=prompt, schema_name="build"
    )

    with _after_the_session(ctx):
        changed = ctx.repo.changed_paths_in_worktree(wt)
        violations = checks_module.allowed_edit_violations(
            changed,
            stage="build",
            issue=ctx.issue,
            config=ctx.config,
            ignore=[prompt_rel.as_posix()],
        )
        if violations:
            _gate_failure(
                ctx,
                f"build changed paths it may not touch: {', '.join(violations)}",
                paths=violations,
                hint="protected paths and everything under work/ except plan.md are off limits (design §9); "
                "make those edits by hand, commit, and re-run",
            )
        missing = checks_module.paths_missing_from_plan(
            changed, _read_work_file(ctx, "plan.md"), issue=ctx.issue
        )
        if missing:
            _gate_failure(
                ctx,
                f"build changed paths that work/{ctx.issue}/plan.md does not list: {', '.join(missing)}",
                paths=missing,
                hint="list every changed path verbatim under '## Files that change' in plan.md "
                "(the build role is told to update the plan in the same pass)",
            )
        verdict = _run_checks(ctx)
        if not verdict.ok:
            _keep_check_log(ctx, "build", 1, verdict.log, near=result.transcript_path)
            _gate_failure(
                ctx,
                f"checks failed after build: {', '.join(verdict.failed)}",
                hint=f"the output is next to the transcript {result.transcript_path}",
            )

        checks_module.write_check_log(wt, ctx.issue, "build", 1, verdict.log)
        _write_work_file(ctx, "build-1.json", json.dumps(out, indent=2) + "\n")
        if out.get("deviations"):
            ctx.log(f"build reported {len(out['deviations'])} deviation(s) from the plan")
        record_stage(ctx, "build", result, start_commit)
        commit_and_push(ctx, f"factory({ctx.issue}): build")


def _run_checks(ctx: Context) -> checks_module.CheckRun:
    """The configured checks over a tree the last session cannot have poisoned.

    `git add -A` first, so the index describes exactly what the stage produced, and then remove every IGNORED
    untracked file except `.factory/` (repo.clean_ignored): `.venv/`, `node_modules/`, `__pycache__` — the
    droppings a write stage's `Bash(uv *)`/`Bash(make *)` leaves behind, none of which `git status` would ever
    show. A builder that edited `.venv/bin/pytest` (or left a stale `.pyc` shadowing a module it deleted) would
    otherwise be running the check on its own tooling; the toolchain rebuilds what it needs. `.factory/` stays
    because the review diff and the transcripts live there (deviation 11). Used for the baseline, build, fix and
    finalize check runs alike."""
    wt = _require_worktree(ctx, "the checks")
    ctx.repo.stage_all(wt)
    ctx.repo.clean_ignored(wt)
    return checks_module.run_checks(
        wt, ctx.config, checks_env(ctx.harness_env), ctx.config.stage_timeout_s
    )


def _keep_check_log(
    ctx: Context, stage: str, round: int, log: str, *, near: Path | None = None
) -> Path:
    """A failing stage's check output belongs outside the worktree, which is about to be reset: next to the
    transcript when the stage had one, else beside the issue's other host-local files."""
    if near is not None:
        path = near.with_name(f"{near.stem}.{stage}-{round}.checks.log")
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = (
            ctx.repo.factory_dir
            / "transcripts"
            / str(ctx.issue)
            / f"{stage}-{round}-{stamp}.checks.log"
        )
    state_module.atomic_write_text(path, log if log.endswith("\n") else log + "\n")
    ctx.log(f"check output kept at {path}")
    return path


def write_review_diff(ctx: Context, round: int) -> tuple[str, int, int, bool, list[str]]:
    """Write `git diff <base sha>...HEAD -- . ':!work'` to <worktree>/.factory/tmp/review-<round>.diff (transient,
    excluded, readable by path). If len > config.max_diff_bytes: write diff_stat + per-file diffs largest-first until the
    cap + a banner naming the omitted files. Returns (path rel to worktree, bytes, lines, truncated, omitted_paths)."""
    wt = _require_worktree(ctx, "the review diff")
    base = _require_state(ctx, "the review diff").base["sha"]
    rel = f".factory/tmp/review-{round}.diff"
    full = ctx.repo.diff(wt, base, "HEAD")
    cap = ctx.config.max_diff_bytes
    truncated = _nbytes(full) > cap
    text, omitted = (full, []) if not truncated else _truncated_diff(ctx, base, cap)
    state_module.atomic_write_text(wt / rel, text)
    return rel, _nbytes(text), len(text.splitlines()), truncated, omitted


def _truncated_diff(ctx: Context, base: str, cap: int) -> tuple[str, list[str]]:
    """Diffstat + whole-file diffs, largest first, until the cap; the rest are named in a banner at the top."""
    wt = ctx.worktree
    rows = sorted(ctx.repo.diff_numstat(wt, base, "HEAD"), key=lambda row: -(row[0] + row[1]))
    stat = ctx.repo.diff_stat(wt, base, "HEAD")
    budget = max(cap - _TRUNCATION_RESERVE - _nbytes(stat), 0)
    kept: list[str] = []
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for _added, _deleted, path in rows:
        chunk = ctx.repo.diff_paths(wt, base, "HEAD", [path])
        size = _nbytes(chunk)
        if omitted or used + size > budget:
            omitted.append(path)
            continue
        kept.append(chunk)
        included.append(path)
        used += size
    banner = [
        f"# TRUNCATED by the factory: this diff exceeded max_diff_bytes ({cap}).",
        f"# {len(included)} of {len(rows)} changed files are reproduced in full below, largest first.",
    ]
    if omitted:
        banner.append("# Omitted (not visible to you; do not raise findings about them):")
        banner += [f"#   {path}" for path in omitted]
    banner += ["", "# Diffstat of the whole change:", stat.rstrip("\n"), ""]
    return "\n".join(banner) + "\n" + "".join(kept), omitted


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def review(ctx: Context) -> None:
    """Always runs a new round (design §6). Requires stages.build else FactoryError. Parked -> re-raise the recorded
    gate (§11) before the diff or the session. round = len(reviews)+1;
    reviewed = HEAD. Inputs: spec, plan, diff (describe_diff over write_review_diff), checks (tail of latest_check_log,
    200 lines), review_policy (worktree REVIEW.md or load_template("REVIEW.md"), plus the enforced nit-cap line),
    ledger (format_ledger_for_review), stage_note (round). mode read; schema "review".
    Gates (reset + GateViolation, nothing saved): (copy, stats) = ledger.merged_copy(out, round);
      stats.missing_updates non-empty -> "reviewer did not update F3, F5"; stats.new_nits > config.max_nits -> "nit cap".
    Accept: ctx.ledger = copy; work/<n>/review-<round>.json = out; reviews.append(ReviewRecord(round, sha=reviewed,
    important_open=len(copy.open_important()), important_resolved=stats.resolved, nits=stats.new_nits,
    reraised_dropped=stats.reraised_dropped, fix_rounds_at=state.fix_rounds, diff_truncated));
    commit_and_push("factory(N): review <round>"); ensure_pr; gh.pr_comment(render_review_comment(...),
    marker=REVIEW_MARKER(round, reviewed))."""
    _require_stage(ctx, "build", "review")
    check_parked(ctx)
    state, wt = ctx.state, ctx.worktree
    round = len(state.reviews) + 1
    reviewed = ctx.repo.head(wt)
    diff_rel, nbytes, nlines, truncated, omitted = write_review_diff(ctx, round)
    ctx.log(
        f"review {round}: diff {diff_rel} ({nbytes} bytes, {nlines} lines{', truncated' if truncated else ''})"
    )

    prompt = prompts.render_role(
        "review",
        {
            "issue": str(ctx.issue),
            "spec": _read_work_file(ctx, "spec.md"),
            "plan": _read_work_file(ctx, "plan.md"),
            "diff": prompts.describe_diff(diff_rel, nbytes, nlines, truncated, omitted),
            "checks": _checks_text(ctx),
            "review_policy": _review_policy(ctx),
            "ledger": prompts.format_ledger_for_review([f.to_dict() for f in ctx.ledger.findings]),
            "stage_note": _stage_note(
                ctx,
                notes=[
                    f"This is review round {round} of issue {ctx.issue}, over the commit {reviewed[:12]} "
                    f"({state.fix_rounds} fix round(s) so far)."
                ],
            ),
        },
    )
    out, _result, _ = run_harness_stage(
        ctx, stage="review", round=round, mode="read", prompt_text=prompt, schema_name="review"
    )

    with _after_the_session(ctx):
        # merged_copy validates the reviewer's own entries: a finding with no title or an unknown severity is a
        # FactoryError, and it must reset the worktree like any other rejected round (the read stage wrote the
        # diff into .factory/tmp, which the reset keeps).
        candidate, stats = ctx.ledger.merged_copy(out, round)
        if stats.missing_updates:
            _gate_failure(
                ctx,
                f"review {round} gave no update for open finding(s): {', '.join(stats.missing_updates)}",
                paths=list(stats.missing_updates),
                hint="every finding the ledger marks NEEDS UPDATE needs exactly one entry in `updates` (design §9)",
            )
        if stats.new_nits > ctx.config.max_nits:
            _gate_failure(
                ctx,
                f"review {round} raised {stats.new_nits} new nits, above the cap of {ctx.config.max_nits}",
                hint="tighten REVIEW.md's skip list or raise [factory] max_nits",
            )

        ctx.ledger = candidate
        _write_work_file(ctx, f"review-{round}.json", json.dumps(out, indent=2) + "\n")
        open_important = len(candidate.open_important())
        state.reviews.append(
            ReviewRecord(
                round=round,
                sha=reviewed,
                important_open=open_important,
                important_resolved=stats.resolved,
                nits=stats.new_nits,
                reraised_dropped=stats.reraised_dropped,
                fix_rounds_at=state.fix_rounds,
                diff_truncated=truncated,
            )
        )
        commit_and_push(ctx, f"factory({ctx.issue}): review {round}")
    ctx.log(
        f"review {round}: {open_important} Important open, {stats.resolved} resolved, "
        f"{stats.new_nits} new nit(s), {stats.reraised_dropped} re-raised and dropped"
    )

    pr = ensure_pr(ctx)
    if pr is not None:
        stats_view = {
            "important_open": open_important,
            "important_resolved": stats.resolved,
            "new_important": stats.new_important,
            "new_nits": stats.new_nits,
            "unresolved": stats.unresolved,
            "merged_duplicates": stats.merged_duplicates,
            "reraised_dropped": stats.reraised_dropped,
            "reopened": stats.reopened,
            "diff_truncated": truncated,
        }
        _post_comment(
            ctx,
            pr,
            gh_module.render_review_comment(
                round,
                reviewed,
                str(out.get("summary") or ""),
                candidate.render_markdown(),
                stats_view,
            ),
            marker=gh_module.REVIEW_MARKER.format(round=round, sha=reviewed),
            what=f"review {round} comment",
        )


def _review_policy(ctx: Context) -> str:
    """The worktree's REVIEW.md (a protected path, so the branch cannot rewrite it under us) or the packaged
    default, plus the cap the factory actually enforces this round."""
    path = ctx.worktree / "REVIEW.md"
    policy = (
        path.read_text(encoding="utf-8") if path.is_file() else prompts.load_template("REVIEW.md")
    )
    return (
        f"{policy.rstrip()}\n\n"
        f"Nit cap enforced by the factory this round: {ctx.config.max_nits}. Exceeding it fails the round.\n"
    )


def fix(ctx: Context) -> None:
    """Requires at least one review. Parked -> re-raise the recorded gate (§11) before the session.
    open Important == 0 -> log "nothing to fix", return.
    fix_rounds >= config.max_fix_rounds -> park("rounds_exhausted", ...) (§11; the same gate run raises).
    repo.code_changed_between(reviews[-1].sha, HEAD) -> FactoryError("code changed since review <n>; run `factory review N`").
    round = fix_rounds + 1. prompt = render_role("fix", {issue, findings (open Important), checks, stage_note});
    mode write; schema "fix". Gates (reset + GateViolation): allowed_edit_violations(stage="fix", ignore=[prompt_rel])
    (tests and all of work/ are violations); run_checks red. Green: write_check_log("fix", round); work/<n>/fix-<round>.json
    = out; fix_rounds = round; commit_and_push("factory(N): fix <round>"). `how` is a claim; only the next review changes
    a status."""
    state, wt = _require_state(ctx, "fix"), _require_worktree(ctx, "fix")
    check_parked(ctx)
    last = state.last_review()
    if last is None:
        raise FactoryError(
            f"fix needs a review round for issue {ctx.issue}",
            hint=f"run `factory review {ctx.issue}` first",
        )
    open_important = ctx.ledger.open_important()
    if not open_important:
        ctx.log("nothing to fix: no Important findings are open")
        return
    if state.fix_rounds >= ctx.config.max_fix_rounds:
        park(ctx, "rounds_exhausted", what_clears("rounds_exhausted", ctx))
    if ctx.repo.code_changed_between(wt, last.sha, "HEAD"):
        raise FactoryError(
            f"code changed since review {last.round} ({last.sha[:12]}); the findings may be stale",
            hint=f"run `factory review {ctx.issue}` to review the current commit first",
        )

    round = state.fix_rounds + 1
    prompt = prompts.render_role(
        "fix",
        {
            "issue": str(ctx.issue),
            "findings": prompts.format_findings_for_fix([f.to_dict() for f in open_important]),
            "checks": _checks_text(ctx),
            "stage_note": _stage_note(
                ctx,
                notes=[
                    f"This is fix round {round} of at most {ctx.config.max_fix_rounds} for issue {ctx.issue}, "
                    f"over review round {last.round}.",
                    _previous_not_addressed(ctx, round),
                ],
            ),
        },
    )
    out, result, prompt_rel = run_harness_stage(
        ctx, stage="fix", round=round, mode="write", prompt_text=prompt, schema_name="fix"
    )

    with _after_the_session(ctx):
        violations = checks_module.allowed_edit_violations(
            ctx.repo.changed_paths_in_worktree(wt),
            stage="fix",
            issue=ctx.issue,
            config=ctx.config,
            ignore=[prompt_rel.as_posix()],
        )
        if violations:
            _gate_failure(
                ctx,
                f"fix {round} changed paths it may not touch: {', '.join(violations)}",
                paths=violations,
                hint="a fixer may not edit tests, protected paths, or anything under work/ (design §9)",
            )
        verdict = _run_checks(ctx)
        if not verdict.ok:
            _keep_check_log(ctx, "fix", round, verdict.log, near=result.transcript_path)
            _gate_failure(
                ctx,
                f"checks failed after fix {round}: {', '.join(verdict.failed)}",
                hint=f"the output is next to the transcript {result.transcript_path}",
            )

        checks_module.write_check_log(wt, ctx.issue, "fix", round, verdict.log)
        _write_work_file(ctx, f"fix-{round}.json", json.dumps(out, indent=2) + "\n")
        state.fix_rounds = round
        not_addressed = out.get("not_addressed") or []
        if not_addressed:
            ctx.log(
                f"fix {round} left {len(not_addressed)} finding(s) unaddressed; the next review adjudicates"
            )
        commit_and_push(ctx, f"factory({ctx.issue}): fix {round}")


def _previous_not_addressed(ctx: Context, round: int) -> str:
    """What the previous fix round said it could not do — the fixer should not silently repeat it."""
    if round < 2:
        return ""
    path = state_module.work_dir(ctx.worktree, ctx.issue) / f"fix-{round - 1}.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entries = previous.get("not_addressed") if isinstance(previous, dict) else None
    if not isinstance(entries, list) or not entries:
        return ""
    lines = [
        f"  - {entry.get('id', '?')}: {entry.get('why', '')}"
        for entry in entries
        if isinstance(entry, dict)
    ]
    return "The previous fix round did not address:\n" + "\n".join(lines)


def finalize(ctx: Context) -> None:
    """Parked -> re-raise the recorded gate (§11) before the checks run.
    Requires: at least one review; open Important == 0 (else FactoryError naming them: "dismiss or fix them");
    no code change since reviews[-1].sha (else FactoryError "run `factory review N`"). outcome == "done" and no code
    change since outcome_sha -> log "already finalized", still re-run the idempotent gh steps, return. (The
    comparison is against outcome_sha, not the last review: an operator commit AFTER a finished run is reviewed by
    `run` and must then be checked and finalized again, not waved through as already done.)
    Checks green (§11): run_checks(...); red -> GateViolation naming the failing command (PR stays draft). Green:
    write_check_log("finalize", 1); state.set_outcome("done", HEAD); commit_and_push("factory(N): finalize");
    ensure_pr(required=True); gh.pr_ready; gh.pr_comment(render_summary_comment(...), marker=SUMMARY_MARKER(HEAD)).
    The intake label is NOT removed (poll skips `done` from local state)."""
    state, wt = _require_state(ctx, "finalize"), _require_worktree(ctx, "finalize")
    check_parked(ctx)
    last = state.last_review()
    if last is None:
        raise FactoryError(
            f"finalize needs a review round for issue {ctx.issue}",
            hint=f"run `factory review {ctx.issue}` first",
        )
    open_important = ctx.ledger.open_important()
    if open_important:
        raise FactoryError(
            f"{len(open_important)} Important finding(s) are still open: "
            f"{', '.join(f.id for f in open_important)}",
            hint=f"fix them (`factory run {ctx.issue}`) or dismiss them "
            f'(`factory dismiss {ctx.issue} <id> "reason"`)',
        )
    if ctx.repo.code_changed_between(wt, last.sha, "HEAD"):
        raise FactoryError(
            f"code changed since review {last.round} ({last.sha[:12]}); it has not been reviewed",
            hint=f"run `factory review {ctx.issue}` before finalizing",
        )

    if state.outcome == "done" and not _code_changed_since_finalize(ctx):
        ctx.log("already finalized")
        _finalize_github(ctx, state.outcome_sha or ctx.repo.head(wt))
        return

    with _after_the_session(ctx):
        verdict = _run_checks(ctx)
        if not verdict.ok:
            log_path = _keep_check_log(ctx, "finalize", 1, verdict.log)
            _gate_failure(
                ctx,
                f"checks failed at finalize: {', '.join(verdict.failed)}",
                hint=f"the pull request stays a draft until they are green; the output is at {log_path}",
            )
        checks_module.write_check_log(wt, ctx.issue, "finalize", 1, verdict.log)
        done_sha = ctx.repo.head(wt)
        state.set_outcome("done", done_sha)
        commit_and_push(ctx, f"factory({ctx.issue}): finalize")
    _finalize_github(ctx, done_sha)
    ctx.log(f"issue {ctx.issue} is done; the pull request is ready for human review")


def _code_changed_since_finalize(ctx: Context) -> bool:
    """True when HEAD carries code the finished run never checked — an operator commit after `outcome: done`.
    Without an outcome_sha (a state file written before one was recorded) nothing can be compared, so the run
    counts as finished."""
    state, wt = ctx.state, ctx.worktree
    if not state.outcome_sha:
        return False
    return ctx.repo.code_changed_between(wt, state.outcome_sha, "HEAD")


def _finalize_github(ctx: Context, sha: str) -> None:
    """The idempotent GitHub half of finalize: flip the draft and post the summary once per outcome sha."""
    pr = ensure_pr(ctx, required=True)
    ctx.gh.pr_ready(pr.number)
    _, checks_text = checks_module.latest_check_log(ctx.worktree, ctx.issue)
    _post_comment(
        ctx,
        pr,
        gh_module.render_summary_comment(
            ctx.state.to_dict(), ctx.ledger.render_markdown(), checks_text, sha
        ),
        marker=gh_module.SUMMARY_MARKER.format(sha=sha),
        what="summary comment",
    )


def run(ctx: Context) -> None:
    """Termination rule (design §11). If ctx.worktree is None (no branch yet): spec(ctx) first (it sets ctx.worktree
    and ctx.state). Else check_parked(ctx). Then, with force=False throughout:
      not stages.spec -> spec; spec_needs_acceptance -> park(open_questions) [spec itself parks]
      not stages.plan -> plan; not stages.build -> build; no reviews -> review
      while ledger.open_important():
        the last review is stale — it is `gated` (it raised no_progress/rounds_exhausted) or code changed since
          reviews[-1].sha -> review, and start the loop again
        no_progress(state) -> park("no_progress", ...)
        fix_rounds >= max_fix_rounds -> park("rounds_exhausted", <list of open Important ids and titles>)
        else fix then review
      code changed since reviews[-1].sha (a `gated` review is not enough here: the gate was about findings, and
        none are open) -> review it, then re-enter the loop if that reopened Important findings
      outcome == "done" and no code change since outcome_sha -> log "already finalized", return
      finalize.

    Why the stale-review test comes FIRST (design §11, "only your action re-opens the loop"): every operator
    action moves HEAD, but only some of them touch code. A hand fix changes code; `dismiss` and `accept` change
    only work/. Deciding on `code_changed` alone would therefore re-park a dismissal on the very gate the
    dismissal answered, and the run could never move again. `ReviewRecord.gated` closes that: the review the gate
    was raised over cannot also be the review that justifies the next fix or the next park — it has to be redone
    over what the operator did. Each operator action buys exactly one review, and if that review still leaves
    Important findings open with no progress or no rounds left, the gate comes back."""
    ctx.force = False  # `run` never rewinds; --force is a per-stage operator action (design §6)
    if ctx.worktree is None or ctx.state is None:
        spec(ctx)
    else:
        check_parked(ctx)
        if not ctx.state.stage_done("spec"):
            spec(ctx)
    state = _require_state(ctx, "run")
    if state.spec_needs_acceptance():
        park(ctx, "open_questions", what_clears("open_questions", ctx))
    if not state.stage_done("plan"):
        plan(ctx)
    if not state.stage_done("build"):
        build(ctx)
    if not state.reviews:
        review(ctx)

    while True:
        while ctx.ledger.open_important():
            if _review_is_stale(ctx, before="fixing"):
                review(ctx)
                continue
            if state_module.no_progress(state):
                park(ctx, "no_progress", what_clears("no_progress", ctx))
            if state.fix_rounds >= ctx.config.max_fix_rounds:
                park(ctx, "rounds_exhausted", what_clears("rounds_exhausted", ctx))
            fix(ctx)
            review(ctx)
        if not _unreviewed_code(ctx):
            break
        # No Important finding is open, but HEAD carries code the last review never saw: an operator commit
        # after it has to be adjudicated, and finalize would otherwise refuse this HEAD for ever. A `gated`
        # review is NOT a reason to come back here — the gate was about open findings, and none are.
        ctx.log("code changed since the last review; reviewing it before finalizing")
        review(ctx)

    if state.outcome == "done" and not _code_changed_since_finalize(ctx):
        ctx.log("already finalized")
        return
    finalize(ctx)


def _review_is_stale(ctx: Context, *, before: str) -> bool:
    """True when reviews[-1] cannot decide what happens next: it raised the gate the operator has since answered
    (ReviewRecord.gated), or it reviewed a commit whose code HEAD no longer matches. Either way the next step is
    another review, and `before` names what that review comes before in the log line. False when no review has run
    at all — `run` reviews first in that case anyway."""
    last = ctx.state.last_review() if ctx.state else None
    if last is None:
        return False
    if last.gated:
        ctx.log(
            f"review {last.round} raised the gate you answered; reviewing your change before {before}"
        )
        return True
    if _unreviewed_code(ctx):
        ctx.log(f"code changed since the last review; reviewing it before {before}")
        return True
    return False


def _unreviewed_code(ctx: Context) -> bool:
    """True when HEAD's code differs from the commit the last review looked at — an operator hand fix, or a
    commit pushed from elsewhere and fast-forwarded in. False when no review has run."""
    last = ctx.state.last_review() if ctx.state else None
    if last is None:
        return False
    return ctx.repo.code_changed_between(ctx.worktree, last.sha, "HEAD")


def run_issue(
    repo: Repo, gh: GitHub, config: Config, issue: int, *, parent_env: dict, out=None
) -> int:
    """Builds a fresh Context for one issue, prepare(need_state=False), run(); maps to an exit code: 0 normal,
    2 NeedsHuman, 1 FactoryError (message + transcript path logged via out); unexpected exceptions -> 1 with traceback
    logged. Never raises except KeyboardInterrupt, which propagates to cli.main.

    In a finally it clears the RunLock, but only the one this process took (ctx.holds_lock + clear_if_owned: a live
    lock another container holds is not ours to remove) and never after an interrupt — the dying pid is precisely
    what tells the next command that this stage was interrupted (design §7, §15)."""
    ctx = Context(repo=repo, gh=gh, config=config, issue=issue, parent_env=parent_env, out=out)
    interrupted = False
    try:
        prepare(ctx, need_state=False)
        run(ctx)
        return 0
    except NeedsHuman as exc:
        ctx.log(f"issue {issue} needs human: {exc.gate}")
        ctx.log(exc.what_clears_it)
        return 2
    except FactoryError as exc:
        ctx.log(f"issue {issue} failed: {exc.message}")
        if exc.hint:
            ctx.log(exc.hint)
        transcript = getattr(exc, "transcript_path", None)
        if transcript:
            ctx.log(f"transcript: {transcript}")
        return 1
    except KeyboardInterrupt:
        interrupted = True
        raise
    except Exception:
        ctx.log(f"issue {issue} failed unexpectedly:\n{traceback.format_exc().rstrip()}")
        return 1
    finally:
        if ctx.holds_lock and not interrupted:
            RunLock.clear_if_owned(repo.factory_dir, issue)


def status(ctx: Context) -> str:
    """From local state only (design §6): does NOT call prepare. Opens repo.worktree_path(issue) (FactoryError "no worktree
    for issue N; nothing to report" if absent), loads State + Ledger read-only, HEAD via git (local). No fetch, no gh,
    no write. Returns text: issue, branch, HEAD, stages done (with harness/model), reviews (round, open, resolved),
    fix_rounds, open Important findings (id, title, file:line), outcome, parked?, PR url."""
    wt = ctx.repo.worktree_path(ctx.issue)
    if not wt.is_dir():
        raise FactoryError(
            f"no worktree for issue {ctx.issue}; nothing to report",
            hint=f"run `factory spec {ctx.issue}` (a worktree is rebuilt from origin/{ctx.branch} when it exists)",
        )
    state = State.load(wt, ctx.issue)
    ledger = Ledger.load(wt, ctx.issue)
    head = ctx.repo.head(wt)
    parked = state_module.is_parked(
        state, head, ctx.repo.parent(head, wt), ctx.repo.changed_paths_of_commit(wt, head)
    )

    lines = [
        f"issue {ctx.issue} on {state.branch} (base {state.base.get('branch')} {str(state.base.get('sha'))[:12]})",
        f"worktree {wt}",
        f"HEAD     {head[:12]}",
        f"PR       {state.pr['url'] if state.pr else '(none)'}",
        "",
        "stages:",
    ]
    for name in STAGE_ORDER:
        record = state.stages.get(name)
        lines.append(
            f"  {name:<6} {record.at} {record.harness} {record.model} ({record.auth}, cli {record.cli_version})"
            if record
            else f"  {name:<6} (not run)"
        )
    lines += ["", f"reviews: {len(state.reviews)} · fix rounds: {state.fix_rounds}"]
    for rec in state.reviews:
        lines.append(
            f"  round {rec.round} over {rec.sha[:12]}: {rec.important_open} Important open, "
            f"{rec.important_resolved} resolved, {rec.nits} nit(s)"
            + (" [diff truncated]" if rec.diff_truncated else "")
        )
    open_important = ledger.open_important()
    lines += ["", f"open Important findings: {len(open_important)}"]
    for finding in open_important:
        where = (
            f"{finding.file}:{finding.line}" if finding.line is not None else finding.file or "-"
        )
        lines.append(f"  {finding.id} {where} — {finding.title}")
    lines += [
        "",
        f"spec open questions: {len(state.spec_open_questions)}"
        + (
            " (accepted)"
            if state.spec_accepted
            else " (not accepted)"
            if state.spec_open_questions
            else ""
        ),
        f"outcome: {state.outcome or 'null'}"
        + (f" at {state.outcome_sha[:12]}" if state.outcome_sha else ""),
        f"parked: {'yes' if parked else 'no'}",
    ]
    return "\n".join(lines)


def dismiss(ctx: Context, finding_id: str, reason: str) -> None:
    """ledger.dismiss(id, reason, round=len(reviews)); state.set_outcome(None, None); commit_and_push("factory(N): dismiss F3")."""
    state = _require_state(ctx, "dismiss")
    if ctx.ledger is None:
        raise FactoryError(f"no ledger for issue {ctx.issue}")
    finding = ctx.ledger.dismiss(finding_id, reason, len(state.reviews))
    state.set_outcome(None, None)
    commit_and_push(ctx, f"factory({ctx.issue}): dismiss {finding.id}")
    ctx.log(f"dismissed {finding.id} ({finding.title}): {finding.dismissed_reason}")


def abandon(ctx: Context) -> None:
    """Externally visible order per §6; each step idempotent and failure-tolerant (a failure is logged and the remaining
    steps still run, so a half-abandoned issue is finished by re-running):
    1. gh.remove_label(issue, config.poll_label)  2. gh.pr_close(pr number if known/findable, comment "abandoned")
    3. repo.remove_worktree(issue)  4. repo.delete_branch(branch, remote=True) then local
    5. RunLock.clear; drop the issue from poll.json."""
    for what, action in (
        (
            f"remove the {ctx.config.poll_label} label",
            lambda: ctx.gh.remove_label(ctx.issue, ctx.config.poll_label),
        ),
        ("close the pull request", lambda: _close_pr(ctx)),
        ("remove the worktree", lambda: ctx.repo.remove_worktree(ctx.issue)),
        ("delete the branch", lambda: ctx.repo.delete_branch(ctx.branch, remote=True)),
        ("clear the host-local run state", lambda: _forget_issue(ctx)),
    ):
        try:
            action()
            ctx.log(f"abandon: {what} — done")
        except FactoryError as exc:
            ctx.log(f"abandon: could not {what}: {exc.message}")
    ctx.worktree = None
    ctx.state = None


def _close_pr(ctx: Context) -> None:
    number = int(ctx.state.pr["number"]) if ctx.state and ctx.state.pr else None
    if number is None:
        found = ctx.gh.find_pr_for_branch(ctx.branch)
        number = found.number if found else None
    if number is None:
        return
    ctx.gh.pr_close(number, comment=f"Abandoned by `factory abandon {ctx.issue}`.")


def _forget_issue(ctx: Context) -> None:
    """Every host-local trace of the issue: the lock (unconditionally — the run is over), the recorded
    last error, and the issue's entry in the poll journal."""
    RunLock.clear(ctx.repo.factory_dir, ctx.issue)
    state_module.clear_last_error(ctx.repo.factory_dir, ctx.issue)
    journal = state_module.read_poll_journal(ctx.repo.factory_dir)
    if journal.pop(str(ctx.issue), None) is not None:
        state_module.write_poll_journal(ctx.repo.factory_dir, journal)
