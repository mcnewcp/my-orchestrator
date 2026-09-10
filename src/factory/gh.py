"""GitHub writes and their idempotency checks; no workflow decisions."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .repo import issue_number


class GitHub:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(["gh", *args], cwd=self.root, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise RuntimeError(f"Cannot run gh: {exc}") from exc
        if result.returncode:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout.strip()

    def _json(self, *args: str):
        try:
            return json.loads(self._run(*args))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh returned invalid JSON for {' '.join(args[:3])}") from exc

    def issue(self, number: int) -> dict:
        return self._json("issue", "view", str(issue_number(number)), "--json", "number,url,title,body,labels,createdAt,updatedAt")

    def issues(self, label: str) -> list[dict]:
        # gh paginates up to --limit; refuse truncation rather than skip intake.
        limit = 1000000
        issues = self._json("issue", "list", "--state", "open", "--label", label, "--limit", str(limit), "--json", "number")
        if len(issues) == limit:
            raise RuntimeError("Too many factory issues to enumerate safely")
        return sorted(issues, key=lambda item: item["number"])

    def ensure_label(self, label: str) -> None:
        output = self._run("label", "list", "--search", label, "--limit", "1000000", "--json", "name")
        # gh 2.100.0 returns empty stdout for a successful search with no matches.
        try:
            labels = json.loads(output) if output.strip() else []
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh returned invalid JSON for label list") from exc
        if not any(item["name"] == label for item in labels):
            self._run("label", "create", label, "--color", "5319E7", "--description", "Selected for the software factory")

    def remove_label(self, issue: int, label: str) -> None:
        labels = self._json("issue", "view", str(issue_number(issue)), "--json", "labels")["labels"]
        if any(item["name"] == label for item in labels):
            self._run("issue", "edit", str(issue), "--remove-label", label)

    def ensure_pr(self, issue: int, branch: str, base: str, title: str, body: str) -> dict:
        if branch != f"factory/{issue_number(issue)}":
            raise RuntimeError("Factory PRs must use the issue's factory branch")
        prs = self._json("pr", "list", "--head", branch, "--state", "all", "--limit", "100", "--json", "number,url,state,baseRefName")
        if prs:
            if len(prs) != 1:
                raise RuntimeError(f"Multiple PRs exist for {branch}; resolve manually")
            pr = prs[0]
            if pr["state"] != "OPEN":
                raise RuntimeError(f"PR #{pr['number']} is {pr['state'].lower()}; use factory abandon to close the run")
            if pr.get("baseRefName", base) != base:
                raise RuntimeError(f"PR #{pr['number']} targets a different base branch")
            return {"number": pr["number"], "url": pr["url"]}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as handle:
            handle.write(body)
            handle.flush()
            self._run("pr", "create", "--draft", "--head", branch, "--base", base, "--title", title, "--body-file", handle.name)
        pr = self._json("pr", "view", branch, "--json", "number,url")
        return {"number": pr["number"], "url": pr["url"]}

    def comment(self, pr_number: int, body: str, marker: str) -> None:
        """Only this account's matching marker may deduplicate a factory write."""
        pr_number = issue_number(pr_number)
        login = self._json("api", "user")["login"]
        pages = self._json("api", f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments?per_page=100", "--paginate", "--slurp")
        marker_line = marker if marker.startswith("<!--") else f"<!-- {marker} -->"
        for page in pages:
            for comment in page:
                if comment.get("user", {}).get("login") == login and marker_line in comment.get("body", "").splitlines():
                    return
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as handle:
            handle.write(f"{body.rstrip()}\n\n{marker_line}\n")
            handle.flush()
            self._run("pr", "comment", str(pr_number), "--body-file", handle.name)

    def ready(self, pr: int) -> None:
        number = str(issue_number(pr))
        current = self._json("pr", "view", number, "--json", "state,isDraft")
        if current["state"] != "OPEN":
            raise RuntimeError(f"Cannot finalize PR #{pr}: it is {current['state'].lower()}")
        if current["isDraft"]:
            self._run("pr", "ready", number)

    def draft(self, pr: int) -> None:
        number = str(issue_number(pr))
        current = self._json("pr", "view", number, "--json", "state,isDraft")
        if current["state"] != "OPEN":
            raise RuntimeError(f"Cannot rewrite PR #{pr}: it is {current['state'].lower()}")
        if not current["isDraft"]:
            self._run("pr", "ready", number, "--undo")

    def close(self, pr: int) -> None:
        number = str(issue_number(pr))
        current = self._json("pr", "view", number, "--json", "state")
        if current["state"] == "OPEN":
            self._run("pr", "close", number)
