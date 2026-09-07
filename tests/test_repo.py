"""Unit tests for factory.repo.

Real git against a local bare "origin" (tests/FAKES.md: git is the thing under test, so it is not faked).
Self-contained on purpose: no conftest fixtures, no factory fakes, no network — every repository below is
built in tmp_path, and the git identity/config environment is pinned so a developer's ~/.gitconfig cannot
change a result.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.errors import FactoryError
from factory.repo import Repo, branch_name, default_identity_env, git

IDENTITY = {
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


# ---------------------------------------------------------------- helpers


def run_git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(cwd: Path, message: str) -> str:
    run_git(["add", "-A"], cwd)
    run_git(["commit", "-m", message], cwd)
    return run_git(["rev-parse", "HEAD"], cwd).strip()


@dataclass
class Sandbox:
    origin: Path
    checkout: Path
    repo: Repo
    base: str  # sha of the initial commit on main

    def clone(self, name: str) -> Path:
        path = self.origin.parent / name
        run_git(["clone", str(self.origin), str(path)], self.origin.parent)
        return path

    def origin_sha(self, ref: str) -> str | None:
        res = git(["rev-parse", "--verify", ref], self.origin, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def move_main(self, filename: str = "mainfile.txt") -> str:
        """Advance origin/main from a second clone (nothing local knows about it until fetch)."""
        other = self.clone(f"mover-{filename}")
        write(other / filename, "moved\n")
        sha = commit(other, f"main: add {filename}")
        run_git(["push", "origin", "main"], other)
        return sha


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def git_env(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))  # deliberately absent
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    return home


@pytest.fixture
def sandbox(tmp_path, git_env) -> Sandbox:
    origin = tmp_path / "origin.git"
    run_git(["init", "--bare", "--initial-branch=main", str(origin)], tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_git(["init", "--initial-branch=main"], checkout)
    write(checkout / "src" / "app.py", "print('hello')\n")
    write(checkout / "README.md", "# demo\n")
    base = commit(checkout, "init")
    run_git(["remote", "add", "origin", str(origin)], checkout)
    run_git(["push", "-u", "origin", "main"], checkout)
    return Sandbox(origin=origin, checkout=checkout, repo=Repo(checkout), base=base)


@pytest.fixture
def wt(sandbox) -> Path:
    return sandbox.repo.ensure_worktree(42, start_point=sandbox.base)


# ---------------------------------------------------------------- git() and discover


def test_branch_name_and_default_identity():
    assert branch_name(42) == "factory/42"
    assert default_identity_env() == {
        "GIT_AUTHOR_NAME": "factory",
        "GIT_AUTHOR_EMAIL": "factory@localhost",
        "GIT_COMMITTER_NAME": "factory",
        "GIT_COMMITTER_EMAIL": "factory@localhost",
    }


def test_git_returns_the_result_when_check_is_false(sandbox):
    res = git(["rev-parse", "--verify", "no-such-ref"], sandbox.checkout, check=False)
    assert res.returncode != 0
    assert res.stdout == ""
    assert "fatal" in res.stderr


def test_git_error_names_the_command_and_the_directory(sandbox):
    with pytest.raises(FactoryError) as exc:
        git(["rev-parse", "--verify", "no-such-ref"], sandbox.checkout)
    assert "rev-parse" in exc.value.message
    assert str(sandbox.checkout) in exc.value.message


def test_git_error_names_a_missing_directory(tmp_path, git_env):
    with pytest.raises(FactoryError) as exc:
        git(["status"], tmp_path / "gone")
    assert "gone" in exc.value.message


def test_discover_finds_the_checkout_root_from_a_subdirectory(sandbox):
    repo = Repo.discover(sandbox.checkout / "src")
    assert repo.root.resolve() == sandbox.checkout.resolve()
    assert repo.factory_dir == repo.root / ".factory"


def test_discover_refuses_a_path_inside_a_factory_worktree(sandbox, wt):
    with pytest.raises(FactoryError) as exc:
        Repo.discover(wt)
    assert "factory worktree" in exc.value.message
    assert str(wt) in exc.value.message
    with pytest.raises(FactoryError):
        Repo.discover(wt / "src")


def test_discover_outside_a_checkout(tmp_path, git_env):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(FactoryError) as exc:
        Repo.discover(outside)
    assert "not inside a git checkout" in exc.value.message


# ---------------------------------------------------------------- worktree recovery (design §7)


def test_ensure_worktree_creates_the_branch_from_a_start_point(sandbox):
    repo = sandbox.repo
    created = repo.ensure_worktree(42, start_point=sandbox.base)
    assert created == sandbox.checkout / ".factory" / "worktrees" / "42"
    assert (created / "src" / "app.py").exists()
    assert repo.local_branch_exists("factory/42")
    assert repo.worktree_for_issue_is_on_branch(created, "factory/42")
    assert repo.head(created) == sandbox.base
    assert repo.worktree_exists(42)


def test_ensure_worktree_returns_the_existing_worktree(sandbox, wt):
    repo = sandbox.repo
    write(wt / "scratch.txt", "keep me\n")
    again = repo.ensure_worktree(42)
    assert again == wt
    assert (wt / "scratch.txt").exists()  # an existing worktree is returned untouched


def test_ensure_worktree_rebuilds_from_the_local_branch(sandbox, wt):
    repo = sandbox.repo
    shutil.rmtree(wt)  # a wiped .factory/ (rule 6) leaves a stale worktree registration behind
    assert not repo.worktree_exists(42)
    rebuilt = repo.ensure_worktree(42)
    assert rebuilt == wt
    assert (rebuilt / "src" / "app.py").exists()
    assert repo.head(rebuilt) == sandbox.base


def test_ensure_worktree_recovers_from_the_remote_branch(sandbox, wt):
    repo = sandbox.repo
    write(wt / "src" / "new.py", "x\n")
    pushed = repo.commit_all(wt, "factory(42): build")
    repo.push(wt, "factory/42")
    repo.remove_worktree(42)
    repo.delete_branch("factory/42", remote=False)
    assert not repo.local_branch_exists("factory/42")

    repo.fetch()
    recovered = repo.ensure_worktree(42)
    assert repo.head(recovered) == pushed
    assert (recovered / "src" / "new.py").exists()
    upstream = git(["rev-parse", "--abbrev-ref", "@{u}"], recovered).stdout.strip()
    assert upstream == "origin/factory/42"


def test_ensure_worktree_without_any_branch_names_the_command_that_starts_one(sandbox):
    with pytest.raises(FactoryError) as exc:
        sandbox.repo.ensure_worktree(99)
    assert "run `factory spec 99`" in exc.value.message


def test_remove_worktree_and_delete_branch_are_idempotent(sandbox, wt):
    repo = sandbox.repo
    repo.push(wt, "factory/42")
    assert sandbox.origin_sha("factory/42") is not None

    repo.remove_worktree(42)
    assert not wt.exists()
    repo.remove_worktree(42)  # absent worktree: no error

    repo.delete_branch("factory/42", remote=True)
    assert not repo.local_branch_exists("factory/42")
    assert sandbox.origin_sha("factory/42") is None
    repo.delete_branch("factory/42", remote=True)  # absent branch: no error


def test_worktree_for_issue_is_on_branch(sandbox, wt):
    repo = sandbox.repo
    assert repo.worktree_for_issue_is_on_branch(wt, "factory/42")
    assert not repo.worktree_for_issue_is_on_branch(wt, "main")
    run_git(["checkout", "--detach"], wt)
    assert not repo.worktree_for_issue_is_on_branch(wt, "factory/42")


# ---------------------------------------------------------------- excludes (deviation 12)


def test_ensure_excludes_is_idempotent(sandbox):
    repo = sandbox.repo
    repo.ensure_excludes([".factory/", "*.tmp"])
    repo.ensure_excludes([".factory/", "*.tmp", "scratch/"])
    lines = [
        line.strip()
        for line in (sandbox.checkout / ".git" / "info" / "exclude").read_text().splitlines()
    ]
    assert lines.count(".factory/") == 1
    assert lines.count("*.tmp") == 1
    assert lines.count("scratch/") == 1
    assert sum(1 for line in lines if line.startswith("# factory:")) == 1


def test_a_worktree_inherits_the_excludes_written_by_ensure_worktree(sandbox, wt):
    repo = sandbox.repo
    write(wt / ".factory" / "tmp" / "review-1.diff", "diff --git ...\n")
    write(wt / "__pycache__" / "app.pyc", "junk\n")
    assert repo.status_porcelain(wt) == []
    assert repo.is_clean(wt)


# ---------------------------------------------------------------- working tree state


def test_status_and_changed_paths_cover_untracked_and_renames(sandbox, wt):
    repo = sandbox.repo
    write(wt / "src" / "app.py", "print('bye')\n")
    write(wt / "untracked.txt", "u\n")
    run_git(["mv", "README.md", "DOCS.md"], wt)

    lines = repo.status_porcelain(wt)
    assert len(lines) == 3
    assert any(line.startswith("R ") for line in lines)

    paths = repo.changed_paths_in_worktree(wt)
    assert set(paths) == {"src/app.py", "untracked.txt", "DOCS.md"}
    assert "README.md" not in paths  # a rename reports the new path
    assert not repo.is_clean(wt)


def test_has_changes_can_be_scoped_to_paths(sandbox, wt):
    repo = sandbox.repo
    assert repo.has_changes(wt) is False
    write(wt / "work" / "42" / "state.json", "{}\n")
    assert repo.has_changes(wt) is True
    assert repo.has_changes(wt, ["work/42"]) is True
    assert repo.has_changes(wt, ["src"]) is False


def test_reset_hard_discards_edits_untracked_and_ignored_files_but_keeps_dot_factory(sandbox, wt):
    """A rejected stage must leave nothing behind, including under an ignored path: `.venv/bin/pytest` or a
    stale `__pycache__` would otherwise still be there when the next attempt runs the checks."""
    repo = sandbox.repo
    write(wt / "src" / "app.py", "print('broken')\n")
    write(wt / "junk" / "leftover.txt", "junk\n")
    write(wt / ".venv" / "bin" / "pytest", "#!/bin/sh\nexit 0\n")
    write(wt / "src" / "__pycache__" / "app.cpython-312.pyc", "stale\n")
    write(wt / ".factory" / "tmp" / "keep.diff", "kept\n")

    repo.reset_hard(wt)

    assert (wt / "src" / "app.py").read_text() == "print('hello')\n"
    assert not (wt / "junk").exists()
    assert not (wt / ".venv").exists(), "an ignored path is not a hiding place (git clean -fdx)"
    assert not (wt / "src" / "__pycache__").exists()
    # the worktree's own host-local directory survives: a gate failure is still reading the diff it wrote there
    assert (wt / ".factory" / "tmp" / "keep.diff").exists()
    assert repo.is_clean(wt)


def test_stage_all_stages_every_change_without_committing(sandbox, wt):
    repo = sandbox.repo
    head = repo.head(wt)
    write(wt / "src" / "app.py", "print('edited')\n")
    write(wt / "new.txt", "new\n")
    (wt / "README.md").unlink()
    write(wt / ".venv" / "bin" / "pytest", "ignored\n")

    repo.stage_all(wt)

    staged = run_git(["diff", "--cached", "--name-only"], wt).split()
    assert sorted(staged) == ["README.md", "new.txt", "src/app.py"]
    assert repo.head(wt) == head, "stage_all never commits"


def test_clean_ignored_removes_only_ignored_files(sandbox, wt):
    repo = sandbox.repo
    write(wt / ".venv" / "bin" / "pytest", "#!/bin/sh\nexit 0\n")
    write(wt / "src" / "__pycache__" / "app.cpython-312.pyc", "stale\n")
    write(wt / "src" / "app.py", "print('work in progress')\n")
    write(wt / "new_module.py", "x = 1\n")
    write(wt / ".factory" / "tmp" / "review-1.diff", "kept\n")

    repo.clean_ignored(wt)

    assert not (wt / ".venv").exists()
    assert not (wt / "src" / "__pycache__").exists()
    # the work under review is untouched: edits, new untracked files and .factory/ all survive
    assert (wt / "src" / "app.py").read_text() == "print('work in progress')\n"
    assert (wt / "new_module.py").exists()
    assert (wt / ".factory" / "tmp" / "review-1.diff").exists()


def test_clean_ignored_honours_the_keep_list(sandbox, wt):
    repo = sandbox.repo
    write(wt / ".venv" / "bin" / "pytest", "x\n")
    write(wt / "node_modules" / "pkg" / "index.js", "x\n")
    write(wt / ".factory" / "tmp" / "keep.diff", "kept\n")

    repo.clean_ignored(wt, keep=(".factory/", "node_modules/"))

    assert not (wt / ".venv").exists()
    assert (wt / "node_modules" / "pkg" / "index.js").exists()
    assert (wt / ".factory" / "tmp" / "keep.diff").exists()


def test_clean_ignored_is_a_no_op_when_nothing_is_ignored(sandbox, wt):
    repo = sandbox.repo
    write(wt / "src" / "app.py", "print('edited')\n")

    repo.clean_ignored(wt)

    assert (wt / "src" / "app.py").read_text() == "print('edited')\n"


def test_reset_hard_rewinds_to_a_ref(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    first = repo.commit_all(wt, "one")
    write(wt / "b.txt", "2\n")
    repo.commit_all(wt, "two")

    repo.reset_hard(wt, first)

    assert repo.head(wt) == first
    assert not (wt / "b.txt").exists()


# ---------------------------------------------------------------- commits


def test_commit_all_commits_only_the_named_paths(sandbox, wt):
    repo = sandbox.repo
    write(wt / "work" / "42" / "state.json", '{"outcome": null}\n')
    write(wt / "src" / "other.py", "x\n")

    sha = repo.commit_all(wt, "factory(42): park", paths=["work/42/state.json"])

    assert repo.head(wt) == sha
    assert repo.changed_paths_of_commit(wt, sha) == ["work/42/state.json"]
    assert repo.changed_paths_in_worktree(wt) == ["src/other.py"]
    assert repo.has_changes(wt, ["work/42"]) is False


def test_commit_all_commits_everything_by_default(sandbox, wt):
    repo = sandbox.repo
    write(wt / "work" / "42" / "spec.md", "# spec\n")
    write(wt / "src" / "app.py", "print('changed')\n")

    sha = repo.commit_all(wt, "factory(42): spec")

    assert set(repo.changed_paths_of_commit(wt, sha)) == {"work/42/spec.md", "src/app.py"}
    assert repo.is_clean(wt)
    assert run_git(["log", "-1", "--format=%s"], wt).strip() == "factory(42): spec"


def test_commit_all_refuses_when_there_is_nothing_to_commit(sandbox, wt):
    with pytest.raises(FactoryError) as exc:
        sandbox.repo.commit_all(wt, "empty")
    assert str(wt) in exc.value.message


def test_commit_all_falls_back_to_the_default_identity(sandbox, wt, monkeypatch):
    for key in IDENTITY:
        monkeypatch.delenv(key, raising=False)
    write(wt / "f.txt", "f\n")

    sha = sandbox.repo.commit_all(wt, "no identity configured")

    assert sandbox.repo.head(wt) == sha
    ident = run_git(["log", "-1", "--format=%an <%ae>|%cn <%ce>"], wt).strip()
    assert ident == "factory <factory@localhost>|factory <factory@localhost>"


def test_commit_all_keeps_the_configured_identity_when_there_is_one(sandbox, wt):
    write(wt / "f.txt", "f\n")
    sandbox.repo.commit_all(wt, "with identity")
    assert run_git(["log", "-1", "--format=%an <%ae>"], wt).strip() == "Tester <tester@example.com>"


def test_checkout_paths_restores_files_and_ignores_missing_ones(sandbox, wt):
    repo = sandbox.repo
    write(wt / "keep.txt", "v1\n")
    sha = repo.commit_all(wt, "add keep")
    (wt / "keep.txt").unlink()

    repo.checkout_paths(wt, sha, ["keep.txt", "never-existed.txt"])
    assert (wt / "keep.txt").read_text() == "v1\n"

    repo.checkout_paths(wt, sha, ["never-existed.txt"])  # nothing matches: no error
    repo.checkout_paths(wt, sha, [])
    assert not (wt / "never-existed.txt").exists()


# ---------------------------------------------------------------- diffs


def test_diff_uses_the_three_dot_form_and_excludes_work(sandbox, wt):
    repo = sandbox.repo
    write(wt / "src" / "app.py", "print('changed')\n")
    write(wt / "work" / "42" / "spec.md", "# spec\n")
    repo.commit_all(wt, "factory(42): build")
    sandbox.move_main()
    repo.fetch()

    text = repo.diff(wt, "origin/main")
    assert "src/app.py" in text
    assert "work/42/spec.md" not in text
    assert "mainfile.txt" not in text  # three dots: from the merge base, not from the moved tip

    two_dot = git(["diff", "origin/main", "HEAD", "--", "."], wt).stdout
    assert "mainfile.txt" in two_dot  # the contrast that proves the three dots

    from_sha = repo.diff(wt, sandbox.base)
    assert "src/app.py" in from_sha
    assert "work/42/spec.md" not in from_sha
    assert "work/42/spec.md" in repo.diff(wt, sandbox.base, exclude=())


def test_diff_stat_numstat_and_diff_paths(sandbox, wt):
    repo = sandbox.repo
    write(wt / "src" / "app.py", "a\nb\nc\n")
    (wt / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    write(wt / "work" / "42" / "notes.md", "n\n")
    repo.commit_all(wt, "factory(42): build")

    stat = repo.diff_stat(wt, sandbox.base)
    assert "src/app.py" in stat
    assert "work/42" not in stat

    rows = repo.diff_numstat(wt, sandbox.base)
    by_path = {path: (added, deleted) for added, deleted, path in rows}
    assert set(by_path) == {"src/app.py", "data.bin"}
    assert by_path["data.bin"] == (0, 0)  # binary
    assert by_path["src/app.py"] == (3, 1)

    only_app = repo.diff_paths(wt, sandbox.base, "HEAD", ["src/app.py"])
    assert "src/app.py" in only_app
    assert "data.bin" not in only_app
    assert repo.diff_paths(wt, sandbox.base, "HEAD", []) == ""


def test_diff_numstat_reports_the_new_path_of_a_rename(sandbox, wt):
    repo = sandbox.repo
    run_git(["mv", "src/app.py", "src/renamed.py"], wt)
    repo.commit_all(wt, "factory(42): rename")

    paths = [path for _, _, path in repo.diff_numstat(wt, sandbox.base)]

    assert paths == ["src/renamed.py"]
    assert all("=>" not in path for path in paths)


def test_changed_paths_between_reports_both_sides_of_a_rename(sandbox, wt):
    repo = sandbox.repo
    run_git(["mv", "README.md", "notes.md"], wt)
    sha = repo.commit_all(wt, "factory(42): move the readme")

    # the protected-path gate must see the deletion, not only the new path
    assert set(repo.changed_paths_between(wt, sandbox.base)) == {"README.md", "notes.md"}
    assert set(repo.changed_paths_of_commit(wt, sha)) == {"README.md", "notes.md"}


def test_changed_paths_between_two_commits(sandbox, wt):
    repo = sandbox.repo
    write(wt / "one.txt", "1\n")
    first = repo.commit_all(wt, "one")
    write(wt / "two.txt", "2\n")
    second = repo.commit_all(wt, "two")

    assert repo.changed_paths_between(wt, first, second) == ["two.txt"]
    assert set(repo.changed_paths_between(wt, sandbox.base)) == {"one.txt", "two.txt"}


def test_parent_and_changed_paths_of_a_root_commit(sandbox):
    repo = sandbox.repo
    root = git(["rev-list", "--max-parents=0", "HEAD"], sandbox.checkout).stdout.strip()

    assert root == sandbox.base
    assert repo.parent(root) is None
    assert set(repo.changed_paths_of_commit(sandbox.checkout, root)) == {"README.md", "src/app.py"}


def test_parent_of_a_child_commit(sandbox, wt):
    repo = sandbox.repo
    write(wt / "child.txt", "c\n")
    child = repo.commit_all(wt, "child")
    assert repo.parent(child, wt) == sandbox.base


def test_is_ancestor(sandbox, wt):
    repo = sandbox.repo
    write(wt / "later.txt", "l\n")
    later = repo.commit_all(wt, "later")

    assert repo.is_ancestor(sandbox.base, later, wt) is True
    assert repo.is_ancestor(later, sandbox.base, wt) is False
    assert repo.is_ancestor("0" * 40, "HEAD", wt) is False  # a commit we do not have is unreachable


def test_code_changed_between_ignores_work(sandbox, wt):
    repo = sandbox.repo
    start = repo.head(wt)

    write(wt / "work" / "42" / "state.json", "{}\n")
    repo.commit_all(wt, "factory(42): state only")
    assert repo.code_changed_between(wt, start) is False

    write(wt / "src" / "app.py", "print('fixed')\n")
    repo.commit_all(wt, "factory(42): fix 1")
    assert repo.code_changed_between(wt, start) is True


# ---------------------------------------------------------------- remote


def test_push_then_force_with_lease_after_a_rewind(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    first = repo.commit_all(wt, "one")
    repo.push(wt, "factory/42")
    assert sandbox.origin_sha("factory/42") == first

    write(wt / "b.txt", "2\n")
    second = repo.commit_all(wt, "two")
    repo.push(wt, "factory/42")
    assert sandbox.origin_sha("factory/42") == second

    repo.reset_hard(wt, first)  # what `--force` does to the branch
    with pytest.raises(FactoryError) as exc:
        repo.push(wt, "factory/42")
    assert "push" in exc.value.message

    repo.push(wt, "factory/42", force_with_lease=True)
    assert sandbox.origin_sha("factory/42") == first


def test_push_if_ahead_only_pushes_when_it_must(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    repo.commit_all(wt, "one")

    assert repo.push_if_ahead(wt, "factory/42") is True  # remote branch absent
    assert repo.push_if_ahead(wt, "factory/42") is False  # equal

    write(wt / "b.txt", "2\n")
    ahead = repo.commit_all(wt, "two")
    assert repo.push_if_ahead(wt, "factory/42") is True
    assert sandbox.origin_sha("factory/42") == ahead


def test_remote_ahead_state_transitions(sandbox, wt):
    repo = sandbox.repo
    assert repo.remote_ahead_state(wt, "factory/42") == "absent"

    repo.push(wt, "factory/42")
    assert repo.remote_ahead_state(wt, "factory/42") == "equal"

    write(wt / "local.txt", "l\n")
    repo.commit_all(wt, "local work")
    assert repo.remote_ahead_state(wt, "factory/42") == "local_ahead"
    repo.push(wt, "factory/42")

    _push_from_another_clone(sandbox, "remote.txt")
    repo.fetch()
    assert repo.remote_ahead_state(wt, "factory/42") == "remote_ahead"

    write(wt / "local2.txt", "l2\n")
    repo.commit_all(wt, "more local work")
    assert repo.remote_ahead_state(wt, "factory/42") == "diverged"


def _push_from_another_clone(sandbox: Sandbox, filename: str) -> str:
    """Commit `filename` on factory/42 from a second clone and push it (someone else's host)."""
    other = sandbox.clone(f"other-{filename}")
    run_git(["checkout", "factory/42"], other)
    write(other / filename, "from elsewhere\n")
    sha = commit(other, f"elsewhere: {filename}")
    run_git(["push", "origin", "factory/42"], other)
    return sha


def test_sync_with_remote_fast_forwards(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    repo.commit_all(wt, "one")
    repo.push(wt, "factory/42")
    remote_sha = _push_from_another_clone(sandbox, "remote.txt")
    repo.fetch()

    head = repo.sync_with_remote(wt, "factory/42")

    assert head == remote_sha
    assert repo.head(wt) == remote_sha
    assert (wt / "remote.txt").exists()


def test_sync_with_remote_refuses_a_diverged_branch(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    repo.commit_all(wt, "one")
    repo.push(wt, "factory/42")
    _push_from_another_clone(sandbox, "remote.txt")
    write(wt / "local.txt", "l\n")
    repo.commit_all(wt, "local work")
    repo.fetch()

    with pytest.raises(FactoryError) as exc:
        repo.sync_with_remote(wt, "factory/42")

    assert "diverged" in exc.value.message
    assert "factory/42" in exc.value.message


def test_sync_with_remote_is_a_no_op_when_the_remote_branch_is_absent(sandbox, wt):
    repo = sandbox.repo
    write(wt / "a.txt", "1\n")
    head = repo.commit_all(wt, "one")
    assert repo.sync_with_remote(wt, "factory/42") == head
    assert repo.head(wt) == head


def test_sync_with_remote_leaves_a_local_ahead_branch_alone(sandbox, wt):
    repo = sandbox.repo
    repo.push(wt, "factory/42")
    write(wt / "a.txt", "1\n")
    head = repo.commit_all(wt, "one")
    assert repo.sync_with_remote(wt, "factory/42") == head


def test_base_sha_follows_origin_only_after_a_fetch(sandbox):
    repo = sandbox.repo
    assert repo.base_sha("main") == sandbox.base
    moved = sandbox.move_main()

    assert repo.base_sha("main") == sandbox.base  # remote-tracking ref is still stale
    repo.fetch()
    assert repo.base_sha("main") == moved


def test_show_file(sandbox, wt):
    repo = sandbox.repo
    assert repo.show_file("HEAD", "src/app.py") == "print('hello')\n"
    assert repo.show_file("HEAD", "src/app.py", wt) == "print('hello')\n"
    assert repo.show_file("HEAD", "work/42/spec.md") is None
    assert repo.show_file("no-such-ref", "src/app.py") is None


def test_log_touching(sandbox, wt):
    repo = sandbox.repo
    write(wt / "t.txt", "1\n")
    first = repo.commit_all(wt, "t1")
    write(wt / "other.txt", "o\n")
    repo.commit_all(wt, "unrelated")
    write(wt / "t.txt", "2\n")
    second = repo.commit_all(wt, "t2")

    assert repo.log_touching(wt, "t.txt") == [second]
    assert repo.log_touching(wt, "t.txt", 2) == [second, first]
    assert repo.log_touching(wt, "never.txt") == []
