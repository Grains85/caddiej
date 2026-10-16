"""Does the coaching fit on the actual projector, in actual pixels.

The first hardware test produced a floor with a decapitated score, a clipped
instruction and half-height labels, and every layout test in the suite passed
while it did. They were all asking the same question -- do the regions divide
the space correctly and stay off the mat -- and the answer was yes. Nothing was
asking whether the words fitted in the regions, because the regions were
computed in Python and the words were sized in a stylesheet, and no test
looked at both.

This file looks at both. It takes the real bay's strip, the real projector's
1024 x 768, the region shares from `coach_layout` and the type scales from
`floor.css`, and works out how tall each line actually is in pixels. The
numbers it checks against are measured: the failures it would have caught were
the score asking for 78 pixels of a 69-pixel box, the instruction 93 of 64 and
the readings 45 of 37.

The type scales are parsed out of the stylesheet rather than written down
again. A test that restates the CSS proves the test and the CSS agree with the
test, which is what the old region tests were doing one level up. Parsing means
that changing a multiplier in `floor.css` without room for it fails here, and
that is the only arrangement under which this file is worth having.

What it cannot do is lay out text. It has no font metrics, so it works in line
boxes -- font size times line height -- which is what decides whether a stack
of lines fits its container and is exactly the quantity that overflowed. Glyph
shapes, letter spacing and the width of a particular word are outside it; those
are checked against the browser by hand, and the measurements are in the
hardware-correction notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.unit.test_coach_v3 import THE_94_SHOT

from caddiej.coaching.metrics_strip import strip_metrics
from caddiej.coaching.vx import parse_metrics
from caddiej.floor.chamfer import chamfer_depth
from caddiej.floor.coach_layout import (
    EVIDENCE_WIDTH,
    MARGIN,
    TEXT_WIDTH,
    CoachRegions,
    coach_regions,
)
from caddiej.floor.geometry import DEFAULT_CALIBRATION, PhysicalRect, project_rect

# The projector in the bay the hardware test ran in. Not a round number chosen
# for arithmetic: 4:3 is what made width-sized type and height-sized boxes
# disagree badly enough to clip, and a 16:9 test would have missed it.
VIEWPORT = (1024, 768)

# The bay itself, from its own configuration: a 0.55 x 0.315 m strip 0.65 m up
# a 1 x 1 m floor. This leaves 265 of the 768 lines for the whole result, which
# is the constraint the composition has to work inside.
REAL_BAY = PhysicalRect(x_m=0.52, y_m=0.65, width_m=0.55, depth_m=0.315)

FLOOR_CSS = (
    Path(__file__).resolve().parents[2]
    / "src/caddiej/adapters/hud/static/css/floor.css"
).read_text(encoding="utf-8")

# Readable from standing height on a floor, rather than on a monitor at desk
# distance. The old metric label was fifteen pixels and the player could not
# read it; eighteen is the floor this pass is willing to claim, and the labels
# land well above it.
SMALLEST_READABLE = 18.0


def css_rule(selector: str) -> str:
    """Every declaration the stylesheet applies to one selector.

    All of the blocks it appears in, not the first: a selector that shares a
    grouped rule for its alignment and has its own for its box would otherwise
    be read as having only the alignment, which is a test passing because it
    looked in the wrong place.
    """

    found = [
        block.split("\n}", 1)[0]
        for block in FLOOR_CSS.split(f"{selector} {{\n")[1:]
        # A grouped selector list ends in a comma, so the text after the split
        # is the next selector rather than the block. Those are the same rule,
        # reached through its last selector instead.
        if not block.split("\n}", 1)[0].lstrip().startswith(".")
    ]
    assert found, f"no rule for {selector}"
    return "\n".join(found)


def scale_of(selector: str) -> float:
    """The multiplier a region rule applies to its box height.

    Parsed, not restated. `font-size: calc(var(--region-px) * 0.74)` is the
    contract between this file and the stylesheet, and reading it is what makes
    a change to the number fail here instead of on the projector.
    """

    rule = css_rule(f'.advice[data-regions="true"] {selector}')
    match = re.search(r"font-size:\s*calc\(var\(--region-px\)\s*\*\s*([\d.]+)\)", rule)
    assert match is not None, f"{selector} is not sized from its own region"
    return float(match.group(1))


def line_height_of(selector: str, default: float = 1.2) -> float:
    """The rule's line height, or the browser's rough default for `normal`."""

    rule = css_rule(f'.advice[data-regions="true"] {selector}')
    match = re.search(r"line-height:\s*([\d.]+)", rule)
    return default if match is None else float(match.group(1))


@pytest.fixture
def regions() -> CoachRegions:
    strip = project_rect(REAL_BAY, DEFAULT_CALIBRATION)
    laid = coach_regions(
        strip, chamfer_depth=chamfer_depth(REAL_BAY, DEFAULT_CALIBRATION, 6.0)
    )
    assert laid is not None, "the real bay has room above its mat"
    return laid


def pixels(rect, *, viewport: tuple[int, int] = VIEWPORT) -> tuple[float, ...]:
    width, height = viewport
    return (
        rect.x * width,
        rect.y * height,
        rect.width * width,
        rect.height * height,
    )


def box_height(rect, *, viewport: tuple[int, int] = VIEWPORT) -> float:
    return rect.height * viewport[1]


# --- 11: the score is all there -------------------------------------------


class TestTheScoreIsCompletelyVisible:
    def test_its_box_is_inside_the_projector(self, regions) -> None:
        x, y, width, height = pixels(regions.score)

        assert y >= 0.0
        assert x >= 0.0
        assert y + height <= VIEWPORT[1]
        assert x + width <= VIEWPORT[0]

    def test_the_number_fits_the_box_it_is_given(self, regions) -> None:
        # 123 pixels of digits in a 69-pixel box was the hardware failure.
        height = box_height(regions.score)
        value = height * scale_of(".advice__score-value")

        assert value * line_height_of(".advice__score-value") <= height

    def test_the_label_and_the_number_share_one_line(self, regions) -> None:
        # Stacked, the label took a quarter of the box and the figure needed
        # all of it. On one baseline the figure gets the height and the floor
        # reads "SWING SCORE 65" as a sentence.
        height = box_height(regions.score)
        tallest = max(
            height * scale_of(selector) * line_height_of(selector)
            for selector in (
                ".advice__score-label",
                ".advice__score-value",
                ".advice__score-scale",
            )
        )

        assert tallest <= height
        assert "flex-direction: row;" in css_rule(
            '.advice[data-regions="true"] #advice-score'
        )

    def test_the_number_is_the_largest_thing_on_the_floor(self, regions) -> None:
        score = box_height(regions.score) * scale_of(".advice__score-value")
        action = box_height(regions.observation) * scale_of(".advice__text")

        assert score > action


# --- 12, 13: nothing is drawn on top of anything else ----------------------


class TestNothingOverlapsAnythingElse:
    def test_no_two_regions_share_a_pixel(self, regions) -> None:
        assert regions.are_disjoint()

    def test_nothing_reaches_the_mat_or_the_ball_status_band(self, regions) -> None:
        assert regions.clears_protected()

    def test_the_still_is_beside_the_words_and_never_behind_them(self, regions) -> None:
        # The old evidence region was the full width of the image, so the
        # picture sat in a band across everything. Its panel now starts after
        # the text column ends.
        assert regions.score.right <= regions.evidence.x
        assert regions.observation.right <= regions.evidence.x

    def test_the_readings_are_below_all_three(self, regions) -> None:
        assert regions.score.bottom <= regions.metrics.y
        assert regions.observation.bottom <= regions.metrics.y
        assert regions.evidence.bottom <= regions.metrics.y

    def test_there_is_a_gutter_between_the_words_and_the_still(self, regions) -> None:
        gutter = (regions.evidence.x - regions.observation.right) * VIEWPORT[0]

        assert gutter > 0.0


# --- 14, 15: the still has its own panel, at its own shape -----------------


class TestTheStillGetsItsOwnPanel:
    def test_the_panel_is_a_third_of_the_width(self, regions) -> None:
        assert regions.evidence.width == pytest.approx(EVIDENCE_WIDTH)
        assert regions.score.width == pytest.approx(TEXT_WIDTH)

    def test_it_is_tall_enough_to_recognise_a_swing_in(self, regions) -> None:
        _, _, width, height = pixels(regions.evidence)

        # Measured on the projector: 338 x 196, which a 1792 x 1480 frame from
        # behind fills to 237 x 196. Small enough to be a panel and large
        # enough to see which way the shoulder went.
        assert width >= 300.0
        assert height >= 180.0

    def test_the_picture_keeps_its_own_shape_inside_it(self) -> None:
        rule = css_rule('.advice[data-regions="true"] #advice-evidence')

        assert "object-fit: contain;" in rule
        # The unanchored column's viewport-sized height must not survive into
        # a region, or the panel is sized by the projector and the box by the
        # mat, and the two have no reason to agree.
        assert "height: calc(var(--region-h) * 100%);" in rule

    def test_it_is_the_full_height_of_the_text_beside_it(self, regions) -> None:
        text = regions.score.height + regions.observation.height

        assert regions.evidence.height == pytest.approx(text)


# --- 16: the grid of numbers is never the player's picture -----------------


class TestTheAllValuesGridIsNotAPanel:
    def test_the_overlay_has_one_evidence_element_and_it_is_the_frame(self) -> None:
        # Belt and braces beside the service-level test: whatever the coach
        # resolves, the floor has exactly one place a picture can appear and
        # it is fed an evidence frame id.
        index = (
            Path(__file__).resolve().parents[2]
            / "src/caddiej/adapters/hud/static/index.html"
        ).read_text(encoding="utf-8")

        assert index.count('id="advice-evidence"') == 1
        assert "all_values" not in index.lower()


# --- 17, 18: four readings, readable, and gone cleanly when absent ---------


class TestTheReadingsAreReadable:
    def test_the_label_and_the_value_both_fit_their_box(self, regions) -> None:
        # 45 pixels of label and value in a 37-pixel box clipped both.
        height = box_height(regions.metrics)
        stacked = sum(
            height * scale_of(selector) * line_height_of(selector, 1.1)
            for selector in (".advice__metric-label", ".advice__metric-value")
        )

        assert stacked <= height

    def test_the_label_is_large_enough_to_read_standing_up(self, regions) -> None:
        label = box_height(regions.metrics) * scale_of(".advice__metric-label")

        assert label >= SMALLEST_READABLE

    def test_and_larger_than_it_was_before_the_hardware_test(self, regions) -> None:
        # The old rule was clamp(0.7rem, 1.5vw, 1.5rem), which is 15.36px on a
        # 1024-wide projector. The player could not read it from the mat.
        label = box_height(regions.metrics) * scale_of(".advice__metric-label")

        assert label > 15.36

    def test_the_value_is_larger_than_the_label_naming_it(self, regions) -> None:
        height = box_height(regions.metrics)
        label = height * scale_of(".advice__metric-label")
        value = height * scale_of(".advice__metric-value")

        assert value > label

    def test_there_is_width_for_four_of_them(self, regions) -> None:
        _, _, width, _ = pixels(regions.metrics)

        # Four cells across the full width, less the edge margins. The longest
        # value in the strip is "131.3 km/h" at ten characters.
        assert width / 4.0 >= 200.0

    def test_a_device_that_did_not_report_one_leaves_three(self, regions) -> None:
        """A missing reading costs a cell, not the strip.

        `strip_metrics` drops a null rather than writing a dash, so the floor
        can be handed three. `space-between` spreads whatever it is given, so
        the only question is whether each cell is still wide enough to read,
        and three cells are wider than four.
        """

        shown = strip_metrics(parse_metrics(dict(THE_94_SHOT, carry=None)))
        _, _, width, _ = pixels(regions.metrics)

        assert len(shown) == 3
        assert width / len(shown) >= 200.0
        assert "justify-content: space-between;" in css_rule(
            '.advice[data-regions="true"] #advice-metrics'
        )

    def test_the_strip_spans_the_floor_rather_than_the_text_column(
        self, regions
    ) -> None:
        assert regions.metrics.width == pytest.approx(1.0 - 2 * MARGIN)
        assert regions.metrics.width > regions.score.width


# --- the words themselves, in the box they are given ----------------------


class TestTheObservationAndTheActionBothFit:
    def test_two_lines_of_words_fit_their_region(self, regions) -> None:
        # 93 pixels of observation and instruction in a 64-pixel box clipped
        # both, which is how "FACE OPEN" arrived cut off against the picture.
        height = box_height(regions.observation)
        stacked = sum(
            height * scale_of(selector) * line_height_of(selector)
            for selector in (".advice__observation", ".advice__text")
        )

        assert stacked <= height

    def test_the_action_is_larger_than_the_observation(self, regions) -> None:
        # What to do next is the point; what was seen is the reason for it.
        height = box_height(regions.observation)

        assert scale_of(".advice__text") > scale_of(".advice__observation")
        assert height * scale_of(".advice__text") >= 40.0

    def test_the_observation_no_longer_carries_its_own_margins(self) -> None:
        # Margins in em of a line whose size comes from the box were the other
        # half of that overflow: the box grew the type and the type grew the
        # margins and both counted against the same height.
        rule = css_rule('.advice[data-regions="true"] .advice__observation')

        assert "margin: 0;" in rule


# --- the same arithmetic on bays that are not this one ---------------------


class TestItHoldsOnOtherBaysAndOtherProjectors:
    @pytest.mark.parametrize(
        "strip",
        [
            PhysicalRect(x_m=0.52, y_m=0.65, width_m=0.55, depth_m=0.315),
            PhysicalRect(x_m=0.5, y_m=0.5, width_m=1.0, depth_m=0.4),
            PhysicalRect(x_m=0.3, y_m=0.8, width_m=0.4, depth_m=0.2),
            PhysicalRect(x_m=0.8, y_m=0.72, width_m=0.6, depth_m=0.25),
        ],
    )
    @pytest.mark.parametrize("viewport", [(1024, 768), (1920, 1080), (1280, 800)])
    def test_every_line_fits_every_box(self, strip, viewport) -> None:
        """The point of sizing type from its box: this cannot fail.

        It is asserted anyway, over four bays and three projectors, because
        "cannot fail" was also true of the old arrangement right up until a
        4:3 projector met a mat two thirds of the way up its image.
        """

        laid = coach_regions(
            project_rect(strip, DEFAULT_CALIBRATION),
            chamfer_depth=chamfer_depth(strip, DEFAULT_CALIBRATION, 6.0),
        )
        assert laid is not None

        for rect, selectors in (
            (laid.score, (".advice__score-value",)),
            (laid.observation, (".advice__observation", ".advice__text")),
            (laid.metrics, (".advice__metric-label", ".advice__metric-value")),
        ):
            height = box_height(rect, viewport=viewport)
            stacked = sum(
                height * scale_of(selector) * line_height_of(selector)
                for selector in selectors
            )
            assert stacked <= height + 1e-9

        assert laid.are_disjoint()
        assert laid.clears_protected()
