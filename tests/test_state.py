"""Unit tests for factory.state (design §7, §10, §11).

Self-contained: tmp_path only, no fixtures from conftest.py, no fakes, no network, no git.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

from factory.errors import FactoryError
from factory.state import (
    Finding,
    Ledger,
    ReviewRecord,
    RunLock,
    StageRecord,
    State,
    atomic_write_json,
    atomic_write_text,
    finding_key,
    is_parked,
    no_progress,
    normalize_title,
    now_iso,
    parse_open_questions,
    read_doctor_records,
    read_poll_journal,
    render_intent,
    render_spec_md,
    sha256_text,
    state_only_paths,
    work_dir,
    write_doctor_record,
    write_poll_journal,
)

ISSUE = 42
SHA_BASE = "a" * 40
SHA_HEAD = "b" * 40
SHA_OTHER = "c" * 40


# ---------------------------------------------------------------- builders


def make_state(**overrides) -> State:
    state = State(
        issue={
            "number": ISSUE,
            "snapshot_sha256": sha256_text("body"),
            "snapshot_at": "2026-09-06T10:00:00Z",
        },
        base={"branch": "main", "sha": SHA_BASE},
        branch=f"factory/{ISSUE}",
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def stage_record(start_commit: str = SHA_BASE) -> StageRecord:
    return StageRecord(
        start_commit=start_commit,
        at="2026-09-06T10:01:00Z",
        harness="claude",
        model="claude-fake-1",
        cli_version="9.9.9",
        auth="subscription",
        permission_denials=2,
    )


def review_record(round_: int, *, open_: int, resolved: int, fix_rounds_at: int) -> ReviewRecord:
    return ReviewRecord(
        round=round_,
        sha=f"{round_}" * 40,
        important_open=open_,
        important_resolved=resolved,
        nits=0,
        reraised_dropped=0,
        fix_rounds_at=fix_rounds_at,
    )


def new_finding(
    *,
    severity: str = "important",
    pass_: str = "bugs",
    file: str = "svc/x.py",
    line: int | None = 41,
    title: str = "Unchecked index",
    detail: str = "detail",
    evidence: str = "evidence",
) -> dict:
    return {
        "severity": severity,
        "pass": pass_,
        "file": file,
        "line": line,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


def review_output(*, updates: list[dict] | None = None, new: list[dict] | None = None) -> dict:
    return {"summary": "s", "updates": updates or [], "new": new or []}


def ledger_with_one(status: str, *, severity: str = "important", round_: int = 1) -> Ledger:
    """A ledger holding exactly F1 in `status`, raised from new_finding()'s defaults."""
    ledger = Ledger()
    ledger.merge_review(review_output(new=[new_finding(severity=severity)]), round_)
    finding = ledger.get("F1")
    assert finding is not None
    if status == "resolved":
        ledger.merge_review(
            review_output(updates=[{"id": "F1", "status": "resolved", "evidence": "fixed in r2"}]),
            round_ + 1,
        )
    elif status == "dismissed":
        ledger.dismiss("F1", "not a real defect", round_)
    assert finding.status == status
    return ledger


# ---------------------------------------------------------------- atomic writes


def test_atomic_write_text_creates_parents_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "work" / "42" / "spec.md"
    atomic_write_text(target, "# Spec\n")
    assert target.read_text(encoding="utf-8") == "# Spec\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["spec.md"]


def test_atomic_write_text_replaces_existing_content(tmp_path):
    target = tmp_path / "a.txt"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_json_is_pretty_with_trailing_newline(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"b": 1, "a": [1, 2]})
    text = target.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text.splitlines()[0] == "{"
    assert '  "b": 1' in text
    assert list(json.loads(text)) == ["b", "a"]  # insertion order preserved (sort_keys=False)


