"""Runtime read-only boundary for the generated challenge catalog."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol, overload

from app.domain.models import ChallengeSpec, ChallengeStatus, LevelGroup

_EXPECTED_CHALLENGE_COUNT = 20
_EXPECTED_CHALLENGES_PER_LEVEL = 5


class CatalogValidationError(ValueError):
    """Raised when generated catalog data is missing, malformed, or inconsistent."""


class ChallengeSource(Protocol):
    """Validated challenge lookup operations used by runtime services."""

    def all(self) -> tuple[ChallengeSpec, ...]:
        """Return every persisted challenge in deterministic ID order."""

    def get(self, challenge_id: str) -> ChallengeSpec:
        """Return one challenge by ID."""

    def for_level(self, level: LevelGroup | str) -> tuple[ChallengeSpec, ...]:
        """Return challenges for one level."""


class ChallengeCatalog(Sequence[ChallengeSpec]):
    """Validated catalog facade for generated or repository-backed challenges.

    ``load`` creates a build-output snapshot for startup validation.  Runtime
    services use ``from_repository`` so every lookup reconstructs validated
    records from durable storage rather than retaining the JSON snapshot.
    """

    def __init__(
        self,
        challenges: Sequence[ChallengeSpec] | None = None,
        *,
        repository: ChallengeSource | None = None,
    ) -> None:
        if (challenges is None) == (repository is None):
            raise TypeError("provide either challenges or repository")
        self._repository = repository
        if repository is not None:
            self._challenges = None
            self._by_id = None
            return
        specs = list(challenges or ())
        _validate_catalog(specs)
        self._challenges = tuple(sorted(specs, key=lambda challenge: challenge.id))
        self._by_id = {challenge.id: challenge for challenge in self._challenges}

    @classmethod
    def from_repository(cls, repository: ChallengeSource) -> ChallengeCatalog:
        """Create a facade whose reads are delegated to a validated repository."""

        return cls(repository=repository)

    @classmethod
    def load(cls, catalog_path: Path | str) -> ChallengeCatalog:
        """Load and Dictify-validate one generated catalog JSON file."""

        path = Path(catalog_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogValidationError(f"cannot read catalog {path}: {error}") from error
        try:
            if not isinstance(payload, dict):
                raise TypeError("catalog root must be a mapping")
            if set(payload) != {"schema_version", "challenges"}:
                raise ValueError("catalog root has unknown or missing fields")
            if payload["schema_version"] != 1:
                raise ValueError("unsupported catalog schema")
            raw_challenges = payload["challenges"]
            if type(raw_challenges) is not list:
                raise TypeError("catalog challenges must be a list")
            challenges = [ChallengeSpec(item) for item in raw_challenges]
            return cls(challenges)
        except (TypeError, ValueError, KeyError, ChallengeSpec.Error) as error:
            raise CatalogValidationError(f"invalid catalog {path}: {error}") from error

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> ChallengeCatalog:
        """Validate a decoded generated catalog mapping without filesystem access."""

        if set(payload) != {"schema_version", "challenges"} or payload.get("schema_version") != 1:
            raise CatalogValidationError("invalid catalog envelope")
        raw_challenges = payload.get("challenges")
        if type(raw_challenges) is not list:
            raise CatalogValidationError("catalog challenges must be a list")
        try:
            return cls([ChallengeSpec(item) for item in raw_challenges])
        except (TypeError, ValueError, KeyError, ChallengeSpec.Error) as error:
            raise CatalogValidationError(f"invalid catalog: {error}") from error

    def __len__(self) -> int:
        return len(self.challenges)

    def __iter__(self) -> Iterator[ChallengeSpec]:
        return iter(self.challenges)

    @overload
    def __getitem__(self, index: int) -> ChallengeSpec: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ChallengeSpec, ...]: ...

    def __getitem__(self, index: int | slice) -> ChallengeSpec | tuple[ChallengeSpec, ...]:
        return self.challenges[index]

    def all(self) -> tuple[ChallengeSpec, ...]:
        """Return every challenge in deterministic ID order."""

        if self._repository is not None:
            return self._repository.all()
        return self._challenges or ()

    def get(self, challenge_id: str) -> ChallengeSpec:
        """Return one challenge by ID or raise ``KeyError``."""

        if self._repository is not None:
            return self._repository.get(challenge_id)
        if self._by_id is None:
            raise RuntimeError("catalog is not initialized")
        return self._by_id[challenge_id]

    def for_level(self, level: LevelGroup | str) -> tuple[ChallengeSpec, ...]:
        """Return the deterministic subset for one age band."""

        selected_level = level if isinstance(level, LevelGroup) else LevelGroup(level)
        if self._repository is not None:
            return self._repository.for_level(selected_level)
        return tuple(challenge for challenge in self if challenge.level is selected_level)

    @property
    def challenges(self) -> tuple[ChallengeSpec, ...]:
        """Expose an immutable ordered view for selectors and tests."""

        return self.all()


def _validate_catalog(specs: list[ChallengeSpec]) -> None:
    if any(not isinstance(spec, ChallengeSpec) for spec in specs):
        raise CatalogValidationError("catalog entries must be ChallengeSpec records")
    if len(specs) != _EXPECTED_CHALLENGE_COUNT:
        raise CatalogValidationError(
            f"expected exactly {_EXPECTED_CHALLENGE_COUNT} challenges, found {len(specs)}"
        )
    ids = [spec.id for spec in specs]
    if len(ids) != len(set(ids)):
        raise CatalogValidationError("catalog contains duplicate challenge ids")
    if any(spec.status is not ChallengeStatus.APPROVED for spec in specs):
        raise CatalogValidationError("catalog contains a non-approved challenge")
    counts = {level: sum(spec.level is level for spec in specs) for level in LevelGroup}
    if any(count != _EXPECTED_CHALLENGES_PER_LEVEL for count in counts.values()):
        raise CatalogValidationError(
            "catalog must contain exactly five challenges for each level: "
            + repr({level.value: count for level, count in counts.items()})
        )
    for spec in specs:
        if not spec.target_asset_url.startswith("/") or "://" in spec.target_asset_url:
            raise CatalogValidationError(
                f"challenge {spec.id} target_asset_url must be a browser URL path"
            )


__all__ = ["CatalogValidationError", "ChallengeCatalog", "ChallengeSource"]
