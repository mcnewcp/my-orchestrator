"""Atomic branch state and the deterministic review ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import tempfile
import unicodedata
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path | str, data: object) -> None:
    """Replace a JSON file atomically, leaving its previous contents on failure."""
    path = Path(path)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_json(path: Path | str, default: object = None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)


def open_important(ledger: dict) -> list[dict]:
    return [item for item in ledger.get("findings", [])
            if item["status"] == "open" and item["severity"] == "important"]


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"finding {field} must be a nonempty string")
    return value


def _key(finding: dict) -> str:
    title = unicodedata.normalize("NFKC", finding["title"]).casefold()
    title = " ".join(re.sub(r"[^\w\s]", " ", title).split())
    filename = posixpath.normpath(finding["file"].strip())
    return hashlib.sha1(f"{finding['pass']}|{filename}|{title}".encode()).hexdigest()


def _validate_new(finding: object) -> None:
    if not isinstance(finding, dict):
        raise ValueError("new findings must be objects")
    for field in ("file", "title", "detail", "evidence"):
        _nonempty(finding.get(field), field)
    if finding.get("severity") not in ("important", "nit"):
        raise ValueError("finding severity must be important or nit")
    if finding.get("pass") not in ("bugs", "security", "compliance"):
        raise ValueError("finding pass must be bugs, security, or compliance")
    if "line" not in finding or (finding["line"] is not None
                                and type(finding["line"]) is not int):
        raise ValueError("finding line must be an integer or null")


def merge_ledger(ledger: dict, review: dict, round_number: int,
                 nit_cap: int = 5) -> tuple[dict, dict]:
    """Validate a complete review and merge it without modifying either input."""
    if type(round_number) is not int or round_number < 1:
        raise ValueError("round_number must be a positive integer")
    if type(nit_cap) is not int or nit_cap < 0:
        raise ValueError("nit_cap must be a nonnegative integer")
    if not isinstance(ledger, dict) or not isinstance(ledger.get("findings", []), list):
        raise ValueError("ledger findings must be a list")
    if not isinstance(review, dict) or any(not isinstance(review.get(k), list)
                                           for k in ("updates", "new")):
        raise ValueError("review must contain updates and new lists")

    merged = copy.deepcopy(ledger)
    findings = merged.setdefault("findings", [])
    by_id, by_key = {}, {}
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("ledger findings must be objects")
        _validate_new(finding)
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not re.fullmatch(r"F[1-9]\d*", finding_id):
            raise ValueError("ledger finding id must be F followed by a positive integer")
        if finding_id in by_id:
            raise ValueError(f"duplicate ledger finding id: {finding_id}")
        if finding.get("status") not in ("open", "resolved", "dismissed"):
            raise ValueError(f"invalid ledger status for {finding_id}")
        finding["key"] = _key(finding)
        if finding["key"] in by_key:
            raise ValueError(f"duplicate ledger finding key: {finding_id}")
        by_id[finding_id] = finding
        by_key[finding["key"]] = finding

    prior_open = {key for key, value in by_id.items() if value["status"] == "open"}
    prior_important = {key for key in prior_open if by_id[key]["severity"] == "important"}
    seen = set()
    for update in review["updates"]:
        if not isinstance(update, dict) or not isinstance(update.get("id"), str):
            raise ValueError("each finding update must have an id")
        finding_id = update["id"]
        if finding_id not in prior_open:
            raise ValueError(f"update is not for an open finding: {finding_id}")
        if finding_id in seen:
            raise ValueError(f"duplicate update for {finding_id}")
        if update.get("status") not in ("resolved", "unresolved"):
            raise ValueError(f"invalid update status for {finding_id}")
        _nonempty(update.get("evidence"), "update evidence")
        seen.add(finding_id)
        by_id[finding_id].update(status="resolved" if update["status"] == "resolved" else "open",
                                 status_round=round_number,
                                 status_evidence=update["evidence"])
    missing = prior_open - seen
    if missing:
        raise ValueError("review omitted updates for: " + ", ".join(sorted(missing)))

    for finding in review["new"]:
        _validate_new(finding)
    nits = sum(finding["severity"] == "nit" for finding in review["new"])
    if nits > nit_cap:
        raise ValueError(f"review raised {nits} nits; cap is {nit_cap}")
    next_id = max((int(key[1:]) for key in by_id), default=0) + 1
    dropped = 0
    for new in review["new"]:
        key = _key(new)
        existing = by_key.get(key)
        if existing is not None and existing["status"] == "dismissed":
            dropped += 1
            continue
        fields = {field: copy.deepcopy(new[field]) for field in
                  ("pass", "severity", "file", "line", "title", "detail", "evidence")}
        if existing is None:
            existing = dict(id=f"F{next_id}", key=key, opened_round=round_number,
                            dismissed_reason=None)
            next_id += 1
            findings.append(existing)
            by_key[key] = existing
            by_id[existing["id"]] = existing
        existing.update(fields)
        existing.update(status="open", status_round=round_number,
                        status_evidence=new["evidence"])

    stats = {"important_open": len(open_important(merged)),
             "important_resolved": sum(by_id[key]["status"] == "resolved"
                                       for key in prior_important),
             "nits": nits, "reraised_dropped": dropped}
    return merged, stats
