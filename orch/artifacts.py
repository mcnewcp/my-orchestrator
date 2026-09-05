"""Readers and writers for the scratch-directory artifacts (conventions.md section 4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_JSON = "run.json"
CONTRACT_MD = "contract.md"
CLARIFICATION_MD = "clarification.md"
ESCALATION_MD = "escalation.md"
SUMMARY_MD = "summary.md"
LEDGER_JSON = "ledger.json"
LOGS_DIR = "logs"

_AUDIT_RE = re.compile(r"^audit-(\d+)\.json$")
_REVIEW_RE = re.compile(r"^review-(\d+)\.md$")
_INT_RE = re.compile(r"^-?\d+$")


class ArtifactError(ValueError):
    """An artifact is missing, malformed, or has the wrong shape."""


# --------------------------------------------------------------------------- text


def read_text(path: Path) -> str:
    """Read a file as text with CRLF normalised to LF."""
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise ArtifactError(f"{path}: cannot read: {exc}") from exc


def write_text(path: Path, text: str) -> Path:
    """Write text to `path`, creating parent directories, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    return write_text(path, json.dumps(data, indent=2))


# ------------------------------------------------------------------- front matter


def _strip_comment(value: str) -> str:
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].strip()
    return value.strip()


def _scalar(value: str, where: str) -> Any:
    value = value.strip()
    if value.startswith('"') or value.startswith("'"):
        quote = value[0]
        end = value.find(quote, 1)
        while end != -1 and value[end - 1] == "\\":
            end = value.find(quote, end + 1)
        if end == -1:
            raise ArtifactError(f"{where}: unterminated quoted value: {value}")
        trailing = value[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise ArtifactError(
                f"{where}: text after the closing quote — an embedded {quote} must be "
                f"escaped as \\{quote}: {value}"
            )
        body = value[1:end]
        return body.replace('\\"', '"') if quote == '"' else body
    if value.startswith("["):
        depth, end = 0, -1
        for i, ch in enumerate(value):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            raise ArtifactError(f"{where}: unterminated list value: {value}")
        try:
            return json.loads(value[: end + 1])
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"{where}: list is not valid JSON: {value}") from exc
    value = _strip_comment(value)
    if _INT_RE.match(value):
        return int(value)
    if value in ("true", "false"):
        return value == "true"
    if value in ("null", "~", ""):
        return None
    return value


def parse_front_matter(text: str, source: Path | str) -> tuple[dict[str, Any], str]:
    """Parse the flat YAML front-matter subset; return (front matter, remaining body)."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        raise ArtifactError(f"{source}: expected YAML front matter opening '---'")
    try:
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ArtifactError(f"{source}: front matter is never closed with '---'") from None

    front: dict[str, Any] = {}
    block = lines[start + 1 : end]
    i = 0
    while i < len(block):
        raw = block[i].rstrip()
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0].isspace():
            raise ArtifactError(f"{source}: unexpected indentation in front matter: {raw!r}")
        key, sep, rest = raw.partition(":")
        if not sep:
            raise ArtifactError(f"{source}: front matter line is not 'key: value': {raw!r}")
        key = key.strip()
        rest = rest.strip()
        if rest == "" or rest.startswith("#"):
            mapping: dict[str, Any] = {}
            while i < len(block) and (block[i][:1].isspace() or not block[i].strip()):
                sub = block[i].rstrip()
                i += 1
                if not sub.strip() or sub.lstrip().startswith("#"):
                    continue
                sub_key, sub_sep, sub_val = sub.strip().partition(":")
                if not sub_sep:
                    raise ArtifactError(
                        f"{source}: nested front matter line is not 'key: value': {sub!r}"
                    )
                mapping[sub_key.strip()] = _scalar(sub_val, f"{source} ({key}.{sub_key.strip()})")
            front[key] = mapping
        else:
            front[key] = _scalar(rest, f"{source} ({key})")
    return front, "\n".join(lines[end + 1 :])


def split_sections(body: str) -> dict[str, str]:
    """Split a markdown body into a {level-2 heading: text} mapping, order preserved."""
    sections: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if name is not None:
                sections[name] = "\n".join(buf).strip()
            name = line[3:].strip()
            buf = []
        elif name is not None:
            buf.append(line)
    if name is not None:
        sections[name] = "\n".join(buf).strip()
    return sections


# ---------------------------------------------------------------------- run.json


def run_path(scratch: Path) -> Path:
    """Path of run.json inside the scratch directory."""
    return scratch / RUN_JSON


def read_run(scratch: Path) -> dict[str, Any] | None:
    """Read run.json, or None when it does not exist."""
    path = run_path(scratch)
    return _read_json_object(path) if path.exists() else None


def write_run(scratch: Path, data: dict[str, Any]) -> Path:
    """Write run.json."""
    return _write_json(run_path(scratch), data)


def ensure_run(scratch: Path, issue: int) -> dict[str, Any]:
    """Create run.json with identifiers only if absent; return its contents."""
    existing = read_run(scratch)
    if existing is not None:
        return existing
    data = {
        "issue": issue,
        "branch": None,
        "pr_number": None,
        "pr_url": None,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_run(scratch, data)
    return data


# -------------------------------------------------------------------- contract.md


@dataclass(frozen=True)
class Contract:
    """Parsed contract.md: front matter plus its markdown sections."""

    path: Path
    front: dict[str, Any]
    sections: dict[str, str]

    @property
    def issue(self) -> int:
        """Issue number the contract was written for."""
        return self.front["issue"]

    @property
    def title(self) -> str:
        """Verbatim issue title."""
        return self.front["title"]

    @property
    def test_budget(self) -> int:
        """Maximum number of new tests this PR may add."""
        return self.front["test_budget"]

    @property
    def scope_paths(self) -> list[str]:
        """Glob patterns the diff must stay inside."""
        return self.front["scope_paths"]

    @property
    def commands(self) -> dict[str, str]:
        """Verification commands discovered by the Contractor (`test` always present)."""
        return self.front["commands"]

    @property
    def summary(self) -> str:
        """Text of the contract's `## Summary` section."""
        return self.sections.get("Summary", "")

    @property
    def acceptance_criteria(self) -> str:
        """Text of the contract's `## Acceptance Criteria` section."""
        return self.sections.get("Acceptance Criteria", "")


