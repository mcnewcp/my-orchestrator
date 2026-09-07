"""`factory init` (design §6): factory.toml, REVIEW.md, intent issue template, label if absent, .factory/ in .gitignore.

Idempotent: existing files are left untouched (reported as "exists"). Nothing is committed — the report says so, and
notes that the .gitignore entry only affects this checkout (worktree transients are handled by .git/info/exclude,
see Repo.ensure_excludes). Templates come from prompts.load_template.

A `gh` failure on the label step is reported as a line rather than raised: the four local files are the useful part
of `init` and must stay installed on a machine with no `gh` login. Re-running `init` retries the label.
"""

from __future__ import annotations

from pathlib import Path

from . import prompts
from .errors import FactoryError
from .gh import GitHub

GITIGNORE_ENTRY = ".factory/"
LABEL_DESCRIPTION = "Issues the software factory turns into pull requests"
FOOTER = "nothing was committed; review and commit these files"
# (destination relative to the checkout root, name of the file under templates/)
INSTALLED_FILES: tuple[tuple[str, str], ...] = (
    ("factory.toml", "factory.toml"),
    ("REVIEW.md", "REVIEW.md"),
    (".github/ISSUE_TEMPLATE/intent.md", "intent.md"),
)


def init(checkout_root: Path, gh: GitHub | None, *, label: str = "factory") -> list[str]:
    """Returns one line per action ("wrote factory.toml", "exists REVIEW.md", "wrote .github/ISSUE_TEMPLATE/intent.md",
    "label factory created" / "label factory exists", "wrote .gitignore entry .factory/ (uncommitted)", and a final
    "nothing was committed; review and commit these files"). gh=None skips the label step (tests / offline)."""
    lines = [_install(checkout_root, dest, template) for dest, template in INSTALLED_FILES]
    if gh is not None:
        lines.append(_label_line(gh, label))
    lines.append(_gitignore_line(checkout_root))
    lines.append(FOOTER)
    return lines


def ensure_gitignore(checkout_root: Path, entry: str = ".factory/") -> bool:
    """Append `entry` to <checkout_root>/.gitignore unless a line already matches it (trailing slash ignored, so
    ".factory" counts as present). Returns True when the file was created or appended to. Existing lines are never
    rewritten and a missing final newline is repaired before appending."""
    path = checkout_root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    wanted = entry.strip().rstrip("/")
    if any(line.strip().rstrip("/") == wanted for line in text.splitlines()):
        return False
    separator = "" if text == "" or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{entry}\n", encoding="utf-8")
    return True


def _install(checkout_root: Path, dest: str, template: str) -> str:
    path = checkout_root / dest
    if path.exists():
        return f"exists {dest}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompts.load_template(template), encoding="utf-8")
    return f"wrote {dest}"


def _label_line(gh: GitHub, label: str) -> str:
    try:
        created = gh.ensure_label(label, description=LABEL_DESCRIPTION)
    except FactoryError as exc:
        return f"label {label} not created: {exc.message}"
    return f"label {label} created" if created else f"label {label} exists"


def _gitignore_line(checkout_root: Path) -> str:
    if ensure_gitignore(checkout_root, GITIGNORE_ENTRY):
        return f"wrote .gitignore entry {GITIGNORE_ENTRY} (uncommitted)"
    return f"exists .gitignore entry {GITIGNORE_ENTRY}"
