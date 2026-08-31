"""The GSPro adapter: observed bytes in, typed domain events out.

The adapter translates and reports. It holds no golf state: which club a shot
was hit with, whether it counts, and what the floor should show are all
decisions for the domain. What it does keep is transport bookkeeping — the last
device flags so a change can be told from a repeat, recently seen shot numbers
so a retransmission can be told from a shot, and the link state.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from caddiej.adapters.gspro.assembly import (
    Assembly,
    ChangeDebouncer,
    ShotAssembler,
    ShotFragment,
)
from caddiej.adapters.gspro.errors import GSProBindError, GSProProtocolError
from caddiej.adapters.gspro.interposer import (
    Direction,
    FrameObserver,
    Interposer,
    LinkObserver,
    MonitorListener,
    StreamPair,
)
from caddiej.adapters.gspro.translate import (
    player_from_response,
    to_ball_launch,
    to_club_delivery,
    to_monitor_status,
)
from caddiej.adapters.gspro.wire import (
    WireLaunchMonitorMessage,
    WireSimulatorResponse,
    parse_launch_monitor_message,
    parse_simulator_response,
)
from caddiej.core.events import Event
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    DuplicateShotIgnored,
    HandednessChanged,
    IncompleteShotDiscarded,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RejectionReason,
    ShotIngested,
    ShotMessageRejected,
    SimulatorAcknowledgedShot,
    SimulatorReportedError,
)
from caddiej.domain.shot import Club, Detection, Handedness, Shot, ShotSource

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
InterposerFactory = Callable[[FrameObserver, LinkObserver], Interposer]

_QUEUE_LIMIT = 256
_ACK_CODE = 200
_PLAYER_INFO_CODE = 201
# The documented responses are 200, 201, and 501/5xx. Anything at or above this
# is treated as the simulator reporting a failure.
_ERROR_CODE_FLOOR = 400


@dataclass(frozen=True, slots=True)
class GSProHealth:
    link_state: LinkState
    shots_ingested: int
    messages_rejected: int
    duplicates_ignored: int
    reused_shot_numbers: int
    late_data_discarded: int
    frames_dropped: int


@dataclass(frozen=True, slots=True)
class _QueuedFrame:
    """A framed message observed on the byte path."""

    direction: Direction
    payload: bytes
    session_id: UUID


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    """An event raised from a synchronous callback on the byte path.

    Link changes travel on the same queue as frames so that one wakeup path
    serves both. Publishing them from a separate list would leave them waiting
    for the next frame, which for "bridged" could be a long time.
    """

    event: Event


@dataclass(frozen=True, slots=True)
class _QueuedSessionEnd:
    """A marker saying a connection finished, placed in the queue behind its data.

    Per-connection state cannot be reset the moment a device disconnects: that
    device's last messages may still be queued, and discarding them would throw
    away a shot that was genuinely hit. Ending the session in the queue means
    every frame is interpreted with the state belonging to it, in order.
    """

    session_id: UUID


_Queued = _QueuedFrame | _QueuedEvent | _QueuedSessionEnd


class GSProConnectAdapter:
    """Observes the Open Connect conversation and publishes what it sees."""

    def __init__(
        self,
        listener: MonitorListener,
        interposer_factory: InterposerFactory,
        publish: Publish,
        assembly_window_seconds: float = 0.25,
        ball_present_debounce_seconds: float = 0.0,
        ball_absent_debounce_seconds: float = 1.0,
        raw_payload_max_bytes: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._listener = listener
        self._publish = publish
        self._assembly_window = assembly_window_seconds
        self._ball_present_debounce = ball_present_debounce_seconds
        self._ball_absent_debounce = ball_absent_debounce_seconds
        # Zero switches capture off entirely, rather than capturing nothing:
        # with it off, no message's bytes are retained past interpretation.
        self._raw_payload_max_bytes = raw_payload_max_bytes
        self._monotonic = monotonic
        self._now = now

        self._interposer = interposer_factory(self._observe_frame, self._observe_link)
        self._queue: asyncio.Queue[_Queued] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._waiting: asyncio.Task[_Queued] | None = None
        self._session_id = uuid4()

        self._link_state = LinkState.DISABLED
        self._assembler = ShotAssembler(assembly_window_seconds, monotonic)
        self._ready: ChangeDebouncer[bool] = ChangeDebouncer(False, 0.0, monotonic)
        self._ball: ChangeDebouncer[Detection] = ChangeDebouncer(
            Detection.UNKNOWN, self._ball_delay_for, monotonic
        )
        self._club: Club | None = None
        self._handedness = Handedness.UNKNOWN
        self._device_id: str | None = None

        self.shots_ingested = 0
        self.messages_rejected = 0
        self.duplicates_ignored = 0
        self.reused_shot_numbers = 0
        self.late_data_discarded = 0
        self.frames_dropped = 0

    def _ball_delay_for(self, detection: Detection) -> float:
        """How long a change in ball detection must hold to be believed.

        A ball appearing is acted on at once, because the floor has to stop
        projecting onto it. A ball disappearing is given time, because detection
        blinking off for one heartbeat would otherwise flash the marker back on
        under the player's club.
        """

        if detection is Detection.PRESENT:
            return self._ball_present_debounce
        return self._ball_absent_debounce

    @property
    def link_state(self) -> LinkState:
        return self._link_state

    @property
    def captures_raw_payloads(self) -> bool:
        """Whether shots are carrying the bytes they were read from."""

        return self._raw_payload_max_bytes > 0

    def health(self) -> GSProHealth:
        return GSProHealth(
            link_state=self._link_state,
            shots_ingested=self.shots_ingested,
            messages_rejected=self.messages_rejected,
            duplicates_ignored=self.duplicates_ignored,
            reused_shot_numbers=self.reused_shot_numbers,
            late_data_discarded=self.late_data_discarded,
            frames_dropped=self.frames_dropped,
        )

    # --- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        """Run until cancelled: accept the monitor and interpret what flows.

        A failing launch monitor session must not stop CaddieJ listening, and
        being unable to listen at all must not stop CaddieJ running. This is an
        optional source of facts, not a precondition for the process.
        """

        interpreter = asyncio.create_task(self._interpret(), name="gspro-interpret")
        try:
            # Enqueued rather than awaited: publishing must never be able to
            # delay the moment CaddieJ starts accepting the launch monitor.
            self._note_link(LinkState.LISTENING, None)
            await self._listener.serve(self._handle_monitor)
        except GSProBindError as error:
            # Almost always a port already in use. Loud, and not fatal.
            logger.error("gspro_bind_failed", exc_info=error)
            self._note_link(LinkState.BIND_FAILED, str(error))
            await self._drain()
        finally:
            interpreter.cancel()
            with suppress(asyncio.CancelledError):
                await interpreter

    async def _handle_monitor(self, downstream: StreamPair) -> None:
        """One launch monitor connection, isolated from the listener."""

        # A device connection starts a new identity space: shot numbers reset
        # when a device restarts, so nothing may carry over. A connection that
        # arrives while one is already bridged owns no session and must not
        # disturb the one in progress.
        owns_session = not self._interposer.is_bridging
        if owns_session:
            self._session_id = uuid4()
        session_id = self._session_id
        try:
            await self._interposer.bridge(downstream)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "gspro_session_failed", extra={"gspro_peer": downstream.peer}
            )
        finally:
            if owns_session:
                self._enqueue(_QueuedSessionEnd(session_id), "session_end")

    async def _end_session(self, session_id: UUID) -> None:
        """Wind up a finished connection, in order behind its own messages."""

        for fragment in self._assembler.flush():
            await self._emit_fragment(fragment)
        self._assembler = ShotAssembler(self._assembly_window, self._monotonic)
        self._ready = ChangeDebouncer(False, 0.0, self._monotonic)
        self._ball = ChangeDebouncer(
            Detection.UNKNOWN, self._ball_delay_for, self._monotonic
        )
        self._device_id = None
        logger.debug("gspro_session_ended", extra={"gspro_session": str(session_id)})

    # --- observation, called synchronously from the byte path -------------

    def _observe_frame(self, direction: Direction, payload: bytes) -> None:
        """Take a framed message off the byte path. Must not block or await.

        These bytes have already been forwarded. Interpretation happens on
        another task so nothing CaddieJ does can push back on the wire.
        """

        self._enqueue(
            _QueuedFrame(
                direction=direction, payload=payload, session_id=self._session_id
            ),
            description=direction.value,
        )

    def _observe_link(self, state: LinkState, detail: str | None) -> None:
        self._note_link(state, detail)

    def _note_link(self, state: LinkState, detail: str | None) -> None:
        self._link_state = state
        logger.info(
            "gspro_link_changed",
            extra={"gspro_link_state": state.value, "gspro_detail": detail},
        )
        self._enqueue(
            _QueuedEvent(LaunchMonitorLinkChanged(state=state, detail=detail)),
            description="link_change",
        )

    def _enqueue(self, item: _Queued, description: str) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Something has to go, and it must not be what just arrived: the
            # newest item is the most time-critical and the most likely to be a
            # shot, while the oldest is almost certainly one of the heartbeats
            # that arrive every second. Reaching this at all means the
            # interpreter is wedged, so it is reported as an error.
            self.frames_dropped += 1
            discarded = self._discard_oldest()
            logger.error(
                "gspro_frame_dropped",
                extra={
                    "gspro_dropped": self.frames_dropped,
                    "gspro_direction": discarded,
                },
            )
            with suppress(asyncio.QueueFull):
                self._queue.put_nowait(item)

    def _discard_oldest(self) -> str:
        """Make room by removing the oldest queued item, and say what it was."""

        try:
            stale = self._queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - full queue, cannot be empty
            return "nothing"
        if isinstance(stale, _QueuedFrame):
            return stale.direction.value
        return type(stale).__name__

    # --- interpretation ---------------------------------------------------

    async def _interpret(self) -> None:
        try:
            while True:
                item = await self._next_item()
                try:
                    await self._handle_item(item)
                    await self._handle_deadlines()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One unusable message must not stop every later message
                    # being interpreted, exactly as it must not cost the
                    # connection. This is a bug, so it is logged in full.
                    logger.exception("gspro_interpretation_failed")
        finally:
            if self._waiting is not None:
                self._waiting.cancel()
                self._waiting = None

    async def _handle_item(self, item: _Queued | None) -> None:
        match item:
            case None:
                return
            case _QueuedEvent():
                await self._publish(item.event)
            case _QueuedSessionEnd():
                await self._end_session(item.session_id)
            case _QueuedFrame():
                await self._handle_frame(item)

    async def _drain(self) -> None:
        """Publish whatever is queued. Used when the listener has given up."""

        while not self._queue.empty():
            await self._handle_item(self._queue.get_nowait())

    async def _next_item(self) -> _Queued | None:
        """Wait for work, waking in time for the next pending deadline.

        The pending read is kept alive across timeouts rather than cancelled and
        retried. Cancelling a queue read that has already been handed an item
        risks dropping it, and here that item could be a shot.
        """

        if self._waiting is None:
            self._waiting = asyncio.create_task(
                self._queue.get(), name="gspro-queue-read"
            )
        done, _ = await asyncio.wait(
            {self._waiting}, timeout=self._seconds_until_next_deadline()
        )
        if self._waiting not in done:
            return None
        item = self._waiting.result()
        self._waiting = None
        return item

    def _seconds_until_next_deadline(self) -> float | None:
        deadlines = [
            deadline
            for deadline in (
                self._assembler.next_deadline(),
                self._ball.next_deadline(),
            )
            if deadline is not None
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - self._monotonic())

    async def _handle_deadlines(self) -> None:
        for fragment in self._assembler.expire():
            await self._emit_fragment(fragment)
        transition = self._ball.due()
        if transition is not None:
            await self._publish_ball_transition(transition.previous, transition.current)

    async def _handle_frame(self, frame: _QueuedFrame) -> None:
        if frame.direction is Direction.TO_SIMULATOR:
            await self._handle_monitor_message(frame)
        else:
            await self._handle_simulator_message(frame.payload)

    # --- launch monitor messages ------------------------------------------

    async def _handle_monitor_message(self, frame: _QueuedFrame) -> None:
        try:
            message = parse_launch_monitor_message(frame.payload)
        except GSProProtocolError as error:
            await self._reject(error.reason, error.detail)
            return

        if message.device_id is not None:
            self._device_id = message.device_id

        await self._apply_status(message)

        if not (message.declares_ball_data or message.declares_club_data):
            # A heartbeat or status-only message. Not a shot, not an error.
            return

        try:
            fragment = self._to_fragment(message, frame)
        except ValueError as error:
            # A number the domain refuses to represent, such as a negative ball
            # speed. A rejected message, not an internal failure.
            await self._reject(RejectionReason.IMPLAUSIBLE_VALUE, str(error))
            return

        if fragment.is_empty:
            await self._reject(
                RejectionReason.NO_SHOT_DATA,
                "message declared shot data but carried none",
            )
            return

        await self._offer_fragment(fragment)

    def _to_fragment(
        self, message: WireLaunchMonitorMessage, frame: _QueuedFrame
    ) -> ShotFragment:
        ball = (
            to_ball_launch(message.ball_data, message.units)
            if message.declares_ball_data and message.ball_data is not None
            else None
        )
        club = (
            to_club_delivery(message.club_data)
            if message.declares_club_data and message.club_data is not None
            else None
        )
        # A club block of nothing but nulls is not club data.
        if club is not None and club.is_empty:
            club = None
        return ShotFragment(
            sequence=message.shot_number,
            # The remembered identifier, not this message's: a device that omits
            # it from one half of a split shot would otherwise have the two
            # halves treated as unrelated shots.
            device_id=message.device_id or self._device_id,
            units=message.units,
            api_version=message.api_version,
            ball=ball,
            club_delivery=club,
            session_id=frame.session_id,
            raw=(frame.payload,) if self._raw_payload_max_bytes > 0 else (),
        )

    async def _offer_fragment(self, fragment: ShotFragment) -> None:
        result = self._assembler.offer(fragment)

        for shot in result.emit:
            await self._emit_fragment(shot)

        match result.outcome:
            case Assembly.COMPLETE:
                pass
            case Assembly.HELD:
                logger.debug(
                    "gspro_shot_held_for_completion",
                    extra={"gspro_sequence": fragment.sequence},
                )
            case Assembly.REUSED_NUMBER:
                # The shots were published above. This only records that the
                # device is not incrementing, which makes its shot numbers
                # useless for spotting retransmissions.
                self.reused_shot_numbers += 1
                logger.warning(
                    "gspro_shot_number_reused",
                    extra={
                        "gspro_sequence": fragment.sequence,
                        "gspro_device_id": fragment.device_id,
                    },
                )
            case Assembly.DUPLICATE:
                self.duplicates_ignored += 1
                logger.info(
                    "gspro_duplicate_shot_ignored",
                    extra={
                        "gspro_sequence": fragment.sequence,
                        "gspro_device_id": fragment.device_id,
                    },
                )
                await self._publish(
                    DuplicateShotIgnored(
                        device_sequence=fragment.sequence,
                        device_id=fragment.device_id,
                    )
                )
            case Assembly.LATE:
                # The shot was already published and a published shot is
                # immutable, so this data cannot be attached to anything.
                self.late_data_discarded += 1
                logger.warning(
                    "gspro_shot_data_arrived_too_late",
                    extra={"gspro_sequence": fragment.sequence},
                )

    async def _emit_fragment(self, fragment: ShotFragment) -> None:
        if fragment.ball is None:
            # Club data alone is not a shot: there is no ball flight to show,
            # score, or record.
            logger.info(
                "gspro_incomplete_shot_discarded",
                extra={"gspro_sequence": fragment.sequence},
            )
            await self._publish(
                IncompleteShotDiscarded(
                    device_sequence=fragment.sequence,
                    had_ball_data=False,
                    had_club_data=fragment.club_delivery is not None,
                )
            )
            return

        shot = Shot(
            shot_id=uuid4(),
            source=ShotSource(
                kind="gspro_connect",
                device_session_id=fragment.session_id or self._session_id,
                observed_at=self._now(),
                observed_monotonic=self._monotonic(),
                device_id=fragment.device_id,
                device_sequence=fragment.sequence,
                reported_units=fragment.units,
                api_version=fragment.api_version,
                raw_payload=self._raw_payload(fragment),
            ),
            ball=fragment.ball,
            club_delivery=fragment.club_delivery,
            handedness=self._handedness,
        )
        self.shots_ingested += 1
        logger.info(
            "gspro_shot_ingested",
            extra={
                "gspro_sequence": fragment.sequence,
                "gspro_device_id": fragment.device_id,
                "shot_id": str(shot.shot_id),
                "gspro_ball_speed_mph": round(fragment.ball.speed_mph, 2),
            },
        )
        await self._publish(ShotIngested(shot=shot))

    def _raw_payload(self, fragment: ShotFragment) -> str | None:
        """The messages a shot was read from, as one newline-delimited string.

        Newline-delimited rather than a JSON array, because the point of keeping
        it is to see what the device actually sent: re-encoding it into another
        structure would lose the formatting that a parsing bug turns on.

        Undecodable bytes are replaced rather than dropped. A payload only ever
        gets read by a person looking for a fault, and where the fault is is
        exactly what the replacement characters would show.
        """

        limit = self._raw_payload_max_bytes
        if limit <= 0 or not fragment.raw:
            return None
        joined = b"\n".join(fragment.raw)
        truncated = joined[:limit]
        text = truncated.decode("utf-8", errors="replace")
        if len(joined) > limit:
            logger.debug(
                "gspro_raw_payload_truncated",
                extra={"gspro_frame_bytes": len(joined)},
            )
            return f"{text}\n[truncated: {len(joined)} bytes captured, {limit} kept]"
        return text

    # --- device status ----------------------------------------------------

    async def _apply_status(self, message: WireLaunchMonitorMessage) -> None:
        status = to_monitor_status(message)

        if status.ready is not None:
            readiness = self._ready.offer(status.ready)
            if readiness is not None:
                logger.info(
                    "gspro_readiness_changed",
                    extra={"gspro_ready": readiness.current},
                )
                await self._publish(
                    LaunchMonitorReadinessChanged(
                        ready=readiness.current, device_id=self._device_id
                    )
                )

        if status.ball is not Detection.UNKNOWN:
            detection = self._ball.offer(status.ball)
            if detection is not None:
                await self._publish_ball_transition(
                    detection.previous, detection.current
                )

    async def _publish_ball_transition(
        self, previous: Detection, current: Detection
    ) -> None:
        if current is Detection.PRESENT:
            logger.info("gspro_ball_detected")
            await self._publish(BallDetected(device_id=self._device_id))
        elif current is Detection.ABSENT and previous is Detection.PRESENT:
            # Only a ball that was there can be removed. Reporting absent from
            # unknown is a device describing itself for the first time.
            logger.info("gspro_ball_cleared")
            await self._publish(BallCleared(device_id=self._device_id))

    # --- simulator messages -----------------------------------------------

    async def _handle_simulator_message(self, payload: bytes) -> None:
        try:
            response = parse_simulator_response(payload)
        except GSProProtocolError as error:
            await self._reject(error.reason, error.detail)
            return

        # Player information is applied whenever GSPro sent a Player block,
        # including on a 200 acknowledgement. The code decides what else the
        # response means; it does not gate whether a present Player is used.
        if response.player is not None:
            await self._apply_player_info(response)

        if response.code == _ACK_CODE:
            await self._publish(SimulatorAcknowledgedShot())
        elif response.code == _PLAYER_INFO_CODE:
            pass
        elif response.code >= _ERROR_CODE_FLOOR:
            logger.warning(
                "gspro_simulator_reported_error",
                extra={
                    "gspro_response_code": response.code,
                    "gspro_detail": response.message,
                },
            )
            await self._publish(
                SimulatorReportedError(
                    code=response.code, message=response.message or ""
                )
            )
        else:
            # A code the protocol does not document. Nothing can be done with
            # it, but it must not vanish: it is the first clue that GSPro has
            # started saying something new.
            logger.info(
                "gspro_simulator_response_unrecognised",
                extra={
                    "gspro_response_code": response.code,
                    "gspro_detail": response.message,
                },
            )

    async def _apply_player_info(self, response: WireSimulatorResponse) -> None:
        handedness, club = player_from_response(response)

        if handedness is not None and handedness != self._handedness:
            self._handedness = handedness
            logger.info(
                "gspro_handedness_changed",
                extra={"gspro_handedness": handedness.value},
            )
            await self._publish(HandednessChanged(handedness=handedness))

        if club is not None and club != self._club:
            self._club = club
            logger.info(
                "gspro_club_selected",
                extra={
                    "gspro_club_code": club.vendor_code,
                    "gspro_club": club.kind.value,
                },
            )
            await self._publish(ClubSelected(club=club))

    # --- publishing -------------------------------------------------------

    async def _reject(self, reason: RejectionReason, detail: str) -> None:
        self.messages_rejected += 1
        logger.warning(
            "gspro_message_rejected",
            extra={"gspro_reject_reason": reason.value, "gspro_detail": detail},
        )
        await self._publish(ShotMessageRejected(reason=reason, detail=detail))
