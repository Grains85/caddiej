"""The coach's result, from the event that carries it to what the floor draws.

On hardware a swing scored 76 and had its frames on disk, and the projector
showed neither. Every Python link in the chain was already correct; the page
on the projector was running an overlay build from before the score existed,
because a Browser Source outlives the CaddieJ that served it and nothing ever
told it to load the new one. These tests hold the chain end to end — event,
floor state, wire, the renderer the server actually serves — and hold the
recovery that makes an old page load the current build by itself, including
the limit that keeps a page which cannot upgrade from reloading forever.

The overlay has no JS test runner, so its half is pinned at the strings, the
way `test_hud_overlay_js` and `test_hud_result_js` already do.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_capture_scene import a_burst
from tests.unit.test_coach_result_on_floor import a_score
from tests.unit.test_hud_overlay_js import CLIENT_JS, FLOOR_CSS, INDEX_HTML, STATE_JS
from tests.unit.test_hud_server import drain_received, text_frame
from tests.unit.test_obs_client import _Bay
from tests.unit.test_play_flow import a_diagnosis, an_advice, flow

from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.adapters.hud.evidence import EvidenceFrames
from caddiej.adapters.hud.protocol import FloorStatePayload
from caddiej.adapters.hud.server import (
    ASSETS_RELOAD_LIMIT,
    assets_reload_url,
    overlay_assets_version,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    CoachingProgress,
    CoachingStage,
    EvidenceFrame,
    ShotDetected,
    ShotDiagnosed,
    SwingFramesReady,
)
from caddiej.domain.play import PlayFlow
from caddiej.domain.ports import CoachingResult
from caddiej.floor import FloorService
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.swing_capture.store import frame_files

STALE = "65530f0bf338"


def a_floor() -> tuple[FloorService, PlayFlow]:
    """The real presenter and floor state under the real play flow."""

    service = FloorService()
    presenter = FloorPresenter(service, layout_from_tees(), club_path_length=0.4)
    return service, flow(presenter)  # type: ignore[arg-type]


def wire_advice(service: FloorService) -> dict[str, object]:
    payload = FloorStatePayload.from_state(service.state).model_dump()
    advice = payload["advice"]
    assert isinstance(advice, dict)
    return advice


def function_body(source: str, name: str) -> str:
    return source.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


def css_rule(selector: str) -> str:
    """The body of the stylesheet's first rule for exactly this selector."""

    return FLOOR_CSS.split(f"\n{selector} {{\n", 1)[1].split("\n}", 1)[0]


async def a_stored_capture(tmp_path: Path) -> tuple[TestClient, str, str]:
    """A real burst on disk, behind the real evidence resolver."""

    published, _ = await a_burst(tmp_path, _Bay())
    ready = next(e for e in published if isinstance(e, SwingFramesReady))
    root = tmp_path / "media" / "swing_shots"
    app = create_app(
        FloorService(), ConnectionRegistry(), evidence=EvidenceFrames(root).resolve
    )
    frame = next(iter(frame_files(ready.capture)))
    return TestClient(app), str(ready.capture.capture_id), frame


class TestSwingScore76ReachesFloorState:
    async def test_from_the_template_verdict(self) -> None:
        service, subject = a_floor()

        await subject.on_shot_diagnosed(
            ShotDiagnosed(diagnosis=a_diagnosis(uuid4()), swing_score=a_score(76))
        )

        assert service.state.advice is not None
        assert service.state.advice.swing_score == 76
        assert wire_advice(service)["swing_score"] == 76
        await subject.aclose()

    async def test_from_the_coached_line(self) -> None:
        service, subject = a_floor()
        shot_id = uuid4()
        advice = replace(
            an_advice(shot_id, display="DELIVER IT FROM THE INSIDE"),
            observation="YOU'RE CUTTING ACROSS",
            swing_score=a_score(76),
        )

        await subject.on_advice_ready(AnalysisCompleted(shot_id=shot_id, advice=advice))

        wire = wire_advice(service)
        assert wire["swing_score"] == 76
        assert wire["observation"] == "YOU'RE CUTTING ACROSS"
        assert wire["text"] == "DELIVER IT FROM THE INSIDE"
        await subject.aclose()


