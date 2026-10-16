/* Rendering only.
 *
 * This module writes the server's snapshot to the DOM. It derives nothing about
 * the round: positions and the club path arrive as calibrated surface
 * coordinates (0..1) and are converted to pixels here. Golf geometry — path
 * angles, metres — has already been done. This file owns only pixels.
 *
 * The last snapshot is kept solely so the surface can be repainted when the
 * window or projector output changes size. It is cleared on disconnect, so a
 * reconnect still renders nothing until the server has spoken. */

const surfaceEl = document.getElementById("surface");
const floorEl = document.getElementById("floor");
const statusEl = document.getElementById("status");
const statusTextEl = document.getElementById("status-text");
const playStatusEl = document.getElementById("play-status");
const addressHudEl = document.getElementById("address-hud");
const addressDistanceEl = document.getElementById("address-distance");
const addressDistanceFigureEl = document.getElementById("address-distance-figure");
const addressClubEl = document.getElementById("address-club");
const addressClubFigureEl = document.getElementById("address-club-figure");
const markerEl = document.getElementById("marker");
const markerHaloEl = document.getElementById("marker-halo");
const markerRingEl = document.getElementById("marker-ring");
const markerBallEl = document.getElementById("marker-ball");
const hittingStripEl = document.getElementById("hitting-strip");
const stripSidesEl = document.getElementById("strip-sides");
const ballStatusEdgeEl = document.getElementById("ball-status-edge");
const ballStatusEl = document.getElementById("ball-status");
const ballStatusLampEl = document.getElementById("ball-status-lamp");
const ballStatusLampCoreEl = document.getElementById("ball-status-lamp-core");
const ballStatusLabelEl = document.getElementById("ball-status-label");
const puttingLaneEl = document.getElementById("putting-lane");
const puttingLaneLineEl = document.getElementById("putting-lane-line");
const puttPathEl = document.getElementById("putt-path");
const lockAckEl = document.getElementById("lock-ack");
const lockAckMarkEl = document.getElementById("lock-ack-mark");
const clubPathEl = document.getElementById("club-path");
const metricsEl = document.getElementById("metrics");
const roundResultEl = document.getElementById("round-result");
const roundShotDistanceEl = document.getElementById("round-shot-distance");
const roundShotFigureEl = document.getElementById("round-shot-figure");
const roundRemainingEl = document.getElementById("round-remaining");
const roundRemainingFigureEl = document.getElementById("round-remaining-figure");
const ballFlightEl = document.getElementById("ball-flight");
const adviceEl = document.getElementById("advice");
const adviceTextEl = document.getElementById("advice-text");
const adviceCueEl = document.getElementById("advice-cue");
const adviceEvidenceEl = document.getElementById("advice-evidence");
const adviceScoreEl = document.getElementById("advice-score");
const adviceScoreValueEl = document.getElementById("advice-score-value");
const adviceObservationEl = document.getElementById("advice-observation");
const adviceMetricsEl = document.getElementById("advice-metrics");
const progressEl = document.getElementById("progress");
const progressLabelEl = document.getElementById("progress-label");
const flourishEl = document.getElementById("flourish");
const flourishLabelEl = document.getElementById("flourish-label");

/* Marker sizes as a fraction of the smaller surface dimension, so the marker
   keeps its proportions on any projector. The driver zone reads as the widest
   because it is where the ball must be furthest forward. */
const MARKER_SCALE = {
  driver: { halo: 0.085, ring: 0.055, ball: 0.014 },
  normal: { halo: 0.06, ring: 0.038, ball: 0.014 },
  putter: { halo: 0.04, ring: 0.024, ball: 0.012 },
};

let lastSeq = -1;
let lastSnapshot = null;
let surface = { width: 0, height: 0 };
/* Which celebration is currently on screen, so a repaint or an unrelated
   snapshot does not restart one that is already running. */
let flourishKey = "";
/* True while Python is holding a lock acknowledgement, so a snapshot that is
   merely still set does not restart the pulse. */
let lockAckActive = false;

