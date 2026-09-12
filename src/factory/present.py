"""Every line the factory says to a human, and the words it says them in.

Call sites pass facts - an issue number, a stage name, a resolved settings dict,
elapsed seconds, a path, an exception, a state document. This module owns the
wording, the layout, the ordering, and the choice of stream. Nothing else in the
package calls print(). Streams are read from sys at call time and flushed on
every line, so contextlib.redirect_stdout works in tests and an operator
watching a live run sees each line as it happens.
"""

import json
import sys
import time


# The exact next command(s) for each gate reason. This is the only copy: the
# terminal gate line, factory status, and the PR gate comment all quote it.
GATES = {
    "open_questions": "Resolve .factory/issues/{issue}/spec.md, then run factory accept {issue}.",
    "baseline_failing": "Fix the baseline checks by hand and commit on factory/{issue}.",
    "no_progress": "Fix and commit by hand, or dismiss a finding with a reason.",
    "rounds_exhausted": "Fix and commit by hand, or dismiss the remaining Important findings.",
}

# One line saying what happened, for each gate reason.
WHY = {
    "open_questions": "The spec asks questions only you can answer.",
    "baseline_failing": "The configured checks already fail before any factory change.",
    "no_progress": "The last fix round resolved no Important finding.",
    "rounds_exhausted": "The fix round limit was reached with Important findings still open.",
}

LOCAL_ONLY_NOTE = "Local state only; status does not fetch."
NO_PR = "none yet; it opens after build produces a code diff"
STAGES = ("spec", "plan", "build")
ROUNDS = ("review", "fix")


def gate_guidance(reason, issue):
    """The exact next command(s) for one gate, addressed to this issue."""
    return GATES[reason].format(issue=issue)


def timer():
    """Start a stopwatch; call the returned function for elapsed seconds."""
    started = time.monotonic()
    return lambda: time.monotonic() - started


# --- progress -------------------------------------------------------------

def stage_start(issue, stage, settings, auth, number):
    _out(f"Issue #{issue}: {_label(stage, number)} start "
         f"({_settings(settings)}, {auth} auth)")


def stage_done(issue, stage, settings, seconds, number):
    _out(f"Issue #{issue}: {_label(stage, number)} done in {_duration(seconds)} "
         f"({_settings(settings)})")


def stage_failed(issue, stage, settings, seconds, number):
    """A stage that died still reports which stage and how long it ran."""
    _out(f"Issue #{issue}: {_label(stage, number)} failed after {_duration(seconds)} "
         f"({_settings(settings)})")


def checks_start(issue, stage, number):
    _out(f"Issue #{issue}: {_label(stage, number)} checks start")


def checks_done(issue, stage, passed, log_path, seconds, number):
    _out(f"Issue #{issue}: {_label(stage, number)} checks "
         f"{'passed' if passed else 'failed'} in {_duration(seconds)}")
    if not passed and _exists(log_path):
        _out(f"  check log: {log_path}")


# --- outcomes -------------------------------------------------------------

def gate(issue, reason, guidance):
    _out(f"Issue #{issue} needs human input: {reason}",
         f"  {_why(reason)}",
         f"  Next: {guidance}")


def completed(issue, command, *, pr_url, artifacts, transcripts, seconds):
    _out(f"Issue #{issue}: {command} complete in {_duration(seconds)}",
         f"  PR: {pr_url or NO_PR}",
         f"  Artifacts: {artifacts}/",
         f"  Transcripts: {transcripts}/")


def failure(exc):
    _err(f"factory: {_headline(exc)}", *_evidence(exc))


def interrupted():
    _err("factory: interrupted; rerun the same command")


# --- setup and usage ------------------------------------------------------

def created(name):
    _out(f"Created {name}")


def initialized():
    _out("Initialized. Configure checks and auth in factory.toml, then commit and "
         "push these files to the base branch.")