class TestTheScoreRendersInTheHud:
    def test_the_served_renderer_paints_the_score_from_the_advice(self) -> None:
        client = TestClient(create_app(FloorService(), ConnectionRegistry()))
        served = client.get(f"/static/hud/{overlay_assets_version()}/state.js").text

        assert "renderAdviceScore(advice.swing_score)" in served
        body = function_body(served, "renderAdviceScore")
        assert "adviceScoreValueEl.textContent = String(Math.round(score));" in body
        assert "adviceScoreEl.hidden = false;" in body

    def test_the_markup_reads_score_out_of_a_hundred_above_the_frame(self) -> None:
        advice = INDEX_HTML.split('id="advice"', 1)[1].split('id="progress"', 1)[0]

        assert "SWING SCORE" in advice
        assert "/ 100" in advice
        order = [
            advice.index(marker)
            for marker in (
                'id="advice-score"',
                'id="advice-evidence"',
                'id="advice-observation"',
                'id="advice-text"',
            )
        ]
        assert order == sorted(order)

    def test_the_unanchored_number_is_sized_like_the_result_figures(self) -> None:
        """Read from the tee box, so it gets the scale the distance gets.

        Only on a floor with no regions, which is the centred column the
        overlay has always drawn. A bay that knows where its mat is sizes the
        score from the box the mat leaves room for instead -- see
        `test_the_regioned_number_is_sized_from_its_own_box`.
        """

        rule = css_rule(".advice__score-value")
        figure = FLOOR_CSS.split(".info-figure__value {", 1)[1].split("}", 1)[0]

        assert "font-size: clamp(4.2rem, 12vw, 13rem);" in rule
        assert "font-size: clamp(4.2rem, 12vw, 13rem);" in figure

    def test_the_regioned_number_is_sized_from_its_own_box(self) -> None:
        """The whole of the first hardware test's clipping, in one rule.

        Twelve per cent of the viewport *width* is 123 pixels on a 1024-wide
        projector, and the box the mat left for it was 69 tall. Sizing the
        number as a fraction of its own region is what makes that impossible
        rather than unlikely.
        """

        rule = css_rule('.advice[data-regions="true"] .advice__score-value')

        assert "font-size: calc(var(--region-px) * 0.74);" in rule
        assert "vw" not in rule
        # A line box shorter than the font put the tops of the digits outside
        # it, at the very top of the image, with nowhere to overflow to.
        assert "line-height: 1;" in rule

    def test_the_observation_is_held_apart_from_the_frame_and_the_instruction(
        self,
    ) -> None:
        """Its own margins, in its own ems: the column gap alone left the
        frame, this line and the instruction's glow a few pixels apart, and
        on the floor that read as the line clipped under the picture."""

        rule = css_rule(".advice__observation")

        assert "margin: 0.6em 0 0.4em;" in rule
        assert "font-size: clamp(1.2rem, 3vw, 3.2rem);" in rule
        properties = {line.strip().split(":", 1)[0] for line in rule.splitlines()}
        assert not properties & {"position", "transform", "top", "bottom"}

    def test_the_lift_measures_every_line_of_the_stack(self) -> None:
        """Nothing caps or clips the panel, so the box `keepAdviceOnSurface`
        measures includes the observation and its margins, and a frame that
        arrives after the first paint measures the stack again."""

        panel = css_rule(".advice")
        fit = function_body(STATE_JS, "keepAdviceOnSurface")
        onload = function_body(STATE_JS, "renderAdviceEvidence").split(
            "adviceEvidenceEl.onload = () => {", 1
        )[1]

        for capped in ("height", "overflow", "max-height"):
            assert f"{capped}:" not in panel
        assert "const box = adviceEl.getBoundingClientRect();" in fit
        assert "keepAdviceOnSurface();" in onload.split("};", 1)[0]

    def test_an_absent_frame_takes_no_room_in_the_stack(self) -> None:
        """Hidden is `display: none`, so the column closes up rather than
        leaving a hole the height of a picture, and no spacing the stack
        depends on lives on the frame."""

        evidence = css_rule(".advice__evidence")

        assert "[hidden] {\n  display: none !important;\n}" in FLOOR_CSS
        assert "display: none;" in evidence
        assert "margin" not in evidence
        assert "display: block;" in css_rule(".advice__evidence:not([hidden])")

    def test_a_result_taller_than_the_floor_left_below_it_is_lifted_onto_it(
        self,
    ) -> None:
        paint = function_body(STATE_JS, "paint")
        fit = function_body(STATE_JS, "keepAdviceOnSurface")

        assert paint.index("renderAdvice(") < paint.index("keepAdviceOnSurface()")
        assert "box.bottom - bounds.bottom" in fit
        assert "box.top - bounds.top" in fit, "never lifted off the top"
        assert "var(--advice-lift, 0px)" in FLOOR_CSS


