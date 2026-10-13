#!/usr/bin/env python3
"""Keep the Streamlit Community Cloud deployment awake, wake it if it sleeps,
and check it still renders.

How Community Cloud decides what a visitor sees, read from its own hosting
page code and checked against the live platform (2026-10-13):

  GET  /api/v2/app/status  -> {"status": N, ...} plus an x-csrf-token header.
         5 RUNNING, 12 IS_SHUTDOWN (asleep), 0/1/2/3/4/6/7 booting or updating,
         8-11/13 deleted or failed. The hosting page polls this itself.
  POST /api/v2/app/resume  -> what the "Yes, get this app back up!" button
                              sends. Open to any visitor, but like every POST
                              it must send the latest x-csrf-token back, or
                              the platform answers 403.

The page's API client names these /subdomain/app/...; its request interceptor
rewrites that to /api/v2/app/... before sending, and the platform's proxy adds
/subdomain back on the server side. So /api/v2/subdomain/... from a browser
reaches /api/v2/subdomain/subdomain/... and fails.

The first version of this script judged the app's state from the page text.
On 2026-10-13 the runner got the "taking longer than normal" loader instead
of the sleep page, took it for an awake app, and never pressed the button.
This version asks the status endpoint instead and sends the resume request
itself, the way the page's client builds it, so nothing depends on what the
page happens to render. The button on the page stays as the fallback: on the
first live run the resume request used the wrong path and no token, was
refused, and the button woke the app.

Success is decided the same way as before: a real browser session that opens
the app's /_stcore/stream websocket and renders real content, because that is
what Streamlit counts as a visit.

Exit 0 = awake and serving a session. Exit 1 = it did not come up, with a
screenshot, the page HTML and the full status history saved for the run.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

APP_URL = os.environ.get("APP_URL", "https://marketdash1.streamlit.app/").strip().rstrip("/") + "/"
HOLD_SECONDS = float(os.environ.get("HOLD_SECONDS", "25"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "90000"))
# A cold boot reinstalls every dependency; four minutes was not always enough.
BOOT_TIMEOUT_S = float(os.environ.get("BOOT_TIMEOUT_S", "480"))
SESSION_TIMEOUT_S = float(os.environ.get("SESSION_TIMEOUT_S", "120"))
STATUS_POLL_S = float(os.environ.get("STATUS_POLL_S", "5"))
RESUME_RETRY_S = float(os.environ.get("RESUME_RETRY_S", "60"))
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "keep-alive-diagnostics")

STATUS_PATH = "/api/v2/app/status"
RESUME_PATH = "/api/v2/app/resume"
STREAM_PATH = "/_stcore/stream"

# Community Cloud's own APP_STATUS enum.
NAMES = {0: "UNKNOWN", 1: "CREATING", 2: "CREATED", 3: "UPDATING", 4: "INSTALLING",
         5: "RUNNING", 6: "RESTARTING", 7: "REBOOTING", 8: "DELETING", 9: "DELETED",
         10: "USER_ERROR", 11: "PLATFORM_ERROR", 12: "IS_SHUTDOWN",
         13: "INSTALLER_ERROR", 14: "USER_SCRIPT_ERROR",
         15: "POTENTIAL_MINER_DETECTED"}
RUNNING = {5, 14, 15}            # the platform's own "running" set
ASLEEP = {12}
FAILED = {8, 9, 10, 11, 13}      # deleted, or the app could not start
# Everything else (0-4, 6, 7) is a boot or update in progress: keep waiting.

WAKE_BUTTON = '[data-testid="wakeup-button-viewer"], [data-testid="wakeup-button-owner"]'

# Same-origin requests made from inside the page and built the way the hosting
# page's own API client builds them: every response may hand out a fresh
# x-csrf-token, every POST sends the latest one back, and the browser's
# machineId from localStorage goes along as x-streamlit-machine-id.
JS_FETCH = """async ([path, method]) => {
  const headers = {};
  if (method !== 'GET' && window.__keepAliveCsrf) headers['x-csrf-token'] = window.__keepAliveCsrf;
  try { const id = localStorage.getItem('machineId'); if (id) headers['x-streamlit-machine-id'] = id; } catch (e) {}
  const sentToken = 'x-csrf-token' in headers;
  try {
    const r = await fetch(path, {method, headers, credentials: 'include', cache: 'no-store'});
    const token = r.headers.get('x-csrf-token');
    if (token) window.__keepAliveCsrf = token;
    return {ok: r.ok, http: r.status, body: (await r.text()).slice(0, 400), sentToken};
  } catch (e) {
    return {ok: false, http: 0, body: String(e).slice(0, 400), sentToken};
  }
}"""


def log(msg: str) -> None:
    print(f"[keep-alive] {msg}", flush=True)


def describe(code) -> str:
    return "unreadable" if code is None else f"{code} {NAMES.get(code, 'UNLISTED')}"


def call(page, path: str, method: str = "GET") -> dict:
    """One same-origin request from inside the page; never raises."""
    for _ in range(3):
        try:
            return page.evaluate(JS_FETCH, [path, method])
        except (PlaywrightError, PlaywrightTimeout) as exc:
            last = str(exc)
            time.sleep(1)   # usually a navigation in flight; try again
    return {"ok": False, "http": 0, "body": f"evaluate failed: {last[:300]}"}


def read_status(page):
    """Returns (status code or None, raw response) from the platform."""
    res = call(page, STATUS_PATH)
    if not res["ok"]:
        return None, f"HTTP {res['http']}: {res['body']}"
    try:
        return json.loads(res["body"]).get("status"), res["body"]
    except ValueError:
        return None, f"not JSON: {res['body']}"


def press_wake_button(page) -> bool:
    """Fallback: the button on the sleep page, if the page renders it."""
    try:
        button = page.locator(WAKE_BUTTON).first
        button.wait_for(state="visible", timeout=30_000)
        button.click(timeout=10_000)
        return True
    except (PlaywrightTimeout, PlaywrightError):
        return False


def wake(page) -> None:
    call(page, STATUS_PATH)   # a fresh read hands out the token the POST must carry
    res = call(page, RESUME_PATH, "POST")
    token = "sent" if res.get("sentToken") else "MISSING"
    log(f"resume   : HTTP {res['http']} (csrf token {token}) {res['body'][:160]!r}")
    if res["ok"]:
        return
    log("resume   : request refused - trying the button on the page instead")
    log("resume   : button pressed" if press_wake_button(page) else "warn     : no wake button rendered either")


def page_text(page) -> str:
    """Visible text from every frame. The app itself renders inside an
    <iframe title="streamlitApp">, so the top document alone holds only the
    hosting chrome."""
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.locator("body").inner_text(timeout=5_000))
        except (PlaywrightTimeout, PlaywrightError):
            continue
    return "\n".join(p for p in parts if p)


def save_diagnostics(page, history: list, reason: str) -> None:
    """Everything needed to see what the runner saw, without guessing."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    try:
        page.screenshot(path=os.path.join(ARTIFACT_DIR, "page.png"), full_page=True)
    except (PlaywrightTimeout, PlaywrightError):
        pass
    try:
        with open(os.path.join(ARTIFACT_DIR, "page.html"), "w", encoding="utf-8") as fh:
            fh.write(page.content())
    except (PlaywrightTimeout, PlaywrightError, OSError):
        pass
    with open(os.path.join(ARTIFACT_DIR, "status-history.json"), "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "history": history}, fh, indent=2)
    log(f"saved    : diagnostics in {ARTIFACT_DIR}/")


def fail(page, history: list, reason: str) -> int:
    log(f"FAIL     : {reason}")
    log(f"excerpt  : {page_text(page)[:300]!r}")
    save_diagnostics(page, history, reason)
    return 1


def main() -> int:
    log(f"target   : {APP_URL}")
    started = time.monotonic()
    history: list = []

    def note(code, raw) -> None:
        history.append({"t": round(time.monotonic() - started, 1),
                        "status": code, "name": NAMES.get(code), "raw": raw[:300]})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        sockets: list[str] = []
        page.on("websocket", lambda ws: sockets.append(ws.url))

        try:
            response = page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            log(f"http     : {response.status if response else 'n/a'}")

            code, raw = read_status(page)
            note(code, raw)
            log(f"status   : {describe(code)}   raw={raw[:200]}")

            if code in FAILED:
                return fail(page, history, f"platform reports {describe(code)}")

            if code not in RUNNING:
                if code in ASLEEP:
                    log("state    : ASLEEP - sending the wake request the button sends")
                    wake(page)
                elif code is None:
                    log("state    : status unreadable - trying the wake button")
                    press_wake_button(page)
                else:
                    log(f"state    : already starting ({describe(code)}) - waiting")

                deadline = time.monotonic() + BOOT_TIMEOUT_S
                last_resume = time.monotonic()
                while True:
                    if time.monotonic() > deadline:
                        return fail(page, history,
                                    f"not running {BOOT_TIMEOUT_S:.0f}s after waking; last status {describe(code)}")
                    time.sleep(STATUS_POLL_S)
                    previous = code
                    code, raw = read_status(page)
                    if code != previous:
                        note(code, raw)
                        log(f"status   : {describe(code)} at {time.monotonic() - started:.0f}s")
                    if code in RUNNING:
                        break
                    if code in FAILED:
                        return fail(page, history, f"boot failed: platform reports {describe(code)}")
                    if code in ASLEEP and time.monotonic() - last_resume > RESUME_RETRY_S:
                        log("state    : still asleep - sending the wake request again")
                        wake(page)
                        last_resume = time.monotonic()

                log(f"state    : RUNNING after {time.monotonic() - started:.0f}s - opening the app")
                sockets.clear()
                page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            else:
                log("state    : already running")

            if code == 14:
                log("warn     : platform reports USER_SCRIPT_ERROR - the app's own code raised")

            deadline = time.monotonic() + SESSION_TIMEOUT_S
            while not any(STREAM_PATH in url for url in sockets):
                if time.monotonic() > deadline:
                    return fail(page, history,
                                f"platform says running but no {STREAM_PATH} websocket within {SESSION_TIMEOUT_S:.0f}s")
                page.wait_for_timeout(2_000)
            log(f"session  : websocket open at {time.monotonic() - started:.1f}s")

            # Hold the session so it registers as a real visit, not a connect-and-drop.
            page.wait_for_timeout(int(HOLD_SECONDS * 1_000))

            body = page_text(page)
            if re.search(r"gone to sleep|get this app back up", body, re.I):
                return fail(page, history, "still showing the hibernation page after the session opened")
            if len(body.strip()) < 200:
                return fail(page, history, f"page rendered almost no content ({len(body)} chars)")

            log(f"title    : {page.title()!r}")
            log(f"rendered : {len(body)} chars of body text")
            log(f"OK       : app is awake ({time.monotonic() - started:.1f}s total)")
            return 0

        except PlaywrightTimeout as exc:
            return fail(page, history, f"timed out - {exc}")
        except PlaywrightError as exc:
            return fail(page, history, f"browser error - {exc}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