def test_atomic_write_text_failure_is_a_factory_error_naming_the_path(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(FactoryError) as exc:
        atomic_write_text(blocker / "child.json", "x")
    assert str(blocker / "child.json") in exc.value.message


# ---------------------------------------------------------------- State (de)serialization


def test_state_round_trips_through_disk(tmp_path):
    state = make_state(
        stages={"spec": stage_record(), "plan": stage_record(SHA_HEAD)},
        spec_open_questions=["Which queue?"],
        spec_accepted={"by": "operator", "at": "2026-09-06T11:00:00Z"},
        reviews=[review_record(1, open_=2, resolved=0, fix_rounds_at=0)],
        fix_rounds=1,
        pr={"number": 117, "url": "https://example.invalid/pull/117"},
        outcome="needs_human:no_progress",
        outcome_sha=SHA_HEAD,
    )
    path = state.save(tmp_path)
    assert path == State.path(tmp_path, ISSUE) == work_dir(tmp_path, ISSUE) / "state.json"

    loaded = State.load(tmp_path, ISSUE)
    assert loaded.to_dict() == state.to_dict()
    assert loaded.stages["plan"].start_commit == SHA_HEAD
    assert loaded.stages["spec"].permission_denials == 2
    assert loaded.reviews[0].important_open == 2
    assert loaded.number == ISSUE


def test_state_json_keeps_the_design_key_order(tmp_path):
    make_state().save(tmp_path)
    written = json.loads(State.path(tmp_path, ISSUE).read_text(encoding="utf-8"))
    assert list(written) == [
        "issue",
        "base",
        "branch",
        "stages",
        "spec_open_questions",
        "spec_accepted",
        "reviews",
        "fix_rounds",
        "pr",
        "outcome",
        "outcome_sha",
    ]


def test_state_load_missing_file_raises_and_load_or_none_returns_none(tmp_path):
    assert State.load_or_none(tmp_path, ISSUE) is None
    with pytest.raises(FactoryError) as exc:
        State.load(tmp_path, ISSUE)
    assert str(State.path(tmp_path, ISSUE)) in exc.value.message


def test_state_load_rejects_malformed_json(tmp_path):
    path = State.path(tmp_path, ISSUE)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(FactoryError) as exc:
        State.load(tmp_path, ISSUE)
    assert "not valid JSON" in exc.value.message
    assert str(path) in exc.value.message


def test_state_load_rejects_a_state_file_for_another_issue(tmp_path):
    state = make_state()
    state.issue["number"] = 41
    atomic_write_json(State.path(tmp_path, ISSUE), state.to_dict())
    with pytest.raises(FactoryError) as exc:
        State.load(tmp_path, ISSUE)
    assert "records issue 41" in exc.value.message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.pop("branch"), "branch"),
        (lambda d: d["base"].pop("sha"), "base.sha"),
        (lambda d: d["issue"].pop("number"), "issue.number"),
    ],
)
def test_state_from_dict_names_the_missing_required_key(mutate, expected):
    payload = make_state().to_dict()
    mutate(payload)
    with pytest.raises(FactoryError) as exc:
        State.from_dict(payload)
    assert expected in exc.value.message


def test_state_from_dict_rejects_a_stage_record_without_a_start_commit(tmp_path):
    payload = make_state().to_dict()
    payload["stages"] = {"spec": {}}
    with pytest.raises(FactoryError) as exc:
        State.from_dict(payload)
    assert "stages.spec has no start_commit" in exc.value.message


def test_state_from_dict_defaults_optional_record_fields():
    payload = make_state().to_dict()
    payload["stages"] = {"spec": {"start_commit": SHA_BASE}}
    payload["reviews"] = [{"round": 1, "sha": SHA_HEAD}]
    state = State.from_dict(payload)
    assert state.stages["spec"].permission_denials == 0
    assert state.stages["spec"].harness == ""
    assert state.reviews[0].fix_rounds_at == 0
    assert state.reviews[0].diff_truncated is False


def test_state_derived_predicates():
    state = make_state()
    assert state.stage_done("spec") is False
    assert state.last_review() is None
    assert state.spec_needs_acceptance() is False
    assert state.is_needs_human() is False
    assert state.gate() is None

    state.spec_open_questions = ["Which queue?"]
    assert state.spec_needs_acceptance() is True
    state.spec_accepted = {"by": "operator", "at": now_iso()}
    assert state.spec_needs_acceptance() is False

    state.set_outcome("needs_human:rounds_exhausted", SHA_HEAD)
    assert state.is_needs_human() is True
    assert state.gate() == "rounds_exhausted"
    state.set_outcome("done", SHA_HEAD)
    assert state.is_needs_human() is False
    assert state.gate() is None


# ---------------------------------------------------------------- parked / no_progress


def test_is_parked_true_when_head_is_the_outcome_commit():
    state = make_state(outcome="needs_human:open_questions", outcome_sha=SHA_HEAD)
    assert is_parked(state, SHA_HEAD, SHA_BASE, ["work/42/spec.md"]) is True