function measureSurface() {
  surface = {
    width: surfaceEl.clientWidth,
    height: surfaceEl.clientHeight,
  };
  // A pixel viewBox is what keeps circles circular on a stretched surface.
  // Calibration lives in Python: these pixels are the last stretch, not a
  // place to invent a path endpoint.
  floorEl.setAttribute("viewBox", `0 0 ${surface.width} ${surface.height}`);
}

function toPixels(point) {
  return { x: point.x * surface.width, y: point.y * surface.height };
}

function markerUnit() {
  return Math.min(surface.width, surface.height);
}

function renderMarker(marker) {
  markerEl.dataset.variant = marker.variant;
  markerEl.dataset.visible = String(marker.visible);

  const centre = toPixels(marker.position);
  const scale = MARKER_SCALE[marker.variant] ?? MARKER_SCALE.normal;
  const unit = markerUnit();

  for (const [element, fraction] of [
    [markerHaloEl, scale.halo],
    [markerRingEl, scale.ring],
    [markerBallEl, scale.ball],
  ]) {
    element.setAttribute("cx", centre.x);
    element.setAttribute("cy", centre.y);
    element.setAttribute("r", unit * fraction);
  }
}

function renderHittingStrip(strip) {
  if (strip === null || strip === undefined || !strip.visible) {
    hittingStripEl.dataset.visible = "false";
    hittingStripEl.dataset.chamfer = "false";
    stripSidesEl.dataset.visible = "false";
    return;
  }
  hittingStripEl.setAttribute("points", pointList(strip.corners));
  hittingStripEl.dataset.visible = "true";
  // Whether the strip is a lit pad or an outline is the operator's decision about
  // their room, so it arrives with the state rather than being a stylesheet mode.
  hittingStripEl.dataset.filled = String(strip.filled === true);
}

/* The rear edge is replaced by the chamfer. These are the other three sides,
   joined from the corners Python already sent. No new geometry. */
function showStripSides(strip) {
  const corners = strip.corners;
  if (!Array.isArray(corners) || corners.length < 4) {
    stripSidesEl.dataset.visible = "false";
    return;
  }
  const [backLeft, backRight, frontRight, frontLeft] = corners;
  stripSidesEl.setAttribute(
    "points",
    pointList([backLeft, frontLeft, frontRight, backRight]),
  );
  stripSidesEl.dataset.visible = "true";
  hittingStripEl.dataset.chamfer = "true";
}

function hideChamfer() {
  hittingStripEl.dataset.chamfer = "false";
  stripSidesEl.dataset.visible = "false";
  ballStatusEdgeEl.dataset.visible = "false";
  ballStatusEl.dataset.visible = "false";
  ballStatusEl.dataset.ready = "false";
}

/* Compact chamfer. Points, the words, and the millimetre sizes all arrive
   from Python. The only local arithmetic is placing the lamp against the
   measured words and centring that pair on the point Python named, which is
   typography, and turning one mapped millimetre into pixels. */
