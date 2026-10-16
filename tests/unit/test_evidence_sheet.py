"""The Swing Evidence Sheet: one image to the model, the originals stay evidence.

Two real requests carrying seventeen separate images ran out the thirty-second
timeout on the bay. The fix is transport only: every selected still, the
impact graphic and the All Values grid travel as cells of ONE composed image,
each under a printed ID, and the reply is translated from those IDs back to
the canonical frame ids before anything reads it. These tests hold the parts
of that which must never drift:

* exactly one image goes to OpenAI, and it is the sheet, not the stills;
* every cell is there, under a deterministic ID, and maps to its original;
* the originals are untouched and remain what a fault points at;
* phase identification, view checks and the All Values refusal are exactly
  as strict through a cell ID as they were through a frame id;
* a sheet that cannot be drawn costs the pictures, never the shot.

Real JPEGs, drawn with Pillow, so the real compose runs. Each original is one
flat colour, so a cell can be checked against its original by sampling it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image, JpegImagePlugin
from tests.unit.schema_support import citable_cells, listed_cells
from tests.unit.sheet_support import placeholder_sheet, sheet_cells
from tests.unit.test_coach_protee import (
    BODY_CLAIMS,
    DRIVER,
    NOW,
    Coach,
    a_checkpoint,
    a_reply,
    a_swing,
)
from tests.unit.test_swing_capture_evidence import ReplayObs, Rig

from caddiej.coaching import vx
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.cues import PanelKind, PanelView
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    ClubSelected,
    ShotDetected,
    SwingFramesReady,
)
from caddiej.domain.media import (
    MetricsStill,
    SwingCameraCapture,
    SwingCapture,
    SwingFrame,
)
from caddiej.domain.swing_checkpoints import CheckpointId as C
from caddiej.domain.swing_checkpoints import SwingFaultId as F
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.swing_capture.sheet import (
    MAX_SIDE,
    SHEET_FILE,
    SHEET_LABEL,
    CellKind,
    ComposedSheet,
    EvidenceSheet,
    compose_sheet,
    original_for_cell,
    plan_cells,
    render_evidence_sheet,
    stored_cells,
)
from caddiej.swing_capture.store import (
    EVIDENCE_KEEP,
    METADATA_NAME,
    METRICS_FRAME_ID,
    frame_path,
)

P = SwingPhase
DTL_PHASES = PHASE_ORDER[:8]
FO_PHASES = tuple(phase for phase in DTL_PHASES if phase is not P.TAKEAWAY)
# Each camera on its own replay clock: the same moment is a different time.
DTL_SECONDS = (0.5, 1.2, 1.8, 2.4, 2.7, 2.9, 3.0, 3.3)
FO_SECONDS = (0.6, 1.6, 2.1, 2.5, 2.8, 3.0, 3.4)
BODY_SIZE = (334, 494)
IMPACT_SIZE = (400, 300)
GRID_SIZE = (960, 540)

EXPECTED_CELLS = (
    "DTL_P1",
    "DTL_P2",
    "DTL_P3",
    "DTL_P4",
    "DTL_P5",
    "DTL_P6",
    "DTL_P7",
    "DTL_P8",
    "FO_P1",
    "FO_P3",
    "FO_P4",
    "FO_P5",
    "FO_P6",
    "FO_P7",
    "FO_P8",
    "IMPACT",
    "ALL_VALUES",
)


def colour_of(camera_id: str, index: int) -> tuple[int, int, int]:
    if camera_id == "swing_behind":
        return (220, 25 * index, 30)
    if camera_id == "swing_side":
        return (30, 25 * index, 220)
    if camera_id == "impact":
        return (150, 150, 20)
    return (20, 150, 150)


def a_jpeg(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG", quality=95)
    return out.getvalue()


def a_real_capture(
    tmp_path: Path,
    *,
    strike_id: Any = None,
    trigger_id: str = "51",
    dtl: tuple[SwingPhase, ...] = DTL_PHASES,
    metrics: bool = True,
    published: bool = True,
) -> SwingCapture:
    """A shot as the production selector stores it: sixteen aimed stills in
    the shot's own directory, a copy in `latest_swing`, real JPEG bytes."""

    shot = tmp_path / "media" / "swing_shots" / f"20261007T120000-{trigger_id}-abcd"
    latest = tmp_path / "media" / "latest_swing"
    cameras = []
    for camera_id, view, phases, seconds in (
        ("swing_behind", PanelView.BEHIND, dtl, DTL_SECONDS),
        ("swing_side", PanelView.SIDE, FO_PHASES, FO_SECONDS),
    ):
        frames = []
        for index, (phase, offset) in enumerate(zip(phases, seconds), start=1):
            path = Path(camera_id) / f"frame_{index:03d}.jpg"
            (shot / path).parent.mkdir(parents=True, exist_ok=True)
            (shot / path).write_bytes(a_jpeg(BODY_SIZE, colour_of(camera_id, index)))
            frames.append(
                SwingFrame(
                    camera_id=camera_id,
                    index=index,
                    offset_seconds=offset,
                    path=path,
                    width=BODY_SIZE[0],
                    height=BODY_SIZE[1],
                    phase=phase,
                )
            )
        cameras.append(
            SwingCameraCapture(
                camera_id=camera_id,
                source=f"CaddieJ {camera_id}",
                ok=True,
                frames=tuple(frames),
                captured=len(frames),
                failed=0,
                kind=PanelKind.DYNAMIC,
                view=view,
                supports=BODY_CLAIMS,
            )
        )
    impact = Path("impact") / "frame_001.jpg"
    (shot / impact).parent.mkdir(parents=True, exist_ok=True)
    (shot / impact).write_bytes(a_jpeg(IMPACT_SIZE, colour_of("impact", 1)))
    cameras.append(
        SwingCameraCapture(
            camera_id="impact",
            source="CaddieJ impact",
            ok=True,
            frames=(
                SwingFrame(
                    camera_id="impact",
                    index=1,
                    offset_seconds=3.5,
                    path=impact,
                    width=IMPACT_SIZE[0],
                    height=IMPACT_SIZE[1],
                ),
            ),
            captured=1,
            failed=0,
            kind=PanelKind.STATIC,
            view=PanelView.NONE,
        )
    )
    still = None
    if metrics:
        grid = Path("metrics") / "all_values.jpg"
        (shot / grid).parent.mkdir(parents=True, exist_ok=True)
        (shot / grid).write_bytes(a_jpeg(GRID_SIZE, colour_of("metrics", 1)))
        still = MetricsStill(
            source="ProTee All Values",
            ok=True,
            path=grid,
            width=GRID_SIZE[0],
            height=GRID_SIZE[1],
            captured_at=NOW,
            delay_seconds=3.0,
            offset_seconds=3.0,
        )
    (shot / METADATA_NAME).write_text(
        json.dumps({"trigger_id": trigger_id}), encoding="utf-8"
    )
    if published:
        latest.mkdir(parents=True, exist_ok=True)
        (latest / METADATA_NAME).write_text(
            json.dumps({"trigger_id": trigger_id}), encoding="utf-8"
        )
    return SwingCapture(
        capture_id=uuid4(),
        trigger_id=trigger_id,
        directory=shot,
        started_at=NOW,
        detected_at=NOW,
        cameras=tuple(cameras),
        strike_id=strike_id,
        metrics=still,
        published=latest if published else None,
    )


