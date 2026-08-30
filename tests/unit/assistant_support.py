"""Wiring a real assistant boundary over real services, for the tests above it.

Not a fake façade. The point of these tests is that the projections are right
and that absence is classified correctly, and both of those questions are only
worth asking against a real SQLite history and a real `AnalysisService` — a fake
history would agree with whatever the façade believed about it.

What is faked is only what would be slow or external: coaching has no provider,
so nothing is asked of a model, and the clock is a counter.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from caddiej.assistant.commands import CommandFacade
from caddiej.assistant.contract import SubsystemPayload
from caddiej.assistant.reads import ReadFacade, SubsystemProbe
from caddiej.coaching.service import AnalysisService
from caddiej.core.events import Event
from caddiej.domain.coaching import CoachingFocus
from caddiej.domain.events import ShotCompleted
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import Shot
from caddiej.persistence.service import HistoryService, build_history_service


class NullFloor:
    """A floor that records nothing, because no test here is about drawing."""

    def show_address(self, club: object | None) -> None: ...
    def hide_address(self) -> None: ...
    def show_shot(self, shot: object) -> None: ...
    def clear_shot(self) -> None: ...
    def show_advice(self, text: str, severity: float) -> None: ...
    def clear_advice(self) -> None: ...


async def silent(_event: Event) -> None:
    """A publish that goes nowhere. No test here reads the bus."""


@dataclass
class Boundary:
    """Everything a test needs to ask a question and check the answer."""

    reads: ReadFacade
    commands: CommandFacade
    history: HistoryService
    coaching: AnalysisService
    play: PlayFlow
    ticks: list[float]

    async def record(self, shot: Shot) -> None:
        """Put a shot in the database and wait for it to actually land.

        Polled rather than counted in event-loop turns, because the write really
        does happen on another thread.
        """

        wanted = self.history.shots_recorded + 1
        await self.history.on_shot_completed(ShotCompleted(shot=shot))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while self.history.shots_recorded < wanted:
            if loop.time() >= deadline:
                raise AssertionError("the writer never caught up")
            await asyncio.sleep(0.005)


def probe(enabled: bool, available: bool) -> SubsystemProbe:
    return lambda: SubsystemPayload(enabled=enabled, available=available)


async def boundary(
    tmp_path: Path,
    history_enabled: bool = True,
    include_coaching_prose: bool = False,
    mutation_enabled: bool = True,
    player_name: str | None = "Johan",
    preferred_focus: CoachingFocus | None = None,
    version: str = "0.1.0",
    subsystems: dict[str, SubsystemProbe] | None = None,
) -> Boundary:
    history = build_history_service(
        enabled=history_enabled,
        path=tmp_path / "history.sqlite3",
        player_name=player_name,
    )
    await history.open()

    coaching = AnalysisService(
        publish=silent,
        provider=None,
        record=history.record_advice if history.is_available else None,
        preferred_focus=preferred_focus,
    )
    # Opened, because an unopened service is genuinely not coaching anything and
    # these tests are about a CaddieJ that is running. With no provider it starts
    # its worker, asks nothing of a model, and coaches from templates.
    await coaching.open()
    play = PlayFlow(floor=NullFloor(), publish=silent)

    # A clock that advances one second per reading, so uptime is a number a test
    # can predict rather than something that depends on how fast the machine is.
    ticks: list[float] = []

    def monotonic() -> float:
        ticks.append(float(len(ticks)))
        return ticks[-1]

    reads = ReadFacade(
        history=history,
        coaching=coaching,
        play=play,
        version=version,
        subsystems=subsystems
        if subsystems is not None
        else {"history": probe(history_enabled, history.is_available)},
        player_name=player_name,
        include_coaching_prose=include_coaching_prose,
        mutation_enabled=mutation_enabled,
        monotonic=monotonic,
    )
    return Boundary(
        reads=reads,
        commands=CommandFacade(coaching),
        history=history,
        coaching=coaching,
        play=play,
        ticks=ticks,
    )


async def closing(subject: Boundary) -> AsyncIterator[Boundary]:
    """Yield a boundary and take its services down afterwards."""

    try:
        yield subject
    finally:
        await subject.coaching.aclose()
        await subject.play.aclose()
        await subject.history.aclose()
