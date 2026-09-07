"""Owner-reviewed configuration and immutable per-run inputs."""

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from factory.errors import FactoryError


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FactoryError(f"Expected a regular file: {path}")
    return digest(path.read_bytes())


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    command: list[str] = Field(min_length=1)
    timeout: int = Field(default=300, ge=1, le=7200)

    @field_validator("command")
    @classmethod
    def valid_command(cls, value: list[str]) -> list[str]:
        if any(not arg or "\x00" in arg for arg in value):
            raise ValueError("Check commands require nonempty arguments without NUL bytes")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    checkout: Path
    workspace_root: Path
    host_workspace_root: Path | None = None
    base_branch: str = "main"
    agent: Literal["claude", "codex"] = "codex"
    auth: Literal["subscription", "api"] = "subscription"
    stage_timeout: int = Field(default=1200, ge=1, le=14400)
    ci_timeout: int = Field(default=600, ge=1, le=7200)
    poll_seconds: int = Field(default=3, ge=1, le=60)
    checks: list[Check] = Field(min_length=1)
    required_ci: list[str] = Field(min_length=1)
    protected_paths: list[str] = Field(min_length=1)
    allowed_untracked: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_paths(self) -> "Config":
        for path in (self.checkout, self.workspace_root, self.host_workspace_root):
            if path is not None and not path.is_absolute():
                raise ValueError("Workspace and checkout paths must be absolute")
        if not self.checkout.is_relative_to(self.workspace_root):
            raise ValueError("checkout must be inside workspace_root")
        if self.checkout == self.workspace_root or self.checkout.is_relative_to(
            self.workspace_root / "runs"
        ):
            raise ValueError("checkout must be separate from the runs directory")
        if not self.base_branch or self.base_branch.startswith("-"):
            raise ValueError("Invalid base branch")
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("Check names must be unique")
        if any(not name.strip() for name in self.required_ci):
            raise ValueError("Required CI names cannot be blank")
        if any(
            p.startswith("/") or ".." in Path(p).parts
            for p in [*self.protected_paths, *self.allowed_untracked]
        ):
            raise ValueError("Protected paths must be repository-relative")
        return self

    def host_path(self, path: str | Path) -> str:
        path = Path(path)
        if self.host_workspace_root and path.is_relative_to(self.workspace_root):
            return str(self.host_workspace_root / path.relative_to(self.workspace_root))
        return str(path)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("FACTORY_CONFIG", "factory.toml"))
    try:
        return Config.model_validate(tomllib.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        raise FactoryError(f"Cannot load configuration {path}: {exc}") from exc


def database_url() -> str:
    value = os.environ.get("FACTORY_DATABASE_URL", "")
    if not value:
        raise FactoryError("Set FACTORY_DATABASE_URL (Postgres connection URL)")
    return value


def image_identity() -> str:
    value = os.environ.get("FACTORY_IMAGE_ID", "")
    if not value:
        raise FactoryError("Set FACTORY_IMAGE_ID to the deployed image digest (dev:<id> locally)")
    return value


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
