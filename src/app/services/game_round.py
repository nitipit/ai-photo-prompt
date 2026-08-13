"""Async orchestration for one round's setup and generation lifecycle.

The service keeps repository and claim operations synchronous and complete:
every ShelfDB call runs in one ``asyncio.to_thread`` operation.  Domain state
transitions remain owned by ``RoundStateMachine``; this module supplies facts
such as a selected challenge, a nonblank prompt, or a validated pipeline result
and persists the resulting full record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Protocol
from uuid import uuid4

from statemachine.exceptions import TransitionNotAllowed

from app.ai.results import AIPipelineResult
from app.content.repository import ChallengeCatalog
from app.domain.models import (
    AttemptClaim,
    ChallengeSpec,
    ChallengeStatus,
    FailureDetail,
    GameState,
    LevelGroup,
    PipelineResultStatus,
    PromptSubmissionReason,
    RoundRecord,
    TerminalDisposition,
)
from app.domain.state import RoundStateMachine
from app.persistence.claims import (
    RoundNotClaimableError,
    ShelfDbGenerationClaims,
    StaleAttemptTokenError,
)
from app.persistence.rounds import ShelfDbRoundRepository


class GameRoundValidationError(ValueError):
    """Raised when a round setup or prompt input violates the service contract."""


class GameRoundConflictError(ValueError):
    """Raised when a requested event is stale or invalid for the stored state."""


class GameRoundDeadlineError(ValueError):
    """Raised when a timeout submission is not authorized by its stored deadline."""


ChallengeSelector = Callable[[tuple[ChallengeSpec, ...]], ChallengeSpec]
UtcClock = Callable[[], datetime]


class AIPipelineRunner(Protocol):
    """Async runner compatible with the local fake pipeline boundary."""

    async def run(
        self,
        challenge: ChallengeSpec,
        prompt: str,
        timeout: float,
    ) -> AIPipelineResult: ...


_PROMPT_DEADLINE = timedelta(seconds=90)
_REVEAL_DURATION = timedelta(seconds=5)
_DEFAULT_CLAIM_LEASE = timedelta(seconds=30)
_DEFAULT_PROVIDER_TIMEOUT = 10.0
_PROVIDER_TIMEOUT_CODE = "provider_timeout"
_PROVIDER_TIMEOUT_MESSAGE = "การประมวลผล AI ใช้เวลานานเกินไป"
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
        *,
        generation_claims: ShelfDbGenerationClaims | None = None,
        pipeline: AIPipelineRunner | None = None,
        owner_instance: str | None = None,
        claim_lease_duration: timedelta = _DEFAULT_CLAIM_LEASE,
        provider_timeout: float = _DEFAULT_PROVIDER_TIMEOUT,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._challenge_selector = challenge_selector
        self._utc_clock = utc_clock
        self._generation_claims = generation_claims
        self._pipeline = pipeline
        self._owner_instance = owner_instance
        self._claim_lease_duration = claim_lease_duration
        self._provider_timeout = provider_timeout
        self._validate_generation_timing(claim_lease_duration, provider_timeout)

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

    async def generate_round(self, round_id: str) -> RoundRecord:
        """Run one claimed provider attempt and persist its bounded outcome."""

        claims, pipeline, owner_instance = self._require_generation_dependencies()
        record = await self._get_record(round_id)
        challenge, prompt = self._generation_context(record)

        claimed_at = self._timestamp()
        claim = AttemptClaim(
            attempt_token=str(uuid4()),
            owner_instance=owner_instance,
            claimed_at=claimed_at,
            lease_expires_at=(
                self._as_datetime(claimed_at) + self._claim_lease_duration
            ).isoformat(),
        )
        try:
            await asyncio.to_thread(claims.claim, round_id, claim, claimed_at)
        except RoundNotClaimableError as error:
            raise GameRoundConflictError(f"cannot generate from {record.state.value}") from error

        try:
            result = await asyncio.wait_for(
                pipeline.run(challenge, prompt, timeout=self._provider_timeout),
                timeout=self._provider_timeout,
            )
        except TimeoutError:
            result = AIPipelineResult(
                status=PipelineResultStatus.ERROR,
                failure=FailureDetail(
                    code=_PROVIDER_TIMEOUT_CODE,
                    message=_PROVIDER_TIMEOUT_MESSAGE,
                    retryable=True,
                ),
            )
        if not isinstance(result, AIPipelineResult):
            raise GameRoundValidationError("pipeline returned an invalid result")

        if result.status is PipelineResultStatus.SUCCESS:
            return await self._persist_generation_success(record, result, claims, claim)
        return await self._persist_generation_failure(record, result, claims, claim)

    async def abandon_generation(self, round_id: str) -> RoundRecord:
        """Atomically abandon generation and fence any late provider result."""

        claims = self._require_claims()
        record = await self._get_record(round_id)
        self._generation_context(record)
        machine = RoundStateMachine.from_record(record)
        self._transition(machine, machine.abandon_generation, "abandon_generation")

        timestamp = self._timestamp()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            generated_artifact=None,
            prompt_evaluation=None,
            image_evaluation=None,
            score=None,
            pipeline_failure=None,
            feedback=[],
            terminal_disposition=TerminalDisposition.ABANDONED,
            updated_at=timestamp,
            reveal_deadline=None,
            generated_at=None,
            completed_at=timestamp,
        )
        await asyncio.to_thread(claims.replace_round_and_clear_claim, replacement)
        return replacement

    async def get_round(self, round_id: str) -> RoundRecord:
        """Return the freshly reconstructed durable record for a round."""

        return await self._get_record(round_id)

    async def _persist_generation_success(
        self,
        record: RoundRecord,
        result: AIPipelineResult,
        claims: ShelfDbGenerationClaims,
        claim: AttemptClaim,
    ) -> RoundRecord:
        """Apply a validated successful pipeline result under its attempt token."""

        if (
            result.artifact is None
            or result.prompt_evaluation is None
            or result.image_evaluation is None
            or result.score is None
        ):
            raise GameRoundValidationError("pipeline success result is incomplete")

        machine = RoundStateMachine.from_record(record)
        self._transition(
            machine,
            lambda: machine.pipeline_succeeded(pipeline_valid=True),
            "pipeline_succeeded",
        )
        timestamp = self._timestamp()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            generated_artifact=result.artifact,
            prompt_evaluation=result.prompt_evaluation,
            image_evaluation=result.image_evaluation,
            score=result.score,
            pipeline_failure=None,
            feedback=list(result.feedback),
            updated_at=timestamp,
            generated_at=timestamp,
            reveal_deadline=(self._as_datetime(timestamp) + _REVEAL_DURATION).isoformat(),
        )
        await self._replace_and_release(claims, replacement, claim.attempt_token)
        return replacement

    async def _persist_generation_failure(
        self,
        record: RoundRecord,
        result: AIPipelineResult,
        claims: ShelfDbGenerationClaims,
        claim: AttemptClaim,
    ) -> RoundRecord:
        """Persist only a safe bounded failure and release the attempt token."""

        if result.failure is None:
            raise GameRoundValidationError("pipeline failure result is incomplete")

        machine = RoundStateMachine.from_record(record)
        self._transition(machine, machine.pipeline_failed, "pipeline_failed")
        timestamp = self._timestamp()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            generated_artifact=None,
            prompt_evaluation=None,
            image_evaluation=None,
            score=None,
            pipeline_failure=result.failure,
            feedback=[],
            updated_at=timestamp,
            reveal_deadline=None,
            generated_at=None,
            completed_at=None,
            terminal_disposition=None,
        )
        await self._replace_and_release(claims, replacement, claim.attempt_token)
        return replacement

    async def _replace_and_release(
        self,
        claims: ShelfDbGenerationClaims,
        record: RoundRecord,
        attempt_token: str,
    ) -> None:
        try:
            await asyncio.to_thread(claims.replace_round_and_release, record, attempt_token)
        except StaleAttemptTokenError as error:
            raise GameRoundConflictError("generation attempt is stale") from error

    def _require_generation_dependencies(
        self,
    ) -> tuple[ShelfDbGenerationClaims, AIPipelineRunner, str]:
        if (
            self._generation_claims is None
            or self._pipeline is None
            or self._owner_instance is None
            or not self._owner_instance.strip()
        ):
            raise GameRoundValidationError("generation dependencies are not configured")
        return self._generation_claims, self._pipeline, self._owner_instance

    def _require_claims(self) -> ShelfDbGenerationClaims:
        if self._generation_claims is None:
            raise GameRoundValidationError("generation claims are not configured")
        return self._generation_claims

    def _generation_context(self, record: RoundRecord) -> tuple[ChallengeSpec, str]:
        if record.state is not GameState.GENERATING or record.terminal_disposition is not None:
            raise GameRoundConflictError(f"cannot generate from {record.state.value}")
        if not record.challenge_id or record.prompt is None or not record.prompt.strip():
            raise GameRoundValidationError("generating round is missing challenge or prompt")
        try:
            challenge = self._catalog.get(record.challenge_id)
        except KeyError as error:
            raise GameRoundValidationError("generating round has an unknown challenge") from error
        return challenge, record.prompt

    @staticmethod
    def _validate_generation_timing(
        claim_lease_duration: timedelta,
        provider_timeout: float,
    ) -> None:
        if not isinstance(claim_lease_duration, timedelta):
            raise GameRoundValidationError("claim lease duration must be a timedelta")
        if (
            isinstance(provider_timeout, bool)
            or not isinstance(provider_timeout, (int, float))
            or not isfinite(provider_timeout)
            or provider_timeout <= 0
        ):
            raise GameRoundValidationError("provider timeout must be a positive finite number")
        if claim_lease_duration.total_seconds() <= provider_timeout:
            raise GameRoundValidationError(
                "claim lease duration must be longer than provider timeout"
            )

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
    "AIPipelineRunner",
    "ChallengeSelector",
    "GameRoundConflictError",
    "GameRoundDeadlineError",
    "GameRoundService",
    "GameRoundValidationError",
    "UtcClock",
]