function renderBallStatus(status, strip) {
  if (
    status === null ||
    status === undefined ||
    strip === null ||
    strip === undefined ||
    !strip.visible ||
    !Array.isArray(status.edge) ||
    status.edge.length !== 6
  ) {
    hideChamfer();
    return;
  }
  const textPx = toPixels(status.text);
  const mmPx = toPixels(status.millimetre);
  const pxPerMm = Math.hypot(mmPx.x - textPx.x, mmPx.y - textPx.y);
  if (!(pxPerMm > 0) || typeof status.label !== "string" || status.label === "") {
    hideChamfer();
    return;
  }

  showStripSides(strip);
  ballStatusEdgeEl.setAttribute("points", pointList(status.edge));
  ballStatusEdgeEl.setAttribute("stroke-width", String(status.stroke_mm * pxPerMm));
  ballStatusEdgeEl.dataset.visible = "true";

  const fontPx = status.font_mm * pxPerMm;
  const lampR = (status.lamp_mm * pxPerMm) / 2;
  const lampGap = status.lamp_gap_mm * pxPerMm;
  ballStatusLabelEl.textContent = status.label;
  ballStatusLabelEl.setAttribute("font-size", String(fontPx));
  ballStatusLabelEl.setAttribute("font-weight", String(status.weight));
  ballStatusLabelEl.setAttribute("letter-spacing", `${status.tracking}em`);
  ballStatusLabelEl.setAttribute("x", String(textPx.x));
  ballStatusLabelEl.setAttribute("y", String(textPx.y));

  const box = ballStatusLabelEl.getBBox();
  const lampCx = box.x - lampGap - lampR;
  const shift = textPx.x - (lampCx - lampR + box.x + box.width) / 2;
  ballStatusLabelEl.setAttribute("x", String(textPx.x + shift));
  for (const lamp of [ballStatusLampEl, ballStatusLampCoreEl]) {
    lamp.setAttribute("cx", String(lampCx + shift));
    lamp.setAttribute("cy", String(textPx.y));
  }
  ballStatusLampEl.setAttribute("r", String(lampR));
  ballStatusLampCoreEl.setAttribute("r", String(lampR * 0.4));

  ballStatusEl.dataset.ready = status.ready === true ? "true" : "false";
  ballStatusEl.dataset.visible = "true";
}

function pointList(points) {
  return (points ?? [])
    .map((point) => {
      const pixel = toPixels(point);
      return `${pixel.x},${pixel.y}`;
    })
    .join(" ");
}

/* Where the anchored panels go.
 *
 * Python derives these from the hitting strip in metres and maps them through
 * the calibration; the only thing left here is the percentage. A snapshot
 * without anchors leaves the panels where the stylesheet puts them, which is
 * what an older server and a fresh floor both produce. */
function renderAnchors(anchors) {
  if (anchors === null || anchors === undefined) {
    surfaceEl.dataset.anchored = "false";
    return;
  }
  for (const [name, point] of [
    ["info", anchors.information],
    ["advice", anchors.advice],
  ]) {
    if (point === null || point === undefined) {
      continue;
    }
    surfaceEl.style.setProperty(`--anchor-${name}-x`, `${point.x * 100}%`);
    surfaceEl.style.setProperty(`--anchor-${name}-y`, `${point.y * 100}%`);
  }
  renderCoachRegions(anchors.coach);
  surfaceEl.dataset.anchored = "true";
}

/* Where each part of a coached result goes.
 *
 * Four measured boxes, computed from the hitting strip as it actually
 * projects, so that none of them can land on the mat. A server that sends
 * none — an older one, or a bay whose strip leaves no room above it — leaves
 * the single centred column the stylesheet has always drawn. */
const COACH_REGIONS = ["score", "evidence", "observation", "metrics"];

function renderCoachRegions(coach) {
  if (coach === null || coach === undefined) {
    adviceEl.dataset.regions = "false";
    return;
  }
  for (const name of COACH_REGIONS) {
    const rect = coach[name];
    if (rect === null || rect === undefined) {
      adviceEl.dataset.regions = "false";
      return;
    }
    surfaceEl.style.setProperty(`--coach-${name}-x`, rect.x);
    surfaceEl.style.setProperty(`--coach-${name}-y`, rect.y);
    surfaceEl.style.setProperty(`--coach-${name}-w`, rect.width);
    surfaceEl.style.setProperty(`--coach-${name}-h`, rect.height);
  }
  adviceEl.dataset.regions = "true";
}

function renderPuttingLane(lane) {
  if (lane === null || lane === undefined || !lane.visible) {
    puttingLaneEl.dataset.visible = "false";
    puttingLaneLineEl.dataset.visible = "false";
    return;
  }
  puttingLaneEl.setAttribute("points", pointList(lane.corners));
  puttingLaneEl.dataset.visible = "true";
  const start = toPixels(lane.start);
  const end = toPixels(lane.end);
  puttingLaneLineEl.setAttribute("x1", start.x);
  puttingLaneLineEl.setAttribute("y1", start.y);
  puttingLaneLineEl.setAttribute("x2", end.x);
  puttingLaneLineEl.setAttribute("y2", end.y);
  puttingLaneLineEl.dataset.visible = "true";
}

