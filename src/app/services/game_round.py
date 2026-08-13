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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Protocol
from uuid import uuid4

from statemachine.exceptions import TransitionNotAllowed

from app.ai.results import AIPipelineResult
from app.content.repository import ChallengeSource
from app.domain.models import (
    AttemptClaim,
    ChallengeSpec,
    ChallengeStatus,
    FailureDetail,
    GameState,
    GenerationStatusState,
    LeaderboardEntry,
    LevelGroup,
    PipelineResultStatus,
    PromptSubmissionReason,
    RoundRecord,
    TerminalDisposition,
)
from app.domain.state import RoundStateMachine
from app.persistence.challenges import ChallengeNotFoundError, ChallengeRepositoryError
from app.persistence.claims import (
    RoundNotClaimableError,
    ShelfDbGenerationClaims,
    StaleAttemptTokenError,
)
from app.persistence.rounds import (
    RoundSnapshotConflictError,
    ShelfDbRoundRepository,
)


class GameRoundValidationError(ValueError):
    """Raised when a round setup or prompt input violates the service contract."""


class GameRoundConflictError(ValueError):
    """Raised when a requested event is stale or invalid for the stored state."""


class GameRoundDeadlineError(ValueError):
    """Raised when a prompt submission cannot be authorized by its stored deadline."""


ChallengeSelector = Callable[[tuple[ChallengeSpec, ...]], ChallengeSpec]
UtcClock = Callable[[], datetime]


@dataclass(frozen=True)
class LeaderboardProjection:
    """Immutable completed-round leaderboard entries and the current rank."""

    entries: tuple[LeaderboardEntry, ...]
    current_rank: int


@dataclass(frozen=True)
class GenerationStatus:
    """Read-only generation state projected from one round and its claim."""

    state: GenerationStatusState


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
_LEADERBOARD_DURATION = timedelta(seconds=15)
_MAX_LEADERBOARD_ROWS = 4
_DEFAULT_CLAIM_LEASE = timedelta(seconds=30)
_DEFAULT_PROVIDER_TIMEOUT = 10.0
_PROVIDER_TIMEOUT_CODE = "provider_timeout"
_PROVIDER_TIMEOUT_MESSAGE = "การประมวลผล AI ใช้เวลานานเกินไป"
_PROVIDER_ERROR_CODE = "provider_error"
_PROVIDER_ERROR_MESSAGE = "การประมวลผล AI ล้มเหลวชั่วคราว"
_INVALID_PIPELINE_RESULT_CODE = "invalid_pipeline_result"
_INVALID_PIPELINE_RESULT_MESSAGE = "ผลลัพธ์จาก AI ไม่ถูกต้อง"
_MAX_DISPLAY_NAME_LENGTH = 30
_MAX_PROMPT_LENGTH = 1000
_ANONYMOUS_NAME = "นิรนาม"


