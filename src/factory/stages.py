"""Stage contracts (design §9), termination rule (§11), operator commands (§6).

Every stage: validate state -> render prompt -> run harness -> validate output -> deterministic gate ->
commit artifact + state.json -> push. ANY failure after the harness launched (timeout, non-zero exit, bad output,
gate, allowed-edit violation) resets the worktree to HEAD (repo.reset_hard) BEFORE raising, keeps the transcript,
and exits 1 — so "re-run the same command" (§15) always works. Every exit 2 posts one PR comment naming the gate and
what clears it, idempotent per gate and outcome_sha.

Invariants
  * cli.py calls prepare(ctx) exactly once per process, before dispatch; stage functions assume a prepared ctx and
    never call it (prepare() returns immediately if ctx.prepared). spec and run are dispatched with need_state=False.
    status() and init/doctor/poll never call prepare. abandon calls prepare(need_state=False, commit_operator_edits=False).
  * The RunLock is taken by prepare() for the whole command and cleared by cli.py in a finally; each stage updates its
    `stage` field (RunLock.write) before doing work. run_harness_stage assumes the lock is held.
  * NeedsHuman is constructed in exactly two places: park() (commits the outcome, posts the gate comment) and
    check_parked() (re-raises an already-recorded, already-commented gate). No other function raises it.
  * Round index: review and fix use their round number; spec, plan and build always use 1 (a --force re-run overwrites
    work/<n>/prompts/<stage>-1.md and checks/<stage>-1*.log rather than accumulating attempts).
  * Context.harness_name / auth / model are read from ctx.config (validate_overrides applied CLI flags): one source.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .gh import GitHub, PullRequest
from .harness import Harness, HarnessResult
from .repo import Repo
from .state import Ledger, State

STAGE_ORDER = ("spec", "plan", "build")  # committed once each; review/fix are rounds


@dataclass
class Context:
    repo: Repo
    gh: GitHub
    config: Config  # already has CLI overrides applied (harness, auth, model)
    issue: int
    force: bool = False  # --force was passed (only spec/plan/build accept it; run() calls stages with force=False)
    parent_env: dict | None = None  # os.environ by default; tests inject
    out: Callable[[str], None] | None = None  # progress lines; None -> sys.stderr. stdout is for command output.

    # populated by prepare()
    prepared: bool = False
    worktree: Path | None = None
    state: State | None = None
    ledger: Ledger | None = None
    harness: Harness | None = None
    harness_env: dict | None = None
    force_push: bool = False  # set by rewind_for_force; commit_and_push then uses --force-with-lease
    last_error: str | None = None  # from a previous attempt's RunLock (stage_note input)

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
        raise NotImplementedError


# ---------------------------------------------------------------- shared plumbing


def prepare(ctx: Context, *, need_state: bool = True, fetch: bool = True, commit_operator_edits: bool = True) -> Context:
    """Start-of-command validation (design §7). Returns ctx (prepared=True); returns immediately if already prepared.
    1. ctx.harness = get_harness(name); ctx.harness_env = build_env(name, auth, parent_env, passthrough=...) — this is
       where `api` with no key fails, before any GitHub or git write. (finalize needs harness_env too, for the checks.)
    2. if fetch: repo.fetch(). worktree = repo.ensure_worktree(issue) (recovery from origin/factory/<n>); when neither a
       local nor a remote branch exists: need_state=False -> return with worktree=None (spec creates it; run starts at
       spec), else FactoryError("no branch for issue N; run `factory spec N`").
    3. RunLock: live pid -> FactoryError("issue N: <stage> in progress (pid P)"); dead pid -> repo.reset_hard(HEAD),
       ctx.last_error = lock.last_error, log "interrupted <stage> discarded; transcript kept". Then take the lock for
       this command (stage=<command name>). Step 3's reset runs BEFORE step 5's operator-edit commit, always.
    4. if fetch: repo.sync_with_remote(worktree, branch) (fast-forward if strictly ahead; diverged -> exit 1), then
       repo.push_if_ahead (a local-only accept/dismiss commit must not die with the host; failure logged, not fatal).
    5. Dirty paths: under work/<n>/ and commit_operator_edits -> commit "factory(N): operator edits"; is_transient()
       paths ignored; anything else dirty -> FactoryError naming the paths.
    6. Load State + Ledger; check every stages[*].start_commit and reviews[*].sha is an ancestor of HEAD
       (else FactoryError "state.json records commit X not reachable from HEAD"). Then ensure_pr(ctx) (best effort).
    """
    raise NotImplementedError


def ensure_pr(ctx: Context, *, required: bool = False) -> PullRequest | None:
    """If state.pr is set, return it. Else if origin/<branch> exists: gh.find_pr_for_branch(branch) -> record in state
    (in memory; the next commit persists it); if none and stages['spec'] is recorded: create_draft_pr and record it.
    Idempotent. A gh failure is logged and returns None unless required=True (finalize) -> FactoryError."""
    raise NotImplementedError


def branch_protected_path_violations(ctx: Context) -> list[str]:
    """checks.is_protected over repo.changed_paths_between(worktree, state.base['sha'], HEAD). Called at the top of
    run_harness_stage before EVERY launch (design §19: the check protects the session about to start, including from
    operator commits and fast-forwarded remote commits). Any hit -> FactoryError telling the operator to revert them."""
    raise NotImplementedError


def run_harness_stage(ctx: Context, *, stage: str, round: int, mode: str, prompt_text: str,
                      schema_name: str) -> tuple[dict, HarnessResult, Path]:
    """1. branch_protected_path_violations -> FactoryError.
    2. Write the prompt to work/<n>/prompts/<stage>-<round>.md (prompt_path) and copy it next to the transcript.
    3. RunLock.write(stage=stage). Snapshot repo.status_porcelain (minus transients).
    4. harness.run(cwd=worktree, prompt_file, schema_file=schema.schema_path(schema_name), mode, model=ctx.model,
       auth, env=harness_env, timeout_s=config.stage_timeout_s, transcript_path=
       .factory/transcripts/<n>/<stage>-<round>-<UTC ts>.json, max_turns=<read|write from harness config>,
       max_budget_usd, agents_md=<worktree>/AGENTS.md if exists, writable_dirs=<codex config>).
    5. schema.validate_or_raise(output). Log permission_denials (never a gate) and "hit the turn bound".
    6. mode=read: status after (minus transients) must equal the snapshot (the prompt file is in both) else
       GateViolation("read stage wrote files", paths).
    On ANY exception in 4-6: repo.reset_hard(worktree) first, record str(exc) into the RunLock's last_error, re-raise.
    Returns (validated output, HarnessResult, prompt_path rel to worktree) — the caller records metadata and, for write
    stages, passes the prompt path as `ignore` to allowed_edit_violations."""
    raise NotImplementedError


def record_stage(ctx: Context, stage: str, result: HarnessResult, start_commit: str) -> None:
    """state.stages[stage] = StageRecord(start_commit, at=now, harness=ctx.harness_name, model=result.model or ctx.model
    or "(cli default)", cli_version=result.cli_version, auth=ctx.auth, permission_denials=len(result.permission_denials))."""
    raise NotImplementedError


def commit_and_push(ctx: Context, message: str, *, paths: list[str] | None = None,
                    force_with_lease: bool | None = None) -> str:
    """state.save + ledger.save; if repo.has_changes(paths) commit_all(paths) else skip the commit; then
    repo.push(force_with_lease = ctx.force_push when None). Returns HEAD. A push failure after the commit leaves the commit
    local (design §15) and raises FactoryError saying re-running pushes it."""
    raise NotImplementedError


def park(ctx: Context, gate: str, what_clears_it: str, *, evidence_paths: list[str] | None = None) -> None:
    """Design §9/§11 gate exit. No-op re-park: if state.outcome == needs_human:<gate> and is_parked(ctx) -> raise
    NeedsHuman directly (the recorded gate already describes this HEAD). Otherwise:
    1. commit evidence_paths first in their own commit (e.g. checks/build-1-baseline.log: "factory(N): build baseline (red)")
    2. outcome_sha = HEAD (after 1); state.set_outcome(f"needs_human:{gate}", outcome_sha)
    3. commit ONLY work/<n>/state.json ("factory(N): park <gate>"); push
    4. ensure_pr; gh.pr_comment(render_gate_comment(...), marker=GATE_MARKER(gate, outcome_sha)) when a PR exists
    5. raise NeedsHuman(gate, what_clears_it)."""
    raise NotImplementedError


def is_parked(ctx: Context) -> bool:
    """state.is_parked(state, head, repo.parent(head), repo.changed_paths_of_commit(head))."""
    raise NotImplementedError


def check_parked(ctx: Context) -> None:
    """Parked stays parked (design §11): if is_parked(ctx) raise NeedsHuman(state.gate(), <what clears it for that gate>)
    with no harness call and no new commit or comment."""
    raise NotImplementedError


def what_clears(gate: str, ctx: Context) -> str:
    """Operator-facing sentence per gate (used by park and check_parked so both say the same thing)."""
    raise NotImplementedError


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
    5. ctx.force_push = True. Never pushes itself — the stage's single push at the end is the force-with-lease push."""
    raise NotImplementedError


