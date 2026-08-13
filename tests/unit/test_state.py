from __future__ import annotations

import pytest
from statemachine.exceptions import TransitionNotAllowed

from app.domain.models import GameState, RoundRecord
from app.domain.state import RoundStateMachine


def test_locked_round_lifecycle_and_retry_self_transition(round_record: RoundRecord) -> None:
    machine = RoundStateMachine.from_record(round_record)

    machine.configure(challenge_valid=True)
    machine.continue_challenge()
    machine.submit_prompt(prompt="a clear prompt")
    assert machine.state_value is GameState.GENERATING

    machine.pipeline_failed()
    assert machine.state_value is GameState.GENERATING
    machine.pipeline_succeeded(pipeline_valid=True)
    machine.reveal_elapsed(deadline_elapsed=True)
    machine.show_leaderboard(completed=True)

    assert machine.state_value is GameState.LEADERBOARD
    assert machine.is_terminal
    assert round_record.state is GameState.LEADERBOARD


def test_blank_timeout_reaches_terminal_abandoned_state() -> None:
    machine = RoundStateMachine(state=GameState.PROMPT_ENTRY)

    machine.abandon_blank_timeout(blank=True)

    assert machine.state_value is GameState.ABANDONED
    assert machine.is_terminal


def test_invalid_transition_and_failed_guard_leave_state_unchanged() -> None:
    machine = RoundStateMachine()
    assert machine.state_value is GameState.LEVEL_SELECTION

    with pytest.raises(TransitionNotAllowed):
        machine.continue_challenge()
    assert machine.state_value is GameState.LEVEL_SELECTION

    with pytest.raises(TransitionNotAllowed):
        machine.configure(challenge_valid=False)
    assert machine.state_value is GameState.LEVEL_SELECTION

    machine.configure()
    machine.continue_challenge()
    with pytest.raises(TransitionNotAllowed):
        machine.submit_prompt(prompt="   ")
    assert machine.state_value is GameState.PROMPT_ENTRY


def test_generation_abandonment_is_terminal_and_cannot_be_reentered() -> None:
    machine = RoundStateMachine(state=GameState.GENERATING)

    machine.abandon_generation()
    assert machine.state_value is GameState.ABANDONED

    with pytest.raises(TransitionNotAllowed):
        machine.pipeline_failed()
    assert machine.state_value is GameState.ABANDONED


def test_deadline_and_completion_guards_leave_state_unchanged() -> None:
    machine = RoundStateMachine(state=GameState.GENERATED_REVEAL)

    with pytest.raises(TransitionNotAllowed):
        machine.reveal_elapsed(deadline_elapsed=False)
    assert machine.state_value is GameState.GENERATED_REVEAL

    machine.reveal_elapsed()
    with pytest.raises(TransitionNotAllowed):
        machine.show_leaderboard(completed=False)
    assert machine.state_value is GameState.RESULT


def test_reconstruction_accepts_the_persisted_string_value(round_record: RoundRecord) -> None:
    mapping = round_record.dict()
    mapping["state"] = "result"
    reconstructed = RoundStateMachine.from_record(RoundRecord(mapping))

    assert reconstructed.state_value is GameState.RESULT
    reconstructed.show_leaderboard()
    assert reconstructed.state_value is GameState.LEADERBOARD