class GameRoundService:
    """Coordinate durable round setup without owning persistence transactions.

    The persisted prompt deadline authorizes every submission.  Before it elapses,
    only a nonblank manual submission is accepted.  At or after it elapses, a
    nonblank prompt is stored as a timeout submission and a blank prompt abandons
    the round, regardless of the client-provided reason.
    """

    def __init__(
        self,
        repository: ShelfDbRoundRepository,
        catalog: ChallengeSource,
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
        try:
            candidates = self._catalog.for_level(selected_level)
        except ChallengeRepositoryError as error:
            raise GameRoundValidationError("stored challenge catalog is invalid") from error
        challenge = self._select_challenge(candidates, selected_level)

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
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
        await self._replace_current(record, replacement, "configure")
        return replacement

    async def continue_challenge(self, round_id: str) -> RoundRecord:
        """Enter prompt entry and start the authoritative ninety-second deadline."""

        record = await self._get_record(round_id)
        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
        self._transition(machine, machine.continue_challenge, "continue_challenge")
        timestamp = self._timestamp()
        deadline = (self._as_datetime(timestamp) + _PROMPT_DEADLINE).isoformat()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            prompt_deadline=deadline,
            updated_at=timestamp,
        )
        await self._replace_current(record, replacement, "continue challenge")
        return replacement

    async def submit_prompt(
        self,
        round_id: str,
        prompt: str,
        reason: PromptSubmissionReason | str,
    ) -> RoundRecord:
        """Submit a prompt or atomically abandon a blank elapsed submission.

        The raw prompt is retained for nonblank submissions; surrounding
        whitespace is examined only to decide whether the prompt is blank.  The
        persisted deadline, rather than the client-provided reason, decides
        whether the result is manual, timeout, or abandonment.
        """

        prompt = self._validate_prompt(prompt)
        submission_reason = self._coerce_reason(reason)
        record = await self._get_record(round_id)
        self._require_state(record, GameState.PROMPT_ENTRY, "submit_prompt")
        timestamp = self._timestamp()
        deadline_elapsed = self._prompt_deadline_elapsed(record, timestamp)
        blank = not prompt.strip()

        if not deadline_elapsed:
            if submission_reason is PromptSubmissionReason.TIMEOUT:
                raise GameRoundDeadlineError("prompt deadline has not elapsed")
            if blank:
                raise GameRoundValidationError("manual prompt must not be blank")
            persisted_reason = PromptSubmissionReason.MANUAL
        elif blank:
            persisted_reason = None
        else:
            persisted_reason = PromptSubmissionReason.TIMEOUT

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
        if blank and deadline_elapsed:
            self._transition(
                machine,
                lambda: machine.abandon_blank_timeout(blank=True),
                "abandon_blank_timeout",
            )
            replacement = self._replacement(
                record,
                state=machine.state_value,
                terminal_disposition=TerminalDisposition.ABANDONED,
                updated_at=timestamp,
                completed_at=timestamp,
            )
        else:
            self._transition(
                machine,
                lambda: machine.submit_prompt(prompt_valid=True),
                "submit_prompt",
            )
            replacement = self._replacement(
                record,
                state=machine.state_value,
                prompt=prompt,
                prompt_submission_reason=persisted_reason,
                updated_at=timestamp,
            )

        await self._replace_current(record, replacement, "submit prompt")
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
            await self._acquire_generation_claim(claims, round_id, claim, claimed_at)
        except RoundNotClaimableError as error:
            raise GameRoundConflictError(f"cannot generate from {record.state.value}") from error

        try:
            try:
                raw_result = await asyncio.wait_for(
                    pipeline.run(challenge, prompt, timeout=self._provider_timeout),
                    timeout=self._provider_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raw_result = self._bounded_failure_result(
                    _PROVIDER_TIMEOUT_CODE,
                    _PROVIDER_TIMEOUT_MESSAGE,
                )
            except Exception:
                raw_result = self._bounded_failure_result(
                    _PROVIDER_ERROR_CODE,
                    _PROVIDER_ERROR_MESSAGE,
                )

            result = self._normalize_pipeline_result(raw_result)
            if result.status is PipelineResultStatus.SUCCESS:
                return await self._persist_generation_success(record, result, claims, claim)
            return await self._persist_generation_failure(record, result, claims, claim)
        except asyncio.CancelledError:
            await self._release_cancelled_attempt(claims, round_id, claim)
            raise

    async def abandon_generation(self, round_id: str) -> RoundRecord:
        """Atomically abandon generation and fence any late provider result."""

        claims = self._require_claims()
        record = await self._get_record(round_id)
        self._generation_context(record)
        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
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
        try:
            await asyncio.to_thread(
                claims.replace_round_and_clear_claim,
                replacement,
                expected=record,
            )
        except RoundSnapshotConflictError as error:
            raise GameRoundConflictError("generation round is stale") from error
        return replacement

    async def get_round(self, round_id: str) -> RoundRecord:
        """Return the freshly reconstructed durable record for a round."""

        return await self._get_record(round_id)

    async def get_generation_status(self, round_id: str) -> GenerationStatus:
        """Project a bounded status from the persisted round and active claim."""

        record = await self._get_record(round_id)
        if record.state is GameState.GENERATED_REVEAL:
            if (
                record.generated_artifact is None
                or record.score is None
                or record.reveal_deadline is None
            ):
                raise GameRoundValidationError("generated round is missing status data")
            return GenerationStatus(GenerationStatusState.GENERATED)
        if record.state is not GameState.GENERATING:
            return GenerationStatus(GenerationStatusState.CONFLICT)

        try:
            claim = await asyncio.to_thread(self._require_claims().get, round_id)
        except ValueError as error:
            raise GameRoundValidationError("stored generation claim is invalid") from error
        if claim is not None:
            try:
                claim_is_active = self._as_datetime(claim.lease_expires_at) > self._as_datetime(
                    self._timestamp()
                )
            except GameRoundValidationError as error:
                raise GameRoundValidationError("stored generation claim is invalid") from error
            if claim_is_active:
                return GenerationStatus(GenerationStatusState.RUNNING)
        if record.pipeline_failure is not None:
            return GenerationStatus(GenerationStatusState.FAILURE)
        return GenerationStatus(GenerationStatusState.WAITING)

    async def show_result(self, round_id: str) -> RoundRecord:
        """Authorize the generated reveal timeout and persist the Result scene."""

        record = await self._get_record(round_id)
        self._require_state(record, GameState.GENERATED_REVEAL, "show result")
        if record.terminal_disposition is not None:
            raise GameRoundConflictError("generated reveal round is terminal")
        self._require_result_context(record)
        timestamp = self._timestamp()
        self._require_elapsed_reveal_deadline(record, timestamp)

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
        self._transition(
            machine,
            lambda: machine.reveal_elapsed(deadline_elapsed=True),
            "reveal_elapsed",
        )
        replacement = self._replacement(
            record,
            state=machine.state_value,
            updated_at=timestamp,
        )
        await self._replace_current(record, replacement, "show result")
        return replacement

    async def complete_round(self, round_id: str) -> RoundRecord:
        """Persist a complete Result as terminal completed leaderboard history."""

        record = await self._get_record(round_id)
        self._require_state(record, GameState.RESULT, "complete round")
        if record.terminal_disposition is not None:
            raise GameRoundConflictError("result round is terminal")
        self._require_result_context(record)
        if record.level is None:
            raise GameRoundValidationError("result round is missing level")
        if record.prompt is None or not record.prompt.strip():
            raise GameRoundValidationError("result round is missing prompt")

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
        self._transition(
            machine,
            lambda: machine.show_leaderboard(completed=True),
            "show_leaderboard",
        )
        timestamp = self._timestamp()
        replacement = self._replacement(
            record,
            state=machine.state_value,
            terminal_disposition=TerminalDisposition.COMPLETED,
            completed_at=timestamp,
            updated_at=timestamp,
            leaderboard_deadline=(self._as_datetime(timestamp) + _LEADERBOARD_DURATION).isoformat(),
        )
        await self._replace_current(record, replacement, "complete round")
        return replacement

    def leaderboard_deadline_elapsed(self, record: RoundRecord) -> bool:
        """Compare the persisted leaderboard deadline with the authoritative clock."""

        if record.leaderboard_deadline is None:
            raise GameRoundValidationError("Leaderboard data is incomplete")
        try:
            deadline = self._as_datetime(record.leaderboard_deadline)
        except GameRoundValidationError as error:
            raise GameRoundValidationError(
                "round contains a malformed leaderboard deadline"
            ) from error
        return self._as_datetime(self._timestamp()) >= deadline

    async def get_leaderboard(self, round_id: str) -> LeaderboardProjection:
        """Project the current completed round into its level's ranked history."""

        current = await self._get_record(round_id)
        self._require_state(current, GameState.LEADERBOARD, "get leaderboard")
        if current.terminal_disposition is not TerminalDisposition.COMPLETED:
            raise GameRoundConflictError("leaderboard round is not completed")
        if current.level is None:
            raise GameRoundValidationError("completed round is missing level")
        self._validate_completed_row(current, current.level)

        try:
            completed = await asyncio.to_thread(
                self._repository.list_completed,
                current.level,
            )
        except ValueError as error:
            raise GameRoundValidationError("stored leaderboard row is invalid") from error
        rows = [self._validate_completed_row(row, current.level) for row in completed]
        current_rows = [row for row in rows if row.id == current.id]
        if len(current_rows) != 1:
            raise GameRoundValidationError(
                "current completed round must appear exactly once in leaderboard"
            )

        ordered = sorted(
            rows,
            key=lambda row: (
                -self._visible_score(row),
                row.completed_at or "",
                row.id,
            ),
        )
        entries: list[LeaderboardEntry] = []
        current_rank: int | None = None
        current_index: int | None = None
        previous_rank = 0
        previous_score: int | float | None = None
        for index, row in enumerate(ordered):
            if row.generated_artifact is None or row.prompt is None:
                raise GameRoundValidationError("completed leaderboard row is incomplete")
            score = self._visible_score(row)
            rank = 1 if index == 0 else previous_rank if score == previous_score else index + 1
            entry = LeaderboardEntry(
                round_id=row.id,
                is_current=row.id == current.id,
                rank=rank,
                name=row.display_name,
                score=score,
                generated_image=row.generated_artifact,
                prompt=row.prompt,
            )
            entries.append(entry)
            if row.id == current.id:
                current_rank = rank
                current_index = index
            previous_rank = rank
            previous_score = score

        if current_rank is None or current_index is None:
            raise GameRoundValidationError(
                "current completed round must appear exactly once in leaderboard"
            )
        visible_entries = self._leaderboard_window(entries, current_index)
        return LeaderboardProjection(visible_entries, current_rank)

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
            result = self._bounded_failure_result(
                _INVALID_PIPELINE_RESULT_CODE,
                _INVALID_PIPELINE_RESULT_MESSAGE,
            )
            return await self._persist_generation_failure(record, result, claims, claim)

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
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
        await self._replace_and_release(
            claims,
            replacement,
            claim.attempt_token,
            timestamp,
            record,
        )
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
            result = self._bounded_failure_result(
                _INVALID_PIPELINE_RESULT_CODE,
                _INVALID_PIPELINE_RESULT_MESSAGE,
            )

        machine = RoundStateMachine.from_record(RoundRecord(record.dict()))
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
        await self._replace_and_release(
            claims,
            replacement,
            claim.attempt_token,
            timestamp,
            record,
        )
        return replacement

    async def _acquire_generation_claim(
        self,
        claims: ShelfDbGenerationClaims,
        round_id: str,
        claim: AttemptClaim,
        claimed_at: str,
    ) -> None:
        """Settle acquisition before cancellation cleanup can inspect its claim."""

        acquisition = asyncio.create_task(
            asyncio.to_thread(claims.claim, round_id, claim, claimed_at)
        )
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancellation:
            acquisition_error = await self._settle_cancelled_task(acquisition)
            if acquisition_error is None:
                try:
                    await self._release_cancelled_attempt(claims, round_id, claim)
                except Exception as cleanup_error:
                    raise cancellation from cleanup_error
            raise

    async def _release_cancelled_attempt(
        self,
        claims: ShelfDbGenerationClaims,
        round_id: str,
        claim: AttemptClaim,
    ) -> None:
        """Release a token after cancellation without swallowing cancellation."""

        release = asyncio.create_task(
            asyncio.to_thread(
                claims.release_matching,
                round_id,
                claim.attempt_token,
            )
        )
        release_error = await self._settle_cancelled_task(release)
        if isinstance(release_error, StaleAttemptTokenError):
            # A concurrent abandon or replacement already fenced this token.
            return
        if release_error is not None:
            raise release_error

    @staticmethod
    async def _settle_cancelled_task(task: asyncio.Task[object]) -> BaseException | None:
        """Wait for a shielded worker despite repeated outer cancellation."""

        while True:
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError as error:
                    return error
                except Exception as error:
                    return error
                return None
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep waiting: the worker thread must settle before cleanup or
                # cancellation can complete.
                continue
            except Exception as error:
                return error

    @staticmethod
    def _bounded_failure_result(code: str, message: str) -> AIPipelineResult:
        return AIPipelineResult(
            status=PipelineResultStatus.ERROR,
            failure=FailureDetail(code=code, message=message, retryable=True),
        )

    @classmethod
    def _normalize_pipeline_result(cls, result: object) -> AIPipelineResult:
        """Validate a provider envelope without allowing its details to escape."""

        if not isinstance(result, AIPipelineResult):
            return cls._bounded_failure_result(
                _INVALID_PIPELINE_RESULT_CODE,
                _INVALID_PIPELINE_RESULT_MESSAGE,
            )
        try:
            return AIPipelineResult(result.dict())
        except Exception:
            return cls._bounded_failure_result(
                _INVALID_PIPELINE_RESULT_CODE,
                _INVALID_PIPELINE_RESULT_MESSAGE,
            )

    async def _replace_and_release(
        self,
        claims: ShelfDbGenerationClaims,
        record: RoundRecord,
        attempt_token: str,
        now: str,
        expected: RoundRecord,
    ) -> None:
        try:
            await asyncio.to_thread(
                claims.replace_round_and_release,
                record,
                attempt_token,
                now,
                expected=expected,
            )
        except (RoundSnapshotConflictError, StaleAttemptTokenError) as error:
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
        except (KeyError, ChallengeNotFoundError) as error:
            raise GameRoundValidationError("generating round has an unknown challenge") from error
        except ChallengeRepositoryError as error:
            raise GameRoundValidationError("stored challenge is invalid") from error
        return challenge, record.prompt

    @staticmethod
    def _require_state(record: RoundRecord, expected: GameState, event: str) -> None:
        if record.state is not expected:
            raise GameRoundConflictError(f"cannot {event} from {record.state.value}")

    @staticmethod
    def _require_result_context(record: RoundRecord) -> None:
        if any(
            value is None
            for value in (
                record.generated_artifact,
                record.prompt_evaluation,
                record.image_evaluation,
                record.score,
            )
        ):
            raise GameRoundValidationError("round is missing complete result data")
        if len(record.feedback) not in (2, 3) or any(
            not feedback.strip() for feedback in record.feedback
        ):
            raise GameRoundValidationError("round is missing complete feedback")
        if record.pipeline_failure is not None:
            raise GameRoundValidationError("round has an unexpected pipeline failure")

    @classmethod
    def _require_elapsed_reveal_deadline(cls, record: RoundRecord, current: str) -> None:
        if record.reveal_deadline is None:
            raise GameRoundDeadlineError("round has no reveal deadline")
        if cls._as_datetime(current) < cls._as_datetime(record.reveal_deadline):
            raise GameRoundDeadlineError("reveal deadline has not elapsed")

    @classmethod
    def _validate_completed_row(
        cls,
        record: RoundRecord,
        level: LevelGroup,
    ) -> RoundRecord:
        if not isinstance(record, RoundRecord):
            raise GameRoundValidationError("completed leaderboard row is invalid")
        if record.state is not GameState.LEADERBOARD:
            raise GameRoundValidationError("completed leaderboard row has invalid state")
        if record.terminal_disposition is not TerminalDisposition.COMPLETED:
            raise GameRoundValidationError("completed leaderboard row has invalid disposition")
        if record.level is not level:
            raise GameRoundValidationError("completed leaderboard row has invalid level")
        if not record.display_name.strip():
            raise GameRoundValidationError("completed leaderboard row is missing display name")
        if record.prompt is None or not record.prompt.strip():
            raise GameRoundValidationError("completed leaderboard row is missing prompt")
        if record.completed_at is None or record.leaderboard_deadline is None:
            raise GameRoundValidationError("completed leaderboard row is missing timestamps")
        cls._as_datetime(record.completed_at)
        cls._as_datetime(record.leaderboard_deadline)
        cls._require_result_context(record)
        return record

    @staticmethod
    def _leaderboard_window(
        entries: list[LeaderboardEntry], current_index: int
    ) -> tuple[LeaderboardEntry, ...]:
        """Keep four contiguous rows while retaining global rank context."""

        if len(entries) <= _MAX_LEADERBOARD_ROWS:
            return tuple(entries)
        if current_index < _MAX_LEADERBOARD_ROWS:
            start = 0
        else:
            start = current_index - 2
        start = min(start, len(entries) - _MAX_LEADERBOARD_ROWS)
        return tuple(entries[start : start + _MAX_LEADERBOARD_ROWS])

    @staticmethod
    def _visible_score(record: RoundRecord) -> int | float:
        if record.score is None:
            raise GameRoundValidationError("completed leaderboard row is missing score")
        return record.score.total_score

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

    async def _replace_current(
        self,
        expected: RoundRecord,
        replacement: RoundRecord,
        event: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._repository.replace_if_current,
                replacement,
                expected,
            )
        except RoundSnapshotConflictError as error:
            raise GameRoundConflictError(f"cannot {event}: round snapshot is stale") from error

    async def _get_record(self, round_id: str) -> RoundRecord:
        try:
            record = await asyncio.to_thread(self._repository.get, round_id)
        except ValueError as error:
            raise GameRoundValidationError("stored round is invalid") from error
        if not isinstance(record, RoundRecord):
            raise GameRoundValidationError("stored round is invalid")
        return record

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
        try:
            parsed = datetime.fromisoformat(candidate)
        except (TypeError, ValueError, OverflowError) as error:
            raise GameRoundValidationError("round contains a malformed timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GameRoundValidationError("round contains a timestamp without timezone")
        return parsed.astimezone(UTC)

    @classmethod
    def _prompt_deadline_elapsed(cls, record: RoundRecord, current: str) -> bool:
        if record.prompt_deadline is None:
            raise GameRoundDeadlineError("round has no prompt deadline")
        try:
            deadline = cls._as_datetime(record.prompt_deadline)
        except GameRoundValidationError as error:
            raise GameRoundDeadlineError("round has a malformed prompt deadline") from error
        return cls._as_datetime(current) >= deadline


__all__ = [
    "AIPipelineRunner",
    "ChallengeSelector",
    "GameRoundConflictError",
    "GameRoundDeadlineError",
    "GameRoundService",
    "LeaderboardProjection",
    "GameRoundValidationError",
    "UtcClock",
]