# ---------------------------------------------------------------- stages


def spec(ctx: Context) -> None:
    """Design §9 row `spec` (mode read):
    - if stages.spec recorded: not force -> log "spec already done", return; force -> rewind_for_force(ctx, "spec")
    - base_sha = repo.base_sha(config.base_branch); snapshot issue via gh.issue (the stage's one GitHub read);
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
    raise NotImplementedError


def accept(ctx: Context) -> None:
    """Requires stages.spec. spec_accepted={"by":"operator","at":now}; re-read open questions from spec.md
    (parse_open_questions — the operator may have edited them); state.set_outcome(None, None);
    commit_and_push("factory(N): accept spec"). Idempotent (no changes -> log and return)."""
    raise NotImplementedError


def plan(ctx: Context) -> None:
    """Requires stages.spec else FactoryError. spec_needs_acceptance() -> check_parked() then park(open_questions).
    stages.plan recorded: not force -> "plan already done"; force -> rewind_for_force(ctx, "plan").
    start_commit = HEAD; prompt = render_role("plan", {issue, spec, stage_note}); mode read; schema "plan";
    gate: plan_has_required_sections(markdown) empty else reset + GateViolation(hint naming the missing headings);
    write plan.md; record_stage; commit_and_push("factory(N): plan")."""
    raise NotImplementedError


def build(ctx: Context) -> None:
    """Requires stages.plan. stages.build recorded: not force -> "build already done"; force -> rewind_for_force.
    start_commit = HEAD. Baseline: run_checks(worktree, config, checks_env(harness_env), stage_timeout_s) BEFORE the
    session; red -> write_check_log(stage="build", round=1, suffix="-baseline") and park("baseline_failing", ...,
    evidence_paths=[that log]) (§11). Green: prompt = render_role("build", {issue, plan, spec, checks, stage_note});
    run_harness_stage(mode write, schema "build") -> (out, result, prompt_rel).
    Gates, each: reset_hard + GateViolation (message recorded in RunLock.last_error for the next attempt's stage_note):
      allowed_edit_violations(changed_paths_in_worktree, stage="build", ignore=[prompt_rel]);
      paths_missing_from_plan(changed non-work paths, plan.md as now in the worktree) (hint: list them in plan.md);
      run_checks red (log kept next to the transcript).
    Green: write_check_log("build", 1, log); work/<n>/build-1.json = out; record_stage; commit_and_push("factory(N): build")."""
    raise NotImplementedError


def write_review_diff(ctx: Context, round: int) -> tuple[str, int, int, bool, list[str]]:
    """Write `git diff <base sha>...HEAD -- . ':!work'` to <worktree>/.factory/tmp/review-<round>.diff (transient,
    excluded, readable by path). If len > config.max_diff_bytes: write diff_stat + per-file diffs largest-first until the
    cap + a banner naming the omitted files. Returns (path rel to worktree, bytes, lines, truncated, omitted_paths)."""
    raise NotImplementedError


def review(ctx: Context) -> None:
    """Always runs a new round (design §6). Requires stages.build else FactoryError. round = len(reviews)+1;
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
    raise NotImplementedError


def fix(ctx: Context) -> None:
    """Requires at least one review. open Important == 0 -> log "nothing to fix", return.
    fix_rounds >= config.max_fix_rounds -> park("rounds_exhausted", ...) (§11; the same gate run raises).
    repo.code_changed_between(reviews[-1].sha, HEAD) -> FactoryError("code changed since review <n>; run `factory review N`").
    round = fix_rounds + 1. prompt = render_role("fix", {issue, findings (open Important), checks, stage_note});
    mode write; schema "fix". Gates (reset + GateViolation): allowed_edit_violations(stage="fix", ignore=[prompt_rel])
    (tests and all of work/ are violations); run_checks red. Green: write_check_log("fix", round); work/<n>/fix-<round>.json
    = out; fix_rounds = round; commit_and_push("factory(N): fix <round>"). `how` is a claim; only the next review changes
    a status."""
    raise NotImplementedError


def finalize(ctx: Context) -> None:
    """Requires: at least one review; open Important == 0 (else FactoryError naming them: "dismiss or fix them");
    no code change since reviews[-1].sha (else FactoryError "run `factory review N`"). outcome == "done" and nothing
    changed -> log "already finalized", still re-run the idempotent gh steps, return.
    Checks green (§11): run_checks(...); red -> GateViolation naming the failing command (PR stays draft). Green:
    write_check_log("finalize", 1); state.set_outcome("done", HEAD); commit_and_push("factory(N): finalize");
    ensure_pr(required=True); gh.pr_ready; gh.pr_comment(render_summary_comment(...), marker=SUMMARY_MARKER(HEAD)).
    The intake label is NOT removed (poll skips `done` from local state)."""
    raise NotImplementedError


def run(ctx: Context) -> None:
    """Termination rule (design §11). If ctx.worktree is None (no branch yet): spec(ctx) first (it sets ctx.worktree
    and ctx.state). Else check_parked(ctx). Then, with force=False throughout:
      not stages.spec -> spec; spec_needs_acceptance -> park(open_questions) [spec itself parks]
      not stages.plan -> plan; not stages.build -> build; no reviews -> review
      while ledger.open_important():
        no_progress(state) -> park("no_progress", ...)
        fix_rounds >= max_fix_rounds -> park("rounds_exhausted", <list of open Important ids and titles>)
        code_changed_between(reviews[-1].sha, HEAD) -> review (operator hand-fixed) else fix then review
      outcome == "done" and no code change since reviews[-1].sha -> log "already finalized", return
      finalize."""
    raise NotImplementedError


def run_issue(repo: Repo, gh: GitHub, config: Config, issue: int, *, parent_env: dict, out=None) -> int:
    """Builds a fresh Context for one issue, prepare(need_state=False), run(); maps to an exit code: 0 normal,
    2 NeedsHuman, 1 FactoryError (message + transcript path logged via out); unexpected exceptions -> 1 with traceback
    logged. Never raises. Clears the RunLock in a finally. cli.py passes this to poll as run_issue and uses it for
    `factory run`."""
    raise NotImplementedError


def status(ctx: Context) -> str:
    """From local state only (design §6): does NOT call prepare. Opens repo.worktree_path(issue) (FactoryError "no worktree
    for issue N; nothing to report" if absent), loads State + Ledger read-only, HEAD via git (local). No fetch, no gh,
    no write. Returns text: issue, branch, HEAD, stages done (with harness/model), reviews (round, open, resolved),
    fix_rounds, open Important findings (id, title, file:line), outcome, parked?, PR url."""
    raise NotImplementedError


def dismiss(ctx: Context, finding_id: str, reason: str) -> None:
    """ledger.dismiss(id, reason, round=len(reviews)); state.set_outcome(None, None); commit_and_push("factory(N): dismiss F3")."""
    raise NotImplementedError


def abandon(ctx: Context) -> None:
    """Externally visible order per §6; each step idempotent and failure-tolerant (a failure is logged and the remaining
    steps still run, so a half-abandoned issue is finished by re-running):
    1. gh.remove_label(issue, config.poll_label)  2. gh.pr_close(pr number if known/findable, comment "abandoned")
    3. repo.remove_worktree(issue)  4. repo.delete_branch(branch, remote=True) then local
    5. RunLock.clear; drop the issue from poll.json."""
    raise NotImplementedError