def test_is_parked_true_for_parks_own_state_only_child_commit():
    """park() commits state.json AFTER reading outcome_sha, so HEAD is one state-only commit ahead."""
    state = make_state(outcome="needs_human:baseline_failing", outcome_sha=SHA_BASE)
    assert is_parked(state, SHA_HEAD, SHA_BASE, state_only_paths(ISSUE)) is True
    assert state_only_paths(ISSUE) == ["work/42/state.json"]


def test_is_parked_false_for_a_code_child_commit():
    """An operator hand fix on top of the park commit re-opens the loop."""
    state = make_state(outcome="needs_human:no_progress", outcome_sha=SHA_BASE)
    assert is_parked(state, SHA_HEAD, SHA_BASE, ["svc/x.py"]) is False
    assert is_parked(state, SHA_HEAD, SHA_BASE, ["work/42/state.json", "svc/x.py"]) is False


def test_is_parked_false_when_head_is_unrelated_to_the_outcome_commit():
    state = make_state(outcome="needs_human:no_progress", outcome_sha=SHA_BASE)
    assert is_parked(state, SHA_HEAD, SHA_OTHER, state_only_paths(ISSUE)) is False
    assert is_parked(state, SHA_HEAD, None, state_only_paths(ISSUE)) is False


@pytest.mark.parametrize("outcome", [None, "done"])
def test_is_parked_false_when_the_outcome_is_not_needs_human(outcome):
    state = make_state(outcome=outcome, outcome_sha=SHA_HEAD)
    assert is_parked(state, SHA_HEAD, SHA_BASE, []) is False


def test_is_parked_false_without_an_outcome_sha():
    state = make_state(outcome="needs_human:open_questions", outcome_sha=None)
    assert is_parked(state, SHA_HEAD, SHA_BASE, []) is False


def test_no_progress_true_only_for_a_review_that_followed_a_fix_and_resolved_nothing():
    state = make_state(
        reviews=[
            review_record(1, open_=2, resolved=0, fix_rounds_at=0),
            review_record(2, open_=2, resolved=0, fix_rounds_at=1),
        ]
    )
    assert no_progress(state) is True


def test_no_progress_false_before_two_reviews():
    assert no_progress(make_state()) is False
    assert (
        no_progress(make_state(reviews=[review_record(1, open_=3, resolved=0, fix_rounds_at=0)]))
        is False
    )


def test_no_progress_false_when_the_fixer_resolved_something():
    state = make_state(
        reviews=[
            review_record(1, open_=2, resolved=0, fix_rounds_at=0),
            review_record(2, open_=1, resolved=1, fix_rounds_at=1),
        ]
    )
    assert no_progress(state) is False


def test_no_progress_false_when_nothing_important_stays_open():
    state = make_state(
        reviews=[
            review_record(1, open_=1, resolved=0, fix_rounds_at=0),
            review_record(2, open_=0, resolved=0, fix_rounds_at=1),
        ]
    )
    assert no_progress(state) is False


def test_no_progress_false_when_the_review_did_not_follow_a_fix():
    """Two reviews with no fix between them (an operator hand fix triggered the second)."""
    state = make_state(
        reviews=[
            review_record(1, open_=2, resolved=0, fix_rounds_at=1),
            review_record(2, open_=2, resolved=0, fix_rounds_at=1),
        ]
    )
    assert no_progress(state) is False


# ---------------------------------------------------------------- finding_key / normalization


def test_finding_key_is_sha1_of_pass_file_and_normalized_title():
    expected = hashlib.sha1(b"bugs|svc/x.py|unchecked index").hexdigest()
    assert finding_key("bugs", "svc/x.py", "Unchecked index") == expected


def test_normalize_title_lowercases_collapses_whitespace_and_strips_punctuation():
    assert normalize_title("  Unchecked   INDEX!  ") == "unchecked index"
    assert normalize_title("Unchecked index.") == "unchecked index"
    assert normalize_title("`Unchecked`, index") == "unchecked index"
    assert normalize_title("Unchecked\n index") == "unchecked index"


