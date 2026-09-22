"""Tiny HTTP keepalive for hosts that ping the process to decide it's alive.

It was previously a static string on Flask's *development* server bound to
0.0.0.0: that means anyone who could reach the port got an answer, the dev
server's threading model was a free thread-spawn amplifier, and — worse — the
answer was constant, so a monitor reported "healthy" for a bot whose gateway
was down, whose store had failed to initialise, or which had loaded zero cogs.
There is no liveness/readiness distinction at all.

Now:
  * binds 127.0.0.1 unless KEEPALIVE_HOST says otherwise,
  * /healthz is a real readiness probe driven by a heartbeat the bot updates,
    so it returns 503 until the gateway has actually connected,
  * a non-numeric PORT kills the thread loudly instead of vanishing,
  * the thread is started explicitly rather than at import time, so its log
    lines are not emitted before logging is configured.
"""

import logging
import os
import threading
import time

from flask import Flask

LOG = logging.getLogger("bot.keepalive")

app = Flask("")

# Set by BOT.py once the gateway connects. Kept module-level so the route can
# read it without importing the bot (which would be circular).
_READY_AT: float | None = None
_lock = threading.Lock()


def mark_ready() -> None:
    """Record gateway readiness. Called from on_ready."""
    global _READY_AT
    with _lock:
        _READY_AT = time.time()


def mark_not_ready() -> None:
    global _READY_AT
    with _lock:
        _READY_AT = None


@app.route("/")
def home():
    with _lock:
        ready = _READY_AT
    if ready is None:
        return "Bot is starting…", 503
    return f"Bot is running! 24/7 (ready since {ready:.0f})", 200


@app.route("/healthz")
def healthz():
    """Readiness probe: 200 only once the gateway has connected."""
    with _lock:
        ready = _READY_AT
    if ready is None:
        return "not ready", 503
    return "ok", 200


def run() -> None:
    raw_port = os.getenv("PORT", "8080")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        # Previously this raised inside the daemon thread, so the keepalive
        # disappeared with no trace and nothing noticed.
        LOG.error("KEEPALIVE_PORT/PORT is not a number (%r); keepalive disabled",
                  raw_port)
        return
    host = os.getenv("KEEPALIVE_HOST", "127.0.0.1")
    LOG.info("keepalive listening on %s:%s", host, port)
    app.run(host=host, port=port, threaded=False, use_reloader=False)


def keep_alive() -> threading.Thread | None:
    thread = threading.Thread(target=run, name="keepalive", daemon=True)
    thread.start()
    return thread