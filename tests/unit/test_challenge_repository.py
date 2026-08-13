from __future__ import annotations

from pathlib import Path

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]
from shelfdb.shelf.shelf.shelf import ShelfStore

from app.content.repository import ChallengeCatalog
from app.domain.models import ChallengeSpec, LevelGroup
from app.persistence import (
    ChallengeNotFoundError,
    ChallengeRepositoryError,
    ShelfDbChallengeRepository,
)


def _replace_challenge(challenge: ChallengeSpec, **changes: str) -> ChallengeSpec:
    return ChallengeSpec({**challenge.dict(), **changes})


@pytest.fixture
def challenge_repository(tmp_path: Path, materialized_catalog):
    db = DB(str(tmp_path / "challenge-db"))
    repository = ShelfDbChallengeRepository(db)
    repository.sync(materialized_catalog.challenges)
    try:
        yield repository
    finally:
        db.close()


def test_sync_stores_complete_primitive_records_and_validated_reads(
    challenge_repository: ShelfDbChallengeRepository,
) -> None:
    challenges = challenge_repository.all()

    assert len(challenges) == 20
    assert len(challenge_repository.for_level(LevelGroup.P1_P3)) == 5
    assert len(challenge_repository.for_level("m4-m6")) == 5

    with challenge_repository._db.transaction(write=False) as transaction:  # noqa: SLF001
        payload = transaction.shelf("challenges").key("p1-p3-01").item().value

    assert payload["schema_version"] == 1
    assert payload["id"] == "p1-p3-01"
    assert payload["level"] == "p1-p3"
    assert payload["status"] == "approved"
    assert all(isinstance(value, str) for value in payload["core_anchors"])
    assert all(isinstance(value, str) for value in payload["optional_details"])
    expected = next(challenge for challenge in challenges if challenge.id == "p1-p3-01")
    assert (
        ChallengeCatalog.from_repository(challenge_repository).get("p1-p3-01").dict()
        == expected.dict()
    )


def test_sync_replaces_records_and_removes_stale_ids(
    challenge_repository: ShelfDbChallengeRepository,
) -> None:
    current = list(challenge_repository.all())
    replacement_id = "p1-p3-01-replacement"
    original = next(challenge for challenge in current if challenge.id == "p1-p3-01")
    current[current.index(original)] = _replace_challenge(
        original,
        id=replacement_id,
        target_asset_url=f"/assets/challenges/{replacement_id}.webp",
    )

    challenge_repository.sync(current)

    with pytest.raises(ChallengeNotFoundError):
        challenge_repository.get("p1-p3-01")
    assert challenge_repository.get(replacement_id).id == replacement_id
    assert len(challenge_repository.all()) == 20


def test_invalid_stored_record_and_unknown_id_raise_bounded_errors(
    challenge_repository: ShelfDbChallengeRepository,
) -> None:
    with pytest.raises(ChallengeNotFoundError, match="challenge not found"):
        challenge_repository.get("unknown")

    with challenge_repository._db.transaction(write=True) as transaction:  # noqa: SLF001
        transaction.shelf("challenges").put("p1-p3-01", {"id": "corrupt"})

    with pytest.raises(ChallengeRepositoryError, match="stored challenge is invalid"):
        challenge_repository.get("p1-p3-01")
    with pytest.raises(ChallengeRepositoryError, match="stored challenge is invalid"):
        challenge_repository.all()


def test_sync_transaction_rolls_back_when_a_write_fails(
    challenge_repository: ShelfDbChallengeRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = challenge_repository.all()
    changed = list(before)
    changed[-1] = _replace_challenge(changed[-1], title="Changed after rollback")
    original_put = ShelfStore.put

    def fail_one_write(self, key: str, value: object):
        if key == changed[-1].id and value["title"] == "Changed after rollback":
            raise RuntimeError("forced challenge sync failure")
        return original_put(self, key, value)

    monkeypatch.setattr(ShelfStore, "put", fail_one_write)
    with pytest.raises(RuntimeError, match="forced challenge sync failure"):
        challenge_repository.sync(changed)

    assert challenge_repository.all() == tuple(before)


def test_challenges_survive_close_and_reopen_without_resync(
    tmp_path: Path,
    materialized_catalog,
) -> None:
    path = str(tmp_path / "durable-challenges")
    first_db = DB(path)
    try:
        ShelfDbChallengeRepository(first_db).sync(materialized_catalog.challenges)
    finally:
        first_db.close()

    second_db = DB(path)
    try:
        repository = ShelfDbChallengeRepository(second_db)
        assert len(repository.all()) == 20
        assert repository.get("m4-m6-05").level is LevelGroup.M4_M6
    finally:
        second_db.close()
