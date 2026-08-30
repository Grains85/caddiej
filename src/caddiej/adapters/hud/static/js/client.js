/* Floor state feed client.
 *
 * Reload, suspension, and disconnection are normal here: OBS can refresh or
 * suspend a Browser Source at any time. Every reconnect discards local state and
 * waits for a fresh snapshot rather than merging anything. */

import { clear, render, repaint } from "./state.js";

const RECONNECT_MIN_MS = 250;
const RECONNECT_MAX_MS = 2000;
const PING_INTERVAL_MS = 5000;
// A suspended page can hold a socket that looks open but is never serviced, so
// silence is treated as failure rather than trusting readyState.
const SILENCE_TIMEOUT_MS = 15000;
// Grace period before admitting the link is down, so brief drops stay invisible.
const LINK_DOWN_GRACE_MS = 3000;

const linkEl = document.getElementById("link");
const linkTextEl = document.getElementById("link-text");

let socket = null;
let reconnectDelay = RECONNECT_MIN_MS;
let pingTimer = null;
let silenceTimer = null;
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

function armSilenceTimer() {
  if (silenceTimer !== null) {
    window.clearTimeout(silenceTimer);
  }
  silenceTimer = window.setTimeout(() => {
    report("feed silent; forcing reconnect");
    if (socket !== null) {
      socket.close();
    }
  }, SILENCE_TIMEOUT_MS);
}

function stopTimers() {
  if (pingTimer !== null) {
    window.clearInterval(pingTimer);
    pingTimer = null;
  }
  if (silenceTimer !== null) {
    window.clearTimeout(silenceTimer);
    silenceTimer = null;
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
  armSilenceTimer();

  let message;
  try {
    message = JSON.parse(event.data);
  } catch (error) {
    report(`unparseable message: ${error}`);
    return;
  }

  switch (message.type) {
    case "hello":
      send({ type: "hello", surface: "floor" });
      break;
    case "snapshot":
      render(message);
      markLinkUp();
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

function connect() {
  socket = new WebSocket(feedUrl());

  socket.addEventListener("open", () => {
    reconnectDelay = RECONNECT_MIN_MS;
    armSilenceTimer();
    pingTimer = window.setInterval(() => send({ type: "ping" }), PING_INTERVAL_MS);
    // No resync is needed: the server sends hello and a full snapshot on its own.
  });

  socket.addEventListener("message", handleMessage);

  socket.addEventListener("close", () => {
    stopTimers();
    socket = null;
    // Local state is discarded: the next snapshot is authoritative.
    clear();
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
