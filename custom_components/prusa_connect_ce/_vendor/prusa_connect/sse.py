"""A server-sent events parser, per the WHATWG HTML specification."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSEFrame:
    """One dispatched server-sent event."""

    event: str
    data: str
    id: str | None


class SSEParser:
    """Turns lines of an ``text/event-stream`` body into frames.

    Feed it one line at a time, without the line terminator. It returns a
    frame when a blank line dispatches one. ``last_event_id`` and ``retry``
    keep the values the stream last set, as an EventSource would.
    """

    def __init__(self, last_event_id: str | None = None) -> None:
        self.last_event_id = last_event_id
        self.retry: int | None = None
        self._event = ""
        self._data: list[str] = []
        self._id_seen = False

    def feed(self, line: str) -> SSEFrame | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        elif field == "id":
            if "\0" not in value:
                self.last_event_id = value
                self._id_seen = True
        elif field == "retry" and value.isdigit():
            self.retry = int(value)
        return None

    def _dispatch(self) -> SSEFrame | None:
        event, data, id_seen = self._event, self._data, self._id_seen
        self._event, self._data, self._id_seen = "", [], False
        if not data:
            return None
        return SSEFrame(
            event=event or "message",
            data="\n".join(data),
            id=self.last_event_id if id_seen else None,
        )