def poll_overrides_rejected():
    _err("factory poll accepts no overrides; edit factory.toml")


def force_unsupported():
    _err("--force is supported only on spec, plan, and build")


# --- poll -----------------------------------------------------------------

def poll_busy():
    _out("Another poll is running; skipping.")


def poll_skip_done(issue):
    _out(f"Issue #{issue}: skip done")


def poll_skip_parked(issue, outcome):
    _out(f"Issue #{issue}: skip {outcome}")


def poll_skip_capped(issue, head):
    _out(f"Issue #{issue}: skip consecutive failure cap at {head}")


def poll_retry_gate(issue):
    _out(f"Issue #{issue}: retry pending gate publication")


def poll_failed(issue, exc):
    _out(f"Issue #{issue}: failed: {_headline(exc)}", *_evidence(exc))


# --- status ---------------------------------------------------------------

def json_document(data):
    """The machine-readable output: factory doctor and factory status --json."""
    _out(json.dumps(data, indent=2))


def status_summary(report, ledger, *, artifacts, transcripts):
    """Say, for a human, exactly what factory status --json reports."""
    issue, state = report["issue"], report.get("state") or {}
    note = report.get("note", LOCAL_ONLY_NOTE)
    locations = [f"  Artifacts: {artifacts}/", f"  Transcripts: {transcripts}/"]
    if not state:
        # A run writes its marker before the first stage saves state: say so here
        # too, so the summary and --json never disagree about a live run.
        _out(f"Issue #{issue}: no local run state.", *locations,
             "", f"Next: run factory run {issue}.",
             *_in_flight(report.get("in_flight")), "", note)
        return
    title = (state.get("issue") or {}).get("title")
    lines = [f"Issue #{issue}" + (f": {title}" if title else "")]
    lines += _identity(report, state) + locations
    lines += ["", "Stages:", *_stage_rows(state)]
    lines += ["", f"PR: {(state.get('pr') or {}).get('url') or NO_PR}"]
    lines += ["", *_finding_rows(ledger)]
    lines += ["", *_next_action(issue, state, report.get("head"))]
    lines += _in_flight(report.get("in_flight"))
    lines += ["", note]
    _out(*lines)


def _identity(report, state):
    head, url = report.get("head"), (state.get("issue") or {}).get("url")
    branch = state.get("branch")
    if not branch:
        where = "not created yet"
    else:
        # No HEAD with a recorded branch means the branch and worktree are gone.
        where = f"{branch} at {head[:12]}" if head else f"{branch} (not found locally)"
    lines = [f"  Branch: {where}", f"  Worktree: {report.get('worktree') or 'none'}"]
    return ([f"  Issue: {url}"] if url else []) + lines


def _stage_rows(state):
    rows = []
    for stage in STAGES:
        record = (state.get("stages") or {}).get(stage)
        rows.append(f"  {stage:<7} done {record['at']} ({_settings(record)})" if record
                    else f"  {stage:<7} not started")
    reviews = state.get("reviews") or []
    if reviews:
        last = reviews[-1]
        rows.append(f"  {'review':<7} {_count(len(reviews), 'round')}, last {last['at']} "
                    f"({_settings(last)})")
    else:
        rows.append(f"  {'review':<7} not started")
    fixes = state.get("fixes") or []
    if fixes:
        last = fixes[-1]
        rows.append(f"  {'fix':<7} {_count(state.get('fix_rounds') or len(fixes), 'round')} "
                    f"applied, last {last['at']} ({_settings(last)})")
    else:
        rows.append(f"  {'fix':<7} not started")
    return rows


def _finding_rows(ledger):
    findings = [f for f in (ledger or {}).get("findings", []) if f.get("status") == "open"]
    findings.sort(key=lambda f: (f.get("severity") != "important", _number(f.get("id"))))
    if not findings:
        return ["Open findings: none"]
    return [f"Open findings ({len(findings)}):"] + [
        f"  {f.get('id', '?')}  {f.get('severity', '?')}  {f.get('title', '?')}"
        f"  ({f.get('file', '?')}:{f.get('line') or '?'})" for f in findings
    ]


