/* Floor state feed client.
 *
 * Reload, suspension, and disconnection are normal here: OBS can refresh or
 * suspend a Browser Source at any time. Every reconnect discards local state and
 * waits for a fresh snapshot rather than merging anything. */

/* Relative to this file's URL. The version lives in the directory
   (`/static/hud/<hash>/…`) because OBS's CEF caches ES modules by pathname
   and ignores `?v=` query strings. */
import { invalidate, render, repaint } from "./state.js";

/* Which build of the overlay this page actually loaded. Reported on connect
   because OBS's embedded browser caches ES modules by URL: when the floor shows
   a stale rendering, the only way to tell from CaddieJ's side is to compare this
   against `hud_assets_version` on /healthz. */
const ASSETS_VERSION = "__HUD_ASSETS_VERSION__";

/* Set only when CaddieJ sent this page here because it was running another
   build. Echoed in the hello so the server can count how often it has asked,
   and stop; the page itself never decides to reload. */
const RELOAD_PARAMS = new URLSearchParams(window.location.search);
let reloading = false;

/* Which page this is, for as long as it is loaded.
 *
 * Every overlay reports `surface: "floor"`, so when a hardware test had two of
 * them connected and one was drawing a stale layout, there was no way to say
 * from the log which client the projector was showing. This id survives
 * reconnects and dies with the page, which is exactly the distinction that was
 * missing: a reconnect keeps it, a reload replaces it. The href goes with it
 * because that is what names the OBS Browser Source behind a connection. */
const PAGE_ID = Math.random().toString(36).slice(2, 10);

const RECONNECT_MIN_MS = 250;
const RECONNECT_MAX_MS = 2000;
/* A heartbeat the server can observe, not a watchdog this page acts on.
 *
 * There used to be a silence timer here that closed the socket when no message
 * arrived within fifteen seconds. On an idle feed the only thing producing
 * traffic was this very interval, and OBS's CEF throttles timers in a Browser
 * Source it is not compositing — so the page kept killing healthy sockets. One
 * recorded session reconnected 8,149 times, blanking the projector each time.
 *
 * Dead peers are reaped by the WebSocket protocol ping uvicorn already sends,
 * which CEF answers in its network layer and no JS throttle can delay. This
 * heartbeat's only job now is to let /healthz say how long the page has been
 * quiet, which is an observation the server makes rather than an action this
 * page takes. */
const PING_INTERVAL_MS = 5000;
// Grace period before admitting the link is down, so brief drops stay invisible.
const LINK_DOWN_GRACE_MS = 3000;

const linkEl = document.getElementById("link");
const linkTextEl = document.getElementById("link-text");
document.documentElement.dataset.hudAssets = ASSETS_VERSION;
if (linkEl !== null) {
  linkEl.dataset.assets = ASSETS_VERSION;
}

let socket = null;
let reconnectDelay = RECONNECT_MIN_MS;
let pingTimer = null;
let linkDownTimer = null;

function feedUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws`;
}

/* Only a delivered snapshot counts as a working link. Reconnect attempts
   deliberately leave the pending warning alone: since backoff is shorter than
   the grace period, cancelling it on every attempt would mean the warning could
   never appear, however long CaddieJ stayed away. */
function markLinkUp() {
  if (linkDownTimer !== null) {
    window.clearTimeout(linkDownTimer);
    linkDownTimer = null;
  }
  linkEl.dataset.state = "up";
  linkTextEl.textContent = "live";
}

function markLinkDown() {
  if (linkDownTimer !== null) {
    return;
  }
  linkDownTimer = window.setTimeout(() => {
    linkEl.dataset.state = "down";
    linkTextEl.textContent = "reconnecting";
  }, LINK_DOWN_GRACE_MS);
}

function stopTimers() {
  if (pingTimer !== null) {
    window.clearInterval(pingTimer);
    pingTimer = null;
  }
}

function send(message) {
  if (socket !== null && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

/* Surface browser-side problems in CaddieJ's structured logs: a projector has
   no console anyone will read. */
function report(message) {
  send({ type: "client_log", message: String(message).slice(0, 500) });
}

function handleMessage(event) {
  let message;
  try {
    message = JSON.parse(event.data);
  } catch (error) {
    report(`unparseable message: ${error}`);
    return;
  }

  switch (message.type) {
    case "hello":
      send({
        type: "hello",
        surface: "floor",
        assets_version: ASSETS_VERSION,
        reload_for: RELOAD_PARAMS.get("hud_assets") ?? "",
        reload_attempt: RELOAD_PARAMS.get("hud_reload") ?? "",
        page_id: PAGE_ID,
        href: window.location.href,
      });
      report(`overlay_assets_loaded version=${ASSETS_VERSION}`);
      if (
        typeof message.hud_assets_version === "string" &&
        message.hud_assets_version &&
        message.hud_assets_version !== ASSETS_VERSION
      ) {
        report(
          `overlay_assets_stale loaded=${ASSETS_VERSION} expected=${message.hud_assets_version}`,
        );
      }
      break;
    case "snapshot":
      render(message);
      markLinkUp();
      break;
    case "reload":
      reloadTo(message.url);
      break;
    case "pong":
      break;
    case "error":
      report(`server error: ${message.detail}`);
      break;
    default:
      report(`unknown message type: ${message.type}`);
  }
}

/* `replace`, so a reload leaves no history entry, and only ever to a path on
   this same server whatever the message says: a floor that can be talked into
   navigating somewhere else is a floor somebody else can draw on. */
function reloadTo(url) {
  if (reloading || typeof url !== "string" || !url.startsWith("/?")) {
    return;
  }
  reloading = true;
  window.location.replace(url);
}

function connect() {
  socket = new WebSocket(feedUrl());

  socket.addEventListener("open", () => {
    reconnectDelay = RECONNECT_MIN_MS;
    pingTimer = window.setInterval(() => send({ type: "ping" }), PING_INTERVAL_MS);
    // No resync is needed: the server sends hello and a full snapshot on its own.
  });

  socket.addEventListener("message", handleMessage);

  socket.addEventListener("close", () => {
    stopTimers();
    socket = null;
    /* The sequence is forgotten but the pixels are not.
     *
     * Blanking here was correct in principle — the browser holds no state
     * worth trusting — and wrong in practice: a reconnect takes a quarter of a
     * second, and going dark for it means the projector flickers every time
     * OBS so much as resizes the source. What must not survive is the
     * sequence number, because a restarted CaddieJ counts from zero again and
     * `render` would reject every snapshot of the new run as stale. */
    invalidate();
    markLinkDown();
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    if (socket !== null) {
      socket.close();
    }
  });
}

function scheduleReconnect() {
  const jitter = Math.random() * reconnectDelay * 0.3;
  const delay = Math.min(reconnectDelay + jitter, RECONNECT_MAX_MS);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  window.setTimeout(connect, delay);
}

/* A projector output can change size at any time — OBS canvas changes, a
   resolution switch, a window resize. Repainting from the last snapshot keeps
   the floor correct without asking the server for anything. */
window.addEventListener("resize", repaint);

window.addEventListener("error", (event) => report(`uncaught: ${event.message}`));
window.addEventListener("unhandledrejection", (event) =>
  report(`unhandled rejection: ${event.reason}`),
);

connect();