class TestAValidEvidenceFrame:
    async def test_reaches_floor_state_and_the_wire(self) -> None:
        service, subject = a_floor()
        shot_id = uuid4()
        capture_id = uuid4()

        await subject.on_advice_ready(
            AnalysisCompleted(
                shot_id=shot_id,
                advice=an_advice(shot_id),
                evidence=EvidenceFrame(capture_id=capture_id, frame_id="impact_01"),
            )
        )

        assert service.state.advice is not None
        evidence = service.state.advice.evidence
        assert evidence is not None and evidence.frame_id == "impact_01"
        wire = wire_advice(service)
        assert wire["evidence_url"] == f"/evidence/{capture_id}/impact_01"
        assert wire["evidence_frame_id"] == "impact_01"
        await subject.aclose()

    async def test_the_url_on_the_wire_serves_the_stored_still(self, tmp_path) -> None:
        client, capture_id, frame = await a_stored_capture(tmp_path)
        service = FloorService()
        FloorPresenter(service, layout_from_tees(), club_path_length=0.4).show_advice(
            "FACE OPEN",
            0.6,
            CoachingResult(
                swing_score=76,
                evidence=EvidenceFrame(capture_id=UUID(capture_id), frame_id=frame),
            ),
        )

        url = wire_advice(service)["evidence_url"]
        assert isinstance(url, str)
        response = client.get(url)

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_the_frame_is_shown_once_it_has_loaded(self) -> None:
        body = function_body(STATE_JS, "renderAdviceEvidence")

        assert 'url.startsWith("/evidence/")' in body
        onload = body.split("adviceEvidenceEl.onload = () => {", 1)[1].split("};")[0]
        assert 'adviceEvidenceEl.dataset.evidence = "loaded";' in onload
        assert "adviceEvidenceEl.hidden = false;" in onload


class TestAnInvalidEvidenceFrame:
    async def test_is_not_found_rather_than_substituted(self, tmp_path) -> None:
        client, capture_id, _frame = await a_stored_capture(tmp_path)

        for frame in ("frame_999", "swing_behind_99.jpg", "..%2Fmetadata.json"):
            assert client.get(f"/evidence/{capture_id}/{frame}").status_code == 404

    async def test_an_absent_frame_leaves_the_score_and_no_url(self) -> None:
        service, subject = a_floor()
        shot_id = uuid4()

        advice = replace(
            an_advice(shot_id),
            swing_score=a_score(76),
            evidence_frame_id="made_up_by_a_model",
        )

        await subject.on_advice_ready(
            AnalysisCompleted(shot_id=shot_id, advice=advice, evidence=None)
        )

        wire = wire_advice(service)
        assert wire["swing_score"] == 76
        assert wire["evidence_url"] is None
        await subject.aclose()

    def test_a_refused_frame_stays_hidden_through_later_snapshots(self) -> None:
        """The 404 hides it; the next unrelated snapshot used to unhide it.

        The URL does not change between snapshots, so the old renderer kept
        the request and then set `hidden = false` on every paint — a broken
        image beside the line the moment the ball status so much as blinked.
        """

        body = function_body(STATE_JS, "renderAdviceEvidence")
        onerror = body.split("adviceEvidenceEl.onerror = () => {", 1)[1].split("};")[0]

        assert "adviceEvidenceEl.hidden = true;" in onerror
        assert 'adviceEvidenceEl.dataset.evidence = "failed";' in onerror
        assert body.rstrip().endswith(
            'adviceEvidenceEl.hidden = adviceEvidenceEl.dataset.evidence !== "loaded";'
        )
        tail = body.split("adviceEvidenceEl.src = path;", 1)[1]
        assert "adviceEvidenceEl.hidden = false;" not in tail


class TestProgressRenders:
    async def test_capturing_then_analyzing_then_coaching(self) -> None:
        service, subject = a_floor()
        await subject.on_shot_detected(ShotDetected(trigger_id="70"))

        seen = []
        for stage in (
            CoachingStage.CAPTURING,
            CoachingStage.ANALYZING,
            CoachingStage.COACHING,
        ):
            await subject.on_coaching_progress(
                CoachingProgress(stage=stage, trigger_id="70")
            )
            progress = FloorStatePayload.from_state(service.state).progress
            assert progress is not None
            seen.append(progress.stage)

        assert seen == ["capturing", "analyzing", "coaching"]
        await subject.aclose()

    def test_each_stage_has_a_word_the_served_renderer_paints(self) -> None:
        client = TestClient(create_app(FloorService(), ConnectionRegistry()))
        served = client.get(f"/static/hud/{overlay_assets_version()}/state.js").text

        assert "renderProgress(state.progress)" in served
        for stage, word in (
            ("capturing", "CAPTURING SWING"),
            ("analyzing", "ANALYZING"),
            ("coaching", "COACHING"),
        ):
            assert f'{stage}: "{word}"' in served
        body = function_body(served, "renderProgress")
        assert "progressLabelEl.textContent = label;" in body
        assert "progressEl.hidden = false;" in body


