"""Ordered parallel map for independent image work.

Resize and optical flow release the interpreter lock inside OpenCV. Pairs
from one camera do not depend on each other, so several of them can run at
once. Results stay in input order. A short series stays on the calling thread.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

_PARALLEL_MINIMUM = 8


def worker_count() -> int:
    """A small pool. More threads than this fight OpenCV's own pool."""

    count = os.cpu_count() or 1
    return max(1, min(4, count))


def map_ordered[Item, Result](
    function: Callable[[Item], Result], items: Sequence[Item]
) -> list[Result]:
    """Apply `function` to each item. Order matches `items`."""

    values = list(items)
    workers = worker_count()
    if len(values) < _PARALLEL_MINIMUM or workers == 1:
        return [function(item) for item in values]
    with ThreadPoolExecutor(max_workers=min(workers, len(values))) as pool:
        return list(pool.map(function, values))
