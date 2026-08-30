"""Not paying twice for the same sentence.

The key is the quantized diagnosis rather than the measurements: the fault, its
band, the club, the prompt version and the model. Keying on raw numbers would
give a cache that never hits, because no two swings share a measurement. Keying
on the fault means the fifth time somebody leaves the face open by roughly this
much with this club, the line is free and instant.

The accepted cost is repetition: the same fault gets the same sentence. That is
what a coach does, it is deterministic, and the alternative — several phrasings
per key — means paying for the extra calls that produced them. If it ever grates,
this is the file to change, and nothing outside it would need to know.

In memory only, and lost on restart. A line cached last week was written under
whatever the prompt said then, about a swing that has since changed.
"""

import hashlib
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from caddiej.domain.coaching import Advice


def fingerprint(parts: Mapping[str, str]) -> str:
    """A stable short digest of what makes two requests the same question."""

    material = "|".join(f"{key}={parts[key]}" for key in sorted(parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class AdviceCache:
    """A bounded, least-recently-used store of accepted lines."""

    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("capacity must hold at least one line")
        self._capacity = capacity
        self._entries: OrderedDict[str, Advice] = OrderedDict()

        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str, shot_id: UUID, created_at: datetime) -> Advice | None:
        """A cached line, restamped for the shot that is asking for it.

        Restamped rather than returned as it was stored: an advice names the shot
        it is about, and handing back one that names a shot from twenty minutes
        ago would make every consumer downstream check for it.
        """

        cached = self._entries.get(key)
        if cached is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return replace(cached, shot_id=shot_id, created_at=created_at)

    def put(self, key: str, advice: Advice) -> None:
        self._entries[key] = advice
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
