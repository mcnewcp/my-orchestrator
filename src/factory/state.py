"""Local state (design §7): work/<issue>/state.json, work/<issue>/findings.json, host-local transients.

Both committed files are written atomically (tmp file in the same directory + os.replace).

HEAD-movement semantics (the load-bearing rules; every predicate below is a PURE function of these facts)
--------------------------------------------------------------------------------------------------------
The design keys "did the operator act?" on HEAD moving. The factory's *own* commits also move HEAD, so:

  stages[s].start_commit  = HEAD when stage s began = the commit `--force` on s rewinds to (for spec: the base
                            sha). The artifact commit itself is the next commit that touches work/<n>/state.json;
                            it is never needed for control flow (a commit cannot contain its own sha).
  reviews[-1].sha         = the commit that was REVIEWED (HEAD before the review-record commit).
  "HEAD is the reviewed commit" (fix precondition, finalize precondition, run's review-vs-fix switch) means:
                            no diff outside work/ between reviews[-1].sha and HEAD (Repo.code_changed_between is
                            False). Deliberate divergence from §6's literal "HEAD moved": an accept/dismiss commit
                            touches only work/ and must lead to fix, not burn a review round.
  outcome_sha             = HEAD at the moment `outcome` was set, i.e. the commit BEFORE park()'s state-only commit.
                            park() commits NOTHING but work/<n>/state.json (any evidence log is committed first,
                            and outcome_sha is read after that commit).
  parked (is_parked)      = outcome startswith "needs_human" and (HEAD == outcome_sha or
                            (parent(HEAD) == outcome_sha and HEAD changed only [work/<n>/state.json])).
  no_progress             = len(reviews) >= 2 and reviews[-1].important_open > 0 and reviews[-1].important_resolved == 0
                            and reviews[-1].fix_rounds_at > reviews[-2].fix_rounds_at  (the review followed a fix).

accept and dismiss clear outcome/outcome_sha and commit -> not parked. A hand fix adds a commit whose parent is
not outcome_sha -> not parked. --force rewinds and rebuilds state -> not parked.
"""

from __future__ import annotations

import copy as _copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import FactoryError