def test_finding_key_ignores_wording_noise_but_not_pass_or_file():
    base = finding_key("bugs", "svc/x.py", "Unchecked index")
    assert finding_key("bugs", "svc/x.py", "unchecked  index!") == base
    assert finding_key("security", "svc/x.py", "Unchecked index") != base
    assert finding_key("bugs", "svc/y.py", "Unchecked index") != base
    assert finding_key("bugs", "svc/x.py", "Unchecked index in the loop") != base


# ---------------------------------------------------------------- Finding / Ledger persistence


def test_finding_serializes_pass_under_the_json_key_pass():
    finding = Finding(
        id="F1",
        key=finding_key("bugs", "svc/x.py", "t"),
        pass_="bugs",
        severity="important",
        file="svc/x.py",
        line=41,
        title="t",
        detail="d",
        evidence="e",
        opened_round=1,
    )
    payload = finding.to_dict()
    assert payload["pass"] == "bugs"
    assert "pass_" not in payload
    assert list(payload)[:3] == ["id", "key", "pass"]
    assert Finding.from_dict(payload) == finding


def test_finding_from_dict_recomputes_a_missing_key_and_rejects_a_bad_status():
    payload = {
        "id": "F1",
        "pass": "bugs",
        "severity": "nit",
        "file": "a.py",
        "title": "Trailing space",
    }
    finding = Finding.from_dict(payload)
    assert finding.key == finding_key("bugs", "a.py", "Trailing space")
    assert finding.line is None and finding.status == "open" and finding.opened_round == 0

    with pytest.raises(FactoryError) as exc:
        Finding.from_dict({**payload, "status": "closed"})
    assert "unknown status 'closed'" in exc.value.message

    with pytest.raises(FactoryError) as exc:
        Finding.from_dict({"id": "F1", "severity": "nit", "title": "t"})
    assert "missing 'pass'" in exc.value.message


def test_ledger_round_trips_through_disk_and_missing_file_is_empty(tmp_path):
    assert Ledger.load(tmp_path, ISSUE).findings == []

    ledger = ledger_with_one("open")
    ledger.merge_review(
        review_output(
            updates=[{"id": "F1", "status": "unresolved", "evidence": "still there"}],
            new=[new_finding(severity="nit", title="Trailing whitespace", line=None)],
        ),
        2,
    )
    path = ledger.save(tmp_path, ISSUE)
    assert path == Ledger.path(tmp_path, ISSUE)

    loaded = Ledger.load(tmp_path, ISSUE)
    assert loaded.to_dict() == ledger.to_dict()
    assert [f.id for f in loaded.findings] == ["F1", "F2"]
    assert loaded.get("F2").line is None


def test_ledger_load_rejects_a_malformed_file(tmp_path):
    path = Ledger.path(tmp_path, ISSUE)
    path.parent.mkdir(parents=True)
    path.write_text('{"findings": 3}', encoding="utf-8")
    with pytest.raises(FactoryError) as exc:
        Ledger.load(tmp_path, ISSUE)
    assert str(path) in exc.value.message


def test_next_id_continues_past_the_highest_existing_id():
    ledger = Ledger()
    assert ledger.next_id() == "F1"
    ledger.merge_review(
        review_output(new=[new_finding(), new_finding(title="Second", file="svc/y.py")]), 1
    )
    assert [f.id for f in ledger.findings] == ["F1", "F2"]
    assert ledger.next_id() == "F3"


# ---------------------------------------------------------------- merge_review


def test_merge_appends_new_findings_and_counts_nits_and_important_separately():
    ledger = Ledger()
    stats = ledger.merge_review(
        review_output(
            new=[
                new_finding(),
                new_finding(severity="nit", title="Rename variable", file="svc/y.py"),
                new_finding(severity="nit", title="Stray import", file="svc/z.py"),
            ]
        ),
        1,
    )
    assert (stats.new_important, stats.new_nits) == (1, 2)
    assert stats.missing_updates == []
    assert [f.id for f in ledger.findings] == ["F1", "F2", "F3"]
    assert [f.opened_round for f in ledger.findings] == [1, 1, 1]
    assert len(ledger.open()) == 3
    assert [f.id for f in ledger.open_important()] == ["F1"]