/* The roll the ball made. Points arrive already placed and already rotated
   onto the line the ball set off on; this joins them. */
function renderPuttPath(path) {
  if (path === null || path === undefined) {
    puttPathEl.dataset.visible = "false";
    puttPathEl.dataset.source = "";
    return;
  }
  puttPathEl.setAttribute("points", pointList(path.points));
  puttPathEl.dataset.source = typeof path.source === "string" ? path.source : "";
  puttPathEl.dataset.visible = "true";
}

function renderLockAck(ack, marker) {
  if (ack === null || ack === undefined) {
    lockAckActive = false;
    hittingStripEl.dataset.acknowledge = "false";
    hittingStripEl.classList.remove("hitting-strip--acknowledge");
    lockAckEl.dataset.visible = "false";
    return;
  }

  const seconds = typeof ack.seconds === "number" && ack.seconds > 0 ? ack.seconds : 0.65;
  hittingStripEl.style.setProperty("--lock-ack-seconds", `${seconds}s`);
  lockAckEl.style.setProperty("--lock-ack-seconds", `${seconds}s`);
  hittingStripEl.dataset.acknowledge = "true";

  const centre = toPixels(marker.position);
  const unit = markerUnit();
  lockAckMarkEl.setAttribute("x", centre.x);
  lockAckMarkEl.setAttribute("y", centre.y);
  lockAckMarkEl.setAttribute("font-size", unit * 0.09);
  lockAckEl.dataset.visible = "true";

  if (!lockAckActive) {
    lockAckActive = true;
    hittingStripEl.classList.remove("hitting-strip--acknowledge");
    void hittingStripEl.getBoundingClientRect();
    hittingStripEl.classList.add("hitting-strip--acknowledge");
  }
}

function renderClubPath(clubPath) {
  if (clubPath === null || clubPath === undefined) {
    clubPathEl.dataset.visible = "false";
    return;
  }
  // Both endpoints come from the server; no trigonometry happens here.
  const start = toPixels(clubPath.start);
  const end = toPixels(clubPath.end);
  clubPathEl.setAttribute("x1", start.x);
  clubPathEl.setAttribute("y1", start.y);
  clubPathEl.setAttribute("x2", end.x);
  clubPathEl.setAttribute("y2", end.y);
  clubPathEl.dataset.visible = "true";
}

function renderMetrics(metrics) {
  // Launch numbers stay on the snapshot. They are not drawn: mph and yds are
  // not what a player over the ball can use, and they occupied the same
  // information area as the result.
  metricsEl.hidden = true;
  if (metrics === null || metrics === undefined) {
    metricsEl.dataset.ballSpeed = "";
    metricsEl.dataset.launch = "";
    metricsEl.dataset.carry = "";
    return;
  }
  metricsEl.dataset.ballSpeed = String(metrics.ball_speed_mph);
  metricsEl.dataset.launch =
    metrics.launch_angle_degrees === null ||
    metrics.launch_angle_degrees === undefined
      ? ""
      : String(metrics.launch_angle_degrees);
  metricsEl.dataset.carry =
    metrics.carry_yards === null || metrics.carry_yards === undefined
      ? ""
      : String(metrics.carry_yards);
}

function displayString(value) {
  return typeof value === "string" && value !== "" ? value : "";
}

/* Device state, as attributes. Never textContent and never unhidden: READY and
   FINDING BALL stay inspectable and stay off the floor. The chamfer is a
   different element, and its words arrive already chosen. */
function renderPlayStatus(status, club) {
  playStatusEl.dataset.state = status ?? "";
  playStatusEl.dataset.club = club ?? "";
  playStatusEl.hidden = true;
}

