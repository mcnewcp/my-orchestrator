import json
import subprocess
from pathlib import Path

import pytest

from factory.errors import Blocked
from factory.git_ops import GitOps
from factory.process import ProcessRunner


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    (checkout / "app.py").write_text("original\n")
    (checkout / "policy.md").write_text("protected\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "baseline")
    git(checkout, "push", "origin", "main")
    return GitOps(checkout), remote


def worktree(ops, tmp_path, branch="factory/42/run-1"):
    base = ops.fetch_base("main")
    path = tmp_path / branch.rsplit("/", 1)[-1]
    ops.create_worktree(path, branch, base)
    return path, base


def test_validate_requires_clean_repository_root(repository):
    ops, _ = repository
    ops.validate("main")
    with pytest.raises(Blocked, match="configured GitHub repository"):
        ops.validate("main", "owner/repo")
    (ops.checkout / "untracked.txt").write_text("operator work")
    with pytest.raises(Blocked, match="uncommitted"):
        ops.validate("main")


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com/owner/repo",
    ],
)
def test_validate_origin_formats(repository, remote):
    ops, _ = repository
    git(ops.checkout, "remote", "set-url", "origin", remote)
    ops.validate("main", "owner/repo")


def test_worktree_creation_is_idempotent_and_refuses_wrong_branch(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    ops.create_worktree(path, "factory/42/run-1", base)
    assert ops.head(path) == base
    with pytest.raises(Blocked, match="does not match"):
        ops.create_worktree(path, "factory/43/run-2", base)
    elsewhere = tmp_path / "wrong-worktree"
    with pytest.raises(Blocked, match="already exists"):
        ops.create_worktree(elsewhere, "factory/42/run-1", base)


def test_changed_paths_include_renames_and_unusual_untracked_names(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    git(path, "mv", "policy.md", "renamed.md")
    (path / "untracked\nfile.txt").write_text("new")
    assert ops.changed_paths(path, base) == {"policy.md", "renamed.md", "untracked\nfile.txt"}
    assert set(ops.status(path)) == {"policy.md", "renamed.md", "untracked\nfile.txt"}


def test_untracked_paths_include_ignored_files_by_default(repository, tmp_path):
    ops, _ = repository
    path, _ = worktree(ops, tmp_path)
    (path / ".gitignore").write_text("cache/\n*.ignored\n")
    ops.commit(path, "Ignore generated files")
    (path / "cache").mkdir()
    (path / "cache/partial\nfile.bin").write_bytes(b"partial\x00data")
    (path / "protected.ignored").write_text("unexpected policy")
    (path / "new.txt").write_text("untracked work")
    (path / "app.py").write_text("tracked edit")
    assert ops.untracked_paths(path) == {
        "cache/partial\nfile.bin",
        "protected.ignored",
        "new.txt",
    }
    assert ops.untracked_paths(path, include_ignored=False) == {"new.txt"}


def test_controller_commit_is_adopted_after_database_crash(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    (path / "app.py").write_text("implemented\n")
    candidate = ops.commit(path, "Implement", run_id="run-1", stage="candidate-1")
    assert ops.parent(path, candidate) == base
    assert ops.is_ancestor(path, base, candidate)
    assert ops.checkpoint(path, "run-1", "candidate-1") == candidate
    assert ops.checkpoint(path, "another-run", "candidate-1") is None
    assert ops.commit(path, "Implement", run_id="run-1", stage="candidate-1") == candidate
    assert git(path, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert ops.show(path, candidate, "app.py") == b"implemented\n"
    assert "+implemented" in ops.diff(path, base, candidate)
    (path / "app.py").write_text("unexpected later edit\n")
    with pytest.raises(Blocked, match="unexpected later edits"):
        ops.commit(path, "Implement", run_id="run-1", stage="candidate-1")


def test_trailer_mentions_in_commit_body_do_not_authorize_adoption(repository, tmp_path):
    ops, _ = repository
    path, _ = worktree(ops, tmp_path)
    git(
        path,
        "commit",
        "--allow-empty",
        "-m",
        "Pretend\n\nFactory-Run: run-1\nFactory-Stage: candidate-1\n\nUnrelated ending",
    )
    assert ops.checkpoint(path, "run-1", "candidate-1") is None


def test_push_is_idempotent_and_refuses_divergent_remote(repository, tmp_path):
    ops, remote = repository
    path, _ = worktree(ops, tmp_path)
    (path / "app.py").write_text("ours\n")
    candidate = ops.commit(path, "Ours", run_id="run-1", stage="candidate-1")
    ops.push(path, "factory/42/run-1")
    ops.push(path, "factory/42/run-1")
    assert git(remote, "rev-parse", "refs/heads/factory/42/run-1") == candidate
    other, _ = worktree(ops, tmp_path, "factory/43/other")
    (other / "app.py").write_text("theirs\n")
    conflict = ops.commit(other, "Theirs")
    git(other, "push", "origin", "+HEAD:refs/heads/factory/42/run-1")
    with pytest.raises(Blocked, match="refusing overwrite"):
        ops.push(path, "factory/42/run-1")
    assert git(remote, "rev-parse", "refs/heads/factory/42/run-1") == conflict


def test_recovery_preserves_staged_unstaged_deleted_and_untracked_changes(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    (path / "app.py").write_bytes(b"staged\x00data")
    git(path, "add", "app.py")
    (path / "app.py").write_bytes(b"partial\x00implementation\xff")
    (path / "policy.md").unlink()
    (path / "new.txt").write_text("untracked work")
    (path / "link").symlink_to("app.py")
    saved = ops.preserve_and_restore(path, base, tmp_path / "evidence")
    assert ops.head(path) == base
    assert not ops.status(path)
    assert (path / "app.py").read_text() == "original\n"
    assert (path / "policy.md").read_text() == "protected\n"
    assert (saved / "files/app.py").read_bytes() == b"partial\x00implementation\xff"
    assert (saved / "files/new.txt").read_text() == "untracked work"
    assert (saved / "files/link").is_symlink()
    metadata = json.loads((saved / "metadata.json").read_text())
    assert "policy.md" in metadata["paths"]
    assert b"GIT binary patch" in (saved / "staged.patch").read_bytes()


def test_recovery_blocks_unknown_commit_history_without_destroying_work(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    (path / "app.py").write_text("agent committed unexpectedly")
    git(path, "add", ".")
    git(path, "commit", "-m", "Unknown commit")
    current = ops.head(path)
    with pytest.raises(Blocked, match="Unexpected commit history"):
        ops.preserve_and_restore(path, base, tmp_path / "evidence", run_id="run-1")
    assert ops.head(path) == current
    assert (path / "app.py").read_text() == "agent committed unexpectedly"
    assert not (tmp_path / "evidence").exists()


def test_recovery_preserves_and_removes_ignored_partial_files(repository, tmp_path):
    ops, _ = repository
    path, _ = worktree(ops, tmp_path)
    (path / ".gitignore").write_text("cache/\n*.ignored\n")
    checkpoint = ops.commit(path, "Ignore generated files")
    (path / "cache/nested").mkdir(parents=True)
    (path / "cache/nested/partial\nfile.bin").write_bytes(b"partial\x00data\xff")
    (path / "link.ignored").symlink_to("missing-file")
    saved = ops.preserve_and_restore(path, checkpoint, tmp_path / "evidence")
    assert (saved / "files/cache/nested/partial\nfile.bin").read_bytes() == b"partial\x00data\xff"
    assert (saved / "files/link.ignored").readlink() == Path("missing-file")
    metadata = json.loads((saved / "metadata.json").read_text())
    assert set(metadata["paths"]) == {"cache/nested/partial\nfile.bin", "link.ignored"}
    assert not (path / "cache").exists()
    assert not (path / "link.ignored").is_symlink()
    assert ops.head(path) == checkpoint
    assert not ops.status(path)
    assert not ops.untracked_paths(path)


def test_recovery_retains_controller_commits_under_backup_ref(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    (path / "app.py").write_text("completed checkpoint")
    current = ops.commit(path, "Candidate", run_id="run-1", stage="candidate-1")
    saved = ops.preserve_and_restore(path, base, tmp_path / "evidence", run_id="run-1")
    assert ops.head(path) == base
    backup = "refs/factory-recovery/" + saved.name.removeprefix("recovery-")
    assert git(path, "rev-parse", backup) == current


def test_recovery_refuses_to_store_evidence_inside_worktree(repository, tmp_path):
    ops, _ = repository
    path, base = worktree(ops, tmp_path)
    with pytest.raises(Blocked, match="outside the worktree"):
        ops.preserve_and_restore(path, base, path / "evidence")


def test_show_with_supervised_runner_preserves_exact_blob_bytes(repository, tmp_path):
    ops, _ = repository
    path, _ = worktree(ops, tmp_path)
    raw = b"binary\x00\xff\xfe\r\ncontent"
    (path / "blob.bin").write_bytes(raw)
    sha = ops.commit(path, "Binary artifact")
    supervised = GitOps(ops.checkout, runner=ProcessRunner())
    assert supervised.show(path, sha, "blob.bin") == raw
