"""GitHub facts and idempotent PR publication, transported only through gh."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.errors import Blocked

_PR_FIELDS = (
    "number,url,title,body,state,isDraft,headRefOid,headRefName,baseRefName,"
    "headRepository,headRepositoryOwner"
)
_FAILURES = {"failure", "error", "cancelled", "timed_out", "action_required", "neutral", "skipped"}


class GitHub:
    def __init__(self, repo: str, runner: Any = None, timeout: float = 60):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GitHub repository must be OWNER/REPO")
        self.repo, self.runner, self.timeout = repo, runner, timeout

    def _run(self, args: list[str], *, check: bool = True, timeout: float | None = None):
        argv = ["gh", *args]
        kwargs = {
            "cwd": Path.cwd(),
            "timeout": self.timeout if timeout is None else min(self.timeout, timeout),
            "check": False,
            "env": {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat"},
        }
        try:
            if self.runner is not None:
                result = self.runner.run(argv, **kwargs)
            else:
                result = subprocess.run(argv, **kwargs, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked(f"GitHub command could not finish: {exc}") from exc
        if check and result.returncode:
            raise Blocked(f"GitHub {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result

    def _json(
        self, args: list[str], *, deadline: float | None = None, paginated: bool = False
    ) -> Any:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise Blocked("Required CI timed out while fetching exact-revision evidence")
        result = self._run(args, timeout=remaining)
        try:
            if not paginated:
                return json.loads(result.stdout)

            def invalid_constant(value: str):
                raise ValueError(f"Invalid JSON constant: {value}")

            # Older gh releases concatenate page bodies and do not support --slurp.
            decoder = json.JSONDecoder(parse_constant=invalid_constant)
            pages, offset = [], 0
            raw = result.stdout
            while offset < len(raw):
                if raw[offset] in " \t\r\n":
                    offset += 1
                    continue
                page, offset = decoder.raw_decode(raw, offset)
                pages.append(page)
            if not pages:
                raise ValueError("Missing paginated response")
            return pages
        except (TypeError, ValueError) as exc:
            raise Blocked("GitHub returned invalid JSON") from exc

    def auth(self) -> None:
        self._run(["auth", "status", "--hostname", "github.com"])

    def issue(self, number: int) -> dict:
        if number < 1:
            raise ValueError("Issue number must be positive")
        issue = self._json(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,url",
            ]
        )
        if not isinstance(issue, dict) or not all(k in issue for k in ("title", "body", "url")):
            raise Blocked("GitHub returned an incomplete issue")
        return {**issue, "retrieved_at": datetime.now(UTC).isoformat()}

    def find_pr(self, branch: str) -> dict | None:
        prs = self._json(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "all",
                "--head",
                branch,
                "--limit",
                "100",
                "--json",
                _PR_FIELDS,
            ]
        )
        if not isinstance(prs, list):
            raise Blocked("GitHub returned an invalid PR list")
        matches = [pr for pr in prs if pr.get("headRefName") == branch]
        if len(matches) > 1:
            raise Blocked("Multiple PRs use the run branch; publication is ambiguous")
        return matches[0] if matches else None

    def pr(self, number: int) -> dict:
        pr = self._json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                _PR_FIELDS,
            ]
        )
        if not isinstance(pr, dict) or not all(
            field in pr for field in ("number", "headRefOid", "state", "isDraft", "body")
        ):
            raise Blocked("GitHub returned incomplete PR facts")
        return pr

    @staticmethod
    def marker(run_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("Invalid run ID for PR marker")
        return f"<!-- factory-run:{run_id} -->"

    def _verify_pr(self, pr: dict, branch: str, base: str, marker: str) -> dict:
        if (
            pr.get("headRefName") != branch
            or pr.get("baseRefName") != base
            or marker not in pr.get("body", "").splitlines()
        ):
            raise Blocked("Existing PR does not match the run ID, branch, and base")
        owner = (pr.get("headRepositoryOwner") or {}).get("login")
        head_repo = pr.get("headRepository") or {}
        if owner and head_repo.get("name"):
            if f"{owner}/{head_repo['name']}".lower() != self.repo.lower():
                raise Blocked("Existing PR points to a different head repository")
        if pr.get("state") != "OPEN":
            raise Blocked("The run PR has already closed or merged")
        return pr

    def create_draft(
        self, branch: str, base: str, title: str, body: str, *, run_id: str | None = None
    ) -> dict:
        # Branches are factory/<issue>/<run-id>; an explicit ID is convenient for callers.
        marker = self.marker(run_id or branch.rsplit("/", 1)[-1])
        if marker not in body.splitlines():
            body = body.rstrip() + f"\n\n{marker}\n"
        existing = self.find_pr(branch)
        if existing is not None:
            return self._verify_pr(existing, branch, base, marker)
        with tempfile.TemporaryDirectory(prefix="factory-pr-") as directory:
            body_file = Path(directory) / "body.md"
            body_file.write_text(body, encoding="utf-8")
            created = self._run(
                [
                    "pr",
                    "create",
                    "--repo",
                    self.repo,
                    "--draft",
                    "--head",
                    branch,
                    "--base",
                    base,
                    "--title",
                    title,
                    "--body-file",
                    str(body_file),
                    "--no-maintainer-edit",
                ],
                check=False,
            )
        # A disconnected response may hide a successful creation. Reconcile before retrying.
        existing = self.find_pr(branch)
        if existing is None:
            raise Blocked(f"Could not reconcile draft PR creation: {created.stderr.strip()}")
        return self._verify_pr(existing, branch, base, marker)

    def ready(self, number: int, *, expected_sha: str | None = None) -> dict:
        current = self.pr(number)
        if current["state"] != "OPEN":
            raise Blocked("Cannot mark a closed or merged PR ready")
        if expected_sha is not None and current["headRefOid"] != expected_sha:
            raise Blocked("PR head differs from the accepted candidate")
        if current["isDraft"]:
            self._run(["pr", "ready", str(number), "--repo", self.repo])
        updated = self.pr(number)
        if updated["state"] != "OPEN" or updated["isDraft"]:
            raise Blocked("PR did not become ready for review")
        if updated["headRefOid"] != current["headRefOid"]:
            raise Blocked("PR head changed while marking it ready")
        return updated

    def close(self, number: int) -> dict:
        current = self.pr(number)
        if current["state"] == "OPEN":
            self._run(["pr", "close", str(number), "--repo", self.repo])
        current = self.pr(number)
        if current["state"] == "OPEN":
            raise Blocked("PR remains open")
        return current

    def draft(self, number: int, *, run_id: str) -> dict:
        current = self.pr(number)
        if self.marker(run_id) not in current.get("body", "").splitlines():
            raise Blocked("Cannot restore draft: PR does not match this run")
        if current["state"] != "OPEN":
            raise Blocked("Cannot restore draft: PR has closed or merged")
        if not current["isDraft"]:
            self._run(["pr", "ready", str(number), "--repo", self.repo, "--undo"])
        updated = self.pr(number)
        if updated["state"] != "OPEN" or not updated["isDraft"]:
            raise Blocked("PR could not be restored to draft")
        return updated

    def checks(self, sha: str, required: list[str], *, deadline: float | None = None) -> list[dict]:
        """All required contexts must succeed on this immutable commit revision."""
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
            raise ValueError("CI requires a full commit SHA")
        if not required or len(set(required)) != len(required):
            raise Blocked("At least one unique required CI check name is mandatory")
        checks_pages = self._json(
            [
                "api",
                f"repos/{self.repo}/commits/{sha}/check-runs?filter=latest&per_page=100",
                "--paginate",
            ],
            deadline=deadline,
            paginated=True,
        )
        status_pages = self._json(
            [
                "api",
                f"repos/{self.repo}/commits/{sha}/status?per_page=100",
                "--paginate",
            ],
            deadline=deadline,
            paginated=True,
        )
        if not isinstance(checks_pages, list) or not isinstance(status_pages, list):
            raise Blocked("GitHub returned invalid paginated CI facts")
        sources: dict[str, list[dict]] = {name: [] for name in required}
        latest_checks: dict[tuple, dict] = {}
        for page in checks_pages:
            if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
                raise Blocked("GitHub returned invalid check runs")
            for check in page["check_runs"]:
                if check.get("head_sha") != sha:
                    raise Blocked("GitHub check evidence belongs to a different candidate SHA")
                if check.get("name") not in sources:
                    continue
                key = (check["name"], (check.get("app") or {}).get("id"))
                previous = latest_checks.get(key)
                if previous is None or check.get("id", 0) > previous.get("id", 0):
                    latest_checks[key] = check
        for check in latest_checks.values():
            conclusion = check.get("conclusion")
            state = "pending"
            if check.get("status") == "completed":
                state = "success" if conclusion == "success" else "failure"
            sources[check["name"]].append(
                {
                    "type": "check_run",
                    "state": state,
                    "conclusion": conclusion,
                    "id": check.get("id"),
                    "url": check.get("html_url"),
                }
            )
        latest_statuses: dict[str, dict] = {}
        for page in status_pages:
            if not isinstance(page, dict) or page.get("sha") != sha:
                raise Blocked("GitHub status evidence belongs to a different candidate SHA")
            if not isinstance(page.get("statuses"), list):
                raise Blocked("GitHub returned invalid commit statuses")
            for status in page["statuses"]:
                name = status.get("context")
                if name not in sources:
                    continue
                previous = latest_statuses.get(name)
                if previous is None or status.get("id", 0) > previous.get("id", 0):
                    latest_statuses[name] = status
        for name, status in latest_statuses.items():
            conclusion = status.get("state")
            state = (
                "success"
                if conclusion == "success"
                else ("failure" if conclusion in _FAILURES else "pending")
            )
            sources[name].append(
                {
                    "type": "status",
                    "state": state,
                    "conclusion": conclusion,
                    "id": status.get("id"),
                    "url": status.get("target_url"),
                }
            )
        results = []
        for name, evidence in sources.items():
            states = {source["state"] for source in evidence}
            state = (
                "missing"
                if not states
                else (
                    "failure"
                    if "failure" in states
                    else ("pending" if "pending" in states else "success")
                )
            )
            results.append(
                {
                    "name": name,
                    "state": state,
                    "conclusion": state,
                    "sha": sha,
                    "sources": evidence,
                }
            )
        return results

    def wait_ci(
        self, sha: str, required: list[str], timeout: float, poll_seconds: float = 5
    ) -> list[dict]:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            self._guard()
            checks = self.checks(sha, required, deadline=deadline if timeout > 0 else None)
            if all(check["state"] == "success" for check in checks):
                return checks
            failed = [check["name"] for check in checks if check["state"] == "failure"]
            if failed:
                raise Blocked(f"Required CI failed for {sha}: {', '.join(failed)}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending = [f"{c['name']}={c['state']}" for c in checks if c["state"] != "success"]
                raise Blocked(f"Required CI timed out for {sha}: {', '.join(pending)}")
            # Keep lease-loss/shutdown response bounded even with a long configured poll.
            next_poll = time.monotonic() + min(max(0.01, poll_seconds), remaining)
            while time.monotonic() < next_poll:
                self._guard()
                time.sleep(min(0.25, max(0, next_poll - time.monotonic())))

    def _guard(self) -> None:
        if self.runner is not None and getattr(self.runner, "guard", None):
            self.runner.guard()