def test_merge_applies_resolved_and_unresolved_updates():
    ledger = Ledger()
    ledger.merge_review(
        review_output(new=[new_finding(), new_finding(title="Second bug", file="svc/y.py")]), 1
    )
    stats = ledger.merge_review(
        review_output(
            updates=[
                {"id": "F1", "status": "resolved", "evidence": "svc/x.py:41 bounds check added"},
                {"id": "F2", "status": "unresolved", "evidence": "still unguarded"},
            ]
        ),
        2,
    )
    assert (stats.resolved, stats.unresolved) == (1, 1)
    assert stats.missing_updates == []
    f1, f2 = ledger.get("F1"), ledger.get("F2")
    assert (f1.status, f1.status_round, f1.status_evidence) == (
        "resolved",
        2,
        "svc/x.py:41 bounds check added",
    )
    assert (f2.status, f2.status_evidence) == ("open", "still unguarded")
    assert f2.status_round is None


def test_resolved_counts_important_transitions_only():
    """ReviewRecord.important_resolved feeds the no-progress rule: a resolved nit is not progress."""
    ledger = ledger_with_one("open", severity="nit")
    stats = ledger.merge_review(
        review_output(updates=[{"id": "F1", "status": "resolved", "evidence": "gone"}]), 2
    )
    assert stats.resolved == 0
    assert ledger.get("F1").status == "resolved"


def test_updates_for_unknown_or_already_adjudicated_ids_are_ignored():
    ledger = ledger_with_one("dismissed")
    stats = ledger.merge_review(
        review_output(
            updates=[
                {"id": "F1", "status": "resolved", "evidence": "x"},
                {"id": "F99", "status": "resolved", "evidence": "y"},
            ]
        ),
        2,
    )
    assert (stats.resolved, stats.unresolved) == (0, 0)
    assert ledger.get("F1").status == "dismissed"
    assert ledger.get("F99") is None


def test_missing_updates_lists_every_open_finding_the_reviewer_skipped():
    ledger = Ledger()
    ledger.merge_review(
        review_output(
            new=[
                new_finding(),
                new_finding(title="Second bug", file="svc/y.py"),
                new_finding(severity="nit", title="Third", file="svc/z.py"),
            ]
        ),
        1,
    )
    stats = ledger.merge_review(
        review_output(updates=[{"id": "F2", "status": "unresolved", "evidence": "still there"}]), 2
    )
    assert stats.missing_updates == ["F1", "F3"]


def test_missing_updates_ignores_findings_raised_this_round():
    ledger = Ledger()
    stats = ledger.merge_review(review_output(new=[new_finding()]), 1)
    assert stats.missing_updates == []


def test_duplicate_new_finding_merges_into_the_open_one():
    ledger = ledger_with_one("open")
    stats = ledger.merge_review(
        review_output(
            updates=[{"id": "F1", "status": "unresolved", "evidence": "still there"}],
            new=[
                new_finding(
                    title="  UNCHECKED   index!! ", detail="reworded", evidence="round 2 evidence"
                )
            ],
        ),
        2,
    )
    assert stats.merged_duplicates == 1
    assert (stats.new_important, stats.new_nits, stats.reopened, stats.reraised_dropped) == (
        0,
        0,
        0,
        0,
    )
    assert [f.id for f in ledger.findings] == ["F1"]
    finding = ledger.get("F1")
    assert (finding.status, finding.opened_round, finding.detail) == ("open", 1, "detail")


def test_dismissed_finding_re_raised_is_dropped_and_counted():
    ledger = ledger_with_one("dismissed")
    stats = ledger.merge_review(review_output(new=[new_finding(title="Unchecked  Index")]), 2)
    assert stats.reraised_dropped == 1
    assert (stats.new_important, stats.merged_duplicates, stats.reopened) == (0, 0, 0)
    assert [f.id for f in ledger.findings] == ["F1"]
    finding = ledger.get("F1")
    assert (finding.status, finding.dismissed_reason) == ("dismissed", "not a real defect")


def test_resolved_finding_re_raised_reopens_as_a_regression():
    ledger = ledger_with_one("resolved")
    stats = ledger.merge_review(
        review_output(
            new=[new_finding(detail="came back", evidence="svc/x.py:44 index unguarded again")]
        ),
        3,
    )
    assert stats.reopened == 1
    assert (stats.new_important, stats.merged_duplicates, stats.reraised_dropped) == (0, 0, 0)
    assert [f.id for f in ledger.findings] == ["F1"]
    finding = ledger.get("F1")
    assert finding.status == "open"
    assert finding.status_round == 3
    assert finding.status_evidence == "svc/x.py:44 index unguarded again"
    assert finding.detail == "came back"
    assert finding.opened_round == 1  # the regression keeps the original id and opening round
    assert ledger.open_important() == [finding]


