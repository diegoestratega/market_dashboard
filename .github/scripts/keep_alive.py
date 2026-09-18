#!/usr/bin/env python3
"""Keep the Streamlit Community Cloud deployment awake, and check it still renders.

Community Cloud hibernates an app that receives no traffic for 12 hours. A plain
HTTP GET is not obviously enough: it pulls the static shell, while Streamlit only
registers a real session once the browser opens the websocket at /_stcore/stream.
So this drives a real headless browser, waits for that websocket to open, and
holds the session open for a few seconds.

Waiting on the websocket (rather than on a CSS selector) is deliberate - it is
the thing Streamlit actually counts as traffic, and it does not break when the
DOM or a data-testid changes between Streamlit releases.

Exit 0 = the app is awake and serving a session.
Exit 1 = it did not come up, which makes this double as a cheap uptime check.
"""

from __future__ import annotations

import os
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

APP_URL = os.environ.get("APP_URL", "https://marketdash1.streamlit.app/").strip()
HOLD_SECONDS = float(os.environ.get("HOLD_SECONDS", "25"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "90000"))
WAKE_TIMEOUT_S = float(os.environ.get("WAKE_TIMEOUT_S", "240"))

# The hibernation interstitial and its button, matched loosely so a wording
# change on Streamlit's side does not silently break the wake-up path.
SLEEP_MARKER = re.compile(r"gone to sleep|get this app back up|zzz", re.I)
WAKE_BUTTON = re.compile(r"get this app back up|back up|wake", re.I)
STREAM_PATH = "/_stcore/stream"


def log(msg: str) -> None:
    print(f"[keep-alive] {msg}", flush=True)


def page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5_000)
    except (PlaywrightTimeout, PlaywrightError):
        return ""


def click_wake_button(page) -> bool:
    """Click the 'get this app back up' control if the sleep page is showing."""
    for role in ("button", "link"):
        try:
            control = page.get_by_role(role, name=WAKE_BUTTON).first
            if control.count() > 0:
                control.click(timeout=10_000)
                return True
        except (PlaywrightTimeout, PlaywrightError):
            continue
    # Fall back to a raw text match in case it is neither a button nor a link.
    try:
        control = page.get_by_text(WAKE_BUTTON).first
        if control.count() > 0:
            control.click(timeout=10_000)
            return True
    except (PlaywrightTimeout, PlaywrightError):
        pass
    return False


def main() -> int:
    log(f"target   : {APP_URL}")
    log(f"hold     : {HOLD_SECONDS}s")
    started = time.monotonic()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            # A normal desktop UA; Streamlit Cloud sits behind a CDN and a
            # headless-looking client is more likely to be treated as a bot.
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        sockets: list[str] = []
        page.on("websocket", lambda ws: sockets.append(ws.url))

        try:
            response = page.goto(
                APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            status = response.status if response else "n/a"
            log(f"http     : {status}")

            # If it was hibernating, press the button and let the container boot.
            if SLEEP_MARKER.search(page_text(page)):
                log("state    : app was ASLEEP - clicking the wake-up button")
                if not click_wake_button(page):
                    log("warn     : sleep page detected but no wake control found")
            else:
                log("state    : app was already awake")

            # The websocket is the real signal: Streamlit opens it once the
            # server accepts a session. Cold boots can take a couple of minutes.
            deadline = time.monotonic() + WAKE_TIMEOUT_S
            while time.monotonic() < deadline:
                if any(STREAM_PATH in url for url in sockets):
                    break
                # Re-press the button if the interstitial is still up.
                if SLEEP_MARKER.search(page_text(page)):
                    click_wake_button(page)
                page.wait_for_timeout(2_000)
            else:
                log(f"FAIL     : no {STREAM_PATH} websocket within {WAKE_TIMEOUT_S}s")
                log(f"title    : {page.title()!r}")
                log(f"excerpt  : {page_text(page)[:400]!r}")
                log(f"sockets  : {sockets or 'none'}")
                return 1

            elapsed = time.monotonic() - started
            log(f"session  : websocket open after {elapsed:.1f}s")

            # Hold the session so it registers as a real visit rather than a
            # connect-and-drop.
            page.wait_for_timeout(int(HOLD_SECONDS * 1_000))

            body = page_text(page)
            if SLEEP_MARKER.search(body):
                log("FAIL     : still showing the hibernation page after waking")
                return 1
            if len(body.strip()) < 200:
                log(f"FAIL     : page rendered almost no content ({len(body)} chars)")
                log(f"excerpt  : {body[:400]!r}")
                return 1

            log(f"title    : {page.title()!r}")
            log(f"rendered : {len(body)} chars of body text")
            log(f"OK       : app is awake ({time.monotonic() - started:.1f}s total)")
            return 0

        except PlaywrightTimeout as exc:
            log(f"FAIL     : timed out - {exc}")
            return 1
        except PlaywrightError as exc:
            log(f"FAIL     : browser error - {exc}")
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
