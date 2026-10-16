"""The coach benchmark: one stored shot, replayed, and nothing else touched.

Its whole value is that a comparison between two configurations is a
comparison between configurations, so the promises checked here are the ones
that would quietly make it something else: the same stored bytes in every
request, exactly one request per run, no side effect anywhere, and one bad
configuration costing only its own rows. The offline breakdown and the image
token estimator are checked against numbers worked out by hand.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from caddiej.coaching.benchmark import (
    BenchmarkConfig,
    MeteredOpenAIProvider,
    Usage,
    benchmark_coach,
    build_benchmark_request,
    capture_timing,
    coach_request_report,
    configurations,
    estimate_image_tokens,
    load_evidence,
    patch_image_tokens,
    request_breakdown,
    run_benchmark,
    tile_image_tokens,
    timing_report,
)
from caddiej.coaching.provider import (
    AdviceRequest,
    ProviderRefusedError,
    ProviderReply,
)
from caddiej.domain.vx import VX_METRICS
from caddiej.swing_capture.store import load_capture

# --- a stored shot, as swing capture leaves it on disk ----------------------


def a_jpeg(width: int, height: int, filler: bytes = b"") -> bytes:
    """The smallest file `jpeg_size` reads: a start marker and one SOF0."""

    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + bytes(9)
        + filler
        + b"\xff\xd9"
    )


SUPPORTS = [
    "body_in_backswing",
    "body_through_impact",
    "finish_position",
    "sequence",
    "whole_body",
]


def a_stored_shot(root: Path) -> Path:
    """Three panels and the All Values still, with a metadata.json that says
    the replay replaced a loading screen 2.25 s after the strike."""

    directory = root / "media" / "swing_shots" / "20261002T184705-1-0ea68447"
    panels = (
        ("swing_behind", "dynamic", "behind", (1792, 1480), (0.5, 3.125, 4.437)),
        ("swing_side", "dynamic", "side", (1532, 1490), (0.5, 3.125, 4.437)),
        ("impact", "static", "none", (1352, 598), (4.516,)),
    )
    cameras = []
    for camera_id, kind, view, (width, height), offsets in panels:
        frames = []
        for index, offset in enumerate(offsets, start=1):
            path = directory / camera_id / f"frame_{index:03d}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(a_jpeg(width, height, f"{camera_id}{index}".encode()))
            frames.append(
                {
                    "index": index,
                    "file": f"{camera_id}/frame_{index:03d}.jpg",
                    "offset_seconds": offset,
                    "width": width,
                    "height": height,
                }
            )
        timeline = (
            [
                {"index": 1, "offset_seconds": 0.5, "bytes": 60000},
                {"index": 2, "offset_seconds": 2.141, "bytes": 61000},
                {"index": 3, "offset_seconds": 2.25, "bytes": 155000},
            ]
            if kind == "dynamic"
            else []
        )
        cameras.append(
            {
                "camera_id": camera_id,
                "panel_id": camera_id,
                "source": camera_id,
                "kind": kind,
                "view": view,
                "supports": SUPPORTS if kind == "dynamic" else [],
                "ok": True,
                "captured_frames": 37 if kind == "dynamic" else 3,
                "failed_frames": 0,
                "frames": frames,
                "timeline": timeline,
            }
        )
    (directory / "metrics").mkdir(parents=True)
    (directory / "metrics" / "all_values.jpg").write_bytes(a_jpeg(5102, 435))
    metadata = {
        "capture_id": str(uuid4()),
        "trigger_id": "1",
        "strike_id": str(uuid4()),
        "detected_at": "2026-10-02T18:47:05.028749+00:00",
        "started_at": "2026-10-02T18:47:05.029749+00:00",
        "completed_at": "2026-10-02T18:47:10.055977+00:00",
        "selection_interval": {
            "start_seconds": 0.5,
            "end_seconds": 4.437,
            "detected": False,
        },
        "configuration": {
            "initial_delay_seconds": 0.5,
            "duration_seconds": 4.0,
            "fps": 10.0,
            "static_delay_seconds": 3.5,
            "static_duration_seconds": 1.5,
            "static_fps": 2.0,
        },
        "coach_metrics": {
            "source": "All Values",
            "ok": True,
            "file": "metrics/all_values.jpg",
            "width": 5102,
            "height": 435,
            "captured_at": "2026-10-02T18:47:08.074896+00:00",
            "delay_seconds": 3.0,
            "offset_seconds": 3.046,
        },
        "cameras": cameras,
    }
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return directory


def a_reply() -> dict[str, Any]:
    metrics: dict[str, Any] = {spec.name: None for spec in VX_METRICS}
    metrics["ball_speed"] = {"value": 148.0, "unit": "mph"}
    metrics["club_speed"] = {"value": 112.0, "unit": "mph"}
    metrics["carry"] = {"value": 265.0, "unit": "yd"}
    return {
        "metrics": metrics,
        "coach": {
            "kind": "correction",
            "cue": "FACE_OPEN",
            "observation": "The face was open at impact.",
            "instruction": "Rotate your lead forearm sooner.",
            "reinforcement": None,
            "evidence_frame_id": "all_values",
            "visual_confidence": None,
        },
    }


class FakeProvider:
    """Answers every request with the same reply, and remembers each one."""

    def __init__(self, model: str, *, fail: Exception | None = None) -> None:
        self._model = model
        self.fail = fail
        self.requests: list[AdviceRequest] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_configured(self) -> bool:
        return True

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return ProviderReply(
            payload=a_reply(),
            model=self._model,
            latency_seconds=0.01,
            input_tokens=19692,
            output_tokens=1983,
            reasoning_tokens=1472,
        )

    def usage(self) -> Usage:
        return Usage(input_tokens=19692, output_tokens=1983, cached_tokens=1024)

    async def aclose(self) -> None:
        self.closed = True


def a_config(model: str = "gpt-5-mini", detail: str = "low") -> BenchmarkConfig:
    return BenchmarkConfig(
        model=model,
        reasoning_effort="low",
        max_output_tokens=6000,
        timeout_seconds=30.0,
        swing_detail=detail,
    )


def a_snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


async def a_package(directory: Path, max_images: int = 6) -> Any:
    capture = load_capture(directory)
    assert capture is not None
    return await load_evidence(capture, max_images=max_images)


# --- the replay -------------------------------------------------------------


class TestTheReplay:
    async def test_every_request_is_built_from_the_same_stored_bytes(
        self, tmp_path: Path
    ) -> None:
        directory = a_stored_shot(tmp_path)
        package = await a_package(directory)
        providers: list[FakeProvider] = []

        def make(config: BenchmarkConfig) -> FakeProvider:
            providers.append(FakeProvider(config.model))
            return providers[-1]

        configs = (a_config(detail="low"), a_config(detail="high"))
        results = await run_benchmark(package, configs, repeats=3, make_provider=make)

        assert len(results) == 6
        assert [len(provider.requests) for provider in providers] == [3, 3]
        on_disk = (directory / "swing_behind" / "frame_001.jpg").read_bytes()
        grid = (directory / "metrics" / "all_values.jpg").read_bytes()
        for provider, detail in zip(providers, ("low", "high"), strict=True):
            for request in provider.requests:
                assert request.images[0].data == on_disk
                assert request.images[-1].data == grid
                assert request.images[-1].detail == "high"
                assert {image.detail for image in request.images[:-1]} == {detail}
        assert all(provider.closed for provider in providers)

    async def test_runs_are_interleaved_so_drift_is_shared(
        self, tmp_path: Path
    ) -> None:
        package = await a_package(a_stored_shot(tmp_path))
        order: list[str] = []

        class Logged(FakeProvider):
            async def complete(self, request: AdviceRequest) -> ProviderReply:
                order.append(f"{self.model}:{request.images[0].detail}")
                return await super().complete(request)

        await run_benchmark(
            package,
            (a_config(detail="low"), a_config(detail="high")),
            repeats=2,
            make_provider=lambda config: Logged(config.model),
        )

        assert order == [
            "gpt-5-mini:low",
            "gpt-5-mini:high",
            "gpt-5-mini:low",
            "gpt-5-mini:high",
        ]

    async def test_each_run_records_what_the_reply_cost_and_said(
        self, tmp_path: Path
    ) -> None:
        package = await a_package(a_stored_shot(tmp_path))

        (result,) = await run_benchmark(
            package,
            (a_config(),),
            repeats=1,
            make_provider=lambda config: FakeProvider(config.model),
        )

        assert result.ok
        assert result.wall_seconds is not None
        assert (result.input_tokens, result.output_tokens) == (19692, 1983)
        assert result.reasoning_tokens == 1472
        assert result.cached_tokens == 1024
        assert result.cost_usd == pytest.approx(
            (19692 * 0.25 + 1983 * 2.00) / 1_000_000
        )
        assert result.parsed
        assert result.metrics_read == 3
        assert result.cue == "FACE_OPEN"
        assert result.evidence_frame_id == "all_values"
        assert result.frame_id_sent is True

    async def test_it_publishes_nothing_writes_nothing_and_opens_no_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = a_stored_shot(tmp_path)
        before = a_snapshot(directory)
        working = tmp_path / "cwd"
        working.mkdir()
        monkeypatch.chdir(working)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("the benchmark reached for a side effect")

        monkeypatch.setattr(sqlite3, "connect", refuse)
        monkeypatch.setattr("caddiej.coaching.service.AnalysisService.__init__", refuse)
        monkeypatch.setattr("caddiej.swing_capture.stills.note_provider_images", refuse)
        monkeypatch.setattr(
            "caddiej.swing_capture.store.update_capture_metadata", refuse
        )

        report = await benchmark_coach(
            evidence_dir=directory,
            latest_dir=tmp_path / "media" / "latest_swing",
            configs=(a_config(),),
            repeats=2,
            max_images=6,
            make_provider=lambda config: FakeProvider(config.model),
        )

        assert report.ok, report.report
        assert a_snapshot(directory) == before
        assert list(working.iterdir()) == []

    async def test_a_failing_configuration_is_reported_and_the_rest_still_run(
        self, tmp_path: Path
    ) -> None:
        directory = a_stored_shot(tmp_path)

        def make(config: BenchmarkConfig) -> FakeProvider:
            if config.model == "gpt-unbuilt":
                raise RuntimeError("could not build a client")
            if config.model == "gpt-nope":
                return FakeProvider(
                    config.model,
                    fail=ProviderRefusedError("NotFoundError: model gpt-nope"),
                )
            return FakeProvider(config.model)

        report = await benchmark_coach(
            evidence_dir=directory,
            latest_dir=tmp_path / "media" / "latest_swing",
            configs=configurations(
                models=("gpt-nope", "gpt-unbuilt", "gpt-5-mini"),
                efforts=("low",),
                details=("low",),
                max_output_tokens=6000,
                timeout_seconds=30.0,
            ),
            repeats=2,
            max_images=6,
            make_provider=make,
        )

        by_model = {
            model: [result for result in report.results if result.config.model == model]
            for model in ("gpt-nope", "gpt-unbuilt", "gpt-5-mini")
        }
        assert report.ok
        assert [result.ok for result in by_model["gpt-nope"]] == [False, False]
        assert "model gpt-nope" in (by_model["gpt-nope"][0].error or "")
        assert [result.ok for result in by_model["gpt-unbuilt"]] == [False, False]
        assert "could not build a client" in (by_model["gpt-unbuilt"][0].error or "")
        assert [result.ok for result in by_model["gpt-5-mini"]] == [True, True]
        assert "FAILED" in report.report
        assert "ok 2/2" in report.report

    async def test_every_configuration_failing_is_an_unsuccessful_benchmark(
        self, tmp_path: Path
    ) -> None:
        report = await benchmark_coach(
            evidence_dir=a_stored_shot(tmp_path),
            latest_dir=tmp_path / "media" / "latest_swing",
            configs=(a_config(),),
            repeats=1,
            max_images=6,
            make_provider=lambda config: FakeProvider(
                config.model, fail=ProviderRefusedError("AuthenticationError")
            ),
        )

        assert not report.ok
        assert "AuthenticationError" in report.report

    async def test_with_no_stored_shot_it_says_so(self, tmp_path: Path) -> None:
        report = await benchmark_coach(
            evidence_dir=None,
            latest_dir=tmp_path / "media" / "latest_swing",
            configs=(a_config(),),
            repeats=1,
            max_images=6,
            make_provider=lambda config: FakeProvider(config.model),
        )

        assert not report.ok
        assert "no stored capture" in report.report

    def test_configurations_are_every_combination_once(self) -> None:
        configs = configurations(
            models=("gpt-5-mini", "gpt-4.1-mini", "gpt-5-mini"),
            efforts=("low", None),
            details=("low",),
            max_output_tokens=6000,
            timeout_seconds=30.0,
        )

        assert [(config.model, config.reasoning_effort) for config in configs] == [
            ("gpt-5-mini", "low"),
            ("gpt-5-mini", None),
            ("gpt-4.1-mini", "low"),
            ("gpt-4.1-mini", None),
        ]


class TestTheMeteredProvider:
    async def test_it_reads_cached_tokens_off_the_raw_response(self) -> None:
        response = SimpleNamespace(
            status="completed",
            model="gpt-5-mini",
            output_text=json.dumps(a_reply()),
            usage=SimpleNamespace(
                input_tokens=19692,
                output_tokens=1983,
                total_tokens=21675,
                input_tokens_details=SimpleNamespace(cached_tokens=4096),
                output_tokens_details=SimpleNamespace(reasoning_tokens=1472),
            ),
        )

        class Responses:
            async def create(self, **_kwargs: Any) -> Any:
                return response

        class Client:
            def __init__(self, **_kwargs: Any) -> None:
                self.responses = Responses()

            async def close(self) -> None:
                return None

        provider = MeteredOpenAIProvider(
            api_key="sk-test",
            model="gpt-5-mini",
            module=SimpleNamespace(AsyncOpenAI=Client),
        )
        reply = await provider.complete(
            AdviceRequest(system="s", instructions="i", evidence="e", schema={})
        )
        usage = provider.usage()

        assert reply.input_tokens == 19692
        assert usage is not None
        assert usage.cached_tokens == 4096
        assert usage.reasoning_tokens == 1472
        await provider.aclose()


# --- the offline breakdown --------------------------------------------------


class TestTheOfflineBreakdown:
    async def test_it_needs_no_provider_and_reports_every_image(
        self, tmp_path: Path
    ) -> None:
        report = await coach_request_report(
            evidence_dir=a_stored_shot(tmp_path),
            latest_dir=tmp_path / "media" / "latest_swing",
            models=("gpt-5-mini",),
            details=("low",),
            max_images=6,
        )

        assert report.ok, report.report
        assert report.results == ()
        assert "ESTIMATE" in report.report
        assert "1792x1480  low" in report.report
        assert "5102x435   high" in report.report
        for part in ("system prompt", "instructions", "evidence text", "JSON schema"):
            assert part in report.report

    async def test_the_breakdown_measures_each_part_and_image(
        self, tmp_path: Path
    ) -> None:
        package = await a_package(a_stored_shot(tmp_path))
        request = build_benchmark_request(package, a_config(model="gpt-4o"))

        breakdown = request_breakdown(request, model="gpt-4o")

        sizes = {image.frame_id: image for image in breakdown.images}
        assert len(sizes) == len(request.images) == 7
        assert {"swing_behind_01", "impact_01", "all_values"} <= set(sizes)
        assert (sizes["swing_behind_01"].width, sizes["swing_behind_01"].height) == (
            1792,
            1480,
        )
        assert sizes["swing_behind_01"].detail == "low"
        assert sizes["swing_behind_01"].estimate.tokens == 85
        assert sizes["all_values"].detail == "high"
        assert sizes["all_values"].size_bytes == len(request.images[-1].data)
        system = next(text for text in breakdown.texts if text.part == "system prompt")
        assert system.characters == len(request.system)
        assert breakdown.total_tokens == breakdown.text_tokens + breakdown.image_tokens

    def test_the_capture_clock_names_the_window_that_ends_it(
        self, tmp_path: Path
    ) -> None:
        timing = capture_timing(a_stored_shot(tmp_path))

        assert timing is not None
        assert timing.elapsed_seconds == pytest.approx(5.026, abs=0.001)
        defining = timing.defining
        assert defining is not None
        assert defining.panel_id == "impact"
        assert defining.end_seconds == pytest.approx(5.0)
        behind = next(w for w in timing.windows if w.panel_id == "swing_behind")
        assert behind.end_seconds == pytest.approx(4.5)
        assert behind.content_from == pytest.approx(2.25)
        report = timing_report(timing, ("swing_behind_01",))
        assert "capture end is set by impact" in report
        assert "SENT TO THE MODEL: swing_behind_01" in report


# --- the image token estimator ----------------------------------------------


class TestTheImageTokenEstimate:
    def test_low_detail_is_flat(self) -> None:
        assert tile_image_tokens(4000, 3000, detail="low") == (85, 0)
        assert (
            estimate_image_tokens(1792, 1480, detail="low", model="gpt-4o").tokens == 85
        )

    def test_high_detail_is_a_base_plus_tiles(self) -> None:
        # 1024x1024 -> shortest side to 768 -> 768x768 -> 2x2 tiles.
        assert tile_image_tokens(1024, 1024, detail="high") == (85 + 4 * 170, 4)
        # 2048x4096 -> fit 2048 -> 1024x2048 -> shortest to 768 -> 768x1536 -> 2x3.
        assert tile_image_tokens(2048, 4096, detail="high") == (85 + 6 * 170, 6)
        # 5102x435 -> fit 2048 -> 2048x175, never enlarged -> 4x1.
        assert tile_image_tokens(5102, 435, detail="high") == (85 + 4 * 170, 4)

    def test_patch_models_count_32_pixel_patches(self) -> None:
        # Under the cap: 32 x 32 patches, times the multiplier, rounded up.
        assert patch_image_tokens(1024, 1024, multiplier=1.62) == (1659, 1024)
        # Over it: shrunk to 1056x1408, which is 33 x 44 patches.
        assert patch_image_tokens(1800, 2400, multiplier=1.62)[1] == 1452

    def test_a_dated_snapshot_uses_its_alias_rule(self) -> None:
        estimate = estimate_image_tokens(
            1024, 1024, detail="low", model="gpt-5-mini-2025-08-07"
        )

        assert estimate.rule == "patch"
        assert estimate.tokens == 1229, "1024 patches at the measured x1.2"
        assert not estimate.detail_applied

    def test_gpt_5_mini_is_fitted_to_2048_and_never_capped(self) -> None:
        # Measured on real requests: a 2048 square and a 3072 square both cost
        # 4096 patches, 4916 tokens. The published 1536 cap would say 2489.
        square = estimate_image_tokens(2048, 2048, detail="high", model="gpt-5-mini")
        larger = estimate_image_tokens(3072, 3072, detail="high", model="gpt-5-mini")

        assert (square.tokens, square.units) == (4916, 4096)
        assert larger == square

    def test_an_unknown_model_says_its_rule_was_assumed(self) -> None:
        estimate = estimate_image_tokens(1024, 1024, detail="high", model="mystery")

        assert estimate.rule == "tile (assumed)"
        assert estimate.tokens == 765
