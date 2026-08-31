"""Queue-backed logging must not block the asyncio event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from caddiej.runtime.logging import (
    JsonFormatter,
    LoggingRuntime,
    configure_logging,
    shutdown_logging,
)


def _stop(runtime: LoggingRuntime, timeout: float = 1.0) -> None:
    shutdown_logging(runtime, timeout=timeout)
    logging.basicConfig(level=logging.WARNING, force=True)


class BlockingHandler(logging.Handler):
    """An output sink that will not return from emit until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.emitted: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entered.set()
        self.gate.wait()
        self.emitted.append(record.getMessage())


class CaptureHandler(logging.Handler):
    """Collects formatted lines the listener actually wrote."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.wrote = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        # Do not take `self.lock` here: Handler.handle() already holds it.
        self.lines.append(self.format(record))
        self.wrote.set()


def test_a_blocked_output_handler_does_not_block_the_event_loop() -> None:
    blocked = BlockingHandler()
    runtime = configure_logging("INFO", queue_size=8, output=blocked)
    try:

        async def log_from_the_loop() -> float:
            logging.getLogger("caddiej.test").info("first")
            assert blocked.entered.wait(timeout=1.0)
            started = time.monotonic()
            logging.getLogger("caddiej.test").info("must_not_wait")
            return time.monotonic() - started

        elapsed = asyncio.run(asyncio.wait_for(log_from_the_loop(), timeout=1.0))
        assert elapsed < 0.05
    finally:
        blocked.gate.set()
        _stop(runtime)


def test_a_full_queue_drops_records_instead_of_blocking() -> None:
    blocked = BlockingHandler()
    runtime = configure_logging("INFO", queue_size=2, output=blocked)
    try:
        logging.getLogger("caddiej.test").info("occupy_listener")
        assert blocked.entered.wait(timeout=1.0)
        # The listener has one record in emit(); two more fill the queue.
        for index in range(8):
            logging.getLogger("caddiej.test").info("burst_%s", index)
        assert runtime.dropped >= 1
    finally:
        blocked.gate.set()
        _stop(runtime)


def test_logging_shutdown_is_bounded_when_output_is_blocked() -> None:
    blocked = BlockingHandler()
    runtime = configure_logging("INFO", queue_size=4, output=blocked)
    logging.getLogger("caddiej.test").info("stuck")
    assert blocked.entered.wait(timeout=1.0)

    started = time.monotonic()
    shutdown_logging(runtime, timeout=0.2)
    elapsed = time.monotonic() - started
    logging.basicConfig(level=logging.WARNING, force=True)

    assert elapsed < 1.0
    blocked.gate.set()


def test_json_format_and_allowlist_are_preserved() -> None:
    capture = CaptureHandler()
    runtime = configure_logging("INFO", output=capture)
    try:
        logging.getLogger("caddiej.test").info(
            "gspro_club_selected",
            extra={"gspro_club": "driver", "password": "secret"},
        )
        runtime.stop(timeout=2.0)
        assert capture.lines, "listener never wrote the record"
        payload = json.loads(capture.lines[0])
        assert payload["event"] == "gspro_club_selected"
        assert payload["gspro_club"] == "driver"
        assert "password" not in payload
        assert payload["level"] == "INFO"
    finally:
        logging.basicConfig(level=logging.WARNING, force=True)


def test_log_level_is_honoured() -> None:
    capture = CaptureHandler()
    runtime = configure_logging("WARNING", output=capture)
    try:
        logging.getLogger("caddiej.test").info("too_quiet")
        logging.getLogger("caddiej.test").warning("loud_enough")
        runtime.stop(timeout=2.0)
        events = [json.loads(line)["event"] for line in capture.lines]
        assert "loud_enough" in events
        assert "too_quiet" not in events
    finally:
        logging.basicConfig(level=logging.WARNING, force=True)


def test_formatter_is_the_same_json_shape() -> None:
    record = logging.LogRecord(
        name="caddiej.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hud_started",
        args=(),
        exc_info=None,
    )
    setattr(record, "hud_port", 8730)
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["event"] == "hud_started"
    assert payload["hud_port"] == 8730


def test_logger_exception_reaches_json_with_traceback() -> None:
    capture = CaptureHandler()
    runtime = configure_logging("INFO", output=capture)
    try:
        try:
            raise RuntimeError("probe-detail")
        except RuntimeError:
            logging.getLogger("caddiej.test").exception("probe")
        runtime.stop(timeout=2.0)
        assert capture.lines, "listener never wrote the record"
        payload = json.loads(capture.lines[0])
        assert payload["event"] == "probe"
        assert payload["level"] == "ERROR"
        assert "exception" in payload
        assert "RuntimeError: probe-detail" in payload["exception"]
        assert "Traceback" in payload["exception"]
    finally:
        logging.basicConfig(level=logging.WARNING, force=True)


def test_ordinary_logs_omit_the_exception_field() -> None:
    capture = CaptureHandler()
    runtime = configure_logging("INFO", output=capture)
    try:
        logging.getLogger("caddiej.test").info("quiet_line")
        runtime.stop(timeout=2.0)
        assert capture.lines, "listener never wrote the record"
        payload = json.loads(capture.lines[0])
        assert payload["event"] == "quiet_line"
        assert "exception" not in payload
    finally:
        logging.basicConfig(level=logging.WARNING, force=True)