function renderAddressHud(address) {
  if (address === null || address === undefined) {
    addressHudEl.hidden = true;
    addressDistanceFigureEl.hidden = true;
    addressClubFigureEl.hidden = true;
    addressHudEl.dataset.club = "";
    return;
  }
  const distance =
    typeof address.distance_display === "string" ? address.distance_display : "";
  const club =
    typeof address.recommended_club_display === "string"
      ? address.recommended_club_display
      : "";
  addressDistanceEl.textContent = distance;
  addressClubEl.textContent = club;
  addressDistanceFigureEl.hidden = !distance;
  addressClubFigureEl.hidden = !club;
  // The club the launch monitor detected, which is a different claim from the
  // one above it. Kept where a bay can be inspected, not where it competes.
  addressHudEl.dataset.club =
    typeof address.club_display === "string" ? address.club_display : "";
  addressHudEl.hidden = !distance && !club;
}

function renderRoundResult(result) {
  if (result === null || result === undefined) {
    roundResultEl.hidden = true;
    roundShotFigureEl.hidden = true;
    roundRemainingFigureEl.hidden = true;
    return;
  }
  const shotDistance = displayString(result.shot_distance_display);
  const remaining = displayString(result.remaining_distance_display);
  roundShotDistanceEl.textContent = shotDistance;
  roundRemainingEl.textContent = remaining;
  roundShotFigureEl.hidden = !shotDistance;
  roundRemainingFigureEl.hidden = !remaining;
  // Diagnostics for a live bay. Never textContent of a visible node.
  roundResultEl.dataset.shotDistance = shotDistance;
  roundResultEl.dataset.remaining = remaining;
  roundResultEl.dataset.ballSpeed =
    typeof result.ball_speed_display === "string" ? result.ball_speed_display : "";
  roundResultEl.dataset.player = result.player_name || "";
  roundResultEl.dataset.shotId = result.shot_id || "";
  roundResultEl.hidden = !shotDistance && !remaining;
}

function renderBallFlight(points) {
  ballFlightEl.hidden = true;
  ballFlightEl.dataset.points =
    points === null || points === undefined ? "" : String(points.length);
}

/* The only text on the floor that a language model may have written.
 *
 * textContent, never innerHTML: the server sanitizes and length-caps this
 * string before it is ever sent, and assigning it as text means a mistake there
 * shows up as visible nonsense rather than as markup the browser executes. */
function renderAdvice(advice) {
  if (advice === null || advice === undefined) {
    adviceEl.hidden = true;
    adviceEl.dataset.band = "";
    adviceEl.dataset.pending = "false";
    if (adviceCueEl) {
      adviceCueEl.hidden = true;
      adviceCueEl.removeAttribute("src");
    }
    renderAdviceEvidence(null);
    renderAdviceScore(null);
    renderAdviceObservation("");
    renderAdviceMetrics(null);
    return;
  }
  adviceTextEl.textContent = advice.text;
  adviceEl.dataset.band = advice.band;
  adviceEl.dataset.pending = String(advice.pending);
  renderAdviceEvidence(advice.evidence_url);
  renderAdviceScore(advice.swing_score);
  renderAdviceObservation(advice.observation);
  renderAdviceMetrics(advice.metrics);
  if (adviceCueEl) {
    const asset = typeof advice.cue_asset === "string" ? advice.cue_asset.trim() : "";
    if (asset) {
      adviceCueEl.src = asset;
      adviceCueEl.hidden = false;
    } else {
      adviceCueEl.hidden = true;
      adviceCueEl.removeAttribute("src");
    }
  }
  adviceEl.hidden = false;
}

/* Only a same-origin evidence path is ever assigned, and the frame is shown
 * only once it has actually arrived: the line stands without a picture rather
 * than beside a broken one. Unchanged URLs are left alone so a repaint cannot
 * refetch — and so a repaint cannot unhide a frame the server already
 * refused, which is why whether it loaded is remembered on the element rather
 * than assumed from the URL. */