def test_a_nit_matching_an_open_nit_merges_and_does_not_count_against_the_cap():
    ledger = ledger_with_one("open", severity="nit")
    stats = ledger.merge_review(
        review_output(
            updates=[{"id": "F1", "status": "unresolved", "evidence": "still there"}],
            new=[
                new_finding(severity="nit"),
                new_finding(severity="nit", title="Brand new nit", file="svc/y.py"),
            ],
        ),
        2,
    )
    assert (stats.merged_duplicates, stats.new_nits, stats.new_important) == (1, 1, 0)
    assert len(ledger.findings) == 2


def test_merged_copy_leaves_the_committed_ledger_untouched():
    ledger = ledger_with_one("open")
    out = review_output(
        updates=[{"id": "F1", "status": "resolved", "evidence": "fixed"}],
        new=[new_finding(title="Fresh problem", file="svc/y.py")],
    )
    candidate, stats = ledger.merged_copy(out, 2)

    assert stats.resolved == 1 and stats.new_important == 1
    assert [f.id for f in candidate.findings] == ["F1", "F2"]
    assert candidate.get("F1").status == "resolved"
    # the original is byte-identical to what it was, and no object is shared with the copy
    assert [f.id for f in ledger.findings] == ["F1"]
    assert ledger.get("F1").status == "open"
    assert ledger.get("F1") is not candidate.get("F1")
    candidate.get("F1").title = "mutated"
    assert ledger.get("F1").title == "Unchecked index"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({**new_finding(), "severity": "critical"}, "unknown severity 'critical'"),
        ({**new_finding(), "pass": "style"}, "unknown pass 'style'"),
        ({k: v for k, v in new_finding().items() if k != "title"}, "missing 'title'"),
    ],
)
def test_merge_rejects_a_malformed_new_finding(entry, expected):
    with pytest.raises(FactoryError) as exc:
        Ledger().merge_review(review_output(new=[entry]), 1)
    assert expected in exc.value.message


def test_merge_rejects_a_malformed_update():
    ledger = ledger_with_one("open")
    with pytest.raises(FactoryError) as exc:
        ledger.merge_review(
            review_output(updates=[{"id": "F1", "status": "maybe", "evidence": "x"}]), 2
        )
    assert "status 'maybe'" in exc.value.message

    with pytest.raises(FactoryError) as exc:
        ledger.merge_review(review_output(updates=[{"status": "resolved", "evidence": "x"}]), 2)
    assert "missing 'id'" in exc.value.message

    with pytest.raises(FactoryError) as exc:
        ledger.merge_review({"summary": "s", "updates": "not a list", "new": []}, 2)
    assert "'updates' is not an array" in exc.value.message


def test_merge_accepts_output_without_optional_arrays():
    stats = Ledger().merge_review({"summary": "nothing to report"}, 1)
    assert (stats.new_important, stats.new_nits, stats.missing_updates) == (0, 0, [])


# ---------------------------------------------------------------- dismiss / render


def test_dismiss_records_the_reason_and_round():
    ledger = ledger_with_one("open")
    finding = ledger.dismiss("F1", "  intentional, see ADR 4  ", 2)
    assert (finding.status, finding.status_round) == ("dismissed", 2)
    assert finding.dismissed_reason == "intentional, see ADR 4"
    assert ledger.open_important() == []


def test_dismiss_rejects_unknown_already_adjudicated_and_reasonless_ids():
    ledger = ledger_with_one("open")
    with pytest.raises(FactoryError) as exc:
        ledger.dismiss("F9", "why", 1)
    assert "unknown finding F9" in exc.value.message
    assert "F1" in (exc.value.hint or "")

    with pytest.raises(FactoryError) as exc:
        ledger.dismiss("F1", "   ", 1)
    assert "requires a reason" in exc.value.message
    assert ledger.get("F1").status == "open"

    ledger.dismiss("F1", "not a defect", 1)
    with pytest.raises(FactoryError) as exc:
        ledger.dismiss("F1", "again", 2)
    assert "already dismissed" in exc.value.message


