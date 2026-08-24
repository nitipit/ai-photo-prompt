"""Strict TOML runtime configuration for the production kiosk."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

APP_DIR = Path(__file__).parent
PROJECT_DIR = APP_DIR.parents[1]
TEMPLATE_DIR = APP_DIR / "templates"
DIST_DIR = PROJECT_DIR / "dist"
RUNTIME_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_DB_PATH = RUNTIME_DATA_DIR / "photo-prompt.shelfdb"
DEFAULT_CATALOG_PATH = DIST_DIR / "catalog.json"
DEFAULT_GENERATED_ROOT = RUNTIME_DATA_DIR / "generated"
DEFAULT_PI_WORKSPACE_ROOT = RUNTIME_DATA_DIR / "pi-rpc"
DEFAULT_PI_BRIDGE_PATH = PROJECT_DIR / "deploy" / "codex-bridge.ts"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "conf" / "app.toml"
CONFIG_ENV = "PHOTO_PROMPT_CONFIG"
STAFF_PIN_ENV = "PHOTO_PROMPT_STAFF_PIN"
DEFAULT_PI_EXECUTABLE = "pi"
DEFAULT_PI_PROVIDER = "openai-codex"
DEFAULT_PI_MODEL = "gpt-5.6-luna"
DEFAULT_PI_IMAGE_THINKING = "minimal"
DEFAULT_PI_EVALUATOR_THINKING = "medium"
DEFAULT_PI_TIMEOUT_SECONDS = 240.0
DEFAULT_PI_MAX_OUTPUT_BYTES = 32 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RuntimePaths(_StrictModel):
    """Filesystem paths resolved from the project root exactly once."""

    state_dir: Path
    db_path: Path
    catalog_path: Path
    generated_root: Path
    pi_workspace_root: Path
    pi_bridge_path: Path
    target_static_root: Path

    @field_validator(
        "state_dir",
        "db_path",
        "catalog_path",
        "generated_root",
        "pi_workspace_root",
        "pi_bridge_path",
        "target_static_root",
        mode="before",
    )
    @classmethod
    def _path_type(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if not isinstance(value, str) or not value.strip():
            raise TypeError("paths must be non-empty strings")
        return Path(value)

    @model_validator(mode="after")
    def _safe_relationships(self) -> RuntimePaths:
        paths = self
        if paths.db_path == paths.state_dir or paths.db_path.suffix == "":
            raise ValueError("db_path must be a file path inside state_dir")
        if not _is_relative_to(paths.db_path, paths.state_dir):
            raise ValueError("db_path must be inside state_dir")
        roots = {
            "state_dir": paths.state_dir,
            "generated_root": paths.generated_root,
            "pi_workspace_root": paths.pi_workspace_root,
        }
        for left_name, left in roots.items():
            for right_name, right in roots.items():
                if left_name != right_name and _is_same_or_nested(left, right):
                    raise ValueError(f"{left_name} and {right_name} must be separate roots")
        if paths.pi_bridge_path.suffix != ".ts":
            raise ValueError("pi_bridge_path must be a TypeScript file")
        if not _is_relative_to(paths.catalog_path, paths.target_static_root):
            raise ValueError("catalog_path must be inside target_static_root")
        if _is_same_or_nested(paths.target_static_root, paths.generated_root):
            raise ValueError("target_static_root must not contain generated_root")
        if _is_same_or_nested(paths.target_static_root, paths.pi_workspace_root):
            raise ValueError("target_static_root must not contain pi_workspace_root")
        return self


class PiRuntime(_StrictModel):
    """Fixed production Pi bridge and bounded execution settings."""

    provider: Literal["openai-codex"]
    model: str
    image_thinking: str
    evaluator_thinking: str
    timeout_seconds: Annotated[PositiveFloat, Field(le=86_400)]
    max_stdout_bytes: Annotated[PositiveInt, Field(le=128 * 1024 * 1024)]
    max_stderr_bytes: Annotated[PositiveInt, Field(le=128 * 1024 * 1024)]
    max_artifact_bytes: Annotated[PositiveInt, Field(le=256 * 1024 * 1024)]
    max_artifact_width: Annotated[PositiveInt, Field(le=16_384)]
    max_artifact_height: Annotated[PositiveInt, Field(le=16_384)]
    reconciliation_max_entries: Annotated[PositiveInt, Field(le=1_000_000)]
    claim_lease_seconds: Annotated[PositiveFloat, Field(le=86_400)]
    claim_heartbeat_seconds: Annotated[PositiveFloat, Field(le=86_400)]
    max_concurrent_attempts: Annotated[int, Field(ge=1, le=3)]

    @field_validator("model", "image_thinking", "evaluator_thinking")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Pi string settings must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_timing(self) -> PiRuntime:
        if self.claim_heartbeat_seconds >= self.claim_lease_seconds:
            raise ValueError("claim_heartbeat_seconds must be shorter than claim_lease_seconds")
        return self


class RuntimeConfig(_StrictModel):
    """Complete, single-file application configuration."""

    schema_version: Literal[1]
    paths: RuntimePaths
    pi: PiRuntime

    @classmethod
    def from_toml(cls, value: object, *, project_root: Path = PROJECT_DIR) -> RuntimeConfig:
        if not isinstance(value, dict):
            raise ConfigError("configuration root must be a TOML table")
        try:
            parsed = cls.model_validate(value)
        except ValidationError as error:
            raise ConfigError(str(error)) from error
        return _resolve_config_paths(parsed, project_root.resolve())


class ConfigError(ValueError):
    """Raised when the complete active TOML configuration is unsafe or invalid."""


def _resolve_config_paths(config: RuntimeConfig, project_root: Path) -> RuntimeConfig:
    paths = config.paths
    try:
        resolved = RuntimePaths(
            state_dir=_resolve_path(paths.state_dir, project_root),
            db_path=_resolve_path(paths.db_path, project_root),
            catalog_path=_resolve_path(paths.catalog_path, project_root),
            generated_root=_resolve_path(paths.generated_root, project_root),
            pi_workspace_root=_resolve_path(paths.pi_workspace_root, project_root),
            pi_bridge_path=_resolve_path(paths.pi_bridge_path, project_root),
            target_static_root=_resolve_path(paths.target_static_root, project_root),
        )
    except ValidationError as error:
        raise ConfigError(str(error)) from error
    if not _is_relative_to(resolved.pi_bridge_path, project_root):
        raise ConfigError("pi_bridge_path must be inside the project root")
    if _is_same_or_nested(resolved.pi_bridge_path, resolved.generated_root):
        raise ConfigError("pi_bridge_path must not be inside generated_root")
    if _is_same_or_nested(resolved.pi_bridge_path, resolved.pi_workspace_root):
        raise ConfigError("pi_bridge_path must not be inside pi_workspace_root")
    try:
        return RuntimeConfig(schema_version=config.schema_version, paths=resolved, pi=config.pi)
    except ValidationError as error:
        raise ConfigError(str(error)) from error


def _resolve_path(value: Path, project_root: Path) -> Path:
    if value.is_absolute():
        return value.resolve(strict=False)
    return (project_root / value).resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_same_or_nested(left: Path, right: Path) -> bool:
    return left == right or _is_relative_to(left, right) or _is_relative_to(right, left)


def load_config(
    path: Path | str | None = None,
    *,
    project_root: Path = PROJECT_DIR,
) -> RuntimeConfig:
    """Load and validate one complete active TOML file without merging."""

    selected_value = path if path is not None else os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    selected = Path(selected_value)
    if not selected.is_absolute():
        selected = project_root / selected
    try:
        with selected.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ConfigError(f"active configuration file is missing: {selected}") from error
    except OSError as error:
        raise ConfigError("active configuration file is unavailable") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"active configuration TOML is invalid: {error}") from error
    try:
        return RuntimeConfig.from_toml(raw, project_root=project_root)
    except ConfigError:
        raise
    except Exception as error:
        raise ConfigError("active configuration is invalid") from error


def check_config(path: Path | str | None = None) -> RuntimeConfig:
    """Validate deployment configuration and return it for callers/tests."""

    config = load_config(path)
    print(f"configuration ok: {path or os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)}")
    return config


def main(argv: list[str] | None = None) -> int:
    """Config-check entry point used by deployment preflight."""

    arguments = sys.argv[1:] if argv is None else argv
    selected = arguments[0] if arguments else None
    try:
        check_config(selected)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    return 0


__all__ = [
    "CONFIG_ENV",
    "ConfigError",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DB_PATH",
    "DEFAULT_GENERATED_ROOT",
    "DEFAULT_PI_BRIDGE_PATH",
    "DEFAULT_PI_EVALUATOR_THINKING",
    "DEFAULT_PI_EXECUTABLE",
    "DEFAULT_PI_IMAGE_THINKING",
    "DEFAULT_PI_MAX_OUTPUT_BYTES",
    "DEFAULT_PI_MODEL",
    "DEFAULT_PI_PROVIDER",
    "DEFAULT_PI_TIMEOUT_SECONDS",
    "DEFAULT_PI_WORKSPACE_ROOT",
    "DIST_DIR",
    "PROJECT_DIR",
    "RUNTIME_DATA_DIR",
    "RuntimeConfig",
    "RuntimePaths",
    "PiRuntime",
    "STAFF_PIN_ENV",
    "TEMPLATE_DIR",
    "check_config",
    "load_config",
    "main",
]
