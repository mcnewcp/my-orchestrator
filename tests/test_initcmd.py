"""Unit tests for `factory init` (design §6).

Self-contained: `prompts.load_template` is stubbed (another module owns it) but `initcmd` keeps calling it, and one
test asserts the template files it names really exist under src/factory/templates/.
"""

from __future__ import annotations

import pytest

from factory import initcmd, prompts
from factory.errors import FactoryError

FILES = ("factory.toml", "REVIEW.md", ".github/ISSUE_TEMPLATE/intent.md")


class StubGitHub:
    """Only `ensure_label` is reachable from init."""

    def __init__(self, *, created: bool = True, error: str | None = None):
        self.created = created
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def ensure_label(self, label, *, color="5319e7", description=""):
        self.calls.append((label, description))
        if self.error:
            raise FactoryError(self.error)
        return self.created


@pytest.fixture
def templates(monkeypatch):
    monkeypatch.setattr(prompts, "load_template", lambda name: f"# template {name}\n")


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    return root


def test_init_installs_every_file_and_the_label(checkout, templates):
    gh = StubGitHub(created=True)

    lines = initcmd.init(checkout, gh)

    assert lines == [
        "wrote factory.toml",
        "wrote REVIEW.md",
        "wrote .github/ISSUE_TEMPLATE/intent.md",
        "label factory created",
        "wrote .gitignore entry .factory/ (uncommitted)",
        initcmd.FOOTER,
    ]
    for name in FILES:
        assert (checkout / name).read_text() == f"# template {name.rsplit('/', 1)[-1]}\n"
    assert (checkout / ".gitignore").read_text() == ".factory/\n"
    assert gh.calls == [("factory", initcmd.LABEL_DESCRIPTION)]


def test_init_is_idempotent_and_never_rewrites_a_file(checkout, templates):
    initcmd.init(checkout, None)
    (checkout / "factory.toml").write_text("harness = 'codex'  # edited by hand\n")

    lines = initcmd.init(checkout, StubGitHub(created=False))

    assert lines == [
        "exists factory.toml",
        "exists REVIEW.md",
        "exists .github/ISSUE_TEMPLATE/intent.md",
        "label factory exists",
        "exists .gitignore entry .factory/",
        initcmd.FOOTER,
    ]
    assert (checkout / "factory.toml").read_text() == "harness = 'codex'  # edited by hand\n"


def test_init_without_gh_skips_the_label_step(checkout, templates):
    lines = initcmd.init(checkout, None)

    assert not [line for line in lines if line.startswith("label")]


def test_init_uses_the_requested_label(checkout, templates):
    gh = StubGitHub(created=True)

    lines = initcmd.init(checkout, gh, label="robot")

    assert "label robot created" in lines
    assert gh.calls[0][0] == "robot"


def test_a_gh_failure_is_reported_and_the_files_are_still_installed(checkout, templates):
    lines = initcmd.init(checkout, StubGitHub(error="gh: not authenticated"))

    assert "label factory not created: gh: not authenticated" in lines
    assert (checkout / ".gitignore").exists()
    assert (checkout / "REVIEW.md").exists()


def test_init_creates_the_issue_template_directory(checkout, templates):
    initcmd.init(checkout, None)

    assert (checkout / ".github" / "ISSUE_TEMPLATE").is_dir()


def test_ensure_gitignore_creates_the_file(checkout):
    assert initcmd.ensure_gitignore(checkout) is True
    assert (checkout / ".gitignore").read_text() == ".factory/\n"


def test_ensure_gitignore_appends_after_a_missing_final_newline(checkout):
    (checkout / ".gitignore").write_text("*.pyc\n.venv/")

    assert initcmd.ensure_gitignore(checkout) is True
    assert (checkout / ".gitignore").read_text() == "*.pyc\n.venv/\n.factory/\n"


@pytest.mark.parametrize("existing", [".factory/\n", ".factory\n", "*.pyc\n  .factory/  \n"])
def test_ensure_gitignore_is_a_noop_when_the_entry_is_present(checkout, existing):
    (checkout / ".gitignore").write_text(existing)

    assert initcmd.ensure_gitignore(checkout) is False
    assert (checkout / ".gitignore").read_text() == existing


def test_ensure_gitignore_honours_a_custom_entry(checkout):
    assert initcmd.ensure_gitignore(checkout, "work/tmp/") is True
    assert (checkout / ".gitignore").read_text() == "work/tmp/\n"


def test_every_template_init_installs_exists_on_disk():
    for _dest, template in initcmd.INSTALLED_FILES:
        assert (prompts.TEMPLATES_DIR / template).is_file(), template