def test_render_markdown_table():
    ledger = Ledger()
    ledger.merge_review(
        review_output(
            new=[
                new_finding(title="Unchecked | index"),
                new_finding(
                    severity="nit", pass_="compliance", title="Spec drift", file="", line=None
                ),
            ]
        ),
        1,
    )
    ledger.dismiss("F2", "out of scope", 1)
    rows = ledger.render_markdown().splitlines()
    assert rows[0] == "| id | severity | pass | status | file:line | title |"
    assert rows[1] == "| --- | --- | --- | --- | --- | --- |"
    assert rows[2] == "| F1 | important | bugs | open | svc/x.py:41 | Unchecked \\| index |"
    assert rows[3] == "| F2 | nit | compliance | dismissed | - | Spec drift |"
    assert len(rows) == 4


def test_render_markdown_of_an_empty_ledger():
    assert Ledger().render_markdown() == "_No findings._"


# ---------------------------------------------------------------- intent / spec markdown


def test_render_intent_records_the_snapshot_facts_and_hashes_the_body():
    body = "## Problem\nThe queue drops messages.\n"
    markdown, digest = render_intent(
        {
            "number": 42,
            "title": "Drop no messages",
            "body": body,
            "url": "https://example.invalid/issues/42",
            "labels": [{"name": "factory"}, {"name": "bug"}],
        },
        "2026-09-06T10:00:00Z",
    )
    assert digest == sha256_text(body) == hashlib.sha256(body.encode()).hexdigest()
    lines = markdown.splitlines()
    assert lines[0] == "# Intent: Drop no messages"
    assert "- number: 42" in lines
    assert "- url: https://example.invalid/issues/42" in lines
    assert "- labels: factory, bug" in lines
    assert "- snapshot_at: 2026-09-06T10:00:00Z" in lines
    assert f"- sha256: {digest}" in lines
    assert markdown.split("---\n", 1)[1].strip() == body.strip()


def test_render_intent_accepts_plain_label_names_and_an_empty_body():
    markdown, digest = render_intent(
        {"number": 7, "title": "", "body": "", "url": "", "labels": ["factory"]},
        "2026-09-06T10:00:00Z",
    )
    assert "- labels: factory" in markdown
    assert "# Intent: (no title)" in markdown
    assert digest == sha256_text("")


def test_render_intent_requires_an_issue_number():
    with pytest.raises(FactoryError) as exc:
        render_intent({"title": "t", "body": "b"}, "2026-09-06T10:00:00Z")
    assert "no number" in exc.value.message


def test_spec_open_questions_round_trip():
    questions = ["Which queue does the worker read?", "Is retry idempotent?"]
    spec = render_spec_md("# Spec\n\nBody text.", questions)
    assert spec.startswith("# Spec\n\nBody text.\n\n## Open questions\n")
    assert parse_open_questions(spec) == questions


def test_spec_without_open_questions_renders_none_and_parses_empty():
    spec = render_spec_md("# Spec\n", [])
    assert "## Open questions\n\nNone.\n" in spec
    assert parse_open_questions(spec) == []


def test_parse_open_questions_reads_the_last_heading_and_tolerates_operator_edits():
    spec = "\n".join(
        [
            "# Spec",
            "",
            "## Open questions",
            "",
            "- stale question from an earlier draft",
            "",
            "## Acceptance criteria",
            "",
            "- something else",
            "",
            "## Open questions",
            "",
            "1. Which queue?",
            "* Retry semantics?",
            "  - Indented question",
            "",
        ]
    )
    assert parse_open_questions(spec) == ["Which queue?", "Retry semantics?", "Indented question"]


def test_parse_open_questions_without_the_heading_is_empty():
    assert parse_open_questions("# Spec\n\nNo trailer here.\n") == []
    assert parse_open_questions("") == []


# ---------------------------------------------------------------- RunLock


def test_run_lock_round_trips_and_clear_is_idempotent(tmp_path):
    lock = RunLock(
        issue=ISSUE,
        stage="build",
        pid=os.getpid(),
        started_at=now_iso(),
        worktree=str(tmp_path / "wt"),
    )
    lock.write(tmp_path)
    assert RunLock.path(tmp_path, ISSUE) == tmp_path / "run" / "42.json"

    read_back = RunLock.read(tmp_path, ISSUE)
    assert read_back == lock
    assert read_back.pid_alive() is True

    RunLock.clear(tmp_path, ISSUE)
    RunLock.clear(tmp_path, ISSUE)
    assert RunLock.read(tmp_path, ISSUE) is None


