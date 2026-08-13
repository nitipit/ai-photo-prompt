"""Materialize approved Markdown challenge bundles into a runtime catalog.

This module is build-time only.  It reads ``design/challenges`` and emits one
atomic directory containing a deterministic JSON catalog and browser-addressable
WebP assets.  Runtime code uses :mod:`app.content.repository` instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.domain.models import ChallengeSpec, ChallengeStatus, LevelGroup

CATALOG_FILENAME = "catalog.json"
CHALLENGE_ASSET_DIRECTORY = Path("assets") / "challenges"
EXPECTED_CHALLENGE_COUNT = 20
EXPECTED_CHALLENGES_PER_LEVEL = 5
_SECTION_NAMES = (
    "Concept",
    "Core scoring anchors",
    "Optional details",
    "Example short prompt",
    "Evaluation notes",
    "Feedback focus",
)
_FRONTMATTER_KEYS = {"schema", "id", "title", "level", "status", "target"}
_H2_PATTERN = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ChallengeMaterializationError(ValueError):
    """Raised when a challenge bundle cannot satisfy the approved catalog contract."""


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    """Paths and records produced by one successful atomic materialization."""

    output_dir: Path
    catalog_path: Path
    assets_dir: Path
    challenges: tuple[ChallengeSpec, ...]


def discover_challenge_bundles(source_dir: Path | str) -> list[Path]:
    """Return challenge bundle directories in deterministic path order."""

    root = Path(source_dir)
    if not root.is_dir():
        raise ChallengeMaterializationError(f"challenge source directory does not exist: {root}")
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "challenge.md").is_file()),
        key=lambda path: path.name,
    )


def parse_challenge_bundle(bundle_dir: Path | str) -> ChallengeSpec:
    """Parse and validate one Markdown bundle into a strict ``ChallengeSpec``."""

    bundle = Path(bundle_dir)
    markdown_path = bundle / "challenge.md"
    if not markdown_path.is_file():
        raise ChallengeMaterializationError(f"missing challenge.md: {bundle}")

    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ChallengeMaterializationError(f"cannot read {markdown_path}: {error}") from error

    frontmatter, body = _split_frontmatter(markdown, markdown_path)
    unknown_keys = set(frontmatter) - _FRONTMATTER_KEYS
    missing_keys = _FRONTMATTER_KEYS - set(frontmatter)
    if unknown_keys or missing_keys:
        details = []
        if unknown_keys:
            details.append(f"unknown frontmatter keys: {sorted(unknown_keys)}")
        if missing_keys:
            details.append(f"missing frontmatter keys: {sorted(missing_keys)}")
        raise ChallengeMaterializationError(f"{markdown_path}: " + "; ".join(details))

    schema = frontmatter["schema"]
    if type(schema) is not int or schema != 1:
        raise ChallengeMaterializationError(f"{markdown_path}: unsupported schema {schema!r}")
    challenge_id = _frontmatter_text(frontmatter, "id", markdown_path)
    if not _ID_PATTERN.fullmatch(challenge_id):
        raise ChallengeMaterializationError(
            f"{markdown_path}: invalid challenge id {challenge_id!r}"
        )
    title = _frontmatter_text(frontmatter, "title", markdown_path)
    level = _frontmatter_text(frontmatter, "level", markdown_path)
    status = _frontmatter_text(frontmatter, "status", markdown_path)
    target = _frontmatter_text(frontmatter, "target", markdown_path)
    if status != ChallengeStatus.APPROVED.value:
        raise ChallengeMaterializationError(f"{markdown_path}: challenge is not approved")
    try:
        level_enum = LevelGroup(level)
    except ValueError as error:
        raise ChallengeMaterializationError(
            f"{markdown_path}: unsupported level {level!r}"
        ) from error

    _matching_target(bundle, target, markdown_path)
    sections = _parse_sections(body, markdown_path)
    try:
        return ChallengeSpec(
            schema_version=1,
            id=challenge_id,
            title=title,
            level=level_enum,
            status=ChallengeStatus.APPROVED,
            target_asset_url=f"/assets/challenges/{challenge_id}.webp",
            concept=_section_text(sections["Concept"]),
            core_anchors=_bullet_list(
                sections["Core scoring anchors"], "Core scoring anchors", markdown_path
            ),
            optional_details=_bullet_list(
                sections["Optional details"], "Optional details", markdown_path
            ),
            example_prompt=_example_prompt(sections["Example short prompt"]),
            evaluation_notes=_section_text(sections["Evaluation notes"]),
            feedback_focus=_section_text(sections["Feedback focus"]),
        )
    except Exception as error:
        if isinstance(error, ChallengeMaterializationError):
            raise
        raise ChallengeMaterializationError(
            f"{markdown_path}: invalid challenge fields: {error}"
        ) from error


def materialize_challenges(
    source_dir: Path | str,
    output_dir: Path | str,
) -> MaterializationResult:
    """Validate all bundles and atomically replace a deterministic output directory."""

    source = Path(source_dir)
    destination = Path(output_dir)
    bundles = discover_challenge_bundles(source)
    parsed = [(parse_challenge_bundle(bundle), bundle) for bundle in bundles]
    _validate_catalog_invariants([spec for spec, _ in parsed])
    parsed.sort(key=lambda pair: pair[0].id)
    specs = [spec for spec, _ in parsed]

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        assets_dir = stage / CHALLENGE_ASSET_DIRECTORY
        assets_dir.mkdir(parents=True)
        for spec, bundle in parsed:
            target = _target_from_bundle(bundle)
            shutil.copyfile(target, assets_dir / f"{spec.id}.webp")

        catalog = {
            "schema_version": 1,
            "challenges": [spec.dict() for spec in specs],
        }
        (stage / CATALOG_FILENAME).write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _atomic_replace_directory(stage, destination)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    return MaterializationResult(
        output_dir=destination,
        catalog_path=destination / CATALOG_FILENAME,
        assets_dir=destination / CHALLENGE_ASSET_DIRECTORY,
        challenges=tuple(specs),
    )


def _split_frontmatter(markdown: str, path: Path) -> tuple[dict[str, Any], str]:
    match = re.match(
        r"\A---[ \t]*\r?\n(?P<frontmatter>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|\Z)",
        markdown,
        flags=re.DOTALL,
    )
    if match is None:
        message = (
            "unterminated YAML frontmatter"
            if markdown.startswith("---")
            else "YAML frontmatter is required"
        )
        raise ChallengeMaterializationError(f"{path}: {message}")
    frontmatter_text = match.group("frontmatter")
    body = markdown[match.end() :]
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as error:
        raise ChallengeMaterializationError(
            f"{path}: malformed YAML frontmatter: {error}"
        ) from error
    if not isinstance(frontmatter, dict):
        raise ChallengeMaterializationError(f"{path}: frontmatter must be a mapping")
    return frontmatter, body


def _frontmatter_text(frontmatter: dict[str, Any], key: str, path: Path) -> str:
    value = frontmatter[key]
    if type(value) is not str or not value.strip():
        raise ChallengeMaterializationError(f"{path}: frontmatter {key!r} must be non-empty text")
    return value.strip()


def _parse_sections(body: str, path: Path) -> dict[str, str]:
    headings = list(_H2_PATTERN.finditer(body))
    names = [match.group(1).strip() for match in headings]
    if tuple(names) != _SECTION_NAMES:
        raise ChallengeMaterializationError(
            f"{path}: expected H2 sections in order {_SECTION_NAMES!r}, got {tuple(names)!r}"
        )
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[names[index]] = body[heading.end() : end].strip()
    required_sections = set(_SECTION_NAMES) - {"Optional details"}
    if any(not sections[name] for name in required_sections):
        empty = [name for name in required_sections if not sections[name]]
        raise ChallengeMaterializationError(f"{path}: empty required sections: {empty}")
    return sections


def _section_text(content: str) -> str:
    return re.sub(r"[ \t]+", " ", content).strip()


def _example_prompt(content: str) -> str:
    lines = [re.sub(r"^\s*>\s?", "", line) for line in content.splitlines()]
    return _section_text("\n".join(lines))


def _bullet_list(content: str, section: str, path: Path) -> list[str]:
    values: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if match is None:
            raise ChallengeMaterializationError(f"{path}: {section} must contain bullet items")
        values.append(_section_text(match.group(1)))
    return values


def _matching_target(bundle: Path, target: str, path: Path) -> Path:
    candidate = Path(target)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ChallengeMaterializationError(f"{path}: target must stay inside its bundle")
    if candidate.suffix != ".webp":
        raise ChallengeMaterializationError(f"{path}: target must be a WebP file")
    resolved_bundle = bundle.resolve()
    resolved_target = (bundle / candidate).resolve()
    try:
        resolved_target.relative_to(resolved_bundle)
    except ValueError as error:
        raise ChallengeMaterializationError(
            f"{path}: target must stay inside its bundle"
        ) from error
    if not resolved_target.is_file():
        raise ChallengeMaterializationError(f"{path}: missing target WebP {target!r}")
    return resolved_target


def _target_from_bundle(bundle: Path) -> Path:
    markdown_path = bundle / "challenge.md"
    frontmatter, _ = _split_frontmatter(markdown_path.read_text(encoding="utf-8"), markdown_path)
    return _matching_target(
        bundle, _frontmatter_text(frontmatter, "target", markdown_path), markdown_path
    )


def _validate_catalog_invariants(specs: list[ChallengeSpec]) -> None:
    if len(specs) != EXPECTED_CHALLENGE_COUNT:
        raise ChallengeMaterializationError(
            f"expected exactly {EXPECTED_CHALLENGE_COUNT} challenges, found {len(specs)}"
        )
    ids = [spec.id for spec in specs]
    duplicates = sorted({challenge_id for challenge_id in ids if ids.count(challenge_id) > 1})
    if duplicates:
        raise ChallengeMaterializationError(f"duplicate challenge ids: {duplicates}")
    counts = {level: sum(spec.level is level for spec in specs) for level in LevelGroup}
    invalid_counts = {
        level.value: count
        for level, count in counts.items()
        if count != EXPECTED_CHALLENGES_PER_LEVEL
    }
    if invalid_counts:
        raise ChallengeMaterializationError(
            f"expected {EXPECTED_CHALLENGES_PER_LEVEL} challenges per level, found {invalid_counts}"
        )
    if any(spec.status is not ChallengeStatus.APPROVED for spec in specs):
        raise ChallengeMaterializationError("all materialized challenges must be approved")


def _atomic_replace_directory(stage: Path, destination: Path) -> None:
    backup: Path | None = None
    try:
        if destination.exists() or destination.is_symlink():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.backup.", dir=destination.parent)
            )
            backup.rmdir()
            os.replace(destination, backup)
        os.replace(stage, destination)
    except Exception:
        if backup is not None and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


__all__ = [
    "CATALOG_FILENAME",
    "CHALLENGE_ASSET_DIRECTORY",
    "EXPECTED_CHALLENGE_COUNT",
    "EXPECTED_CHALLENGES_PER_LEVEL",
    "ChallengeMaterializationError",
    "MaterializationResult",
    "discover_challenge_bundles",
    "materialize_challenges",
    "parse_challenge_bundle",
]
