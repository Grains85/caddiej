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
const markerEl = document.getElementById("marker");
const markerHaloEl = document.getElementById("marker-halo");
const markerRingEl = document.getElementById("marker-ring");
const markerBallEl = document.getElementById("marker-ball");
const clubPathEl = document.getElementById("club-path");
const metricsEl = document.getElementById("metrics");
const metricSpeedEl = document.getElementById("metric-speed");
const metricLaunchEl = document.getElementById("metric-launch");
const metricCarryEl = document.getElementById("metric-carry");
const metricCarryRowEl = document.getElementById("metric-carry-row");
const adviceEl = document.getElementById("advice");
const adviceTextEl = document.getElementById("advice-text");
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
  if (metrics === null || metrics === undefined) {
    metricsEl.hidden = true;
    return;
  }
  metricSpeedEl.textContent = metrics.ball_speed_mph.toFixed(1);
  metricLaunchEl.textContent = metrics.launch_angle_degrees.toFixed(1);
  const carry = metrics.carry_yards;
  const hasCarry = carry !== null && carry !== undefined;
  if (hasCarry) {
    metricCarryEl.textContent = carry.toFixed(1);
  }
  metricCarryRowEl.hidden = !hasCarry;
  metricsEl.hidden = false;
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
    return;
  }
  adviceTextEl.textContent = advice.text;
  adviceEl.dataset.band = advice.band;
  adviceEl.dataset.pending = String(advice.pending);
  adviceEl.hidden = false;
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
  renderMarker(state.marker);
  renderClubPath(state.club_path);
  renderMetrics(state.metrics);
  renderAdvice(state.advice);
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

export function clear() {
  lastSeq = -1;
  lastSnapshot = null;
  statusTextEl.textContent = "";
  statusEl.dataset.mode = "";
  markerEl.dataset.visible = "false";
  clubPathEl.dataset.visible = "false";
  metricsEl.hidden = true;
  adviceEl.hidden = true;
  adviceEl.dataset.band = "";
  adviceEl.dataset.pending = "false";
  renderFlourish(null);
}

export function currentSeq() {
  return lastSeq;
}
