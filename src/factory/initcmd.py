"""`factory init` (design §6): factory.toml, REVIEW.md, intent issue template, label if absent, .factory/ in .gitignore.

Idempotent: existing files are left untouched (reported as "exists"). Nothing is committed — the report says so, and
notes that the .gitignore entry only affects this checkout (worktree transients are handled by .git/info/exclude,
see Repo.ensure_excludes). Templates come from prompts.load_template.
"""

from __future__ import annotations

from pathlib import Path

from .gh import GitHub


def init(checkout_root: Path, gh: GitHub | None, *, label: str = "factory") -> list[str]:
    """Returns one line per action ("wrote factory.toml", "exists REVIEW.md", "wrote .github/ISSUE_TEMPLATE/intent.md",
    "label factory created" / "label factory exists", "wrote .gitignore entry .factory/ (uncommitted)", and a final
    "nothing was committed; review and commit these files"). gh=None skips the label step (tests / offline)."""
    raise NotImplementedError


def ensure_gitignore(checkout_root: Path, entry: str = ".factory/") -> bool:
    raise NotImplementedError
