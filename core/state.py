"""The assistant's speech state machine: IDLE -> LISTENING -> THINKING -> SPEAKING."""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Callable

log = logging.getLogger("jarvis.state")


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"


# Every state can always fall back to IDLE (interrupt / error / completion).
# LISTENING is reachable from anywhere so the user can barge in while JARVIS talks.
_ALLOWED: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.SPEAKING}),
    State.LISTENING: frozenset({State.IDLE, State.THINKING}),
    State.THINKING: frozenset({State.IDLE, State.SPEAKING, State.LISTENING}),
    State.SPEAKING: frozenset({State.IDLE, State.LISTENING, State.THINKING}),
}

Listener = Callable[[State, State, str], None]


class StateMachine:
    def __init__(self, on_change: Listener | None = None) -> None:
        self._state = State.IDLE
        self._lock = threading.RLock()
        self._listeners: list[Listener] = [on_change] if on_change else []

    @property
    def state(self) -> State:
        return self._state

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can_transition(self, new: State) -> bool:
        return new == self._state or new in _ALLOWED[self._state]

    def transition(self, new: State | str, reason: str = "") -> bool:
        """Move to ``new``. Returns False (and stays put) for an illegal transition."""
        new = State(new)
        with self._lock:
            old = self._state
            if new == old:
                return True
            if new not in _ALLOWED[old]:
                log.warning("Rejected state transition %s -> %s (%s)", old.value, new.value, reason)
                return False
            self._state = new
        log.debug("State %s -> %s (%s)", old.value, new.value, reason)
        for listener in list(self._listeners):
            try:
                listener(old, new, reason)
            except Exception:  # a broken UI listener must never wedge the voice loop
                log.exception("State listener failed")
        return True
