"""Things an operator wants to hear about: an account logged out, flagged, used up.

The pool calls :func:`emit`; listeners (the Telegram bot) get the events.  The same
event for the same account is repeated at most once per ``THROTTLE`` seconds, so a
busy proxy hitting one exhausted account does not send a message per request.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# kind -> human title (shown in the UI and the bot)
KINDS = {
    "logged_out": "Account logged out",
    "abuse": "Google abuse flag — account disabled",
    "quota": "Quota exhausted",
    "blocked": "Google answered 429 (unusual traffic)",
    "all_down": "No account can serve the request",
    "recovered": "Account working again",
    "node_down": "Linked node unreachable",
    "node_up": "Linked node back online",
}
THROTTLE = 30 * 60.0


@dataclass
class Event:
    kind: str
    account: str | None
    text: str
    ts: float = field(default_factory=time.time)

    @property
    def title(self) -> str:
        return KINDS.get(self.kind, self.kind)


recent: deque[Event] = deque(maxlen=100)
_listeners: list[Callable[[Event], None]] = []
_last: dict[tuple[str, str | None], float] = {}


def subscribe(listener: Callable[[Event], None]) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


def unsubscribe(listener: Callable[[Event], None]) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def emit(kind: str, account: str | None = None, text: str = "") -> Event | None:
    now = time.time()
    key = (kind, account)
    if now - _last.get(key, 0.0) < THROTTLE:
        return None
    _last[key] = now
    if kind == "recovered":  # a later failure of this account is news again
        for other in KINDS:
            _last.pop((other, account), None)
        _last[key] = now
    event = Event(kind, account, text[:500])
    recent.append(event)
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception as exc:  # a listener must not break the request path
            log.warning("event listener: %s", exc)
    return event


def reset() -> None:
    """Forget throttling state (tests)."""
    _last.clear()
    recent.clear()