function renderAdviceEvidence(url) {
  if (!adviceEvidenceEl) {
    return;
  }
  const path = typeof url === "string" && url.startsWith("/evidence/") ? url : "";
  if (!path) {
    adviceEvidenceEl.hidden = true;
    adviceEvidenceEl.onload = null;
    adviceEvidenceEl.onerror = null;
    adviceEvidenceEl.removeAttribute("src");
    adviceEvidenceEl.dataset.evidence = "";
    return;
  }
  if (adviceEvidenceEl.getAttribute("src") !== path) {
    adviceEvidenceEl.dataset.evidence = "loading";
    adviceEvidenceEl.onload = () => {
      adviceEvidenceEl.dataset.evidence = "loaded";
      adviceEvidenceEl.hidden = false;
      keepAdviceOnSurface();
    };
    adviceEvidenceEl.onerror = () => {
      adviceEvidenceEl.hidden = true;
      adviceEvidenceEl.dataset.evidence = "failed";
    };
    adviceEvidenceEl.src = path;
  }
  adviceEvidenceEl.hidden = adviceEvidenceEl.dataset.evidence !== "loaded";
}

/* The anchor says where the result begins, not how much floor is left below
 * it, and on a default bay it sits a few percent above the player's edge. A
 * result taller than that gap would put its last line — the instruction —
 * off the projection. So it is lifted by exactly the overflow and no more,
 * which is typography rather than placement: the anchor is still Python's.
 *
 * Only for the un-regioned fallback. With regions there is nothing to lift:
 * every box was placed inside the floor to begin with, and sliding them would
 * be undoing the measurement that kept them off the mat. */
function keepAdviceOnSurface() {
  adviceEl.style.removeProperty("--advice-lift");
  if (adviceEl.hidden || adviceEl.dataset.regions === "true") {
    return;
  }
  const bounds = surfaceEl.getBoundingClientRect();
  const box = adviceEl.getBoundingClientRect();
  const lift = Math.min(box.bottom - bounds.bottom, box.top - bounds.top);
  if (lift > 0) {
    adviceEl.style.setProperty("--advice-lift", `${lift}px`);
  }
}

function renderAdviceScore(score) {
  if (!adviceScoreEl || !adviceScoreValueEl) {
    return;
  }
  if (typeof score !== "number" || !Number.isFinite(score)) {
    adviceScoreEl.hidden = true;
    adviceScoreValueEl.textContent = "";
    return;
  }
  adviceScoreValueEl.textContent = String(Math.round(score));
  adviceScoreEl.hidden = false;
}

/* Carry, face, path and club speed, exactly as the server formatted them.
 *
 * Rebuilt from scratch each time rather than patched, because a strip that
 * kept a stale cell would be showing one swing's carry beside another's face
 * angle. A reading the device did not give simply is not in the list, and
 * three cells is a correct strip; nothing is filled in with a dash. */
function renderAdviceMetrics(metrics) {
  if (!adviceMetricsEl) {
    return;
  }
  adviceMetricsEl.replaceChildren();
  if (!Array.isArray(metrics) || metrics.length === 0) {
    adviceMetricsEl.hidden = true;
    return;
  }
  for (const metric of metrics) {
    const label = typeof metric?.label === "string" ? metric.label : "";
    const value = typeof metric?.value === "string" ? metric.value : "";
    if (!label || !value) {
      continue;
    }
    const cell = document.createElement("div");
    cell.className = "advice__metric";
    const labelEl = document.createElement("span");
    labelEl.className = "advice__metric-label";
    labelEl.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.className = "advice__metric-value";
    valueEl.textContent = value;
    cell.append(labelEl, valueEl);
    adviceMetricsEl.append(cell);
  }
  adviceMetricsEl.hidden = adviceMetricsEl.childElementCount === 0;
}

function renderAdviceObservation(observation) {
  if (!adviceObservationEl) {
    return;
  }
  const text = typeof observation === "string" ? observation.trim() : "";
  adviceObservationEl.textContent = text;
  adviceObservationEl.hidden = !text;
}

/* A named stage, never a number. The bar is indeterminate because nothing
 * upstream knows how far along a model request is, and the server clears the
 * stage itself when the shot settles or a newer one supersedes it. */