def hello(url: str, loaded: str) -> str:
    """The hello `client.js` sends from a page at `url` running build `loaded`."""

    query = parse_qs(urlsplit(url).query)
    return json.dumps(
        {
            "type": "hello",
            "surface": "floor",
            "assets_version": loaded,
            "reload_for": query.get("hud_assets", [""])[0],
            "reload_attempt": query.get("hud_reload", [""])[0],
        }
    )


async def reloads_for(url: str, loaded: str) -> list[dict]:
    _connection, queued = await drain_received([text_frame(hello(url, loaded))])
    return [message for message in queued if message["type"] == "reload"]


class TestStaleAssetsAreReloaded:
    async def test_a_page_from_an_older_run_is_sent_to_the_current_build(
        self,
    ) -> None:
        current = overlay_assets_version()

        [reload] = await reloads_for("/", STALE)

        assert reload["hud_assets_version"] == current
        assert reload["url"] == f"/?hud_assets={current}&hud_reload=1"

    async def test_a_page_that_reached_the_current_build_is_left_alone(self) -> None:
        current = overlay_assets_version()
        [reload] = await reloads_for("/", STALE)

        assert await reloads_for(reload["url"], current) == []

    async def test_a_page_that_cannot_upgrade_stops_being_asked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No reload storm: a page stuck on an old build is asked a fixed
        number of times, then left drawing and reported as an error."""

        url, asked = "/", 0
        with caplog.at_level("INFO", logger="caddiej.adapters.hud.server"):
            for _ in range(ASSETS_RELOAD_LIMIT + 5):
                reloads = await reloads_for(url, STALE)
                if not reloads:
                    break
                url, asked = reloads[0]["url"], asked + 1

        assert asked == ASSETS_RELOAD_LIMIT
        events = [record.getMessage() for record in caplog.records]
        assert events.count("overlay_assets_reload_requested") == ASSETS_RELOAD_LIMIT
        assert events.count("overlay_assets_reload_exhausted") == 1

    def test_a_count_is_only_a_count_for_the_build_it_names(self) -> None:
        current = overlay_assets_version()

        assert assets_reload_url(current, current, "1") is not None
        assert assets_reload_url(current, current, str(ASSETS_RELOAD_LIMIT)) is None
        assert assets_reload_url(current, STALE, "99") == (
            f"/?hud_assets={current}&hud_reload=1"
        ), "a page that reached an older build gets its tries again"

    @pytest.mark.parametrize("attempt", ["x", "-1", "1.5", "²", "1; DROP"])
    def test_a_garbled_count_is_spent_not_reset(self, attempt: str) -> None:
        current = overlay_assets_version()

        assert assets_reload_url(current, current, attempt) is None

    def test_over_the_real_socket(self) -> None:
        service = FloorService()
        client = TestClient(create_app(service, ConnectionRegistry()))

        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()
            websocket.receive_text()
            websocket.send_text(hello("/", STALE))
            reload = json.loads(websocket.receive_text())

        assert reload["type"] == "reload"
        assert reload["url"].startswith("/?hud_assets=")

    def test_the_page_obeys_once_and_only_to_this_server(self) -> None:
        handler = CLIENT_JS.split('case "reload":', 1)[1].split("break;", 1)[0]
        body = function_body(CLIENT_JS, "reloadTo")

        assert "reloadTo(message.url);" in handler
        assert "reloading ||" in body
        assert '!url.startsWith("/?")' in body
        assert "reloading = true;" in body
        assert "window.location.replace(url);" in body
        assert 'reload_for: RELOAD_PARAMS.get("hud_assets")' in CLIENT_JS
        assert 'reload_attempt: RELOAD_PARAMS.get("hud_reload")' in CLIENT_JS
        assert "location.reload(" not in CLIENT_JS

    def test_an_old_page_asking_for_its_own_client_js_is_told_to_reload(
        self,
    ) -> None:
        """What a page with HTML from another run asks for, instead of a 404
        that left the projector dark until someone walked over to it."""

        current = overlay_assets_version()
        client = TestClient(create_app(FloorService(), ConnectionRegistry()))

        response = client.get(f"/static/hud/{STALE}/client.js")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.headers["cache-control"].startswith("no-store")
        assert f'const expected = "{current}";' in response.text
        assert f"tried < {ASSETS_RELOAD_LIMIT}" in response.text
        assert "window.location.replace(" in response.text
        assert "/?hud_assets=${expected}&hud_reload=${tried + 1}" in response.text
        assert "import" not in response.text, "nothing of the new build is mixed in"
        assert client.get(f"/static/hud/{STALE}/state.js").status_code == 404
        assert client.get(f"/static/hud/{STALE}/floor.css").status_code == 404
