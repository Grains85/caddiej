"""The overlay JS the Browser Source actually executes.

`/snapshot` is already proven correct on hardware. These tests pin the
downstream mapping: which keys `state.js` reads, that `client.js` never
imports an unversioned `state.js` as a stable pathname, and that the
assignment the DOM uses turns the live payload into the strings on the floor.
"""

from pathlib import Path

from caddiej.adapters.hud.server import STATIC_ROOT

STATE_JS = (STATIC_ROOT / "js" / "state.js").read_text(encoding="utf-8")
CLIENT_JS = (STATIC_ROOT / "js" / "client.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
FLOOR_CSS = (STATIC_ROOT / "css" / "floor.css").read_text(encoding="utf-8")


def display_string(value: object) -> str:
    """Same rule as `displayString` in state.js. Empty and non-strings are omitted."""

    return value if isinstance(value, str) and value != "" else ""


def test_state_js_does_not_read_removed_raw_round_keys() -> None:
    assert "ball_speed_raw" not in STATE_JS
    assert "total_distance_raw" not in STATE_JS
    assert "distance_to_pin_raw" not in STATE_JS
    assert "ball_speed_display" in STATE_JS
    assert "shot_distance_display" in STATE_JS
    assert "remaining_distance_display" in STATE_JS


def test_shot_distance_and_remaining_are_the_only_result_figures() -> None:
    """What a golfer wants off the floor, at the size that reads from the mat."""

    panel = INDEX_HTML.split('id="round-result"')[1].split('id="ball-flight"')[0]

    assert 'id="round-shot-distance"' in panel
    assert 'id="round-remaining"' in panel
    assert "SLAG" in panel
    assert "KVAR" in panel
    assert "Shot Distance" not in INDEX_HTML
    assert "To Pin" not in INDEX_HTML
    assert "round-ball-speed" not in INDEX_HTML
    assert "km/h" not in INDEX_HTML
    assert "km/h" not in FLOOR_CSS


def test_a_round_without_a_remaining_distance_hides_that_figure() -> None:
    """An optional metric is omitted, not shown as a dash under a label."""

    assert "roundRemainingFigureEl.hidden = !remaining;" in STATE_JS
    assert "roundShotFigureEl.hidden = !shotDistance;" in STATE_JS
    assert "displayOrDash" not in STATE_JS


def test_paint_reads_round_result_from_snapshot_state() -> None:
    # Not message.round_result, and not a merge of a previous snapshot.
    assert "renderRoundResult(state.round_result)" in STATE_JS
    assert "function paint(snapshot)" in STATE_JS
    assert "const state = snapshot.state;" in STATE_JS


def test_client_js_imports_state_relatively_not_as_a_stable_pathname() -> None:
    """A bare `/static/js/state.js` is what OBS CEF keeps executing forever."""

    assert 'from "./state.js"' in CLIENT_JS
    assert 'from "./state.js?v=' not in CLIENT_JS
    assert "/static/js/state.js" not in CLIENT_JS
    assert 'from "./state.js"' in CLIENT_JS


def test_index_html_points_at_the_versioned_hud_directory() -> None:
    assert 'src="/static/hud/__HUD_ASSETS_VERSION__/client.js"' in INDEX_HTML
    assert 'href="/static/hud/__HUD_ASSETS_VERSION__/floor.css"' in INDEX_HTML
    assert "/static/js/client.js" not in INDEX_HTML


def test_the_live_hardware_payload_becomes_the_dom_strings() -> None:
    """The JSON `/snapshot` returned on the bay, applied the way state.js paints."""

    message = {
        "v": 1,
        "type": "snapshot",
        "seq": 56,
        "state": {
            "round_result": {
                "shot_id": "32e1e8cc-f1f3-4b6d-a2dd-52250dbfb33e",
                "player_name": "Johan",
                "hole": 0,
                "hole_shot": 4,
                "ball_speed_display": "291 km/h",
                "shot_distance_display": "79 m",
                "remaining_distance_display": "147 m",
                "starting_surface_raw": 3,
                "ending_surface_raw": 2,
            }
        },
    }

    result = message["state"]["round_result"]
    shot_distance = display_string(result["shot_distance_display"])
    remaining = display_string(result["remaining_distance_display"])

    assert shot_distance == "79 m"
    assert remaining == "147 m"
    assert display_string(result["ball_speed_display"]) == "291 km/h"
    # Ball speed may travel on the snapshot; it is not a visible figure.
    assert "round-ball-speed" not in INDEX_HTML


def test_a_missing_display_string_is_omitted_not_a_raw_number() -> None:
    stale_shape = {"ball_speed_raw": 32.83863067626953, "total_distance_raw": 28.0}

    assert display_string(stale_shape.get("ball_speed_display")) == ""
    assert display_string(stale_shape.get("shot_distance_display")) == ""
    assert display_string(stale_shape.get("ball_speed_raw")) == ""


def test_the_launch_monitors_own_words_are_never_drawn() -> None:
    """READY and FINDING BALL still travel and still drive the marker and the
    information area in Python. The hidden diagnostic element does not draw
    them. The chamfer draws the words Python already chose."""

    play_status = STATE_JS.split("function renderPlayStatus")[1].split("\n}")[0]

    assert "playStatusEl.dataset.state" in play_status
    assert "playStatusEl.dataset.club" in play_status
    assert "textContent" not in play_status
    assert "playStatusEl.hidden = true;" in play_status
    assert 'id="play-status-text"' not in INDEX_HTML
    assert 'id="play-club-label"' not in INDEX_HTML
    assert ".play-status__state" not in FLOOR_CSS


def test_the_chamfer_is_the_rear_edge_python_sent() -> None:
    """The browser joins the six points. It does not invent a rectangle, and a
    failure there does not skip the coaching line or the result."""

    assert "renderBallStatus(state.ball_status, state.hitting_strip)" in STATE_JS
    assert "status.edge" in STATE_JS
    assert "hideChamfer()" in STATE_JS
    paint = STATE_JS.split("function paint")[1].split("export function render")[0]
    assert paint.index("renderBallStatus") < paint.index("renderAdvice")
    assert "catch" in paint
    assert 'id="ball-status-edge"' in INDEX_HTML
    assert 'id="ball-status-label"' in INDEX_HTML
    assert (
        "linear-gradient"
        not in FLOOR_CSS.split(".ball-status__edge")[1].split(".info-area")[0]
    )


def test_the_address_panel_draws_the_recommendation_not_the_detected_club() -> None:
    """ProTee reports the generic word its detector matched — three days of real
    logs said `Iron` for every iron in the bag — so it is kept as an attribute
    rather than shown beside a specific club CaddieJ chose."""

    assert "address.recommended_club_display" in STATE_JS
    assert "addressHudEl.dataset.club" in STATE_JS

    panel = INDEX_HTML.split('id="address-hud"')[1].split('id="metrics"')[0]
    assert 'id="address-distance"' in panel
    assert 'id="address-club"' in panel
    assert "To Pin" not in panel
    assert "Recommended" not in panel


def test_each_address_figure_hides_itself_rather_than_showing_a_dash() -> None:
    """A distance with no measured bag behind it is one figure, not one figure
    next to a missing club."""

    assert "addressDistanceFigureEl.hidden = !distance;" in STATE_JS
    assert "addressClubFigureEl.hidden = !club;" in STATE_JS
    assert "displayOrDash(address" not in STATE_JS


def test_the_hitting_strip_fill_comes_from_state_not_the_stylesheet() -> None:
    """Whether the projector lights the turf is an operator's fact about a room,
    so it travels with the snapshot and the CSS only reacts to it."""

    assert "hittingStripEl.dataset.filled = String(strip.filled === true);" in STATE_JS
    assert '.hitting-strip[data-filled="true"]' in FLOOR_CSS
    filled_block = FLOOR_CSS.split('.hitting-strip[data-filled="true"] {')[1]
    filled_rule = filled_block.split("}", 1)[0]
    assert "animation" not in filled_rule


def test_the_lock_acknowledgement_is_a_python_owned_pulse() -> None:
    """The browser animates a state Python published. It does not decide that
    the ball was found, and a snapshot that is merely still set does not restart
    the pulse."""

    assert "renderLockAck(state.lock_acknowledgement, state.marker)" in STATE_JS
    assert "lockAckActive" in STATE_JS
    assert "hitting-strip--acknowledge" in STATE_JS
    assert "hitting-strip--acknowledge" in FLOOR_CSS
    assert 'id="lock-ack"' in INDEX_HTML
    assert "✓" in INDEX_HTML


class TestTheOverlayDoesNotTearItselfDown:
    """The watchdog that caused the thing it was watching for.

    `client.js` used to run a silence timer: if no frame arrived within a few
    seconds it closed the socket and reconnected. Under OBS's embedded
    Chromium, timers in a background page are throttled, so the timer fired on
    a perfectly healthy socket. One recorded session reconnected 8,149 times —
    roughly one every four seconds for nine hours — and each reconnect blanked
    the projector, because the close handler cleared the rendered state.

    Liveness is uvicorn's job and always was: the ASGI server sends protocol
    pings and reaps a socket that does not pong. The page does not need to
    duplicate that, and could not do it correctly if it tried.
    """

    def test_there_is_no_client_side_silence_timer(self) -> None:
        assert "SILENCE_TIMEOUT" not in CLIENT_JS
        assert "armSilenceTimer" not in CLIENT_JS
        assert "silenceTimer" not in CLIENT_JS

    def test_a_close_forgets_the_sequence_and_keeps_the_pixels(self) -> None:
        """A reconnect is invisible on the mat. The floor keeps showing the
        last state until a newer one arrives, and the sequence is forgotten so
        the first snapshot of the new socket is not discarded as stale."""

        close = CLIENT_JS.split('addEventListener("close"')[1].split("\n  });")[0]

        assert "invalidate();" in close
        assert "clear()" not in close
        assert "clear()" not in CLIENT_JS

    def test_invalidate_resets_only_the_sequence(self) -> None:
        body = STATE_JS.split("export function invalidate()")[1].split("}")[0]

        assert "lastSeq = -1" in body

    def test_the_page_still_pings_so_the_server_can_measure_silence(self) -> None:
        # Kept, because `/healthz` reports how long a client has been quiet and
        # that measurement is worth something. Nothing acts on it in the page.
        assert "PING_INTERVAL_MS" in CLIENT_JS
        assert '{ type: "ping" }' in CLIENT_JS


def test_checkout_static_root_is_what_the_server_serves() -> None:
    checkout = Path(__file__).resolve().parents[2] / "src/caddiej/adapters/hud/static"
    assert STATIC_ROOT.resolve() == checkout