def _next_action(issue, state, head):
    """What to do now, reading the recorded outcome the way the engine reads it.

    An outcome speaks for the branch only while it still sits at HEAD: the engine
    drops a gate whose commit has been superseded, and refuses to finalize a
    completed run whose branch has moved on. No head means no branch to compare
    against, so there the record is taken at its word.
    """
    outcome = state.get("outcome") or ""
    stale = bool(head) and head != state.get("outcome_sha")
    if outcome == "done":
        if not stale:
            return ["Next: nothing; the PR is ready for human review."]
        return ["Next: review the new commit before factory can finish.",
                "  HEAD moved past the completed commit, so factory run refuses to finalize again.",
                f"  Run factory review {issue}, then factory run {issue}."]
    if outcome.startswith("needs_human:"):
        reason = outcome.split(":", 1)[1]
        if stale and not _still_gated(reason, state):
            return [f"Next: run factory run {issue}.",
                    f"  The {reason} gate was recorded at an earlier commit; HEAD has moved "
                    "since, so the run carries on from here."]
        guidance = gate_guidance(reason, issue) if reason in GATES else "Resolve it by hand."
        return [f"Next: needs human input ({reason}).", f"  {_why(reason)}", f"  {guidance}"]
    return [f"Next: run factory run {issue}."]


def _still_gated(reason, state):
    """Does this gate outlive the commit it was recorded at?

    A moved HEAD retires the gates a hand commit resolves - the engine re-runs the
    baseline checks and the review against the new commit. open_questions is not
    one of them: the engine re-raises it from the recorded spec answers, which no
    commit touches, so it holds until factory accept records an answer.
    """
    return (reason == "open_questions"
            and bool(state.get("spec_open_questions"))
            and not state.get("spec_accepted"))


def _in_flight(marker):
    if not marker:
        return []
    return ["", f"A run holds this issue: {marker.get('stage', 'unknown stage')}, "
                f"pid {marker.get('pid', '?')}, started {marker.get('started_at', '?')}."]


# --- shared wording -------------------------------------------------------

def _label(stage, number):
    """Rounds are numbered so two review or fix rounds never read alike."""
    return f"{stage} {number}" if stage in ROUNDS and number else stage


def _settings(settings):
    """One phrase for a role's harness, model, and effort, however they arrived."""
    settings = settings or {}
    return (f"{settings.get('harness') or 'unknown harness'}, "
            f"model {_value(settings.get('model'))}, effort {_value(settings.get('effort'))}")


def _value(value):
    """An unset model or effort means the harness CLI default, whoever recorded it."""
    return "default" if not value or value == "CLI default" else value


def _duration(seconds):
    seconds = max(float(seconds), 0.0)
    if seconds < 59.95:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _count(number, noun):
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _number(identifier):
    return int(identifier[1:]) if isinstance(identifier, str) and identifier[1:].isdigit() else 0


def _why(reason):
    return WHY.get(reason, "The run stopped for a decision only you can make.")


def _exists(path):
    try:
        return path is not None and path.exists()
    except OSError:
        return False


def _headline(exc):
    """A harness failure repeats its paths in its message; print them once, below."""
    return getattr(exc, "summary", None) or str(exc)


def _evidence(exc):
    transcript, errors = getattr(exc, "transcript", None), getattr(exc, "stderr_log", None)
    if transcript is None or errors is None:
        return ()
    return (f"  transcript: {transcript}", f"  stderr: {errors}")


def _out(*lines):
    _write(sys.stdout, lines)


def _err(*lines):
    _write(sys.stderr, lines)


def _write(stream, lines):
    for line in lines:
        print(line, file=stream, flush=True)
