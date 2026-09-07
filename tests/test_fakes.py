"""The test doubles are a contract other tests build on, so they get their own tests.

Everything here drives `tests/fakes/{claude,codex,gh}` as real subprocesses, exactly as the factory
would, and asserts against tests/FAKES.md.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from conftest import FAKE_BINARIES, TESTS_DIR, fake_bin_dir, pointer_path

SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"markdown": {"type": "string"}, "open_questions": {"type": "array"}},
    "required": ["markdown", "open_questions"],
}
SPEC_OUTPUT = {"markdown": "# Spec\n\nDo the thing.\n", "open_questions": []}
PROMPT_REL = "work/42/prompts/spec-1.md"
PROMPT_TEXT = "# Role: spec\n\nWrite the spec for issue 42.\n"


# ---------------------------------------------------------------- helpers


def run(*argv: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run one fake as the factory would: no shell, stdin closed, PATH from the test environment."""
    environ = dict(os.environ)
    for name, value in (env or {}).items():
        if value is None:
            environ.pop(name, None)
        else:
            environ[name] = str(value)
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        text=True,
        capture_output=True,
        env=environ,
        stdin=subprocess.DEVNULL,
    )


def sentence(prompt_file: str = PROMPT_REL) -> str:
    return f"Follow the instructions in {prompt_file} exactly."


def write_prompt(root: Path, rel: str = PROMPT_REL, text: str = PROMPT_TEXT) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def claude_argv(
    *extra: str, schema: dict | None = SPEC_SCHEMA, prompt: str = PROMPT_REL
) -> list[str]:
    """The design §8 read-mode command line, in the order harness.ClaudeCode.argv builds it."""
    argv = [
        "claude",
        "-p",
        sentence(prompt),
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
    ]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema, separators=(",", ":"), sort_keys=True)]
    argv += [
        "--allowedTools",
        "Read,Grep,Glob",
        "--disallowedTools",
        "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch",
    ]
    return argv + list(extra)


def codex_argv(cwd: Path, schema_file: Path, last_file: Path, *extra: str) -> list[str]:
    """The design §8 codex command line: the prompt sentence is the last positional."""
    return [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_file),
        "-o",
        str(last_file),
        *extra,
        "-C",
        str(cwd),
        sentence(),
    ]


def gh_state_with(**overrides) -> dict:
    state = {
        "repo": "owner/name",
        "auth_ok": True,
        "issues": {},
        "labels": ["factory"],
        "prs": {},
        "next_pr_number": 117,
        "fail_next": [],
    }
    state.update(overrides)
    return state


def issue(number: int, *, labels: list[str], state: str = "OPEN") -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": f"body of {number}",
        "url": f"https://github.com/owner/name/issues/{number}",
        "labels": labels,
        "state": state,
    }


def pull(
    number: int, *, head: str, state: str = "OPEN", is_draft: bool = True, comments=None
) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/owner/name/pull/{number}",
        "headRefName": head,
        "baseRefName": "main",
        "isDraft": is_draft,
        "state": state,
        "title": f"PR {number}",
        "body": "Closes #42",
        "comments": comments or [],
    }


# ---------------------------------------------------------------- wiring


def test_fakes_are_executable_and_first_on_path(fake_dir):
    import shutil

    for name in FAKE_BINARIES:
        path = fake_bin_dir(Path(fake_dir)) / name
        assert os.access(path, os.X_OK), f"{path} is not executable"
        assert shutil.which(name) == str(path)


def test_pointer_file_exists_for_the_duration_of_a_test(fake_dir):
    assert pointer_path(Path(fake_dir)).read_text(encoding="utf-8").strip() == str(fake_dir)


def test_the_fakes_and_their_pointer_are_private_to_this_test(fake_dir):
    """Two pytest processes in one checkout must not share a pointer: a fake launched by one would pop the
    other's queue. Nothing this suite writes may live inside the repository."""
    bin_dir = fake_bin_dir(Path(fake_dir))
    assert TESTS_DIR not in bin_dir.parents
    assert TESTS_DIR not in pointer_path(Path(fake_dir)).parents
    assert not (TESTS_DIR / ".fake_dir").exists()
    for name in FAKE_BINARIES:
        assert not (bin_dir / name).is_symlink(), (
            "a symlink resolves back to the shared tests/fakes"
        )