def contract_path(scratch: Path) -> Path:
    """Path of contract.md inside the scratch directory."""
    return scratch / CONTRACT_MD


def read_contract(scratch: Path) -> Contract:
    """Read and validate contract.md."""
    path = contract_path(scratch)
    if not path.exists():
        raise ArtifactError(f"{path}: contract.md does not exist")
    front, body = parse_front_matter(read_text(path), path)
    for key, kind in (("issue", int), ("title", str), ("test_budget", int)):
        if not isinstance(front.get(key), kind) or isinstance(front.get(key), bool):
            raise ArtifactError(f"{path}: front matter needs a {kind.__name__} '{key}'")
    scope = front.get("scope_paths")
    if not isinstance(scope, list) or not all(isinstance(p, str) for p in scope):
        raise ArtifactError(f"{path}: front matter needs a list of strings 'scope_paths'")
    commands = front.get("commands")
    if not isinstance(commands, dict) or not all(
        isinstance(v, str) and v for v in commands.values()
    ):
        raise ArtifactError(f"{path}: front matter needs a 'commands' mapping of strings")
    if "test" not in commands:
        raise ArtifactError(f"{path}: front matter 'commands' must include a 'test' command")
    return Contract(path=path, front=front, sections=split_sections(body))


# -------------------------------------------------------------- numbered artifacts


def _numbered(scratch: Path, pattern: re.Pattern[str]) -> list[tuple[int, Path]]:
    if not scratch.is_dir():
        return []
    found = [
        (int(m.group(1)), p)
        for p in scratch.iterdir()
        if (m := pattern.match(p.name)) and p.is_file()
    ]
    return sorted(found)


def audit_files(scratch: Path) -> list[tuple[int, Path]]:
    """All audit-<n>.json files as (n, path), ascending."""
    return _numbered(scratch, _AUDIT_RE)


def review_files(scratch: Path) -> list[tuple[int, Path]]:
    """All review-<n>.md files as (n, path), ascending."""
    return _numbered(scratch, _REVIEW_RE)


def read_audit(path: Path) -> dict[str, Any]:
    """Read and validate an audit-<n>.json file."""
    data = _read_json_object(path)
    if not isinstance(data.get("pass"), bool):
        raise ArtifactError(f"{path}: 'pass' must be a boolean")
    if not isinstance(data.get("commit"), str) or not data["commit"]:
        raise ArtifactError(f"{path}: 'commit' must be a non-empty string")
    return data


def read_review_front(path: Path) -> dict[str, Any]:
    """Read and validate the front matter of a review-<n>.md file."""
    front, _ = parse_front_matter(read_text(path), path)
    if front.get("verdict") not in ("APPROVE", "REQUEST_CHANGES"):
        raise ArtifactError(f"{path}: 'verdict' must be APPROVE or REQUEST_CHANGES")
    if not isinstance(front.get("commit"), str) or not front["commit"]:
        raise ArtifactError(f"{path}: 'commit' must be a non-empty string")
    return front


# -------------------------------------------------------------------- ledger.json


def ledger_path(scratch: Path) -> Path:
    """Path of ledger.json inside the scratch directory."""
    return scratch / LEDGER_JSON


def read_ledger(scratch: Path) -> dict[str, Any] | None:
    """Read ledger.json, or None when it does not exist."""
    path = ledger_path(scratch)
    if not path.exists():
        return None
    data = _read_json_object(path)
    rounds = data.setdefault("rounds_completed", 0)
    findings = data.setdefault("findings", [])
    if not isinstance(rounds, int) or isinstance(rounds, bool):
        raise ArtifactError(f"{path}: 'rounds_completed' must be an integer")
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        raise ArtifactError(f"{path}: 'findings' must be a list of objects")
    return data