def test_run_lock_write_updates_the_stage_and_carries_last_error(tmp_path):
    lock = RunLock(
        issue=ISSUE, stage="run", pid=os.getpid(), started_at=now_iso(), worktree=str(tmp_path)
    )
    lock.write(tmp_path)
    lock.last_error = "checks failed: python3 checks.py"
    lock.write(tmp_path, stage="build")

    read_back = RunLock.read(tmp_path, ISSUE)
    assert read_back.stage == "build"
    assert read_back.last_error == "checks failed: python3 checks.py"
    assert json.loads(RunLock.path(tmp_path, ISSUE).read_text(encoding="utf-8"))["stage"] == "build"


def test_run_lock_read_returns_none_for_missing_or_corrupt_files(tmp_path):
    assert RunLock.read(tmp_path, ISSUE) is None
    path = RunLock.path(tmp_path, ISSUE)
    path.parent.mkdir(parents=True)
    path.write_text("{ truncated", encoding="utf-8")
    assert RunLock.read(tmp_path, ISSUE) is None
    path.write_text('{"issue": 42, "stage": "build"}', encoding="utf-8")
    assert RunLock.read(tmp_path, ISSUE) is None  # no pid: not a lock


def test_pid_alive_is_false_for_a_finished_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    lock = RunLock(issue=ISSUE, stage="build", pid=proc.pid, started_at="", worktree="")
    assert lock.pid_alive() is False
    assert (
        RunLock(issue=ISSUE, stage="build", pid=0, started_at="", worktree="").pid_alive() is False
    )


def test_pid_alive_counts_permission_error_as_alive(monkeypatch):
    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", deny)
    lock = RunLock(issue=ISSUE, stage="build", pid=1, started_at="", worktree="")
    assert lock.pid_alive() is True


# ---------------------------------------------------------------- poll journal / doctor records


def test_poll_journal_round_trip_and_missing_is_empty(tmp_path):
    assert read_poll_journal(tmp_path) == {}
    journal = {
        "42": {
            "failures": 2,
            "sha": SHA_HEAD,
            "last_error": "harness exit 1",
            "capped_comment_sha": None,
        }
    }
    write_poll_journal(tmp_path, journal)
    assert read_poll_journal(tmp_path) == journal
    assert (tmp_path / "poll.json").read_text(encoding="utf-8").endswith("\n")


def test_corrupt_transients_read_as_empty(tmp_path):
    (tmp_path / "poll.json").write_text("[]", encoding="utf-8")
    (tmp_path / "doctor.json").write_text("not json", encoding="utf-8")
    assert read_poll_journal(tmp_path) == {}
    assert read_doctor_records(tmp_path) == {}


def test_doctor_records_for_different_combinations_coexist(tmp_path):
    assert read_doctor_records(tmp_path) == {}
    write_doctor_record(
        tmp_path, "claude", "api", {"factory_version": "0.0.1", "cli_version": "2.1.263"}
    )
    write_doctor_record(
        tmp_path, "codex", "api", {"factory_version": "0.0.1", "cli_version": "0.153.4"}
    )
    records = read_doctor_records(tmp_path)
    assert sorted(records) == ["claude:api", "codex:api"]

    write_doctor_record(
        tmp_path, "claude", "api", {"factory_version": "0.0.2", "cli_version": "2.1.300"}
    )
    records = read_doctor_records(tmp_path)
    assert records["claude:api"]["cli_version"] == "2.1.300"
    assert records["codex:api"]["cli_version"] == "0.153.4"


# ---------------------------------------------------------------- small helpers


def test_now_iso_is_a_second_resolution_utc_timestamp():
    stamp = now_iso()
    assert stamp.endswith("Z") and "." not in stamp
    assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset().total_seconds() == 0


def test_paths_are_worktree_relative_under_work(tmp_path):
    assert work_dir(tmp_path, ISSUE) == tmp_path / "work" / "42"
    assert State.path(tmp_path, ISSUE).name == "state.json"
    assert Ledger.path(tmp_path, ISSUE).name == "findings.json"
    assert state_only_paths(7) == ["work/7/state.json"]


def test_sha256_text_matches_hashlib():
    assert sha256_text("body") == hashlib.sha256(b"body").hexdigest()