def test_no_provider_credentials_leak_from_the_workstation():
    for name in ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "CLAUDECODE"):
        assert name not in os.environ


# ---------------------------------------------------------------- versions


def test_claude_version(tmp_path, fake_dir):
    proc = run("claude", "--version", cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "9.9.9 (Claude Code)"
    assert fake_dir.calls() == []


def test_codex_version(tmp_path, fake_dir):
    proc = run("codex", "--version", cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "codex-cli 9.9.9"
    assert fake_dir.calls() == []


def test_version_needs_no_fake_dir(tmp_path, fake_dir):
    pointer_path(Path(fake_dir)).unlink()
    proc = run("claude", "--version", cwd=tmp_path, env={"FACTORY_FAKE_DIR": None})
    assert proc.returncode == 0


# ---------------------------------------------------------------- fake-dir discovery


def test_pointer_file_wins_over_the_environment(tmp_path, fake_dir, tmp_path_factory):
    decoy = tmp_path_factory.mktemp("decoy")
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)
    proc = run(*claude_argv(), cwd=tmp_path, env={"FACTORY_FAKE_DIR": str(decoy)})
    assert proc.returncode == 0
    assert fake_dir.queue_remaining() == []
    assert len(fake_dir.calls()) == 1
    assert not list(decoy.iterdir())


def test_environment_is_the_fallback_when_the_pointer_is_missing(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)
    pointer_path(Path(fake_dir)).unlink()
    proc = run(*claude_argv(), cwd=tmp_path)
    assert proc.returncode == 0
    assert fake_dir.queue_remaining() == []


def test_no_pointer_and_no_environment_is_exit_3(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)
    pointer_path(Path(fake_dir)).unlink()
    proc = run(*claude_argv(), cwd=tmp_path, env={"FACTORY_FAKE_DIR": None})
    assert proc.returncode == 3
    assert ".fake_dir" in proc.stderr
    assert fake_dir.queue_remaining() == [{"output": SPEC_OUTPUT}]


# ---------------------------------------------------------------- fake claude


def test_claude_pops_the_queue_and_returns_structured_output(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)

    proc = run(*claude_argv(), cwd=tmp_path)

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result["is_error"] is False
    assert result["structured_output"] == SPEC_OUTPUT
    assert json.loads(result["result"]) == SPEC_OUTPUT
    assert result["session_id"] == "fake-0"
    assert result["permission_denials"] == []
    assert list(result["modelUsage"]) == ["claude-fake-1"]
    assert fake_dir.queue_remaining() == []


def test_claude_records_the_call(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)

    run(*claude_argv(), cwd=tmp_path, env={"GH_TOKEN": "secret", "ANTHROPIC_API_KEY": "key"})

    (call,) = fake_dir.calls()
    assert call["binary"] == "claude"
    assert call["cwd"] == str(tmp_path)
    assert call["argv"][0] == "-p"
    assert call["argv"][1] == sentence()
    assert "--strict-mcp-config" in call["argv"]
    assert call["prompt_file"] == PROMPT_REL
    assert call["prompt_text"] == PROMPT_TEXT
    assert call["schema"] == SPEC_SCHEMA
    assert call["env"] == {
        "ANTHROPIC_API_KEY": "present",
        "CODEX_API_KEY": "absent",
        "GH_TOKEN": "present",
        "FACTORY_FAKE_DIR": "present",
        "CLAUDECODE": "absent",
    }
    assert "PATH" in call["env_keys"]


def test_claude_applies_writes_and_deletes(tmp_path, fake_dir):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("stale\n", encoding="utf-8")
    fake_dir.queue(
        {
            "output": {"summary": "built it", "deviations": []},
            "writes": {"src/x.py": "print(1)\n", "RED": ""},
            "deletes": ["tests/test_a.py", "tests/never_existed.py"],
        }
    )
    write_prompt(tmp_path)

    proc = run(*claude_argv(), cwd=tmp_path)

    assert proc.returncode == 0
    assert (tmp_path / "src" / "x.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (tmp_path / "RED").read_text(encoding="utf-8") == ""
    assert not (tmp_path / "tests" / "test_a.py").exists()


def test_claude_honours_exit_code(tmp_path, fake_dir):
    fake_dir.queue({"output": SPEC_OUTPUT, "exit_code": 7})
    write_prompt(tmp_path)
    proc = run(*claude_argv(), cwd=tmp_path)
    assert proc.returncode == 7


def test_claude_honours_is_error(tmp_path, fake_dir):
    fake_dir.queue({"is_error": True, "result": "Credit balance is too low", "exit_code": 1})
    write_prompt(tmp_path)

    proc = run(*claude_argv(), cwd=tmp_path)

    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["is_error"] is True
    assert result["subtype"] == "success"
    assert result["result"] == "Credit balance is too low"
    assert "structured_output" not in result


def test_claude_can_omit_structured_output(tmp_path, fake_dir):
    fake_dir.queue({"result": "I refuse to answer in JSON"})
    write_prompt(tmp_path)
    proc = run(*claude_argv(), cwd=tmp_path)
    assert proc.returncode == 0
    assert "structured_output" not in json.loads(proc.stdout)


def test_claude_reports_permission_denials_and_turns(tmp_path, fake_dir):
    fake_dir.queue(
        {
            "output": SPEC_OUTPUT,
            "num_turns": 30,
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ls"}}],
        }
    )
    write_prompt(tmp_path)
    result = json.loads(run(*claude_argv(), cwd=tmp_path).stdout)
    assert result["num_turns"] == 30
    assert result["permission_denials"][0]["tool_name"] == "Bash"


def test_claude_bare_without_key_fails_without_consuming_the_queue(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)

    proc = run(*claude_argv("--bare"), cwd=tmp_path)

    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["is_error"] is True
    assert result["subtype"] == "success"
    assert result["result"] == "Not logged in · Please run /login"
    assert fake_dir.queue_remaining() == [{"output": SPEC_OUTPUT}]


def test_claude_bare_with_key_runs_normally(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)

    proc = run(*claude_argv("--bare"), cwd=tmp_path, env={"ANTHROPIC_API_KEY": "sk-fake"})

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["structured_output"] == SPEC_OUTPUT
    assert fake_dir.queue_remaining() == []
    assert fake_dir.calls()[0]["env"]["ANTHROPIC_API_KEY"] == "present"


def test_claude_sleeps_before_exiting(tmp_path, fake_dir):
    fake_dir.queue({"output": SPEC_OUTPUT, "sleep_s": 0.3})
    write_prompt(tmp_path)
    started = time.monotonic()
    proc = run(*claude_argv(), cwd=tmp_path)
    assert proc.returncode == 0
    assert time.monotonic() - started >= 0.25


def test_claude_hangs_after_applying_writes_until_killed(tmp_path, fake_dir):
    fake_dir.queue({"output": SPEC_OUTPUT, "writes": {"half-done.txt": "partial\n"}, "hang": True})
    write_prompt(tmp_path)

    proc = subprocess.Popen(
        claude_argv(),
        cwd=str(tmp_path),
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (tmp_path / "half-done.txt").exists():
            time.sleep(0.02)
        assert (tmp_path / "half-done.txt").exists(), "writes are applied before the hang"
        assert proc.poll() is None, "the fake is still running"
        assert len(fake_dir.calls()) == 1
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)
    assert fake_dir.queue_remaining() == []


# ---------------------------------------------------------------- fake codex


def test_codex_writes_the_last_message_file_and_events(tmp_path, fake_dir):
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)
    schema_file = tmp_path / "spec.schema.json"
    schema_file.write_text(json.dumps(SPEC_SCHEMA), encoding="utf-8")
    last_file = tmp_path / "transcripts" / "spec-1.last.json"

    proc = run(*codex_argv(tmp_path, schema_file, last_file), cwd=tmp_path)

    assert proc.returncode == 0
    assert json.loads(last_file.read_text(encoding="utf-8")) == SPEC_OUTPUT
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [event["type"] for event in events] == [
        "thread.started",
        "turn.started",
        "item.completed",
        "turn.completed",
    ]
    assert events[0]["thread_id"] == "fake-thread-0"
    assert events[2]["item"]["type"] == "agent_message"
    assert json.loads(events[2]["item"]["text"]) == SPEC_OUTPUT
    assert "usage" in events[3]


def test_codex_records_the_call_with_the_schema_from_its_file(tmp_path, fake_dir):
    fake_dir.queue({"output": SPEC_OUTPUT, "writes": {"src/y.py": "y = 1\n"}})
    write_prompt(tmp_path)
    schema_file = tmp_path / "spec.schema.json"
    schema_file.write_text(json.dumps(SPEC_SCHEMA), encoding="utf-8")

    run(*codex_argv(tmp_path, schema_file, tmp_path / "last.json"), cwd=tmp_path)

    (call,) = fake_dir.calls()
    assert call["binary"] == "codex"
    assert call["argv"][0] == "exec"
    assert call["argv"][-1] == sentence()
    assert call["prompt_file"] == PROMPT_REL
    assert call["prompt_text"] == PROMPT_TEXT
    assert call["schema"] == SPEC_SCHEMA
    assert (tmp_path / "src" / "y.py").exists()


def test_codex_honours_exit_code(tmp_path, fake_dir):
    fake_dir.queue({"output": SPEC_OUTPUT, "exit_code": 4})
    write_prompt(tmp_path)
    schema_file = tmp_path / "s.json"
    schema_file.write_text("{}", encoding="utf-8")
    proc = run(*codex_argv(tmp_path, schema_file, tmp_path / "last.json"), cwd=tmp_path)
    assert proc.returncode == 4


def test_codex_omits_the_last_file_when_the_entry_has_no_output(tmp_path, fake_dir):
    fake_dir.queue({"result": "nothing structured"})
    write_prompt(tmp_path)
    schema_file = tmp_path / "s.json"
    schema_file.write_text("{}", encoding="utf-8")
    last_file = tmp_path / "last.json"
    proc = run(*codex_argv(tmp_path, schema_file, last_file), cwd=tmp_path)
    assert proc.returncode == 0
    assert not last_file.exists()


def test_the_queue_is_shared_between_claude_and_codex(tmp_path, fake_dir):
    first = {"markdown": "# one", "open_questions": []}
    second = {"markdown": "# two", "open_questions": []}
    fake_dir.queue(first, second)
    write_prompt(tmp_path)
    schema_file = tmp_path / "s.json"
    schema_file.write_text("{}", encoding="utf-8")

    claude = run(*claude_argv(), cwd=tmp_path)
    codex = run(*codex_argv(tmp_path, schema_file, tmp_path / "last.json"), cwd=tmp_path)

    assert (claude.returncode, codex.returncode) == (0, 0)
    assert json.loads(claude.stdout)["structured_output"] == first
    assert json.loads((tmp_path / "last.json").read_text(encoding="utf-8")) == second
    assert fake_dir.queue_remaining() == []
    assert [call["binary"] for call in fake_dir.calls()] == ["claude", "codex"]


@pytest.mark.parametrize("binary", ["claude", "codex"])
def test_empty_queue_exits_3(tmp_path, fake_dir, binary):
    write_prompt(tmp_path)
    schema_file = tmp_path / "s.json"
    schema_file.write_text("{}", encoding="utf-8")
    argv = (
        claude_argv()
        if binary == "claude"
        else codex_argv(tmp_path, schema_file, tmp_path / "last.json")
    )
    proc = run(*argv, cwd=tmp_path)
    assert proc.returncode == 3
    assert proc.stderr.strip() == "fake harness: queue empty"


# ---------------------------------------------------------------- fake gh


def test_gh_repo_view(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    proc = run("gh", "repo", "view", "--json", "nameWithOwner", cwd=tmp_path)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"nameWithOwner": "owner/name"}
    assert fake_dir.gh_calls()[0]["argv"] == ["repo", "view", "--json", "nameWithOwner"]
    assert fake_dir.gh_calls()[0]["cwd"] == str(tmp_path)


def test_gh_auth_status_follows_auth_ok(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    assert run("gh", "auth", "status", cwd=tmp_path).returncode == 0
    fake_dir.set_gh_state(gh_state_with(auth_ok=False))
    failed = run("gh", "auth", "status", cwd=tmp_path)
    assert failed.returncode == 1
    assert "not logged" in failed.stderr.lower()


def test_gh_issue_view_renders_labels_as_objects(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(issues={"42": issue(42, labels=["factory", "bug"])}))
    proc = run(
        "gh", "issue", "view", "42", "--json", "number,title,body,url,labels,state", cwd=tmp_path
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["number"] == 42
    assert payload["state"] == "OPEN"
    assert payload["labels"] == [{"name": "factory"}, {"name": "bug"}]


def test_gh_issue_view_unknown_issue_exits_1(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    proc = run("gh", "issue", "view", "99", "--json", "number", cwd=tmp_path)
    assert proc.returncode == 1
    assert "no issue 99" in proc.stderr


def test_gh_issue_list_filters_by_label_and_state_ascending(tmp_path, fake_dir):
    fake_dir.set_gh_state(
        gh_state_with(
            issues={
                "7": issue(7, labels=["factory"]),
                "42": issue(42, labels=["factory"]),
                "9": issue(9, labels=["other"]),
                "11": issue(11, labels=["factory"], state="CLOSED"),
            }
        )
    )
    proc = run(
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "factory",
        "--json",
        "number",
        cwd=tmp_path,
    )
    assert proc.returncode == 0
    assert [row["number"] for row in json.loads(proc.stdout)] == [7, 42]

    limited = run(
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "factory",
        "--limit",
        "1",
        "--json",
        "number",
        cwd=tmp_path,
    )
    assert [row["number"] for row in json.loads(limited.stdout)] == [7]


def test_gh_issue_edit_labels_is_idempotent(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(issues={"42": issue(42, labels=["factory"])}))
    assert (
        run("gh", "issue", "edit", "42", "--remove-label", "factory", cwd=tmp_path).returncode == 0
    )
    assert fake_dir.issue(42)["labels"] == []
    assert (
        run("gh", "issue", "edit", "42", "--remove-label", "factory", cwd=tmp_path).returncode == 0
    )
    assert run("gh", "issue", "edit", "42", "--add-label", "factory", cwd=tmp_path).returncode == 0
    assert fake_dir.issue(42)["labels"] == ["factory"]


def test_gh_labels(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(labels=["factory"]))
    listed = run("gh", "label", "list", "--json", "name", cwd=tmp_path)
    assert json.loads(listed.stdout) == [{"name": "factory"}]

    created = run("gh", "label", "create", "shiny", "--color", "5319e7", cwd=tmp_path)
    assert created.returncode == 0
    assert fake_dir.gh_state()["labels"] == ["factory", "shiny"]

    duplicate = run("gh", "label", "create", "shiny", cwd=tmp_path)
    assert duplicate.returncode == 1
    assert "already exists" in duplicate.stderr
    assert run("gh", "label", "create", "shiny", "--force", cwd=tmp_path).returncode == 0


def test_gh_pr_create_prints_the_url_and_records_the_pr(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    body_file = tmp_path / "body.md"
    body_file.write_text("Closes #42\n", encoding="utf-8")

    proc = run(
        "gh",
        "pr",
        "create",
        "--draft",
        "--head",
        "factory/42",
        "--base",
        "main",
        "--title",
        "Add a greeting helper",
        "--body-file",
        str(body_file),
        cwd=tmp_path,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "https://github.com/owner/name/pull/117"
    created = fake_dir.pr(117)
    assert created["isDraft"] is True
    assert created["state"] == "OPEN"
    assert created["headRefName"] == "factory/42"
    assert created["baseRefName"] == "main"
    assert created["body"] == "Closes #42\n"
    assert fake_dir.gh_state()["next_pr_number"] == 118


def test_gh_pr_list_projects_requested_fields(tmp_path, fake_dir):
    fake_dir.set_gh_state(
        gh_state_with(
            prs={
                "117": pull(117, head="factory/42", state="CLOSED"),
                "118": pull(118, head="factory/42"),
                "119": pull(119, head="factory/7"),
            }
        )
    )
    proc = run(
        "gh",
        "pr",
        "list",
        "--head",
        "factory/42",
        "--state",
        "all",
        "--json",
        "number,url,isDraft,state,headRefName",
        cwd=tmp_path,
    )
    rows = json.loads(proc.stdout)
    assert [row["number"] for row in rows] == [118, 117]
    assert set(rows[0]) == {"number", "url", "isDraft", "state", "headRefName"}

    open_only = run(
        "gh",
        "pr",
        "list",
        "--head",
        "factory/42",
        "--state",
        "open",
        "--json",
        "number",
        cwd=tmp_path,
    )
    assert [row["number"] for row in json.loads(open_only.stdout)] == [118]


def test_gh_pr_view_comments_and_fields(tmp_path, fake_dir):
    fake_dir.set_gh_state(
        gh_state_with(prs={"117": pull(117, head="factory/42", comments=[{"body": "hello"}])})
    )
    comments = run("gh", "pr", "view", "117", "--json", "comments", cwd=tmp_path)
    assert json.loads(comments.stdout) == {"comments": [{"body": "hello"}]}

    fields = run("gh", "pr", "view", "117", "--json", "number,url,isDraft,state", cwd=tmp_path)
    assert json.loads(fields.stdout) == {
        "number": 117,
        "url": "https://github.com/owner/name/pull/117",
        "isDraft": True,
        "state": "OPEN",
    }


def test_gh_pr_comment_appends_from_body_and_body_file(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(prs={"117": pull(117, head="factory/42")}))
    body_file = tmp_path / "gate.md"
    body_file.write_text(
        "<!-- factory:gate:open_questions:abc -->\nAnswer them.\n", encoding="utf-8"
    )

    inline = run("gh", "pr", "comment", "117", "--body", "first", cwd=tmp_path)
    assert inline.returncode == 0
    assert inline.stdout.strip().startswith("https://github.com/owner/name/pull/117#issuecomment-")
    assert (
        run("gh", "pr", "comment", "117", "--body-file", str(body_file), cwd=tmp_path).returncode
        == 0
    )

    bodies = [comment["body"] for comment in fake_dir.pr(117)["comments"]]
    assert bodies[0] == "first"
    assert "factory:gate:open_questions" in bodies[1]


def test_gh_pr_ready_and_close(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(prs={"117": pull(117, head="factory/42")}))
    assert run("gh", "pr", "ready", "117", cwd=tmp_path).returncode == 0
    assert fake_dir.pr(117)["isDraft"] is False
    assert run("gh", "pr", "ready", "117", cwd=tmp_path).returncode == 0

    assert run("gh", "pr", "close", "117", "--comment", "abandoned", cwd=tmp_path).returncode == 0
    assert fake_dir.pr(117)["state"] == "CLOSED"
    assert fake_dir.pr(117)["comments"][-1]["body"] == "abandoned"
    assert run("gh", "pr", "close", "117", cwd=tmp_path).returncode == 0


def test_gh_fail_next_fires_once(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    fake_dir.fail_next("pr create")

    failed = run(
        "gh",
        "pr",
        "create",
        "--draft",
        "--head",
        "factory/42",
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
        cwd=tmp_path,
    )
    assert failed.returncode == 1
    assert "pr create" in failed.stderr
    assert fake_dir.gh_state()["prs"] == {}

    retried = run(
        "gh",
        "pr",
        "create",
        "--draft",
        "--head",
        "factory/42",
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
        cwd=tmp_path,
    )
    assert retried.returncode == 0
    assert fake_dir.pr(117) is not None
    assert fake_dir.gh_state()["fail_next"] == []


def test_gh_unknown_subcommand_exits_2_naming_the_argv(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with())
    proc = run("gh", "api", "repos/owner/name/pulls", cwd=tmp_path)
    assert proc.returncode == 2
    assert "gh api repos/owner/name/pulls" in proc.stderr
    assert fake_dir.gh_calls()[-1]["argv"] == ["api", "repos/owner/name/pulls"]


def test_gh_records_every_call(tmp_path, fake_dir):
    fake_dir.set_gh_state(gh_state_with(issues={"42": issue(42, labels=["factory"])}))
    run("gh", "repo", "view", "--json", "nameWithOwner", cwd=tmp_path)
    run("gh", "issue", "view", "42", "--json", "number", cwd=tmp_path)
    assert [call["argv"][:2] for call in fake_dir.gh_calls()] == [
        ["repo", "view"],
        ["issue", "view"],
    ]


# ---------------------------------------------------------------- repository fixtures


def test_target_is_a_clone_of_origin_seeded_as_the_contract_says(target, origin, git_target):
    assert git_target("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git_target("rev-parse", "HEAD") == git_target("rev-parse", "origin/main")
    for rel in (
        "checks.py",
        "factory.toml",
        "Makefile",
        "AGENTS.md",
        "CLAUDE.md",
        "REVIEW.md",
        ".gitignore",
        "src/app.py",
        "tests/test_app.py",
    ):
        assert (target / rel).is_file(), rel
    assert "@AGENTS.md" in (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".factory/" in (target / ".gitignore").read_text(encoding="utf-8")
    assert 'checks = [["python3", "checks.py"]]' in (target / "factory.toml").read_text(
        encoding="utf-8"
    )
    assert (origin / "refs" / "heads" / "main").exists() or git_target("ls-remote", str(origin))


def test_target_checks_are_green_until_a_file_named_red_appears(target):
    green = run("python3", "checks.py", cwd=target)
    assert green.returncode == 0
    assert green.stdout.strip()
    (target / "RED").write_text("", encoding="utf-8")
    red = run("python3", "checks.py", cwd=target)
    assert red.returncode == 1
    assert red.stdout.strip()


def test_target_registers_issue_42(target, fake_dir):
    state = fake_dir.gh_state()
    assert state["repo"] == "owner/name"
    assert state["issues"]["42"]["labels"] == ["factory"]
    assert state["issues"]["42"]["state"] == "OPEN"
    assert state["prs"] == {}


def test_worktree_fixture_points_where_the_factory_puts_worktrees(target, worktree):
    assert worktree() == target / ".factory" / "worktrees" / "42"
    assert worktree(7) == target / ".factory" / "worktrees" / "7"


# ---------------------------------------------------------------- the real launch conditions


def test_fake_dir_helper_doubles_as_its_path(fake_dir):
    assert Path(fake_dir).is_dir()
    assert fake_dir / "harness_queue.jsonl" == Path(fake_dir) / "harness_queue.jsonl"
    assert str(fake_dir) == os.fspath(fake_dir)


def test_fakes_run_under_the_factorys_allowlisted_environment(tmp_path, fake_dir):
    """Design §8 hands the harness PATH, HOME and locale only: no FACTORY_FAKE_DIR, no GH_TOKEN.

    The pointer file next to the fake is then the only way to find the fake directory, and the
    recorded environment is what the factory's own tests assert its filtering against.
    """
    fake_dir.queue(SPEC_OUTPUT)
    write_prompt(tmp_path)
    allowlisted = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8"}

    proc = subprocess.run(
        claude_argv(),
        cwd=str(tmp_path),
        text=True,
        capture_output=True,
        env=allowlisted,
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["structured_output"] == SPEC_OUTPUT
    (call,) = fake_dir.calls()
    assert call["env_keys"] == ["HOME", "LANG", "PATH"]
    assert call["env"] == {
        "ANTHROPIC_API_KEY": "absent",
        "CODEX_API_KEY": "absent",
        "GH_TOKEN": "absent",
        "FACTORY_FAKE_DIR": "absent",
        "CLAUDECODE": "absent",
    }


def test_a_real_worktree_can_be_built_where_the_factory_puts_it(
    target, git_target, worktree, fake_dir
):
    """The rig is real git: `.factory/worktrees/42` is a working worktree on `factory/42`, and a
    fake harness launched inside it writes into it, exactly as a write stage would."""
    path = worktree(42)
    git_target("worktree", "add", "-b", "factory/42", str(path), "origin/main")
    assert (path / "checks.py").is_file()
    assert git_target("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "factory/42"
    assert git_target("status", "--porcelain") == "", ".factory/ is ignored in the target"

    fake_dir.queue({"output": SPEC_OUTPUT, "writes": {"work/42/spec.md": "# Spec\n"}})
    write_prompt(path)

    proc = run(*claude_argv(), cwd=path)

    assert proc.returncode == 0
    assert (path / "work" / "42" / "spec.md").read_text(encoding="utf-8") == "# Spec\n"
    assert fake_dir.calls()[0]["cwd"] == str(path)
