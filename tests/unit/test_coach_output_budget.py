"""The ProTee reply's output budget, and refusing a reply that ran out of it.

On the bay, gpt-5-mini answered HTTP 200 after 17.7 s with
`status=incomplete, reason=max_output_tokens`. The ceiling was 2000, nothing
set the reasoning effort, and a reasoning model's thinking counts against
`max_output_tokens`. A complete reply with every metric read is a few hundred
tokens, so the ceiling went on reasoning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from tests.unit.schema_support import fixed_length, metric_entry
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import Coach, a_reading, a_reply, an_open_face_grid
from tests.unit.test_openai_provider import FakeSdk, _response, a_provider

from caddiej.coaching import vx
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.provider import (
    AdviceRequest,
    ImageInput,
    ProviderReplyError,
)
from caddiej.config import Settings
from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.cues import CueId
from caddiej.domain.events import AnalysisCompleted, AnalysisFailed
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.vx import VX_METRICS, MetricDirection

DEFAULTS = Settings(_env_file=None)  # type: ignore[call-arg]


def every_metric_read() -> dict[str, Any]:
    """The largest reply there is: every one of the 29 metrics filled."""

    metrics: dict[str, Any] = {}
    for spec in VX_METRICS:
        reading: dict[str, Any] = {"value": 1234.5, "unit": spec.units[0].value}
        if spec.directions:
            reading["direction"] = spec.directions[0].value
        metrics[spec.name] = reading
    return metrics


def a_protee_request(**options: Any) -> AdviceRequest:
    cues = vx.possible_cues(ClubKind.DRIVER, has_metrics=True)
    return vx.build_request(
        evidence=vx.render_evidence(
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            has_metrics=True,
            swing_images=("SWING_BEHIND / DOWN_THE_LINE",),
            cues=cues,
            session_focus=None,
            focus_shots=0,
        ),
        images=(ImageInput(data=b"\xff\xd8grid\xff\xd9", label="ALL_VALUES"),),
        cues=cues,
        max_output_tokens=options.get(
            "max_output_tokens", DEFAULTS.coaching_protee_max_output_tokens
        ),
        timeout_seconds=30.0,
        reasoning_effort=options.get(
            "reasoning_effort", DEFAULTS.coaching_reasoning_effort or None
        ),
    )


def reasoning_response(
    text: str,
    *,
    status: str = "completed",
    reason: str | None = None,
    output: int = 1400,
    reasoning: int = 900,
) -> Any:
    response = _response(
        text,
        status=status,
        incomplete_reason=reason,
        input_tokens=5200,
        output_tokens=output,
    )
    response.usage.output_tokens_details = type(
        "Details", (), {"reasoning_tokens": reasoning}
    )()
    response.usage.total_tokens = 5200 + output
    return response


class TestTheBudget:
    def test_the_defaults_leave_room_for_reasoning_and_the_reply(self) -> None:
        assert DEFAULTS.coaching_protee_max_output_tokens == 10000
        assert DEFAULTS.coaching_reasoning_effort == "low"
        assert DEFAULTS.coaching_model == "gpt-5-mini"

    def test_the_largest_reply_fits_with_most_of_the_ceiling_left(self) -> None:
        reply = {
            "metrics": every_metric_read(),
            "checkpoints": a_reply()["checkpoints"],
            "coach": a_reply()["coach"],
        }
        # Three characters a token is pessimistic for JSON of short keys and
        # numbers; even pretty-printed it stays well inside the ceiling.
        pretty = len(json.dumps(reply, indent=2)) / 3
        assert pretty < 6500
        assert DEFAULTS.coaching_protee_max_output_tokens - pretty > 3000

    def test_a_metric_with_no_qualifier_has_no_direction_to_write(self) -> None:
        schema = vx.reply_schema(vx.possible_cues(ClubKind.DRIVER, has_metrics=True))
        reading = metric_entry(schema)
        assert fixed_length(reading) == vx.METRIC_ENTRY
        lines = a_protee_request().evidence.splitlines()
        for number, spec in enumerate(VX_METRICS, start=1):
            line = next(text for text in lines if text.startswith(f"{number} "))
            assert line.startswith(f"{number} {spec.label}: "), spec.name
            if spec.signed:
                assert line.endswith("; signed as printed, dir 0"), spec.name
            elif spec.directions:
                codes = " ".join(
                    f"{code}={d.value}"
                    for code, d in enumerate(spec.directions, start=1)
                )
                assert line.endswith(f"; dir {codes}"), spec.name
                first = vx.decode_wire(
                    {
                        "metrics": [
                            [1.0, 1, 1] if s is spec else None for s in VX_METRICS
                        ]
                    },
                    (),
                )["metrics"][spec.name]
                assert first["direction"] == spec.directions[0].value
            else:
                assert "dir" not in line, spec.name
                metrics = vx.parse_metrics(
                    vx.decode_wire(
                        {
                            "metrics": [
                                [1.0, 1, 1] if s is spec else None for s in VX_METRICS
                            ]
                        },
                        (),
                    )["metrics"]
                )
                assert metrics.get(spec.name) is None, spec.name
                assert metrics.rejected == (spec.name,)

    def test_a_metric_with_one_unit_has_no_unit_to_write(self) -> None:
        """The wire writes the unit as a code always; a one-unit metric offers one
        code, and any other code is refused rather than guessed."""

        lines = a_protee_request().evidence.splitlines()
        for number, spec in enumerate(VX_METRICS, start=1):
            line = next(text for text in lines if text.startswith(f"{number} "))
            units = " ".join(
                f"{code}={unit.value}" for code, unit in enumerate(spec.units, start=1)
            )
            assert f": unit {units}" in line, spec.name
            beyond = [
                [1.0, len(spec.units) + 1, 0] if s is spec else None for s in VX_METRICS
            ]
            metrics = vx.parse_metrics(
                vx.decode_wire({"metrics": beyond}, ())["metrics"]
            )
            assert metrics.get(spec.name) is None, spec.name
            assert metrics.rejected == (spec.name,)

    def test_the_reply_asks_for_nothing_but_metrics_and_one_coach_line(
        self,
    ) -> None:
        schema = vx.reply_schema((CueId.FACE_OPEN,))
        assert schema["required"] == ["frames", "metrics", "checkpoints", "coach"]
        assert schema["additionalProperties"] is False
        coach = schema["properties"]["coach"]
        assert set(coach["required"]) == {
            "kind",
            "cue",
            "observation",
            "instruction",
            "reinforcement",
            "evidence_frame_id",
        }
        assert coach["additionalProperties"] is False


class TestTheProviderReportsWhatItSpent:
    async def test_the_request_sends_low_reasoning_and_the_new_ceiling(self) -> None:
        sdk = FakeSdk()
        provider = a_provider(sdk)

        await provider.complete(a_protee_request())

        call = sdk.clients[0].calls[-1]
        assert call["max_output_tokens"] == 10000
        assert call["reasoning"] == {"effort": "low"}

    async def test_a_complete_reply_carries_its_reasoning_tokens(self, caplog) -> None:
        sdk = FakeSdk()
        provider = a_provider(sdk)
        request = a_protee_request()
        await provider.complete(request)
        sdk.clients[0].response = reasoning_response(json.dumps(a_reply()))

        with caplog.at_level(logging.INFO, logger="caddiej.adapters.openai"):
            reply = await provider.complete(request)

        assert reply.reasoning_tokens == 900
        assert reply.output_tokens == 1400
        usage = [r for r in caplog.records if r.msg == "coaching_openai_usage"][-1]
        assert usage.coaching_response_status == "completed"  # type: ignore[attr-defined]
        assert usage.coaching_usage_reasoning == 900  # type: ignore[attr-defined]
        assert usage.coaching_output_ceiling == 10000  # type: ignore[attr-defined]

    async def test_an_incomplete_reply_is_refused_and_says_where_it_went(
        self, caplog
    ) -> None:
        sdk = FakeSdk()
        provider = a_provider(sdk)
        request = a_protee_request(max_output_tokens=2000, reasoning_effort=None)
        await provider.complete(request)
        # Valid, whole JSON is still refused when the API says incomplete.
        sdk.clients[0].response = reasoning_response(
            json.dumps(a_reply()),
            status="incomplete",
            reason="max_output_tokens",
            output=2000,
            reasoning=1856,
        )

        with (
            caplog.at_level(logging.INFO, logger="caddiej.adapters.openai"),
            pytest.raises(ProviderReplyError, match="incomplete: max_output_tokens"),
        ):
            await provider.complete(request)

        usage = [r for r in caplog.records if r.msg == "coaching_openai_usage"][-1]
        assert usage.levelno == logging.WARNING
        assert usage.coaching_reason == "max_output_tokens"  # type: ignore[attr-defined]
        assert usage.coaching_usage_output == 2000  # type: ignore[attr-defined]
        assert usage.coaching_usage_reasoning == 1856  # type: ignore[attr-defined]
        assert usage.coaching_reasoning_effort == "default"  # type: ignore[attr-defined]

    async def test_a_partial_json_reply_is_refused(self) -> None:
        sdk = FakeSdk()
        provider = a_provider(sdk)
        request = a_protee_request()
        await provider.complete(request)
        sdk.clients[0].response = reasoning_response(
            '{"metrics": {"ball_speed": {"value": 148',
            status="incomplete",
            reason="max_output_tokens",
        )

        with pytest.raises(ProviderReplyError):
            await provider.complete(request)

    async def test_the_usage_line_holds_no_image_or_key(self, caplog) -> None:
        sdk = FakeSdk()
        provider = a_provider(sdk)
        with caplog.at_level(logging.DEBUG):
            await provider.complete(a_protee_request())
        text = " ".join(str(vars(record)) for record in caplog.records)
        assert "sk-test" not in text
        assert "base64" not in text
        assert "grid" not in text


class TestOneRequestOneVerdict:
    async def test_an_incomplete_reply_coaches_nothing_and_reads_nothing(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(
            replies=(ProviderReplyError("the reply was incomplete: max_output_tokens"),)
        )
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert provider.calls == 1, "a reply error is not retried"
        assert not coach.bus.of(AnalysisCompleted)
        assert [e.reason for e in coach.bus.of(AnalysisFailed)] == [
            AdviceFailure.PROVIDER_ERROR
        ]
        assert coach.results[0].status == "unavailable"
        assert coach.results[0].metrics is None
        assert coach.results[0].speech is None

    async def test_every_metric_and_the_line_come_from_one_reply(
        self, tmp_path
    ) -> None:
        grid = every_metric_read()
        grid.update(an_open_face_grid())
        provider = ScriptedProvider(replies=(a_reply(grid),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert provider.calls == 1, "no separate OCR or metrics request"
        (sheet,) = provider.requests[0].images
        assert sheet_cells(sheet.data).count("ALL_VALUES") == 1
        result = coach.results[0]
        assert result.status == "ok"
        assert result.cue_id == CueId.FACE_OPEN.value
        assert result.metrics is not None
        assert result.metrics.get("face_angle").direction is MetricDirection.OPEN  # type: ignore[union-attr]
        assert len(result.metrics.available) >= 9

    async def test_a_reading_without_a_direction_key_is_accepted(
        self, tmp_path
    ) -> None:
        grid = an_open_face_grid(ball_speed={"value": 148.0, "unit": "mph"})
        provider = ScriptedProvider(replies=(a_reply(grid),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        metrics = coach.results[0].metrics
        assert metrics is not None
        assert metrics.get("ball_speed") is not None
        assert "ball_speed" not in metrics.rejected

    async def test_a_direction_on_a_metric_that_has_none_is_not_believed(
        self, tmp_path
    ) -> None:
        grid = an_open_face_grid(ball_speed=a_reading(148.0, "mph", "left"))
        provider = ScriptedProvider(replies=(a_reply(grid),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        metrics = coach.results[0].metrics
        assert metrics is not None
        assert metrics.get("ball_speed") is None
        assert "ball_speed" in metrics.rejected

    async def test_an_impossible_number_is_not_believed(self, tmp_path) -> None:
        grid = an_open_face_grid(ball_speed=a_reading(4000.0, "mph"))
        provider = ScriptedProvider(replies=(a_reply(grid),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        metrics = coach.results[0].metrics
        assert metrics is not None
        assert "ball_speed" in metrics.rejected

    async def test_a_cue_outside_the_finite_list_is_refused(self, tmp_path) -> None:
        provider = ScriptedProvider(replies=(a_reply(cue="KEEP_YOUR_HEAD_DOWN"),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert coach.results[0].cue_id == "FACE_OPEN"
        assert "KEEP_YOUR_HEAD_DOWN" in (coach.results[0].detail or "")
        assert not any(
            e.advice.cue is not None and e.advice.cue.value == "KEEP_YOUR_HEAD_DOWN"
            for e in coach.bus.of(AnalysisCompleted)
        )

    async def test_the_request_names_the_new_prompt_version(self, tmp_path) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert coach.results[0].prompt_version == vx.PROTEE_PROMPT_VERSION == "vx13"
