"""Flat, side-effect-free round lifecycle state machine."""

from __future__ import annotations

from typing import Any

from statemachine import State, StateMachine

from .models import GameState, RoundRecord


class RoundStateMachine(StateMachine):
    """Own allowed round events while leaving facts and persistence to services.

    Guard arguments are deliberately plain facts supplied by the caller.  They
    let a service reject stale or invalid events without putting clocks, I/O, or
    provider work inside this machine.  Omitting a fact preserves the ergonomic
    event API for callers that have already validated it.
    """

    level_selection = State("Level selection", value=GameState.LEVEL_SELECTION, initial=True)
    challenge_reveal = State("Challenge reveal", value=GameState.CHALLENGE_REVEAL)
    prompt_entry = State("Prompt entry", value=GameState.PROMPT_ENTRY)
    generating = State("Generating", value=GameState.GENERATING)
    generated_reveal = State("Generated reveal", value=GameState.GENERATED_REVEAL)
    result = State("Result", value=GameState.RESULT)
    abandoned = State("Abandoned", value=GameState.ABANDONED, final=True)
    leaderboard = State("Leaderboard", value=GameState.LEADERBOARD, final=True)

    configure = level_selection.to(challenge_reveal, cond="challenge_is_valid")
    continue_challenge = challenge_reveal.to(prompt_entry)
    submit_prompt = prompt_entry.to(generating, cond="prompt_is_nonblank")
    abandon_blank_timeout = prompt_entry.to(abandoned, cond="blank_timeout_is_valid")
    pipeline_succeeded = generating.to(generated_reveal, cond="pipeline_result_is_valid")
    pipeline_failed = generating.to.itself()
    abandon_generation = generating.to(abandoned)
    reveal_elapsed = generated_reveal.to(result, cond="reveal_deadline_is_elapsed")
    show_leaderboard = result.to(leaderboard, cond="completion_is_valid")

    def __init__(
        self,
        model: Any | None = None,
        *,
        state: GameState | str | None = None,
        start_value: GameState | str | None = None,
        **kwargs: Any,
    ) -> None:
        """Construct from a persisted model or reconstruct from a state value."""

        if model is not None and (state is not None or start_value is not None):
            raise TypeError("provide either model or state, not both")
        if state is not None:
            start_value = self._coerce_state(state)
        super().__init__(model=model, state_field="state", start_value=start_value, **kwargs)

    @classmethod
    def from_record(cls, record: RoundRecord) -> RoundStateMachine:
        """Reconstruct the machine view of a validated durable round record."""

        if not isinstance(record, RoundRecord):
            raise TypeError("record must be a RoundRecord")
        return cls(model=record)

    @property
    def state_value(self) -> GameState:
        """Return the current state as the domain enum used by RoundRecord."""

        return GameState(self.current_state_value)

    @property
    def is_terminal(self) -> bool:
        """Whether the machine is in one of its two terminal history states."""

        return self.state_value in {GameState.ABANDONED, GameState.LEADERBOARD}

    @staticmethod
    def _coerce_state(value: GameState | str) -> GameState:
        if isinstance(value, GameState):
            return value
        return GameState(value)

    @staticmethod
    def _fact(kwargs: dict[str, Any], name: str, default: bool = True) -> bool:
        value = kwargs.get(name, default)
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")
        return value

    def challenge_is_valid(self, **kwargs: Any) -> bool:
        """Guard ``configure`` with a service-validated approved challenge fact."""

        return self._fact(kwargs, "challenge_valid")

    def prompt_is_nonblank(self, **kwargs: Any) -> bool:
        """Guard manual or timeout submission against a blank prompt."""

        if "prompt_valid" in kwargs:
            return self._fact(kwargs, "prompt_valid")
        prompt = kwargs.get("prompt")
        return True if prompt is None else bool(isinstance(prompt, str) and prompt.strip())

    def blank_timeout_is_valid(self, **kwargs: Any) -> bool:
        """Guard blank-timeout abandonment with the caller's authoritative fact."""

        if "blank" in kwargs:
            return self._fact(kwargs, "blank")
        prompt = kwargs.get("prompt")
        return True if prompt is None else not bool(isinstance(prompt, str) and prompt.strip())

    def pipeline_result_is_valid(self, **kwargs: Any) -> bool:
        """Guard reveal entry until the pipeline result has been validated."""

        return self._fact(kwargs, "pipeline_valid")

    def reveal_deadline_is_elapsed(self, **kwargs: Any) -> bool:
        """Guard the generated reveal transition with an authoritative deadline fact."""

        return self._fact(kwargs, "deadline_elapsed")

    def completion_is_valid(self, **kwargs: Any) -> bool:
        """Guard leaderboard history with the service's completed disposition fact."""

        return self._fact(kwargs, "completed")


__all__ = ["RoundStateMachine"]