SEVERITIES = ("important", "nit")
PASSES = ("bugs", "security", "compliance")
STATUSES = ("open", "resolved", "dismissed")

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_FINDING_ID = re.compile(r"^F(\d+)$")
_HEADING = re.compile(r"^\s{0,3}#{2,6}\s*open\s+questions\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def work_dir(worktree: Path, issue: int) -> Path:
    return worktree / "work" / str(issue)


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace; creates parent dirs."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise FactoryError(f"could not write {path}: {exc}") from exc


def atomic_write_json(path: Path, obj) -> None:
    """Pretty JSON (indent=2, sort_keys=False, trailing newline) via atomic_write_text."""
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> object:
    """Parse a JSON file; FactoryError names the path for a committed file that cannot be read."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryError(f"could not read {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise FactoryError(f"{path}: not valid JSON ({exc})") from exc


def _read_json_map(path: Path) -> dict:
    """Host-local transient map (design §7: safe to lose). Missing or unreadable -> {}."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _opt_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _require(entry: dict, key: str, what: str) -> str:
    value = entry.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise FactoryError(f"{what} is missing '{key}'")
    return str(value).strip() if isinstance(value, str) else str(value)


# ---------------------------------------------------------------- state.json


@dataclass
class StageRecord:
    start_commit: (
        str  # HEAD when the stage began; what `--force` on this stage rewinds to (spec: base sha)
    )
    at: str
    harness: str
    model: str  # HarnessResult.model or the configured model or "(cli default)"
    cli_version: str
    auth: str
    permission_denials: int = 0  # claude only; logged, never a gate (see harness.HarnessResult)


@dataclass
class ReviewRecord:
    round: int
    sha: str  # the commit that was reviewed
    important_open: int  # open Important AFTER this round's merge
    important_resolved: int  # open -> resolved transitions in THIS round only (not a running total)
    nits: int  # new nits this round
    reraised_dropped: int
    fix_rounds_at: int = (
        0  # state.fix_rounds when this review ran; no_progress() compares consecutive rounds
    )
    diff_truncated: bool = False


def _stage_from_dict(name: str, d) -> StageRecord:
    if not isinstance(d, dict):
        raise FactoryError(f"stages.{name} is not an object")
    start_commit = d.get("start_commit")
    if not start_commit:
        raise FactoryError(f"stages.{name} has no start_commit")
    return StageRecord(
        start_commit=str(start_commit),
        at=str(d.get("at") or ""),
        harness=str(d.get("harness") or ""),
        model=str(d.get("model") or ""),
        cli_version=str(d.get("cli_version") or ""),
        auth=str(d.get("auth") or ""),
        permission_denials=_opt_int(d.get("permission_denials")) or 0,
    )


def _review_from_dict(index: int, d) -> ReviewRecord:
    if not isinstance(d, dict):
        raise FactoryError(f"reviews[{index}] is not an object")
    round_ = _opt_int(d.get("round"))
    sha = d.get("sha")
    if round_ is None or not sha:
        raise FactoryError(f"reviews[{index}] needs both 'round' and 'sha'")
    return ReviewRecord(
        round=round_,
        sha=str(sha),
        important_open=_opt_int(d.get("important_open")) or 0,
        important_resolved=_opt_int(d.get("important_resolved")) or 0,
        nits=_opt_int(d.get("nits")) or 0,
        reraised_dropped=_opt_int(d.get("reraised_dropped")) or 0,
        fix_rounds_at=_opt_int(d.get("fix_rounds_at")) or 0,
        diff_truncated=bool(d.get("diff_truncated")),
    )


@dataclass
class State:
    issue: dict  # {"number": int, "snapshot_sha256": str, "snapshot_at": str}
    base: dict  # {"branch": str, "sha": str}
    branch: str
    stages: dict[str, StageRecord] = field(default_factory=dict)  # keys: spec plan build
    spec_open_questions: list[str] = field(default_factory=list)
    spec_accepted: dict | None = None  # {"by": "auto"|"operator", "at": iso}
    reviews: list[ReviewRecord] = field(default_factory=list)
    fix_rounds: int = 0
    pr: dict | None = None  # {"number": int, "url": str}
    outcome: str | None = None  # None | "done" | "needs_human:<gate>"
    outcome_sha: str | None = None
    # PR-comment idempotency is marker-based in gh.pr_comment (authoritative); no state field caches it.

    # --- persistence
    @staticmethod
    def path(worktree: Path, issue: int) -> Path:
        return work_dir(worktree, issue) / "state.json"

    @classmethod
    def load(cls, worktree: Path, issue: int) -> State:
        """Parse state.json; FactoryError if missing or malformed."""
        path = cls.path(worktree, issue)
        if not path.exists():
            raise FactoryError(
                f"{path}: no state.json for issue {issue}",
                hint=f"run `factory spec {issue}` first",
            )
        raw = _read_json(path)
        try:
            state = cls.from_dict(raw)
        except FactoryError as exc:
            raise FactoryError(f"{path}: {exc.message}", exc.hint) from exc
        if state.number != int(issue):
            raise FactoryError(f"{path}: state.json records issue {state.number}, expected {issue}")
        return state

    @classmethod
    def load_or_none(cls, worktree: Path, issue: int) -> State | None:
        if not cls.path(worktree, issue).exists():
            return None
        return cls.load(worktree, issue)

    def save(self, worktree: Path) -> Path:
        """Atomic write to State.path(worktree, self.issue['number']). Returns the path."""
        path = State.path(worktree, self.number)
        atomic_write_json(path, self.to_dict())
        return path

    def to_dict(self) -> dict:
        return {
            "issue": dict(self.issue),
            "base": dict(self.base),
            "branch": self.branch,
            "stages": {name: asdict(rec) for name, rec in self.stages.items()},
            "spec_open_questions": list(self.spec_open_questions),
            "spec_accepted": dict(self.spec_accepted) if self.spec_accepted else None,
            "reviews": [asdict(rec) for rec in self.reviews],
            "fix_rounds": self.fix_rounds,
            "pr": dict(self.pr) if self.pr else None,
            "outcome": self.outcome,
            "outcome_sha": self.outcome_sha,
        }

    @classmethod
    def from_dict(cls, d: dict) -> State:
        if not isinstance(d, dict):
            raise FactoryError("state.json top level is not an object")
        issue = d.get("issue")
        if not isinstance(issue, dict) or _opt_int(issue.get("number")) is None:
            raise FactoryError("missing required key 'issue.number'")
        base = d.get("base")
        if not isinstance(base, dict) or not base.get("sha"):
            raise FactoryError("missing required key 'base.sha'")
        branch = d.get("branch")
        if not branch:
            raise FactoryError("missing required key 'branch'")
        issue = dict(issue)
        issue["number"] = _opt_int(issue.get("number"))
        stages_raw = d.get("stages") or {}
        if not isinstance(stages_raw, dict):
            raise FactoryError("'stages' is not an object")
        reviews_raw = d.get("reviews") or []
        if not isinstance(reviews_raw, list):
            raise FactoryError("'reviews' is not an array")
        spec_accepted = d.get("spec_accepted")
        pr = d.get("pr")
        return cls(
            issue=issue,
            base=dict(base),
            branch=str(branch),
            stages={name: _stage_from_dict(name, rec) for name, rec in stages_raw.items()},
            spec_open_questions=[str(q) for q in (d.get("spec_open_questions") or [])],
            spec_accepted=dict(spec_accepted) if isinstance(spec_accepted, dict) else None,
            reviews=[_review_from_dict(i, rec) for i, rec in enumerate(reviews_raw)],
            fix_rounds=_opt_int(d.get("fix_rounds")) or 0,
            pr=dict(pr) if isinstance(pr, dict) else None,
            outcome=str(d["outcome"]) if d.get("outcome") else None,
            outcome_sha=str(d["outcome_sha"]) if d.get("outcome_sha") else None,
        )

    # --- derived
    @property
    def number(self) -> int:
        return int(self.issue["number"])

    def last_review(self) -> ReviewRecord | None:
        return self.reviews[-1] if self.reviews else None

    def stage_done(self, stage: str) -> bool:
        return stage in self.stages

    def spec_needs_acceptance(self) -> bool:
        """True when spec produced open questions and none were accepted (design §11 gate)."""
        return bool(self.spec_open_questions) and self.spec_accepted is None

    def is_needs_human(self) -> bool:
        return bool(self.outcome and self.outcome.startswith("needs_human"))

    def set_outcome(self, outcome: str | None, head_sha: str | None) -> None:
        self.outcome = outcome
        self.outcome_sha = head_sha

    def gate(self) -> str | None:
        """'open_questions' for outcome 'needs_human:open_questions'; None otherwise."""
        if not self.is_needs_human():
            return None
        return self.outcome.partition(":")[2] or None


def is_parked(
    state: State, head: str, head_parent: str | None, head_changed_paths: list[str]
) -> bool:
    """Design §11 "parked stays parked", pure: True when state.outcome startswith "needs_human" and
    (head == state.outcome_sha or (head_parent == state.outcome_sha and
     head_changed_paths == [f"work/{state.number}/state.json"])). No git access; callers supply the three facts
    (stages.is_parked and poll.classify both use repo.head/parent/changed_paths_of_commit + this)."""
    if not state.is_needs_human() or not state.outcome_sha:
        return False
    if head == state.outcome_sha:
        return True
    return head_parent == state.outcome_sha and list(head_changed_paths) == state_only_paths(
        state.number
    )


def no_progress(state: State) -> bool:
    """Design §11: the latest review followed a fix and resolved zero Important findings while some stay open.
    See the module docstring for the exact predicate over reviews[-1] and reviews[-2]."""
    if len(state.reviews) < 2:
        return False
    last, previous = state.reviews[-1], state.reviews[-2]
    return (
        last.important_open > 0
        and last.important_resolved == 0
        and last.fix_rounds_at > previous.fix_rounds_at
    )


def state_only_paths(issue: int) -> list[str]:
    return [f"work/{issue}/state.json"]


# ---------------------------------------------------------------- findings.json (the ledger)


def finding_key(pass_: str, file: str, title: str) -> str:
    """sha1(pass|file|normalized title). Normalization: lowercase, collapse whitespace,
    strip punctuation except word chars and spaces."""
    payload = f"{pass_}|{file}|{normalize_title(title)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    return _WS.sub(" ", _PUNCT.sub("", str(title).lower())).strip()


@dataclass
class Finding:
    id: str  # "F1", "F2", ...
    key: str
    pass_: str  # serialized as "pass"
    severity: str
    file: str
    line: int | None
    title: str
    detail: str
    evidence: str
    opened_round: int
    status: str = "open"
    status_round: int | None = None
    status_evidence: str | None = None
    dismissed_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "pass": self.pass_,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "opened_round": self.opened_round,
            "status": self.status,
            "status_round": self.status_round,
            "status_evidence": self.status_evidence,
            "dismissed_reason": self.dismissed_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        if not isinstance(d, dict):
            raise FactoryError("a finding is not an object")
        id_ = _require(d, "id", "finding")
        pass_ = _require(d, "pass", f"finding {id_}")
        severity = _require(d, "severity", f"finding {id_}")
        title = _require(d, "title", f"finding {id_}")
        status = str(d.get("status") or "open")
        if status not in STATUSES:
            raise FactoryError(f"finding {id_} has unknown status '{status}'")
        file = str(d.get("file") or "")
        return cls(
            id=id_,
            key=str(d.get("key") or finding_key(pass_, file, title)),
            pass_=pass_,
            severity=severity,
            file=file,
            line=_opt_int(d.get("line")),
            title=title,
            detail=str(d.get("detail") or ""),
            evidence=str(d.get("evidence") or ""),
            opened_round=_opt_int(d.get("opened_round")) or 0,
            status=status,
            status_round=_opt_int(d.get("status_round")),
            status_evidence=d.get("status_evidence"),
            dismissed_reason=d.get("dismissed_reason"),
        )

    @property
    def is_important(self) -> bool:
        return self.severity == "important"


@dataclass
class MergeStats:
    resolved: int = 0  # Important findings moved open -> resolved this round
    unresolved: int = 0
    new_important: int = 0
    new_nits: int = 0
    merged_duplicates: int = 0  # new finding matched an open one
    reraised_dropped: int = 0  # new finding matched a dismissed one
    reopened: int = 0  # new finding matched a resolved one -> reopened as regression
    missing_updates: list[str] = field(
        default_factory=list
    )  # open finding ids the reviewer did not address


@dataclass
class Ledger:
    findings: list[Finding] = field(default_factory=list)

    @staticmethod
    def path(worktree: Path, issue: int) -> Path:
        return work_dir(worktree, issue) / "findings.json"

    @classmethod
    def load(cls, worktree: Path, issue: int) -> Ledger:
        """Missing file -> empty ledger."""
        path = cls.path(worktree, issue)
        if not path.exists():
            return cls()
        raw = _read_json(path)
        try:
            return cls.from_dict(raw)
        except FactoryError as exc:
            raise FactoryError(f"{path}: {exc.message}", exc.hint) from exc

    def save(self, worktree: Path, issue: int) -> Path:
        path = Ledger.path(worktree, issue)
        atomic_write_json(path, self.to_dict())
        return path

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings]}

    @classmethod
    def from_dict(cls, d: dict) -> Ledger:
        if not isinstance(d, dict):
            raise FactoryError("findings.json top level is not an object")
        raw = d.get("findings")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise FactoryError("'findings' is not an array")
        return cls(findings=[Finding.from_dict(entry) for entry in raw])

    # --- queries
    def get(self, id: str) -> Finding | None:
        for finding in self.findings:
            if finding.id == id:
                return finding
        return None

    def open(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "open"]

    def open_important(self) -> list[Finding]:
        return [f for f in self.open() if f.is_important]

    def next_id(self) -> str:
        highest = 0
        for finding in self.findings:
            match = _FINDING_ID.match(finding.id)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"F{highest + 1}"

    # --- mutations
    def merge_review(self, review_output: dict, round: int) -> MergeStats:
        """Deterministic ledger merge (design §10):
        1. apply `updates`: for each {id, status: resolved|unresolved, evidence} on an OPEN finding,
           `resolved` -> status=resolved, status_round=round, status_evidence=evidence;
           `unresolved` -> stays open, status_evidence=evidence. Updates for unknown or non-open ids are ignored.
           Every open finding (before the merge) lacking an update is listed in stats.missing_updates.
        2. for each `new` finding compute key = finding_key(pass, file, title):
           - key matches an OPEN finding  -> merged (no duplicate; keep the existing entry), merged_duplicates += 1
           - key matches a DISMISSED one   -> dropped, reraised_dropped += 1
           - key matches a RESOLVED one    -> reopened as regression: status=open, status_round=round,
                                              status_evidence=new evidence, detail/evidence refreshed; reopened += 1
           - otherwise appended with next_id(), opened_round=round; new_important/new_nits += 1
        Nits participate in matching like any finding but never block (design §11).
        Returns MergeStats; the caller (stages.review) turns missing_updates / new_nits > cap into GateViolations
        BEFORE saving, so a rejected round leaves the ledger untouched (callers merge on a copy: see merged_copy).

        `stats.resolved` counts IMPORTANT open->resolved transitions only (it is ReviewRecord.important_resolved,
        which the no_progress rule reads); `stats.unresolved` counts every applied 'unresolved' update.
        """
        stats = MergeStats()
        open_before = [f.id for f in self.open()]
        updated: set[str] = set()
        for entry in _entries(review_output, "updates", round):
            self._apply_update(entry, round, stats, updated)
        stats.missing_updates = [fid for fid in open_before if fid not in updated]
        for entry in _entries(review_output, "new", round):
            self._merge_new(entry, round, stats)
        return stats

    def _apply_update(self, entry: dict, round: int, stats: MergeStats, updated: set[str]) -> None:
        what = f"review round {round}: update entry"
        finding_id = _require(entry, "id", what)
        finding = self.get(finding_id)
        if finding is None or finding.status != "open":
            return  # unknown or already adjudicated: an update cannot resurrect it
        status = _require(entry, "status", f"review round {round}: update {finding_id}")
        if status not in ("resolved", "unresolved"):
            raise FactoryError(
                f"review round {round}: update {finding_id} has status '{status}' "
                "(expected 'resolved' or 'unresolved')"
            )
        updated.add(finding_id)
        finding.status_evidence = str(entry.get("evidence") or "")
        if status == "resolved":
            finding.status = "resolved"
            finding.status_round = round
            if finding.is_important:
                stats.resolved += 1
        else:
            stats.unresolved += 1

    def _merge_new(self, entry: dict, round: int, stats: MergeStats) -> None:
        what = f"review round {round}: new finding"
        pass_ = _require(entry, "pass", what)
        severity = _require(entry, "severity", what)
        title = _require(entry, "title", what)
        if pass_ not in PASSES:
            raise FactoryError(
                f"{what} '{title}': unknown pass '{pass_}' (expected one of {', '.join(PASSES)})"
            )
        if severity not in SEVERITIES:
            raise FactoryError(
                f"{what} '{title}': unknown severity '{severity}' (expected one of {', '.join(SEVERITIES)})"
            )
        file = str(entry.get("file") or "")
        detail = str(entry.get("detail") or "")
        evidence = str(entry.get("evidence") or "")
        key = finding_key(pass_, file, title)
        match = self._match_key(key)
        if match is not None:
            self._merge_into(match, round, detail, evidence, stats)
            return
        self.findings.append(
            Finding(
                id=self.next_id(),
                key=key,
                pass_=pass_,
                severity=severity,
                file=file,
                line=_opt_int(entry.get("line")),
                title=title,
                detail=detail,
                evidence=evidence,
                opened_round=round,
                status="open",
            )
        )
        if severity == "important":
            stats.new_important += 1
        else:
            stats.new_nits += 1

    @staticmethod
    def _merge_into(
        match: Finding, round: int, detail: str, evidence: str, stats: MergeStats
    ) -> None:
        if match.status == "open":
            stats.merged_duplicates += 1
        elif match.status == "dismissed":
            stats.reraised_dropped += (
                1  # an adjudicated finding cannot come back as new (design rule 5)
            )
        else:  # resolved -> the diff regressed it
            match.status = "open"
            match.status_round = round
            match.status_evidence = evidence
            match.detail = detail
            match.evidence = evidence
            stats.reopened += 1

    def _match_key(self, key: str) -> Finding | None:
        """The one entry carrying `key`, preferring open, then dismissed, then resolved (an appended finding
        never duplicates a key, so at most one entry matches; the order only makes a hand-edited ledger
        deterministic)."""
        for status in ("open", "dismissed", "resolved"):
            for finding in self.findings:
                if finding.key == key and finding.status == status:
                    return finding
        return None

    def merged_copy(self, review_output: dict, round: int) -> tuple[Ledger, MergeStats]:
        """Deep-copy self, merge_review on the copy, return (copy, stats). Lets a gate reject the round without
        mutating the committed ledger."""
        candidate = _copy.deepcopy(self)
        return candidate, candidate.merge_review(review_output, round)

    def dismiss(self, id: str, reason: str, round: int | None) -> Finding:
        """status=dismissed, dismissed_reason=reason, status_round=round. FactoryError if id unknown or not open."""
        finding = self.get(id)
        if finding is None:
            known = ", ".join(f.id for f in self.findings) or "(none)"
            raise FactoryError(f"unknown finding {id}", hint=f"findings in the ledger: {known}")
        if finding.status != "open":
            raise FactoryError(
                f"finding {id} is already {finding.status}; only open findings can be dismissed"
            )
        if not reason or not reason.strip():
            raise FactoryError(f"dismissing {id} requires a reason; it is recorded in the ledger")
        finding.status = "dismissed"
        finding.dismissed_reason = reason.strip()
        finding.status_round = round
        return finding

    def render_markdown(self) -> str:
        """Table: id | severity | pass | status | file:line | title — used in the PR review comment."""
        if not self.findings:
            return "_No findings._"
        rows = [
            "| id | severity | pass | status | file:line | title |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for f in self.findings:
            where = f"{f.file}:{f.line}" if f.file and f.line is not None else (f.file or "-")
            rows.append(
                f"| {f.id} | {f.severity} | {f.pass_} | {f.status} | {_cell(where)} | {_cell(f.title)} |"
            )
        return "\n".join(rows)


def _entries(review_output: dict, name: str, round: int) -> list[dict]:
    if not isinstance(review_output, dict):
        raise FactoryError(f"review round {round}: output is not an object")
    value = review_output.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise FactoryError(f"review round {round}: '{name}' is not an array")
    for entry in value:
        if not isinstance(entry, dict):
            raise FactoryError(f"review round {round}: '{name}' contains a non-object entry")
    return value


def _cell(text: str) -> str:
    """One markdown table cell: no pipes, no line breaks."""
    return _WS.sub(" ", str(text).replace("|", "\\|")).strip()


# ---------------------------------------------------------------- intent snapshot


def render_intent(issue: dict, snapshot_at: str) -> tuple[str, str]:
    """Build intent.md from a `gh issue view` JSON dict (keys: number, title, body, url, labels[{name}]).

    Returns (markdown, body_sha256). Layout:
        # Intent: <title>
        - number, url, labels, snapshot_at, sha256 (of the body)
        ---
        <body verbatim>

    `labels` accepts either GitHub's `[{"name": ...}]` shape or a plain list of names (gh.Issue.labels).
    """
    if not isinstance(issue, dict) or _opt_int(issue.get("number")) is None:
        raise FactoryError("issue snapshot has no number; cannot render intent.md")
    number = _opt_int(issue.get("number"))
    title = str(issue.get("title") or "").strip() or "(no title)"
    body = str(issue.get("body") or "")
    url = str(issue.get("url") or "")
    labels = _label_names(issue.get("labels"))
    digest = sha256_text(body)
    lines = [
        f"# Intent: {title}",
        "",
        f"- number: {number}",
        f"- url: {url}",
        f"- labels: {', '.join(labels) if labels else '(none)'}",
        f"- snapshot_at: {snapshot_at}",
        f"- sha256: {digest}",
        "",
        "---",
        "",
        body.strip("\n"),
        "",
    ]
    return "\n".join(lines), digest


def _label_names(labels) -> list[str]:
    if not isinstance(labels, list):
        return []
    names = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            names.append(str(name))
    return names


def render_spec_md(markdown: str, open_questions: list[str]) -> str:
    """`markdown` + "\n\n## Open questions\n" + bullet list, or "None." when empty (design §10)."""
    questions = [str(q).strip() for q in (open_questions or []) if str(q).strip()]
    body = str(markdown).rstrip("\n")
    lines = [body, "", "## Open questions", ""]
    lines.extend([f"- {q}" for q in questions] if questions else ["None."])
    lines.append("")
    return "\n".join(lines)


def parse_open_questions(spec_md: str) -> list[str]:
    """Inverse of render_spec_md's trailer: bullets under the final `## Open questions` heading ("None." -> [])."""
    lines = str(spec_md).splitlines()
    start = None
    for index, line in enumerate(lines):
        if _HEADING.match(line):
            start = index
    if start is None:
        return []
    questions: list[str] = []
    for line in lines[start + 1 :]:
        if _ANY_HEADING.match(line):
            break
        bullet = _BULLET.match(line)
        if not bullet:
            continue
        text = bullet.group(1).strip()
        if not text or text.rstrip(".").strip().lower() == "none":
            continue
        questions.append(text)
    return questions


# ---------------------------------------------------------------- host-local transients (design §7)


@dataclass
class RunLock:
    """`.factory/run/<issue>.json`: in-flight stage, pid, started_at, worktree path.
    Presence with a live pid is the per-issue lock; a dead pid means an interrupted stage.

    `last_error` carries the previous attempt's failure into the next attempt's stage_note (prompts.py);
    it is written by whoever holds the lock and read back by prepare() after an interrupted stage.
    """

    issue: int
    stage: str
    pid: int
    started_at: str
    worktree: str
    last_error: str | None = None

    @staticmethod
    def path(factory_dir: Path, issue: int) -> Path:
        return factory_dir / "run" / f"{issue}.json"

    @classmethod
    def read(cls, factory_dir: Path, issue: int) -> RunLock | None:
        data = _read_json_map(RunLock.path(factory_dir, issue))
        if not data or _opt_int(data.get("pid")) is None:
            return None
        return cls(
            issue=_opt_int(data.get("issue")) or int(issue),
            stage=str(data.get("stage") or ""),
            pid=_opt_int(data.get("pid")) or 0,
            started_at=str(data.get("started_at") or ""),
            worktree=str(data.get("worktree") or ""),
            last_error=data.get("last_error"),
        )

    def write(self, factory_dir: Path, *, stage: str | None = None) -> None:
        if stage is not None:
            self.stage = stage
        atomic_write_json(RunLock.path(factory_dir, self.issue), asdict(self))

    @staticmethod
    def clear(factory_dir: Path, issue: int) -> None:
        try:
            RunLock.path(factory_dir, issue).unlink(missing_ok=True)
        except OSError as exc:
            raise FactoryError(
                f"could not clear {RunLock.path(factory_dir, issue)}: {exc}"
            ) from exc

    def pid_alive(self) -> bool:
        """os.kill(pid, 0) semantics; PermissionError counts as alive."""
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # someone else's process: alive, just not ours to signal
        except OSError:
            return False
        return True


def read_poll_journal(factory_dir: Path) -> dict:
    """`.factory/poll.json`: {"<issue>": {"failures": int, "sha": str, "last_error": str, "capped_comment_sha": str|None}};
    missing -> {}. `failures` counts consecutive exit-1s AT `sha`; any HEAD movement replaces the entry (poll.py)."""
    return _read_json_map(factory_dir / "poll.json")


def write_poll_journal(factory_dir: Path, journal: dict) -> None:
    atomic_write_json(factory_dir / "poll.json", journal)


def read_doctor_records(factory_dir: Path) -> dict:
    """`.factory/doctor.json`: {"<harness>:<auth>": {"ok", "factory_version", "cli_version", "at", "checks": [...]}}
    — records for different harness/auth combinations coexist. Missing -> {}. `doctor` writes a record only on a
    pass and stamps `ok: true`; `doctor.doctor_record_is_current` (poll's preflight) requires all of `ok`,
    `factory_version` and `cli_version`."""
    return _read_json_map(factory_dir / "doctor.json")


def write_doctor_record(factory_dir: Path, harness: str, auth: str, record: dict) -> None:
    """Merge one combination's record into doctor.json (atomic)."""
    records = read_doctor_records(factory_dir)
    records[f"{harness}:{auth}"] = dict(record)
    atomic_write_json(factory_dir / "doctor.json", records)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
