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

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import FactoryError

SEVERITIES = ("important", "nit")
PASSES = ("bugs", "security", "compliance")
STATUSES = ("open", "resolved", "dismissed")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def work_dir(worktree: Path, issue: int) -> Path:
    return worktree / "work" / str(issue)


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace; creates parent dirs."""
    raise NotImplementedError


def atomic_write_json(path: Path, obj) -> None:
    """Pretty JSON (indent=2, sort_keys=False, trailing newline) via atomic_write_text."""
    raise NotImplementedError


# ---------------------------------------------------------------- state.json


@dataclass
class StageRecord:
    start_commit: str  # HEAD when the stage began; what `--force` on this stage rewinds to (spec: base sha)
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
    fix_rounds_at: int = 0  # state.fix_rounds when this review ran; no_progress() compares consecutive rounds
    diff_truncated: bool = False


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
        raise NotImplementedError

    @classmethod
    def load_or_none(cls, worktree: Path, issue: int) -> State | None:
        raise NotImplementedError

    def save(self, worktree: Path) -> Path:
        """Atomic write to State.path(worktree, self.issue['number']). Returns the path."""
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, d: dict) -> State:
        raise NotImplementedError

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
        raise NotImplementedError


def is_parked(state: State, head: str, head_parent: str | None, head_changed_paths: list[str]) -> bool:
    """Design §11 "parked stays parked", pure: True when state.outcome startswith "needs_human" and
    (head == state.outcome_sha or (head_parent == state.outcome_sha and
     head_changed_paths == [f"work/{state.number}/state.json"])). No git access; callers supply the three facts
    (stages.is_parked and poll.classify both use repo.head/parent/changed_paths_of_commit + this)."""
    raise NotImplementedError


def no_progress(state: State) -> bool:
    """Design §11: the latest review followed a fix and resolved zero Important findings while some stay open.
    See the module docstring for the exact predicate over reviews[-1] and reviews[-2]."""
    raise NotImplementedError


def state_only_paths(issue: int) -> list[str]:
    return [f"work/{issue}/state.json"]


# ---------------------------------------------------------------- findings.json (the ledger)


def finding_key(pass_: str, file: str, title: str) -> str:
    """sha1(pass|file|normalized title). Normalization: lowercase, collapse whitespace,
    strip punctuation except word chars and spaces."""
    raise NotImplementedError


def normalize_title(title: str) -> str:
    raise NotImplementedError


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
        raise NotImplementedError

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        raise NotImplementedError

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
    missing_updates: list[str] = field(default_factory=list)  # open finding ids the reviewer did not address


@dataclass
class Ledger:
    findings: list[Finding] = field(default_factory=list)

    @staticmethod
    def path(worktree: Path, issue: int) -> Path:
        return work_dir(worktree, issue) / "findings.json"

    @classmethod
    def load(cls, worktree: Path, issue: int) -> Ledger:
        """Missing file -> empty ledger."""
        raise NotImplementedError

    def save(self, worktree: Path, issue: int) -> Path:
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, d: dict) -> Ledger:
        raise NotImplementedError

    # --- queries
    def get(self, id: str) -> Finding | None:
        raise NotImplementedError

    def open(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "open"]

    def open_important(self) -> list[Finding]:
        return [f for f in self.open() if f.is_important]

    def next_id(self) -> str:
        raise NotImplementedError

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
        """
        raise NotImplementedError

    def merged_copy(self, review_output: dict, round: int) -> tuple[Ledger, MergeStats]:
        """Deep-copy self, merge_review on the copy, return (copy, stats). Lets a gate reject the round without
        mutating the committed ledger."""
        raise NotImplementedError

    def dismiss(self, id: str, reason: str, round: int | None) -> Finding:
        """status=dismissed, dismissed_reason=reason, status_round=round. FactoryError if id unknown or not open."""
        raise NotImplementedError

    def render_markdown(self) -> str:
        """Table: id | severity | pass | status | file:line | title — used in the PR review comment."""
        raise NotImplementedError


# ---------------------------------------------------------------- intent snapshot


def render_intent(issue: dict, snapshot_at: str) -> tuple[str, str]:
    """Build intent.md from a `gh issue view` JSON dict (keys: number, title, body, url, labels[{name}]).

    Returns (markdown, body_sha256). Layout:
        # Intent: <title>
        - number, url, labels, snapshot_at, sha256 (of the body)
        ---
        <body verbatim>
    """
    raise NotImplementedError


def render_spec_md(markdown: str, open_questions: list[str]) -> str:
    """`markdown` + "\n\n## Open questions\n" + bullet list, or "None." when empty (design §10)."""
    raise NotImplementedError


def parse_open_questions(spec_md: str) -> list[str]:
    """Inverse of render_spec_md's trailer: bullets under the final `## Open questions` heading ("None." -> [])."""
    raise NotImplementedError


# ---------------------------------------------------------------- host-local transients (design §7)


@dataclass
class RunLock:
    """`.factory/run/<issue>.json`: in-flight stage, pid, started_at, worktree path.
    Presence with a live pid is the per-issue lock; a dead pid means an interrupted stage."""

    issue: int
    stage: str
    pid: int
    started_at: str
    worktree: str

    @staticmethod
    def path(factory_dir: Path, issue: int) -> Path:
        return factory_dir / "run" / f"{issue}.json"

    @classmethod
    def read(cls, factory_dir: Path, issue: int) -> RunLock | None:
        raise NotImplementedError

    def write(self, factory_dir: Path) -> None:
        raise NotImplementedError

    @staticmethod
    def clear(factory_dir: Path, issue: int) -> None:
        raise NotImplementedError

    def pid_alive(self) -> bool:
        """os.kill(pid, 0) semantics; PermissionError counts as alive."""
        raise NotImplementedError


def read_poll_journal(factory_dir: Path) -> dict:
    """`.factory/poll.json`: {"<issue>": {"failures": int, "sha": str, "last_error": str, "capped_comment_sha": str|None}};
    missing -> {}. `failures` counts consecutive exit-1s AT `sha`; any HEAD movement replaces the entry (poll.py)."""
    raise NotImplementedError


def write_poll_journal(factory_dir: Path, journal: dict) -> None:
    raise NotImplementedError


def read_doctor_records(factory_dir: Path) -> dict:
    """`.factory/doctor.json`: {"<harness>:<auth>": {"factory_version", "cli_version", "at", "checks": [...]}} —
    records for different harness/auth combinations coexist. Missing -> {}."""
    raise NotImplementedError


def write_doctor_record(factory_dir: Path, harness: str, auth: str, record: dict) -> None:
    """Merge one combination's record into doctor.json (atomic)."""
    raise NotImplementedError


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_WS = re.compile(r"\s+")
_ = FactoryError