def originals(capture: SwingCapture) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in capture.directory.rglob("frame_*.jpg")
    } | {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in capture.directory.rglob("all_values.jpg")
    }


async def sheet_of(capture: SwingCapture, **options: Any) -> EvidenceSheet:
    sheet = await render_evidence_sheet(capture, plan_cells(capture, 16), **options)
    assert sheet is not None
    return sheet


def pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").getpixel((x, y))  # type: ignore[return-value]


def close(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return all(abs(x - y) <= 12 for x, y in zip(a, b, strict=True))


def frames_seen(**cells: str) -> list[dict[str, str]]:
    return [{"frame_id": cell, "shows": shows} for cell, shows in cells.items()]


def at(cell: str, checkpoint: C, score: int, **extra: Any) -> dict[str, Any]:
    return a_checkpoint(checkpoint, score, **extra) | {"frame_id": cell}


async def coach_real(
    tmp_path: Path, replies: tuple[dict[str, Any], ...], **options: Any
) -> tuple[Coach, SwingCapture, ScriptedProvider]:
    provider = ScriptedProvider(replies=replies)
    options.setdefault("evidence_sheet", render_evidence_sheet)
    coach = Coach(provider, max_images=16, **options)
    await coach.service.open()
    await coach.service.on_club_selected(ClubSelected(club=DRIVER))
    detected = ShotDetected(trigger_id="51")
    await coach.service.on_shot_detected(detected)
    capture = a_real_capture(tmp_path, strike_id=detected.event_id)
    await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
    await coach.service.aclose()
    return coach, capture, provider


# --- one image -----------------------------------------------------------------


class TestOneImageGoesToOpenAI:
    async def test_exactly_one_image_and_it_is_the_sheet(self, tmp_path) -> None:
        coach, capture, provider = await coach_real(tmp_path, (a_reply(),))

        assert provider.calls == 1, "one request per shot"
        (image,) = provider.requests[0].images
        assert image.label == SHEET_LABEL
        assert image.detail == "high"
        assert image.data == (capture.directory / SHEET_FILE).read_bytes()
        assert coach.results[0].input_images == 1
        assert coach.results[0].swing_images == 16

    async def test_no_original_still_travels_beside_it(self, tmp_path) -> None:
        _, capture, provider = await coach_real(tmp_path, (a_reply(),))

        sent = {image.data for image in provider.requests[0].images}
        for path in originals(capture):
            assert path.read_bytes() not in sent, path

    async def test_the_sheet_fits_the_models_limit_without_stretching(
        self, tmp_path
    ) -> None:
        sheet = await sheet_of(a_real_capture(tmp_path))

        with Image.open(io.BytesIO(sheet.data)) as image:
            assert image.format == "JPEG"
            assert max(image.size) <= MAX_SIDE
            assert JpegImagePlugin.get_sampling(image) == 0, "4:4:4 colour"
        for cell in sheet.cells:
            placed = sheet.layout.placement(cell.cell_id)
            assert placed is not None
            assert placed.scale <= 1.0, "never enlarged"
            ratio = cell.width / cell.height
            box = placed.image
            assert abs(box.width / box.height - ratio) < 0.02, cell.cell_id


# --- the cells -----------------------------------------------------------------


class TestEveryCellUnderAStableId:
    def test_all_seventeen_cells_in_order(self, tmp_path) -> None:
        cells = plan_cells(a_real_capture(tmp_path), 16)

        assert tuple(cell.cell_id for cell in cells) == EXPECTED_CELLS

    def test_the_same_shot_always_gets_the_same_ids_and_pixels(self, tmp_path) -> None:
        capture = a_real_capture(tmp_path)

        assert plan_cells(capture, 16) == plan_cells(capture, 16)
        first = compose_sheet(
            [
                (cell, (capture.directory / cell.path).read_bytes())
                for cell in plan_cells(capture, 16)
            ]
        )
        again = compose_sheet(
            [
                (cell, (capture.directory / cell.path).read_bytes())
                for cell in plan_cells(capture, 16)
            ]
        )
        assert first.data == again.data

    def test_each_cell_shows_its_own_original(self, tmp_path) -> None:
        capture = a_real_capture(tmp_path)
        cells = plan_cells(capture, 16)
        composed = compose_sheet(
            [(cell, (capture.directory / cell.path).read_bytes()) for cell in cells]
        )

        for cell in cells:
            placed = composed.layout.placement(cell.cell_id)
            assert placed is not None
            box = placed.image
            seen = pixel(composed.data, box.x + box.width // 2, box.y + box.height // 2)
            index = (
                int(cell.frame_id.rsplit("_", 1)[1])
                if cell.kind is CellKind.BODY
                else 1
            )
            wanted = colour_of(cell.camera_id, index)
            assert close(seen, wanted), (cell.cell_id, seen, wanted)

    async def test_the_stored_table_maps_every_id_to_its_original_file(
        self, tmp_path
    ) -> None:
        _, capture, _ = await coach_real(tmp_path, (a_reply(),))

        table = stored_cells(capture.directory)
        assert tuple(table) == EXPECTED_CELLS
        for cell in plan_cells(capture, 16):
            entry = table[cell.cell_id]
            assert entry["frame_id"] == cell.frame_id
            assert entry["file"] == cell.path.as_posix()
            original = original_for_cell(capture, cell.cell_id)
            assert original == frame_path(capture, cell.frame_id)
            assert original is not None and original.is_file()
            assert original.parent.parent == capture.directory
        assert original_for_cell(capture, "DTL_P9") is None
        assert table["ALL_VALUES"]["frame_id"] == METRICS_FRAME_ID
        assert table["IMPACT"]["frame_id"] == "impact_01"

    def test_dtl_and_fo_stay_two_cameras_on_two_clocks(self, tmp_path) -> None:
        cells = {
            cell.cell_id: cell for cell in plan_cells(a_real_capture(tmp_path), 16)
        }

        for n in (1, 3, 4, 5, 6, 7, 8):
            dtl, fo = cells[f"DTL_P{n}"], cells[f"FO_P{n}"]
            assert dtl.phase is fo.phase, "aimed at the same moment"
            assert dtl.frame_id != fo.frame_id
            assert (dtl.camera_id, dtl.view) == ("swing_behind", PanelView.BEHIND)
            assert (fo.camera_id, fo.view) == ("swing_side", PanelView.SIDE)
        assert cells["DTL_P5"].offset_seconds != cells["FO_P5"].offset_seconds
        assert "FO_P2" not in cells, "face on is not aimed at the takeaway"
        assert cells["DTL_P5"].caption.startswith("~transition"), "an aim, marked"


# --- the reply, through a cell ID ---------------------------------------------


class TestTheReplyIsTranslatedBeforeItIsRead:
    def test_cell_ids_become_frame_ids_and_nothing_else_changes(self) -> None:
        mapping = {"DTL_P8": "swing_behind_08", "ALL_VALUES": "all_values"}
        payload = {
            "frames": [{"frame_id": "DTL_P8", "shows": "release"}],
            "checkpoints": {
                "release_lead_arm_structure": {"frame_id": "DTL_P8", "score": 35},
                "top_trail_elbow": None,
                "impact_arm_structure": {"frame_id": "nowhere", "score": 50},
            },
            "coach": {"evidence_frame_id": "ALL_VALUES", "observation": "DTL_P8"},
        }

        out = vx.canonical_reply(payload, mapping)

        assert out["frames"] == [{"frame_id": "swing_behind_08", "shows": "release"}]
        assert out["checkpoints"]["release_lead_arm_structure"]["frame_id"] == (
            "swing_behind_08"
        )
        assert out["checkpoints"]["top_trail_elbow"] is None
        assert out["checkpoints"]["impact_arm_structure"]["frame_id"] == "nowhere"
        assert out["coach"] == {
            "evidence_frame_id": "all_values",
            "observation": "DTL_P8",
        }
        assert payload["frames"][0]["frame_id"] == "DTL_P8", "the input is not mutated"

    async def test_the_schema_offers_cell_ids_and_never_all_values_to_a_checkpoint(
        self, tmp_path
    ) -> None:
        _, _, provider = await coach_real(tmp_path, (a_reply(),))

        request = provider.requests[0]
        schema = request.schema
        offered = listed_cells(request.evidence)
        cameras = EXPECTED_CELLS[:-2]
        assert tuple(offered) == cameras, "neither IMPACT nor ALL_VALUES"
        assert tuple(schema["properties"]["frames"]["required"]) == cameras
        dtl = [cell for cell in cameras if cell.startswith("DTL_")]
        fo = [cell for cell in cameras if cell.startswith("FO_")]
        assert citable_cells(schema, "transition_dtl") == dtl
        assert citable_cells(schema, "transition_fo") == fo
        assert citable_cells(schema, "transition_any") == list(cameras)
        assert "IMPACT, ALL_VALUES are not camera cells" in request.evidence
        coach = schema["properties"]["coach"]["properties"]
        assert tuple(coach["evidence_frame_id"]["anyOf"][1]["enum"]) == EXPECTED_CELLS
        assert "DTL_P5 ~" in request.evidence
        assert "SWING_BEHIND / DOWN_THE_LINE" not in request.evidence, "said once"
        assert "ONE image, the Swing Evidence Sheet" in request.evidence


class TestPhaseSemanticsDoNotRegress:
    async def test_a_cell_the_model_says_shows_another_moment_is_refused(
        self, tmp_path
    ) -> None:
        """DTL_P8 was AIMED at the release; the model says it shows the finish.
        The model's identification wins and no chicken wing is read from it."""

        reply = a_reply(
            checkpoints=a_swing(
                release_lead_arm_structure=at(
                    "DTL_P8",
                    C.RELEASE_LEAD_ARM_STRUCTURE,
                    30,
                    deviation="bent",
                    phase="release",
                ),
            )
        )
        reply["frames"] = frames_seen(DTL_P8="finish")
        coach, _, _ = await coach_real(tmp_path, (reply,))

        mechanics = coach.results[0].swing_score.mechanics  # type: ignore[union-attr]
        assert mechanics.score_of(C.RELEASE_LEAD_ARM_STRUCTURE) is None
        review = coach.results[0].review
        assert review is not None
        assert F.CHICKEN_WING not in {fault.fault for fault in review.faults}

    @pytest.mark.parametrize(
        ("shows", "scored"), [("transition", 35), ("downswing", None)]
    )
    async def test_an_aim_is_not_a_phase_a_checkpoint_can_borrow(
        self, tmp_path, shows, scored
    ) -> None:
        """A transition checkpoint cited from DTL_P5 -- aimed at the transition
        -- counts when the model says the cell shows the transition, and is
        refused when it says the delivery, whatever the label claims."""

        reply = a_reply(
            checkpoints={
                "transition_trail_shoulder": at(
                    "DTL_P5", C.TRANSITION_TRAIL_SHOULDER, 35, deviation="out"
                )
            }
        )
        reply["frames"] = frames_seen(DTL_P5=shows)
        coach, _, _ = await coach_real(tmp_path, (reply,))

        mechanics = coach.results[0].swing_score.mechanics  # type: ignore[union-attr]
        assert mechanics.score_of(C.TRANSITION_TRAIL_SHOULDER) == scored

    async def test_a_dtl_checkpoint_cited_from_the_fo_cell_is_refused(
        self, tmp_path
    ) -> None:
        reply = a_reply(
            checkpoints={
                "transition_shallowing": at("FO_P5", C.TRANSITION_SHALLOWING, 80)
                | {"view": "down_the_line"}
            }
        )
        coach, _, _ = await coach_real(tmp_path, (reply,))

        mechanics = coach.results[0].swing_score.mechanics  # type: ignore[union-attr]
        assert mechanics.score_of(C.TRANSITION_SHALLOWING) is None

    async def test_a_cell_that_is_not_on_the_sheet_is_refused(self, tmp_path) -> None:
        reply = a_reply(
            checkpoints={
                "release_lead_arm_structure": at(
                    "DTL_P9",
                    C.RELEASE_LEAD_ARM_STRUCTURE,
                    30,
                    deviation="bent",
                    phase="release",
                )
            }
        )
        coach, _, _ = await coach_real(tmp_path, (reply,))

        mechanics = coach.results[0].swing_score.mechanics  # type: ignore[union-attr]
        assert mechanics.score_of(C.RELEASE_LEAD_ARM_STRUCTURE) is None

    async def test_no_release_still_means_no_release_cell_and_no_release_score(
        self, tmp_path
    ) -> None:
        capture = a_real_capture(tmp_path, dtl=DTL_PHASES[:7])
        ids = [cell.cell_id for cell in plan_cells(capture, 16)]

        assert "DTL_P8" not in ids
        assert "FO_P8" in ids


class TestAChickenWingPointsAtItsOriginal:
    async def test_chicken_wing_to_checkpoint_to_cell_to_original_frame(
        self, tmp_path
    ) -> None:
        reply = a_reply(
            checkpoints=a_swing(
                release_lead_arm_structure=at(
                    "DTL_P8",
                    C.RELEASE_LEAD_ARM_STRUCTURE,
                    30,
                    deviation="bent",
                    phase="release",
                ),
                impact_arm_structure=at(
                    "DTL_P7",
                    C.IMPACT_ARM_STRUCTURE,
                    35,
                    deviation="bent",
                    phase="impact",
                ),
            )
        )
        reply["frames"] = frames_seen(DTL_P8="release", DTL_P7="impact")
        coach, capture, _ = await coach_real(tmp_path, (reply,))

        review = coach.results[0].review
        assert review is not None
        wing = next(fault for fault in review.faults if fault.fault is F.CHICKEN_WING)
        assert C.RELEASE_LEAD_ARM_STRUCTURE in wing.triggers
        mechanics = coach.results[0].swing_score.mechanics  # type: ignore[union-attr]
        release = next(
            item
            for item in mechanics.checkpoints
            if item.checkpoint is C.RELEASE_LEAD_ARM_STRUCTURE
        )
        assert release.frame_id == "swing_behind_08", "canonical, never a cell id"
        original = frame_path(capture, release.frame_id)
        assert original == capture.directory / "swing_behind" / "frame_008.jpg"
        assert original_for_cell(capture, "DTL_P8") == original
        with Image.open(original) as picture:
            assert picture.size == BODY_SIZE, "the full original, not a crop"


class TestAllValuesAndImpactKeepTheirRoles:
    async def test_all_values_is_on_the_sheet_and_its_numbers_are_read(
        self, tmp_path
    ) -> None:
        coach, capture, _ = await coach_real(tmp_path, (a_reply(),))

        assert tuple(stored_cells(capture.directory))[-1] == "ALL_VALUES"
        assert coach.results[0].metrics is not None
        assert coach.results[0].metrics_status == "read"

    async def test_a_checkpoint_cited_from_all_values_is_refused(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        reply = a_reply(
            checkpoints={
                "release_lead_arm_structure": at(
                    "ALL_VALUES",
                    C.RELEASE_LEAD_ARM_STRUCTURE,
                    30,
                    deviation="bent",
                    phase="release",
                )
            }
        )
        coach, _, _ = await coach_real(tmp_path, (reply,))

        record = next(
            r for r in caplog.records if r.getMessage() == "coach_checkpoints_assessed"
        )
        assert record.coach_checkpoints_rejections == (  # type: ignore[attr-defined]
            "release_lead_arm_structure:metrics_frame"
        )
        review = coach.results[0].review
        assert review is None or F.CHICKEN_WING not in {f.fault for f in review.faults}

    async def test_all_values_is_never_the_floors_picture(self, tmp_path) -> None:
        reply = a_reply()
        reply["coach"]["evidence_frame_id"] = "ALL_VALUES"
        coach, _, _ = await coach_real(tmp_path, (reply,))

        evidence = coach.bus.of(AnalysisCompleted)[0].evidence
        assert evidence is None or evidence.frame_id != METRICS_FRAME_ID

    async def test_the_impact_graphic_is_refused_as_a_body_as_it_always_was(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        reply = a_reply(
            checkpoints={
                "impact_arm_structure": at("IMPACT", C.IMPACT_ARM_STRUCTURE, 40)
            }
        )
        await coach_real(tmp_path, (reply,))

        record = next(
            r for r in caplog.records if r.getMessage() == "coach_checkpoints_assessed"
        )
        assert record.coach_checkpoints_rejections == (  # type: ignore[attr-defined]
            "impact_arm_structure:view_unknown"
        )

    def test_the_impact_and_grid_cells_are_not_body_cells(self, tmp_path) -> None:
        cells = {
            cell.cell_id: cell for cell in plan_cells(a_real_capture(tmp_path), 16)
        }

        assert cells["IMPACT"].kind is CellKind.PANEL
        assert cells["ALL_VALUES"].kind is CellKind.METRICS
        assert all(cells[name].kind is CellKind.BODY for name in EXPECTED_CELLS[:-2])


# --- storage -------------------------------------------------------------------


class TestTheOriginalsStayCanonical:
    async def test_every_original_is_untouched_after_coaching(self, tmp_path) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider, max_images=16, evidence_sheet=render_evidence_sheet)
        await coach.service.open()
        detected = ShotDetected(trigger_id="51")
        await coach.service.on_shot_detected(detected)
        capture = a_real_capture(tmp_path, strike_id=detected.event_id)
        before = originals(capture)
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        await coach.service.aclose()

        assert originals(capture) == before
        assert len(before) == 17
        assert (capture.directory / SHEET_FILE).is_file()

    async def test_the_sheet_is_drawn_from_the_shot_not_from_latest_swing(
        self, tmp_path
    ) -> None:
        capture = a_real_capture(tmp_path)
        assert capture.published is not None
        # Whatever latest_swing holds is an inspection copy. Make it wrong.
        (capture.published / "swing_behind").mkdir(parents=True, exist_ok=True)
        (capture.published / "swing_behind" / "frame_001.jpg").write_bytes(
            a_jpeg(BODY_SIZE, (0, 0, 0))
        )

        sheet = await sheet_of(capture)

        placed = sheet.layout.placement("DTL_P1")
        assert placed is not None
        box = placed.image
        seen = pixel(sheet.data, box.x + box.width // 2, box.y + box.height // 2)
        assert close(seen, colour_of("swing_behind", 1))
        assert sheet.file == Path(SHEET_FILE)
        assert (capture.directory / SHEET_FILE).read_bytes() == sheet.data

    async def test_latest_swing_gets_a_copy_only_while_it_is_this_shot(
        self, tmp_path
    ) -> None:
        capture = a_real_capture(tmp_path)
        assert capture.published is not None
        sheet = await sheet_of(capture)
        assert sheet.mirrored
        assert (capture.published / SHEET_FILE).read_bytes() == sheet.data

        (capture.published / SHEET_FILE).unlink()
        (capture.published / METADATA_NAME).write_text(
            json.dumps({"trigger_id": "52"}), encoding="utf-8"
        )
        again = await sheet_of(capture)
        assert not again.mirrored, "a newer shot owns latest_swing now"
        assert not (capture.published / SHEET_FILE).exists()

    async def test_the_sheet_is_pruned_with_its_shot(self, tmp_path) -> None:
        rig = Rig(tmp_path, ReplayObs())
        coach = Coach(ScriptedProvider(replies=(a_reply(),) * (EVIDENCE_KEEP + 2)))
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        shots: list[Path] = []
        for trigger in range(EVIDENCE_KEEP + 2):
            detected = ShotDetected(trigger_id=str(60 + trigger), source="protee_log")
            await coach.service.on_shot_detected(detected)
            capture = await rig.strike(detected)
            assert capture is not None
            await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
            async with asyncio.timeout(2.0):
                while len(coach.results) <= trigger:
                    await asyncio.sleep(0.01)
            shots.append(capture.directory)
        await coach.service.aclose()

        root = rig.store.evidence_root
        remaining = sorted(path for path in root.iterdir() if path.is_dir())
        assert len(remaining) <= EVIDENCE_KEEP
        assert remaining == shots[-len(remaining) :]
        for shot in shots[: -len(remaining)]:
            assert not shot.exists(), "pruned whole, sheet and all"
        for shot in remaining[:-1]:
            assert (shot / SHEET_FILE).is_file()
        newest = shots[-1]
        assert (newest / SHEET_FILE).is_file()
        assert stored_cells(newest), "the cell table travels with the shot"


# --- newest shot wins ----------------------------------------------------------


class TestANewerStrikeStillWins:
    async def test_a_strike_during_drawing_supersedes_the_old_request(
        self, tmp_path
    ) -> None:
        provider = ScriptedProvider(replies=(a_reply(),))
        holder: dict[str, Coach] = {}

        async def slow_sheet(capture: SwingCapture, cells: Any) -> EvidenceSheet | None:
            sheet = await placeholder_sheet(capture, cells)
            await holder["coach"].service.on_shot_detected(
                ShotDetected(trigger_id="99")
            )
            return sheet

        coach = Coach(provider, evidence_sheet=slow_sheet)
        holder["coach"] = coach
        await coach.service.open()
        detected = ShotDetected(trigger_id="51")
        await coach.service.on_shot_detected(detected)
        capture = a_real_capture(tmp_path, strike_id=detected.event_id)
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        async with asyncio.timeout(2.0):
            while not coach.bus.of(AnalysisFailed) and not provider.calls:
                await asyncio.sleep(0.01)
        await coach.service.aclose()

        assert provider.calls == 0, "the old shot never reaches OpenAI"
        failed = coach.bus.of(AnalysisFailed)
        assert [event.reason for event in failed] == [AdviceFailure.SUPERSEDED]
        assert not coach.bus.of(AnalysisCompleted)


# --- a sheet that cannot be drawn ---------------------------------------------


def a_broken_compose(error: Exception):
    def compose(items: Any, max_side: int = MAX_SIDE) -> ComposedSheet:
        raise error

    return compose


class TestASheetThatCannotBeDrawn:
    @pytest.mark.parametrize(
        "error",
        [ImportError("No module named 'PIL'"), OSError("cannot identify image file")],
    )
    async def test_the_numbers_still_travel_and_every_checkpoint_is_null(
        self, tmp_path, error
    ) -> None:
        async def broken(capture: SwingCapture, cells: Any) -> EvidenceSheet | None:
            return await render_evidence_sheet(
                capture, cells, compose=a_broken_compose(error)
            )

        coach, capture, provider = await coach_real(
            tmp_path, (a_reply(),), evidence_sheet=broken
        )

        assert provider.calls == 1
        (image,) = provider.requests[0].images
        assert image.label == "ALL_VALUES"
        assert image.frame_id == "ALL_VALUES"
        assert (
            image.data
            == (capture.directory / "metrics" / "all_values.jpg").read_bytes()
        )
        result = coach.results[0]
        assert result.status == "ok"
        assert result.metrics is not None
        assert result.swing_images == 0
        mechanics = result.swing_score.mechanics if result.swing_score else None
        assert mechanics is None or mechanics.value is None
        assert not (capture.directory / SHEET_FILE).exists()
        payload = json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))
        assert payload["coach_request"]["transport"] == "all_values_only"
        assert payload["evidence_sheet"] == {"ok": False}

    async def test_a_renderer_that_raises_never_costs_the_shot(self, tmp_path) -> None:
        async def exploding(capture: SwingCapture, cells: Any) -> EvidenceSheet | None:
            raise RuntimeError("disk on fire")

        coach, _, provider = await coach_real(
            tmp_path, (a_reply(),), evidence_sheet=exploding
        )

        assert provider.calls == 1
        assert [image.label for image in provider.requests[0].images] == ["ALL_VALUES"]
        assert coach.bus.of(AnalysisCompleted)

    async def test_no_sheet_and_no_numbers_is_no_request(self, tmp_path) -> None:
        async def nothing(capture: SwingCapture, cells: Any) -> EvidenceSheet | None:
            return None

        provider = ScriptedProvider(replies=(a_reply(),))
        coach = Coach(provider, evidence_sheet=nothing)
        await coach.service.open()
        detected = ShotDetected(trigger_id="51")
        await coach.service.on_shot_detected(detected)
        capture = a_real_capture(tmp_path, strike_id=detected.event_id, metrics=False)
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        await coach.service.aclose()

        assert provider.calls == 0
        assert coach.results[0].status != "ok"

    async def test_an_unreadable_original_is_left_off_rather_than_guessed(
        self, tmp_path
    ) -> None:
        capture = a_real_capture(tmp_path)
        (capture.directory / "swing_side" / "frame_004.jpg").write_bytes(b"garbage")

        sheet = await sheet_of(capture)

        ids = [cell.cell_id for cell in sheet.cells]
        assert "FO_P5" not in ids, "no ID is printed without a picture under it"
        assert "FO_P5" in sheet.dropped
        assert len(ids) == 16

    async def test_a_sheet_that_cannot_be_stored_still_travels(
        self, tmp_path, monkeypatch
    ) -> None:
        import caddiej.swing_capture.sheet as sheet_module

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise PermissionError("locked")

        monkeypatch.setattr(sheet_module.os, "replace", refuse)
        sheet = await sheet_of(a_real_capture(tmp_path))

        assert sheet.data
        assert not sheet.stored
        assert sheet.file is None


class TestThePlaceholderHelper:
    """The placeholder other tests use lays out the same cells for real."""

    async def test_it_carries_the_same_cells_as_the_real_sheet(self, tmp_path) -> None:
        capture = a_real_capture(tmp_path)
        cells = plan_cells(capture, 16)

        real = await render_evidence_sheet(capture, cells)
        fake = await placeholder_sheet(capture, cells)

        assert real is not None and fake is not None
        assert sheet_cells(fake.data) == tuple(cell.cell_id for cell in real.cells)
        assert (fake.width, fake.height) == (real.width, real.height)