const PROGRESS_LABELS = {
  capturing: "CAPTURING SWING",
  analyzing: "ANALYZING",
  coaching: "COACHING",
};

function renderProgress(progress) {
  if (!progressEl) {
    return;
  }
  const label = progress ? PROGRESS_LABELS[progress.stage] : undefined;
  if (!label) {
    progressEl.hidden = true;
    progressEl.dataset.stage = "";
    progressLabelEl.textContent = "";
    return;
  }
  progressEl.dataset.stage = progress.stage;
  progressLabelEl.textContent = label;
  progressEl.hidden = false;
}

/* A celebration.
 *
 * Everything about it comes from the server: how long it runs, how big it is,
 * and which word it shows. Nothing is timed here, because the server clears this
 * state when the effect ends and the animation is paced to the same duration —
 * a browser that also kept its own clock would drift from the lights and there
 * would be nothing to blame for the difference.
 *
 * A client that connects part-way through an effect finds this state already set
 * and starts the animation from its beginning, which is slightly generous and
 * the right way round: showing a full celebration a moment late is better than
 * showing the tail of one. */
function renderFlourish(flourish) {
  if (flourish === null || flourish === undefined) {
    flourishEl.hidden = true;
    flourishEl.dataset.cue = "";
    flourishEl.classList.remove("flourish--running");
    flourishKey = "";
    return;
  }

  flourishLabelEl.textContent = flourish.label;
  flourishEl.dataset.cue = flourish.cue;
  flourishEl.style.setProperty("--flourish-seconds", `${flourish.seconds}s`);
  flourishEl.style.setProperty("--flourish-intensity", String(flourish.intensity));
  flourishEl.hidden = false;

  const key = `${flourish.cue}:${flourish.seconds}:${flourish.intensity}`;
  if (key !== flourishKey) {
    flourishKey = key;
    restartAnimations(flourishEl);
  }
}

/* Take the animation class off and put it back, which is the only reliable way
 * to replay a CSS animation. Reading a layout property in between is what makes
 * it work: without it the browser coalesces both class changes into one frame
 * and concludes that nothing changed. */
function restartAnimations(element) {
  element.classList.remove("flourish--running");
  void element.offsetWidth;
  element.classList.add("flourish--running");
}

function paint(snapshot) {
  const state = snapshot.state;
  statusTextEl.textContent = state.status_text;
  statusEl.dataset.mode = state.mode;
  renderAnchors(state.anchors);
  renderHittingStrip(state.hitting_strip);
  try {
    renderBallStatus(state.ball_status, state.hitting_strip);
  } catch (error) {
    hideChamfer();
  }
  renderPuttingLane(state.putting_lane);
  renderPuttPath(state.putt_path);
  renderMarker(state.marker);
  renderLockAck(state.lock_acknowledgement, state.marker);
  renderClubPath(state.club_path);
  renderMetrics(state.metrics);
  renderPlayStatus(state.play_status, state.club_label);
  renderAddressHud(state.address);
  renderRoundResult(state.round_result);
  renderBallFlight(state.ball_flight);
  renderAdvice(state.advice);
  keepAdviceOnSurface();
  renderProgress(state.progress);
  renderFlourish(state.flourish);
}

export function render(snapshot) {
  // Ignore anything older than what is already on screen; snapshots may arrive
  // out of order across a reconnect.
  if (snapshot.seq < lastSeq) {
    return false;
  }
  lastSeq = snapshot.seq;
  lastSnapshot = snapshot;
  measureSurface();
  paint(snapshot);
  return true;
}

export function repaint() {
  if (lastSnapshot === null) {
    return;
  }
  measureSurface();
  paint(lastSnapshot);
}

/* Forget which snapshot is on screen without erasing it.
 *
 * Used across a reconnect. The pixels stay so the floor does not flicker, and
 * the sequence goes so that the first snapshot of a restarted CaddieJ — whose
 * version counter starts at zero again — is accepted rather than discarded as
 * older than what is already drawn. */
export function invalidate() {
  lastSeq = -1;
}

