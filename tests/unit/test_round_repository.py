from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from shelfdb.shelf import DB  # type: ignore[import-untyped]

from app.domain.models import (
    GameState,
    ImageArtifact,
    LevelGroup,
    RoundRecord,
    TerminalDisposition,
)
from app.persistence import (
    RoundConflictError,
    RoundNotFoundError,
    RoundRepositoryLimitError,
    RoundSnapshotConflictError,
    ShelfDbRoundRepository,
)

FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC).isoformat()


def make_round(
    *,
    level: LevelGroup | None = None,
    disposition: TerminalDisposition | None = None,
    completed_at: str | None = None,
) -> RoundRecord:
    return RoundRecord(
        id=str(uuid4()),
        state=(
            GameState.LEADERBOARD
            if disposition is TerminalDisposition.COMPLETED
            else GameState.ABANDONED
            if disposition is TerminalDisposition.ABANDONED
            else GameState.LEVEL_SELECTION
        ),
        display_name="Tester",
        level=level,
        terminal_disposition=disposition,
        created_at=FIXED_TIMESTAMP,
        updated_at=FIXED_TIMESTAMP,
        completed_at=completed_at,
    )


@pytest.fixture
def repository(tmp_path: Path):
    db = DB(str(tmp_path / "rounds-db"))
    try:
        yield ShelfDbRoundRepository(db)
    finally:
        db.close()


def test_round_mapping_round_trip_is_strict(repository: ShelfDbRoundRepository) -> None:
    record = make_round(level=LevelGroup.P1_P3)

    repository.create(record)
    rebuilt = repository.get(record.id)

    assert rebuilt is not record
    assert rebuilt.dict() == record.dict()
    assert rebuilt.level is LevelGroup.P1_P3


def test_duplicate_create_and_missing_operations_raise_focused_errors(
    repository: ShelfDbRoundRepository,
) -> None:
    record = make_round()
    repository.create(record)

    with pytest.raises(RoundConflictError):
        repository.create(record)
    with pytest.raises(RoundNotFoundError):
        repository.get(str(uuid4()))
    with pytest.raises(RoundNotFoundError):
        repository.replace(make_round())


def test_compare_and_swap_rejects_a_stale_snapshot(repository: ShelfDbRoundRepository) -> None:
    original = make_round(level=LevelGroup.P1_P3)
    repository.create(original)
    replacement = RoundRecord(
        **{
            **original.dict(),
            "state": GameState.CHALLENGE_REVEAL,
            "level": LevelGroup.M4_M6,
        }
    )
    changed = RoundRecord(**{**original.dict(), "state": GameState.CHALLENGE_REVEAL})
    repository.replace_unsafe(changed)

    with pytest.raises(RoundSnapshotConflictError):
        repository.replace_if_current(replacement, original)

    assert repository.get(original.id).dict() == changed.dict()


def test_compare_and_swap_allows_only_one_of_ten_same_snapshot_writers(
    repository: ShelfDbRoundRepository,
) -> None:
    original = make_round()
    repository.create(original)
    barrier = Barrier(10)

    def attempt(index: int) -> str:
        replacement = RoundRecord(
            **{
                **original.dict(),
                "state": GameState.CHALLENGE_REVEAL,
                "display_name": f"Player {index}",
            }
        )
        barrier.wait()
        try:
            repository.replace_if_current(replacement, original)
        except RoundSnapshotConflictError:
            return "stale"
        return "stored"

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(attempt, range(10)))

    assert outcomes.count("stored") == 1
    assert outcomes.count("stale") == 9
    assert repository.get(original.id).state is GameState.CHALLENGE_REVEAL


def test_replace_is_a_full_replacement(repository: ShelfDbRoundRepository) -> None:
    original = make_round(level=LevelGroup.P1_P3)
    repository.create(original)
    replacement = RoundRecord(
        **{
            **original.dict(),
            "state": GameState.PROMPT_ENTRY,
            "display_name": "Replaced",
            "level": LevelGroup.M4_M6,
        }
    )

    repository.replace(replacement)

    assert repository.get(original.id).dict() == replacement.dict()


def test_generated_artifact_urls_include_all_durable_round_states(
    repository: ShelfDbRoundRepository,
) -> None:
    in_progress = make_round()
    abandoned = make_round(disposition=TerminalDisposition.ABANDONED)
    completed = make_round(
        level=LevelGroup.P1_P3,
        disposition=TerminalDisposition.COMPLETED,
        completed_at="2026-01-01T00:00:01+00:00",
    )
    urls = {
        f"/generated/{record.id}/{uuid4()}.png" for record in (in_progress, abandoned, completed)
    }
    for record, url in zip((in_progress, abandoned, completed), sorted(urls), strict=True):
        repository.create(
            RoundRecord(
                **{
                    **record.dict(),
                    "generated_artifact": ImageArtifact(url=url),
                }
            )
        )
    repository.create(make_round())

    assert repository.list_generated_artifact_urls(max_records=4) == sorted(urls)


def test_generated_artifact_url_snapshot_is_bounded_while_iterating(
    repository: ShelfDbRoundRepository,
) -> None:
    for _ in range(3):
        repository.create(make_round())

    with pytest.raises(RoundRepositoryLimitError, match="record bound"):
        repository.list_generated_artifact_urls(max_records=2)
    with pytest.raises(ValueError, match="positive integer"):
        repository.list_generated_artifact_urls(max_records=0)


def test_completed_listing_filters_level_and_orders_by_completion_then_id(
    repository: ShelfDbRoundRepository,
) -> None:
    first = make_round(
        level=LevelGroup.P1_P3,
        disposition=TerminalDisposition.COMPLETED,
        completed_at="2026-01-01T00:00:01+00:00",
    )
    second = make_round(
        level=LevelGroup.P1_P3,
        disposition=TerminalDisposition.COMPLETED,
        completed_at=first.completed_at,
    )
    third = make_round(
        level=LevelGroup.M4_M6,
        disposition=TerminalDisposition.COMPLETED,
        completed_at="2026-01-01T00:00:00+00:00",
    )
    abandoned = make_round(
        level=LevelGroup.P1_P3,
        disposition=TerminalDisposition.ABANDONED,
        completed_at="2025-01-01T00:00:00+00:00",
    )
    in_progress = make_round(level=LevelGroup.P1_P3)
    for record in (first, second, third, abandoned, in_progress):
        repository.create(record)

    all_completed = repository.list_completed()

    assert [record.id for record in all_completed] == [
        third.id,
        *sorted((first.id, second.id)),
    ]
    assert repository.list_completed(LevelGroup.P1_P3) == sorted(
        [first, second], key=lambda record: (record.completed_at or "", record.id)
    )


def test_failed_transaction_rolls_back_create(repository: ShelfDbRoundRepository) -> None:
    record = make_round()

    with pytest.raises(RoundConflictError):
        with repository._db.transaction(write=True) as transaction:  # noqa: SLF001
            transaction.shelf("rounds").put(record.id, record.dict())
            raise RoundConflictError("forced rollback")

    with pytest.raises(RoundNotFoundError):
        repository.get(record.id)


def test_rounds_survive_close_and_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "durable-rounds")
    record = make_round()
    first_db = DB(path)
    try:
        ShelfDbRoundRepository(first_db).create(record)
    finally:
        first_db.close()

    second_db = DB(path)
    try:
        assert ShelfDbRoundRepository(second_db).get(record.id).dict() == record.dict()
    finally:
        second_db.close()
