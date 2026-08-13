"""Async orchestration for the setup and prompt-entry scenes of one round.

The service keeps repository operations synchronous and complete: every call to
``ShelfDbRoundRepository`` runs in one ``asyncio.to_thread`` operation.  Domain
state transitions remain owned by ``RoundStateMachine``; this module only
supplies facts such as a selected challenge, a nonblank prompt, or an elapsed
prompt deadline and persists the resulting full record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from statemachine.exceptions import TransitionNotAllowed

from app.content.repository import ChallengeCatalog
from app.domain.models import (
    ChallengeSpec,
    ChallengeStatus,
    GameState,
    LevelGroup,
    PromptSubmissionReason,
    RoundRecord,
    TerminalDisposition,
)
from app.domain.state import RoundStateMachine
from app.persistence.rounds import ShelfDbRoundRepository


class GameRoundValidationError(ValueError):
    """Raised when a round setup or prompt input violates the service contract."""


class GameRoundConflictError(ValueError):
    """Raised when a requested event is stale or invalid for the stored state."""


class GameRoundDeadlineError(ValueError):
    """Raised when a timeout submission is not authorized by its stored deadline."""


ChallengeSelector = Callable[[tuple[ChallengeSpec, ...]], ChallengeSpec]
UtcClock = Callable[[], datetime]

_PROMPT_DEADLINE = timedelta(seconds=90)
_MAX_DISPLAY_NAME_LENGTH = 30
_MAX_PROMPT_LENGTH = 1000
_ANONYMOUS_NAME = "นิรนาม"


class GameRoundService:
    """Coordinate durable round setup without owning persistence transactions.

    Manual prompt submissions are intentionally accepted whenever the persisted
    state is ``prompt_entry``, even if their deadline has passed.  The simple
    skeleton boundary treats only submissions explicitly marked ``timeout`` as
    deadline-authorized; browser and server callers can therefore race without
    making a manual submission unexpectedly fail.
    """

    def __init__(
        self,
        repository: ShelfDbRoundRepository,
        catalog: ChallengeCatalog,
        challenge_selector: ChallengeSelector,
        utc_clock: UtcClock,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._challenge_selector = challenge_selector
        self._utc_clock = utc_clock

    async def create_round(self, display_name: str = "") -> RoundRecord:
        """Create a fresh round in level selection with normalized identity."""

        normalized_name = self._normalize_display_name(display_name)
        timestamp = self._timestamp()
        record = RoundRecord(
            id=str(uuid4()),
            state=GameState.LEVEL_SELECTION,
            display_name=normalized_name,
            created_at=timestamp,
            updated_at=timestamp,
        )
        await asyncio.to_thread(self._repository.create, record)
        return record

    async def configure_round(self, round_id: str, level: LevelGroup | str) -> RoundRecord:
        """Select and persist an approved challenge, entering its reveal scene."""

        record = await self._get_record(round_id)
        selected_level = self._coerce_level(level)
        candidates = self._catalog.for_level(selected_level)
        challenge = self._select_challenge(candidates, selected_level)

        machine = RoundStateMachine.from_record(record)
        self._transition(
            machine,
            lambda: machine.configure(challenge_valid=True),
            "configure",
        )
        timestamp = self._timestamp()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            level=selected_level,
            challenge_id=challenge.id,
            updated_at=timestamp,
        )
        await asyncio.to_thread(self._repository.replace, replacement)
        return replacement

    async def continue_challenge(self, round_id: str) -> RoundRecord:
        """Enter prompt entry and start the authoritative ninety-second deadline."""

        record = await self._get_record(round_id)
        machine = RoundStateMachine.from_record(record)
        self._transition(machine, machine.continue_challenge, "continue_challenge")
        timestamp = self._timestamp()
        deadline = (self._as_datetime(timestamp) + _PROMPT_DEADLINE).isoformat()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            prompt_deadline=deadline,
            updated_at=timestamp,
        )
        await asyncio.to_thread(self._repository.replace, replacement)
        return replacement

    async def submit_prompt(
        self,
        round_id: str,
        prompt: str,
        reason: PromptSubmissionReason | str,
    ) -> RoundRecord:
        """Submit a prompt or atomically abandon a blank timeout.

        The raw prompt is retained for nonblank submissions; surrounding
        whitespace is examined only to decide whether the prompt is blank.
        ``timeout`` requires the injected clock to be at or after the persisted
        prompt deadline, while ``manual`` follows the simple state-only boundary
        documented on :class:`GameRoundService`.
        """

        prompt = self._validate_prompt(prompt)
        submission_reason = self._coerce_reason(reason)
        record = await self._get_record(round_id)
        machine = RoundStateMachine.from_record(record)
        blank = not prompt.strip()

        if submission_reason is PromptSubmissionReason.MANUAL:
            if blank:
                raise GameRoundValidationError("manual prompt must not be blank")
            self._transition(
                machine,
                lambda: machine.submit_prompt(prompt_valid=True),
                "submit_prompt",
            )
            timestamp = self._timestamp()
            replacement = self._replacement(
                record,
                state=machine.state_value,
                prompt=prompt,
                prompt_submission_reason=submission_reason,
                updated_at=timestamp,
            )
        else:
            if blank:
                self._transition(
                    machine,
                    lambda: machine.abandon_blank_timeout(blank=True),
                    "abandon_blank_timeout",
                )
            else:
                self._transition(
                    machine,
                    lambda: machine.submit_prompt(prompt_valid=True),
                    "submit_prompt",
                )

            timestamp = self._timestamp()
            self._require_elapsed_prompt_deadline(record, timestamp)
            if blank:
                replacement = self._replacement(
                    record,
                    state=machine.state_value,
                    terminal_disposition=TerminalDisposition.ABANDONED,
                    updated_at=timestamp,
                    completed_at=timestamp,
                )
            else:
                replacement = self._replacement(
                    record,
                    state=machine.state_value,
                    prompt=prompt,
                    prompt_submission_reason=submission_reason,
                    updated_at=timestamp,
                )

        await asyncio.to_thread(self._repository.replace, replacement)
        return replacement

    async def get_round(self, round_id: str) -> RoundRecord:
        """Return the freshly reconstructed durable record for a round."""

        return await self._get_record(round_id)

    async def _get_record(self, round_id: str) -> RoundRecord:
        return await asyncio.to_thread(self._repository.get, round_id)

    @staticmethod
    def _replacement(record: RoundRecord, **changes: object) -> RoundRecord:
        return RoundRecord(**{**record.dict(), **changes})

    @staticmethod
    def _normalize_display_name(display_name: str) -> str:
        if not isinstance(display_name, str):
            raise GameRoundValidationError("display name must be a string")
        normalized = display_name.strip() or _ANONYMOUS_NAME
        if len(normalized) > _MAX_DISPLAY_NAME_LENGTH:
            raise GameRoundValidationError("display name must be at most 30 characters")
        return normalized

    @staticmethod
    def _validate_prompt(prompt: str) -> str:
        if not isinstance(prompt, str):
            raise GameRoundValidationError("prompt must be a string")
        if len(prompt) > _MAX_PROMPT_LENGTH:
            raise GameRoundValidationError("prompt must be at most 1000 characters")
        return prompt

    @staticmethod
    def _coerce_level(level: LevelGroup | str) -> LevelGroup:
        try:
            return level if isinstance(level, LevelGroup) else LevelGroup(level)
        except (TypeError, ValueError) as error:
            raise GameRoundValidationError(f"invalid level: {level!r}") from error

    @staticmethod
    def _coerce_reason(reason: PromptSubmissionReason | str) -> PromptSubmissionReason:
        try:
            return (
                reason
                if isinstance(reason, PromptSubmissionReason)
                else PromptSubmissionReason(reason)
            )
        except (TypeError, ValueError) as error:
            raise GameRoundValidationError(
                f"invalid prompt submission reason: {reason!r}"
            ) from error

    def _select_challenge(
        self,
        candidates: tuple[ChallengeSpec, ...],
        level: LevelGroup,
    ) -> ChallengeSpec:
        try:
            selected = self._challenge_selector(candidates)
        except (LookupError, TypeError, ValueError) as error:
            raise GameRoundValidationError(
                "challenge selector did not select a challenge"
            ) from error
        if not isinstance(selected, ChallengeSpec) or not any(
            selected is candidate for candidate in candidates
        ):
            raise GameRoundValidationError("challenge selector returned an unavailable challenge")
        if selected.level is not level or selected.status is not ChallengeStatus.APPROVED:
            raise GameRoundValidationError("challenge selector returned an invalid challenge")
        return selected

    @staticmethod
    def _transition(
        machine: RoundStateMachine,
        transition: Callable[[], object],
        event: str,
    ) -> None:
        try:
            transition()
        except TransitionNotAllowed as error:
            raise GameRoundConflictError(
                f"cannot {event} from {machine.state_value.value}"
            ) from error

    def _timestamp(self) -> str:
        current = self._utc_clock()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise GameRoundValidationError("UTC clock must return an aware datetime")
        return current.astimezone(UTC).isoformat()

    @staticmethod
    def _as_datetime(timestamp: str) -> datetime:
        candidate = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        return datetime.fromisoformat(candidate).astimezone(UTC)

    @classmethod
    def _require_elapsed_prompt_deadline(cls, record: RoundRecord, current: str) -> None:
        if record.prompt_deadline is None:
            raise GameRoundDeadlineError("round has no prompt deadline")
        if cls._as_datetime(current) < cls._as_datetime(record.prompt_deadline):
            raise GameRoundDeadlineError("prompt deadline has not elapsed")


__all__ = [
    "ChallengeSelector",
    "GameRoundConflictError",
    "GameRoundDeadlineError",
    "GameRoundService",
    "GameRoundValidationError",
    "UtcClock",
]
