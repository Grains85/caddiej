"""Splitting a raw TCP byte stream into JSON messages.

GSPro Open Connect has no length prefix and no guaranteed delimiter. One read
may contain half a message, three messages, or a message followed by padding,
so `readline` and "one read is one message" are both wrong.

This framer tracks brace depth while respecting string literals, so a brace
inside a string value cannot desynchronize it. Anything it cannot make sense of
is skipped up to the next plausible start, because losing one message must never
cost the connection: the player is mid-round.
"""

import logging

logger = logging.getLogger(__name__)

_OPEN = ord("{")
_CLOSE = ord("}")
_QUOTE = ord('"')
_BACKSLASH = ord("\\")


class JsonFramer:
    """Incremental framer yielding complete top-level JSON objects."""

    def __init__(self, max_message_bytes: int = 65536) -> None:
        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self.skipped_bytes = 0
        self.oversized_messages = 0

    @property
    def pending_bytes(self) -> int:
        """Bytes held for an object that has not finished arriving."""

        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes and return whatever messages are now complete."""

        messages: list[bytes] = []
        for byte in chunk:
            if self._depth == 0:
                if byte == _OPEN:
                    self._buffer.append(byte)
                    self._depth = 1
                else:
                    # Whitespace, newlines, and the NUL padding some devices
                    # append all land here, as does anything left over after a
                    # resynchronisation.
                    self.skipped_bytes += 1
                continue

            self._buffer.append(byte)

            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif byte == _BACKSLASH:
                    self._escaped = True
                elif byte == _QUOTE:
                    self._in_string = False
            elif byte == _QUOTE:
                self._in_string = True
            elif byte == _OPEN:
                self._depth += 1
            elif byte == _CLOSE:
                self._depth -= 1
                if self._depth == 0:
                    messages.append(bytes(self._buffer))
                    self._reset()
                    continue

            if len(self._buffer) > self._max_message_bytes:
                self.oversized_messages += 1
                logger.warning(
                    "gspro_frame_oversized",
                    extra={"gspro_frame_bytes": len(self._buffer)},
                )
                self._reset()

        return messages

    def _reset(self) -> None:
        self._buffer.clear()
        self._depth = 0
        self._in_string = False
        self._escaped = False
