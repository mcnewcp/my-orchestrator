"""GitHub via the `gh` CLI (design §5 gh.py, §7 "The GitHub rule").

Reads happen for exactly three purposes: issue snapshot at spec, idempotency of the factory's own
writes (does a PR / comment already exist), and `poll`'s label query. `gh` runs with the parent
environment (it needs GH_TOKEN or its own login) — harness subprocesses never get that environment.

All calls: subprocess.run(["gh", ...], cwd=checkout_root, shell=False). Non-zero -> FactoryError with stderr tail.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import FactoryError

# Markers make every PR write idempotent (pr_comment scans existing comments for the marker — authoritative;
# nothing in state.json caches this). `sha` in GATE_MARKER is state.outcome_sha, which is stable across park()'s
# own state-only commit, so a re-park at the same HEAD finds its marker.
GATE_MARKER = "<!-- factory:gate:{gate}:{sha} -->"
REVIEW_MARKER = "<!-- factory:review:{round}:{sha} -->"
SUMMARY_MARKER = "<!-- factory:summary:{sha} -->"
CAPPED_MARKER = "<!-- factory:capped:{sha} -->"

LIST_LIMIT = 200  # --limit on every list query; v0 never pages
_STDERR_TAIL = 600  # characters of stderr quoted in a FactoryError
_CHECKS_TAIL_CHARS = 8000  # check output quoted in the finalize summary comment
_ERROR_TAIL_CHARS = 2000  # error text quoted in the poll "capped" comment

# Phrases that mean "the write you asked for is already the state of the world".
_ALREADY_READY = ("already marked as ready", "already ready", "not a draft")
_ALREADY_CLOSED = ("already closed",)
_LABEL_ABSENT = ("not found", "does not exist", "not applied", "no such label")


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str]
    state: str

    def to_dict(self) -> dict:
        """The shape `gh issue view --json ...` returns, i.e. what state.render_intent consumes
        (labels as `[{"name": ...}]`)."""
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "labels": [{"name": name} for name in self.labels],
            "state": self.state,
        }


@dataclass
class PullRequest:
    number: int
    url: str
    is_draft: bool
    state: str  # OPEN | CLOSED | MERGED
    head: str


class GitHub:
    def __init__(self, checkout_root: Path):
        self.root = checkout_root

    # --- plumbing
    def _attempt(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        """Run `gh <args>` and return the completed process, whatever its exit code."""
        try:
            return subprocess.run(
                ["gh", *args],
                cwd=self.root,
                shell=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise FactoryError(
                "`gh` is not on PATH",
                hint="install the GitHub CLI and run `gh auth login`; `factory doctor` checks it",
            ) from exc
        except OSError as exc:
            raise FactoryError(
                f"could not run `gh {' '.join(args)}` in {self.root}: {exc}"
            ) from exc

    def _run(self, args: list[str]) -> str:
        """`gh <args>`; returns stdout. Non-zero exit -> FactoryError with the stderr tail. Calls whose
        failure can mean "already done" use `_attempt` instead, so they can read gh's own message."""
        proc = self._attempt(args)
        if proc.returncode != 0:
            raise FactoryError(self._failure_message(args, proc))
        return proc.stdout

    def _failure_message(self, args: Sequence[str], proc: subprocess.CompletedProcess) -> str:
        detail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL:]
        message = f"`gh {' '.join(args)}` failed (exit {proc.returncode}) in {self.root}"
        return f"{message}: {detail}" if detail else message

    def _json(self, args: list[str]):
        out = self._run(args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise FactoryError(
                f"`gh {' '.join(args)}` did not return JSON: {out.strip()[:200]!r}"
            ) from exc

    def repo_slug(self) -> str:
        """`gh repo view --json nameWithOwner`."""
        data = self._json(["repo", "view", "--json", "nameWithOwner"])
        slug = (data or {}).get("nameWithOwner")
        if not slug:
            raise FactoryError(f"`gh repo view` returned no nameWithOwner for {self.root}")
        return str(slug)

    def auth_ok(self) -> tuple[bool, str]:
        """`gh auth status`; (ok, one-line detail)."""
        proc = self._attempt(["auth", "status"])
        detail = _first_line(proc.stdout) or _first_line(proc.stderr)
        if proc.returncode == 0:
            return True, detail or "gh is authenticated"
        return False, detail or f"`gh auth status` exited {proc.returncode}"

    # --- issues
    def issue(self, number: int) -> Issue:
        """`gh issue view N --json number,title,body,url,labels,state`."""
        fields = "number,title,body,url,labels,state"
        data = self._json(["issue", "view", str(number), "--json", fields])
        if not isinstance(data, dict) or "number" not in data:
            raise FactoryError(f"`gh issue view {number}` returned no issue for {self.root}")
        return Issue(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            url=str(data.get("url") or ""),
            labels=_label_names(data.get("labels")),
            state=str(data.get("state") or "").upper(),
        )

    def list_open_issues_with_label(self, label: str) -> list[int]:
        """Ascending issue numbers (design §12 step 2)."""
        args = ["issue", "list", "--state", "open", "--label", label]
        args += ["--json", "number", "--limit", str(LIST_LIMIT)]
        rows = self._json(args)
        numbers = {int(row["number"]) for row in rows or [] if row.get("number") is not None}
        return sorted(numbers)

    def remove_label(self, number: int, label: str) -> None:
        """Idempotent: absent label is not an error."""
        args = ["issue", "edit", str(number), "--remove-label", label]
        proc = self._attempt(args)
        if proc.returncode == 0:
            return
        if _mentions(proc, _LABEL_ABSENT, needle=label):
            return
        raise FactoryError(self._failure_message(args, proc))

    def ensure_label(self, label: str, *, color: str = "5319e7", description: str = "") -> bool:
        """Create the label if absent. Returns True if created."""
        rows = self._json(["label", "list", "--json", "name", "--limit", str(LIST_LIMIT)])
        existing = {str(row.get("name") or "").casefold() for row in rows or []}
        if label.casefold() in existing:
            return False
        args = ["label", "create", label, "--color", color]
        if description:
            args += ["--description", description]
        args.append("--force")  # a label created between the list and the create is not an error
        self._run(args)
        return True

    # --- pull requests
    def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        """`gh pr list --head <branch> --state all --json ...`; prefer OPEN, else most recent."""
        args = ["pr", "list", "--head", branch, "--state", "all"]
        args += ["--json", "number,url,isDraft,state,headRefName"]
        rows = self._json(args)
        prs = [_pr_from_json(row) for row in rows or [] if row.get("number") is not None]
        prs = [pr for pr in prs if not pr.head or pr.head == branch]
        if not prs:
            return None
        open_prs = [pr for pr in prs if pr.state == "OPEN"]
        return max(open_prs or prs, key=lambda pr: pr.number)

    def create_draft_pr(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        """Idempotent: if an OPEN PR for branch exists, return it instead (design §15)."""
        existing = self.find_pr_for_branch(branch)
        if existing is not None and existing.state == "OPEN":
            return existing
        with _body_file(body) as path:
            args = ["pr", "create", "--draft", "--head", branch, "--base", base]
            args += ["--title", title, "--body-file", str(path)]
            out = self._run(args)
        created = self.find_pr_for_branch(branch)
        if created is not None and created.state == "OPEN":
            return created
        url = _first_url(out)
        number = _pr_number_from_url(url)
        if number is None:
            raise FactoryError(
                f"`gh pr create` for {branch} reported no pull request URL: {out.strip()[:200]!r}"
            )
        return PullRequest(number=number, url=url, is_draft=True, state="OPEN", head=branch)

    def pr_comments(self, number: int) -> list[str]:
        """Bodies of existing comments (for marker-based idempotency)."""
        data = self._json(["pr", "view", str(number), "--json", "comments"])
        comments = (data or {}).get("comments") or []
        return [str(c.get("body") or "") for c in comments]

    def pr_comment(self, number: int, body: str, *, marker: str | None = None) -> bool:
        """Post a comment unless one containing `marker` already exists. Returns True if posted."""
        if marker and any(marker in existing for existing in self.pr_comments(number)):
            return False
        with _body_file(body) as path:
            self._run(["pr", "comment", str(number), "--body-file", str(path)])
        return True

    def pr_ready(self, number: int) -> None:
        """`gh pr ready N`; idempotent (already-ready is not an error)."""
        args = ["pr", "ready", str(number)]
        proc = self._attempt(args)
        if proc.returncode == 0 or _mentions(proc, _ALREADY_READY):
            return
        raise FactoryError(self._failure_message(args, proc))

    def pr_close(self, number: int, comment: str | None = None) -> None:
        """Idempotent."""
        args = ["pr", "close", str(number)]
        if comment:
            args += ["--comment", comment]
        proc = self._attempt(args)
        if proc.returncode == 0 or _mentions(proc, _ALREADY_CLOSED):
            return
        raise FactoryError(self._failure_message(args, proc))


# ---------------------------------------------------------------- helpers


@contextlib.contextmanager
def _body_file(body: str) -> Iterator[Path]:
    """A temp file holding `body`, for `--body-file` (never `--body` with multi-KB markdown)."""
    with tempfile.TemporaryDirectory(prefix="factory-gh-") as tmp:
        path = Path(tmp) / "body.md"
        path.write_text(body, encoding="utf-8")
        yield path


def _mentions(proc: subprocess.CompletedProcess, phrases: Sequence[str], needle: str = "") -> bool:
    """True when gh's own output says the write was already done. `needle` (a label name) must appear
    too, so an unrelated failure is never swallowed."""
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".casefold()
    if needle and needle.casefold() not in text:
        return False
    return any(phrase in text for phrase in phrases)


def _label_names(labels) -> list[str]:
    names = []
    for label in labels or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            names.append(str(name))
    return names


def _pr_from_json(row: dict) -> PullRequest:
    return PullRequest(
        number=int(row["number"]),
        url=str(row.get("url") or ""),
        is_draft=bool(row.get("isDraft", False)),
        state=str(row.get("state") or "").upper(),
        head=str(row.get("headRefName") or ""),
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _first_url(text: str) -> str:
    for token in (text or "").split():
        if token.startswith("http"):
            return token.strip()
    return ""


def _pr_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    return int(tail) if tail.isdigit() else None


def _short(sha: str) -> str:
    return sha[:7] if sha else "unknown"


def _fence(text: str, limit: int) -> str:
    body = (text or "").rstrip()
    if not body:
        return "_(no output)_"
    if len(body) > limit:
        body = "… (truncated)\n" + body[-limit:]
    return f"```\n{body}\n```"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


_STAT_LABELS = {
    "important_open": "Important open",
    "important_resolved": "Important resolved this round",
    "new_important": "New important",
    "new_nits": "New nits",
    "nits": "New nits",
    "unresolved": "Still unresolved",
    "merged_duplicates": "Merged into an open finding",
    "reraised_dropped": "Re-raised after dismissal (dropped)",
    "reopened": "Reopened as regression",
    "missing_updates": "Findings the reviewer did not address",
    "diff_truncated": "Diff truncated",
}


def _stat_row(key: str, value) -> tuple[str, str]:
    label = _STAT_LABELS.get(key, key.replace("_", " ").capitalize())
    if isinstance(value, list):
        return label, ", ".join(str(v) for v in value) if value else "none"
    if isinstance(value, bool):
        return label, "yes" if value else "no"
    return label, str(value)


# ---------------------------------------------------------------- rendered comment bodies


def render_gate_comment(gate: str, what_clears_it: str, sha: str, issue: int) -> str:
    """PR comment body for an exit-2 gate (design §9): names the gate and what clears it; includes GATE_MARKER."""
    lines = [
        GATE_MARKER.format(gate=gate, sha=sha),
        "",
        f"## Factory stopped — needs human (`{gate}`)",
        "",
        f"Issue #{issue} · commit `{_short(sha)}`",
        "",
        "**What clears it**",
        "",
        (what_clears_it or "").strip() or "_(no detail recorded)_",
        "",
        f"The run stays parked until HEAD moves on this branch — `factory accept {issue}`, "
        f'`factory dismiss {issue} <id> "reason"`, a commit of your own, or a `--force` re-run — '
        f"then `factory run {issue}` (or the next `poll` tick) picks it up.",
        "",
        f"`factory status {issue}` prints the full local state.",
    ]
    return "\n".join(lines) + "\n"


def render_review_comment(round: int, sha: str, summary: str, ledger_md: str, stats: dict) -> str:
    """PR comment body after a review round: summary, rendered ledger, counts; includes REVIEW_MARKER."""
    lines = [
        REVIEW_MARKER.format(round=round, sha=sha),
        "",
        f"## Factory review — round {round}",
        "",
        f"Reviewed commit `{_short(sha)}`",
        "",
        (summary or "").strip() or "_(the reviewer returned no summary)_",
        "",
    ]
    rows = [_stat_row(key, value) for key, value in (stats or {}).items() if value is not None]
    if rows:
        lines += _table(("metric", "count"), rows) + [""]
    lines += ["### Ledger", "", (ledger_md or "").strip() or "_No findings._"]
    return "\n".join(lines) + "\n"


def render_summary_comment(state: dict, ledger_md: str, checks_tail: str, sha: str) -> str:
    """Finalize summary (design §2 step 6): spec/plan pointers, check output tail, ledger, harness + versions;
    includes SUMMARY_MARKER."""
    data = state.to_dict() if hasattr(state, "to_dict") else dict(state or {})
    issue = (data.get("issue") or {}).get("number")
    work = f"work/{issue}" if issue is not None else "work/<issue>"
    stages = data.get("stages") or {}
    reviews = data.get("reviews") or []
    last_review = reviews[-1] if reviews else {}

    lines = [
        SUMMARY_MARKER.format(sha=sha),
        "",
        "## Factory run complete — ready for review",
        "",
        f"Issue #{issue} · branch `{data.get('branch') or '(unknown)'}` · commit `{_short(sha)}`",
        "",
        f"**Artifacts on this branch:** `{work}/intent.md`, `{work}/spec.md`, `{work}/plan.md`, "
        f"`{work}/state.json`, `{work}/findings.json`",
        "",
        "### Stages",
        "",
    ]
    stage_rows = [
        (
            name,
            record.get("harness", ""),
            record.get("model", ""),
            record.get("cli_version", ""),
            record.get("auth", ""),
            record.get("at", ""),
        )
        for name, record in stages.items()
        if isinstance(record, dict)
    ]
    lines += (
        _table(("stage", "harness", "model", "cli", "auth", "at"), stage_rows)
        if stage_rows
        else ["_(no stages recorded)_"]
    )
    lines += ["", "### Review", ""]
    review_rows = [
        (
            r.get("round", ""),
            f"`{_short(str(r.get('sha') or ''))}`",
            r.get("important_open", ""),
            r.get("important_resolved", ""),
            r.get("nits", ""),
        )
        for r in reviews
        if isinstance(r, dict)
    ]
    lines += (
        _table(("round", "commit", "important open", "resolved", "nits"), review_rows)
        if review_rows
        else ["_(no review rounds)_"]
    )
    lines += [
        "",
        f"Fix rounds: {data.get('fix_rounds', 0)} · "
        f"open Important at the last review: {last_review.get('important_open', 0)}",
        "",
        "### Findings",
        "",
        (ledger_md or "").strip() or "_No findings._",
        "",
        "### Checks",
        "",
        _fence(checks_tail, _CHECKS_TAIL_CHARS),
        "",
        "The factory never merges: review, approve and merge as usual.",
    ]
    return "\n".join(lines) + "\n"


def render_capped_comment(issue: int, sha: str, failures: int, last_error: str) -> str:
    """poll: posted once when an issue hits max_consecutive_failures at `sha` (design §12 has no comment for exit 1;
    without this an unattended issue goes dark). Says the count clears when HEAD moves. Includes CAPPED_MARKER."""
    lines = [
        CAPPED_MARKER.format(sha=sha),
        "",
        "## Factory paused — repeated failures",
        "",
        f"`factory run {issue}` failed {failures} time(s) in a row at commit `{_short(sha)}`, "
        "so `poll` will skip this issue from now on.",
        "",
        "**Last error**",
        "",
        _fence(last_error, _ERROR_TAIL_CHARS),
        "",
        "The counter is per commit: any new commit on this branch — a hand fix, "
        f'`factory accept {issue}`, `factory dismiss {issue} <id> "reason"` — clears it and the next '
        "`poll` tick retries. Use `factory abandon "
        f"{issue}` to stop the run instead of closing the PR by hand.",
    ]
    return "\n".join(lines) + "\n"
