"""
PartsCheck Automation — Quote-directed Microcat selection

Automation flow:

1. Open PartsCheck login page
2. Login automatically
3. Wait for dashboard
4. Detect "Welcome to PartsCheck" modal in ANY frame
5. Select Daniel Turner using changeFingerPrint(23915)
6. Start continuous 2-minute monitoring loop
7. ALWAYS open "New Quote Request"
8. Open Incoming Quotes
9. Find Direct Purchase rows
10. Process Direct Purchase EVEN when status is "Opened"
11. Open quote / price-quote.php page
12. Extract VIN
13. Copy VIN to local clipboard
14. Show browser toast
15. Open Microcat/SuperService in another tab
16. Login automatically if required
17. Open Microcat EPC
18. Paste VIN into #genericSearchInput
19. Navigate only requested part families; select unique matching cart rows
19. Click #genericSearchIcon
20. Return PartsCheck to dashboard
21. Open Microcat Cart after this quote and stop automation
22. Keep browser open for manual review; do not search another VIN

Requirements:

    pip install playwright pyperclip
    playwright install chromium

Scope and review:

    Routes: front/rear bumper, bonnet/hood, front guard/fender, headlamp.
    Other part families are reported UNSUPPORTED_PART, never sent to TOOLS.
    FRONT BUMPER means outer cover/main assembly, excluding reinforcement.
    Different OEM numbers are ambiguous even when descriptions are identical.
    Matching parts are added by double-clicking applicable illustration callouts.
    CART_ADDED_COUNT_CONFIRMED means cart +1 and the OEM row/quantity confirmed.
    cart_verified remains false: no independent cart-detail audit is performed.
    Each quote line is discovered, matched and added before the next is searched.
    Split view exposes the complete virtualized grid before choosing a part.
    Navigation accepts Microcat automatically opening/collapsing subgroup levels.
    No checkout or quote submission. Do not manually change cart while running.
    Reports are keyed by draft ID. Reruns preserve prior cart actions and retry
    only unfinished lines; uncertain additions still require review.
    Preserved successes are historical, not a fresh audit of the current cart.
    Do not remove a report and retry without reviewing existing cart entries.
    Existing login, Welcome modal, and quote-monitoring code is retained.

Run:

    py PartscheckAuto_merged.py
"""

import asyncio
import json
from pathlib import Path
import os
import re
import time
from urllib.parse import urlparse, parse_qs

import pyperclip

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# PARTSCHECK CONFIGURATION
# ============================================================

LOGIN_URL = "https://www.partscheck.com.au/global/login.php"

DASHBOARD_URL = "http://v1.partscheck.com.au/app/dashboard.php"

CHECK_INTERVAL_SECONDS = 120

VIN_REGEX = re.compile(
    r"^[A-HJ-NPR-Z0-9]{17}$"
)

DANIEL_USER_ID = 23915
DANIEL_NAME = "Daniel Turner"

PROCESSED_QUOTES = set()
INSPECTED_QUOTES = set()  # Candidate report produced, pending cart verification.


# ============================================================
# MICROCAt / SUPERSERVICE CONFIGURATION
# ============================================================

MICROCAT_LOGIN_URL = (
    "https://login.superservice.com/login/en-us/"
    "?goto=https:%2F%2Fmicrocat-apac.superservice.com"
    "%2Fcontent%2Fmicrocat-epc%2F%23%2Fidentify"
    "%3FappName%3DMicrocat_EPC"
    "%26subscription%3DDYN0000000008467BB"
    "%26subscriptionAssignment%3DDYN000000001674999"
)

MICROCAT_URL = (
    "https://microcat-apac.superservice.com/content/"
    "microcat-epc/"
    "#/identify?appName=Microcat_EPC"
    "&subscription=DYN0000000008467BB"
    "&subscriptionAssignment=DYN000000001674999"
)

# ============================================================
# MICROCAT CREDENTIALS
# ============================================================

# Environment variables are preferred.
# Fallback credentials are used if environment variables
# are not configured.

MICROCAT_USERNAME = os.getenv(
    "MICROCAT_USERNAME",
    "phillip.mathieson@pattersoncheney.com.au"
)

MICROCAT_PASSWORD = os.getenv(
    "MICROCAT_PASSWORD",
    "1494Patterson!"
)

# This will hold the separate Microcat tab.
microcat_page = None


# ============================================================
# CREDENTIALS
# ============================================================

# Environment variables are preferred.
# If they are not configured, these fallback credentials
# allow the automation to run immediately.

USERNAME = os.getenv(
    "PARTSCHECK_USERNAME",
    "PAK1077"
)

PASSWORD = os.getenv(
    "PARTSCHECK_PASSWORD",
    "Catfish0125"
)

# ============================================================
# SMALL HELPERS
# ============================================================

SCRIPT_VERSION = '2026-09-23-r7-bumper-finish'
STOP_AFTER_CURRENT_QUOTE = False

# Never replay a cart-affecting action automatically after a restart.
PRESERVED_CART_STATUSES = {
    'CART_ADDED_COUNT_CONFIRMED', 'CART_CONTROL_SELECTED', 'SELECTION_IN_PROGRESS',
    'ADD_IN_PROGRESS', 'ADD_UNCERTAIN_REVIEW', 'QUANTITY_UPDATE_IN_PROGRESS',
    'ALREADY_SELECTED_REVIEW',
}


def draft_id_from_action(action):
    match = re.search(r"onclick_request\s*\(\s*['\"](\d+)['\"]", action or '')
    return match.group(1) if match else None


async def row_draft_id(row):
    action = row.locator("[onclick*='onclick_request']").first
    if await action.count():
        value = draft_id_from_action(await action.get_attribute('onclick'))
        if value: return value
    return await row.get_attribute('data-draftid')


def prepare_resume_report(previous, requests, vin, draft_id):
    fresh = [{'request': r, 'candidates': [], 'status': 'PENDING',
              'cart_verified': False} for r in requests]
    if previous is None: return fresh
    if str(previous.get('draft_id')) != str(draft_id) or previous.get('vin', '').upper() != vin.upper():
        raise RuntimeError('Saved report VIN/draft ID differs; report must be reconciled before reuse')
    def signature(r):
        return (normalize_part(r['description']), int(r['quantity']),
                re.sub(r'[^A-Z0-9]', '', r.get('part_number', '').upper()))
    old = previous.get('parts', [])
    if [signature(e['request']) for e in old] != [signature(r) for r in requests]:
        raise RuntimeError('Quote lines changed since the saved cart report; review existing additions first')
    for entry, saved in zip(fresh, old):
        if saved.get('status') in PRESERVED_CART_STATUSES:
            entry.update(saved)
            entry['preserved_from_previous_run'] = True
        else:
            entry['previous_attempt'] = saved
    return fresh


async def safe_inner_text(locator, timeout=1500):
    """
    Safely get inner text from a Playwright locator.
    """

    try:
        return (
            await locator.inner_text(
                timeout=timeout
            )
        ).strip()

    except Exception:
        return ""


async def is_visible(locator):
    """
    Safely check whether a locator is visible.
    """

    try:
        return await locator.is_visible()

    except Exception:
        return False


# ============================================================
# PARTSCHECK LOGIN
# ============================================================

async def auto_login(page):
    """
    Login to PartsCheck and wait until dashboard.php
    is ready.
    """

    print("🌐 Navigating to PartsCheck login page...")

    await page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    # --------------------------------------------------------
    # Existing saved session
    # --------------------------------------------------------

    remember_me_btn = await page.query_selector(
        "#loginRememberMeButton"
    )

    if (
        remember_me_btn
        and await remember_me_btn.is_visible()
    ):

        text = await remember_me_btn.inner_text()

        print(
            f"  🖱️ Found saved session: "
            f"'{text.strip()}' — clicking..."
        )

        await remember_me_btn.click()

    else:

        print(
            "  🔐 No saved session found — "
            "entering credentials..."
        )

        # ----------------------------------------------------
        # Make login area visible if hidden
        # ----------------------------------------------------

        login_area = await page.query_selector(
            "#loginArea"
        )

        if login_area:

            style = (
                await login_area.get_attribute(
                    "style"
                )
                or ""
            )

            if "display: none" in style:

                await page.evaluate(
                    """
                    () => {
                        const el =
                            document.getElementById(
                                "loginArea"
                            );

                        if (el) {
                            el.style.display = "block";
                        }
                    }
                    """
                )

        # ----------------------------------------------------
        # Username
        # ----------------------------------------------------

        await page.fill(
            "#myuser",
            USERNAME,
        )

        # ----------------------------------------------------
        # Password
        # ----------------------------------------------------

        await page.fill(
            "#mypass",
            PASSWORD,
        )

        # ----------------------------------------------------
        # Remember Me
        # ----------------------------------------------------

        remember_checkbox = await page.query_selector(
            "#rememberMe"
        )

        if remember_checkbox:

            try:

                if not await remember_checkbox.is_checked():

                    await remember_checkbox.check()

            except Exception:
                pass

        # ----------------------------------------------------
        # Submit
        # ----------------------------------------------------

        await page.click(
            "#loginButton"
        )

        print(
            "  🖱️ Submitted login form"
        )

    # --------------------------------------------------------
    # Wait for dashboard
    # --------------------------------------------------------

    print(
        "  ⏳ Waiting for dashboard to load..."
    )

    try:

        await page.wait_for_selector(
            ".toplisttext",
            timeout=60_000,
        )

    except PlaywrightTimeoutError:

        print(
            "  ⚠️ Dashboard selector was not found."
        )

        print(
            f"  🌐 Current URL: {page.url}"
        )

        raise

    print(
        f"  🌐 Current URL: {page.url}"
    )

    print(
        "  ✅ Logged in! Dashboard is ready.\n"
    )


# ============================================================
# FIND WELCOME MODAL IN ANY FRAME
# ============================================================

async def find_welcome_frame(
    page,
    timeout_seconds=60,
):
    """
    Search every frame for the PartsCheck Welcome modal.
    """

    print(
        "  👀 Waiting for "
        "'Welcome to PartsCheck' modal..."
    )

    deadline = (
        time.monotonic()
        + timeout_seconds
    )

    while time.monotonic() < deadline:

        frames = list(page.frames)

        for frame in frames:

            try:

                body = frame.locator("body")

                if await body.count() == 0:
                    continue

                text = await body.inner_text(
                    timeout=1200
                )

                if (
                    "Welcome to PartsCheck"
                    in text
                    or
                    "Please fill in details or "
                    "select your name from below"
                    in text
                ):

                    print(
                        "  📋 Welcome modal detected!"
                    )

                    print(
                        f"  🔎 Modal frame: "
                        f"{frame.url}"
                    )

                    return frame

            except Exception:
                continue

        await asyncio.sleep(0.5)

    print(
        f"  ⚠️ Welcome modal was not found "
        f"within {timeout_seconds} seconds."
    )

    print(
        "  🐛 Available frames:"
    )

    for frame in page.frames:

        print(
            f"      - {frame.url}"
        )

    return None


# ============================================================
# HANDLE WELCOME MODAL / DANIEL TURNER
# ============================================================

async def handle_welcome_modal(
    page,
    required=True,
):
    """
    Handle PartsCheck user-selection modal.
    """

    frame = await find_welcome_frame(
        page,
        timeout_seconds=60 if required else 10,
    )

    if frame is None:

        if required:
            return False

        print(
            "  ℹ️ No Welcome modal detected."
        )

        return True

    print(
        f"  👤 Waiting for {DANIEL_NAME}..."
    )

    daniel_row = None

    deadline = (
        time.monotonic()
        + 30
    )

    while time.monotonic() < deadline:

        try:

            rows = frame.locator(
                "div.userRow"
            )

            count = await rows.count()

            for i in range(count):

                row = rows.nth(i)

                text = await safe_inner_text(
                    row,
                    timeout=1200,
                )

                if DANIEL_NAME.lower() in text.lower():

                    daniel_row = row

                    break

            if daniel_row is not None:
                break

        except Exception:
            pass

        await asyncio.sleep(0.5)

    if daniel_row is None:

        print(
            f"  ❌ {DANIEL_NAME} was not found "
            "inside the Welcome modal."
        )

        try:

            rows = frame.locator(
                "div.userRow"
            )

            count = await rows.count()

            print(
                "  🐛 Available user rows:"
            )

            for i in range(count):

                text = await safe_inner_text(
                    rows.nth(i)
                )

                print(
                    f"      Row {i + 1}: "
                    f"{text!r}"
                )

        except Exception:
            pass

        return False

    print(
        f"  ✅ Found {DANIEL_NAME}."
    )

    try:

        onclick = await daniel_row.get_attribute(
            "onclick"
        )

        print(
            f"  🐛 Daniel onclick: {onclick}"
        )

    except Exception:
        onclick = None

    print(
        f"  🖱️ Selecting {DANIEL_NAME}..."
    )

    clicked = False

    # --------------------------------------------------------
    # IMPORTANT:
    # Keep the original working DOM click.
    # --------------------------------------------------------

    try:

        await daniel_row.evaluate(
            """
            (element) => {
                element.click();
            }
            """
        )

        clicked = True

        print(
            "  ✅ Daniel DOM click executed."
        )

    except Exception as e:

        print(
            f"  ⚠️ DOM click failed: {e}"
        )

    # --------------------------------------------------------
    # Fallback exact function
    # --------------------------------------------------------

    if not clicked:

        try:

            await frame.evaluate(
                """
                (userId) => {

                    if (
                        typeof window.changeFingerPrint
                        !== "function"
                    ) {
                        throw new Error(
                            "changeFingerPrint() "
                            "is not available "
                            "in this frame."
                        );
                    }

                    window.changeFingerPrint(userId);
                }
                """,
                DANIEL_USER_ID,
            )

            clicked = True

            print(
                "  ✅ changeFingerPrint(23915) "
                "executed."
            )

        except Exception as e:

            print(
                f"  ❌ Could not execute "
                f"changeFingerPrint(): {e}"
            )

    if not clicked:
        return False

    print(
        "  ⏳ Waiting for Daniel selection "
        "to finish..."
    )

    deadline = (
        time.monotonic()
        + 30
    )

    while time.monotonic() < deadline:

        try:

            if frame not in page.frames:

                print(
                    "  ✅ Welcome iframe disappeared."
                )

                break

            body = frame.locator("body")

            text = await body.inner_text(
                timeout=1000
            )

            if (
                "Please fill in details or "
                "select your name from below"
                not in text
                and
                DANIEL_NAME.lower()
                not in text.lower()
            ):

                print(
                    "  ✅ Welcome modal closed."
                )

                break

        except Exception:
            break

        await asyncio.sleep(0.5)

    await asyncio.sleep(1)

    print(
        "  ✅ Daniel Turner selection completed."
    )

    return True


# ============================================================
# SESSION CHECK
# ============================================================

async def ensure_logged_in(page):

    current_url = page.url.lower()

    if (
        "login.php" in current_url
        or "index.php" in current_url
    ):

        print(
            "  🔄 Session expired — "
            "logging in again..."
        )

        await auto_login(page)

        await handle_welcome_modal(
            page,
            required=True,
        )


# ============================================================
# QUOTE COUNTER
# ============================================================

async def get_new_quote_count(page):

    try:

        counter = await page.query_selector(
            "#counter_quoteNew"
        )

        if counter is None:

            print(
                "  🐛 [debug] "
                "#counter_quoteNew not found"
            )

            return 0

        class_list = (
            await counter.get_attribute(
                "class"
            )
            or ""
        )

        text = await counter.inner_text()

        print(
            f"  🐛 [debug] counter raw text="
            f"'{text.strip()}' "
            f"class='{class_list.strip()}'"
        )

        if "hide" in class_list:
            return 0

        value = text.strip()

        return (
            int(value)
            if value.isdigit()
            else 0
        )

    except Exception as e:

        print(
            f"  ⚠️ Could not read quote counter: {e}"
        )

        return 0


# ============================================================
# OPEN NEW QUOTE REQUEST
# ============================================================

async def click_new_quote_request(page):

    try:

        print(
            "  🔎 Looking for "
            "'New Quote Request'..."
        )

        # ----------------------------------------------------
        # Already open?
        # ----------------------------------------------------

        try:

            colorbox = page.locator(
                "#colorbox"
            )

            if await colorbox.is_visible():

                print(
                    "  ℹ️ Incoming Quotes "
                    "is already open."
                )

                return True

        except Exception:
            pass

        # ----------------------------------------------------
        # ORIGINAL WORKING SELECTOR
        # ----------------------------------------------------

        elements = page.locator(
            ".toplist.cboxlink"
        )

        count = await elements.count()

        print(
            f"  🔎 Found {count} dashboard "
            "navigation element(s)."
        )

        for i in range(count):

            el = elements.nth(i)

            text = await safe_inner_text(
                el
            )

            if (
                "New Quote Request"
                not in text
            ):
                continue

            print(
                "  ✅ Found "
                "'New Quote Request'."
            )

            await el.scroll_into_view_if_needed()

            await el.evaluate(
                """
                (element) => {
                    element.click();
                }
                """
            )

            print(
                "  🖱️ Clicked "
                "'New Quote Request'."
            )

            try:

                await page.wait_for_selector(
                    "#colorbox",
                    state="visible",
                    timeout=15_000,
                )

                print(
                    "  ✅ Incoming Quotes "
                    "modal opened."
                )

                return True

            except PlaywrightTimeoutError:

                print(
                    "  ⚠️ Click executed but "
                    "Incoming Quotes modal "
                    "did not appear."
                )

                return False

        print(
            "  ❌ 'New Quote Request' "
            "element was not found."
        )

        return False

    except Exception as e:

        print(
            f"  ⚠️ Error clicking "
            f"New Quote Request: {e}"
        )

        return False


# ============================================================
# GET QUOTES FRAME
# ============================================================

async def get_quotes_frame(page):

    print(
        "  ⏳ Waiting for Incoming Quotes popup..."
    )

    await page.wait_for_selector(
        "#colorbox",
        state="visible",
        timeout=15_000,
    )

    iframe_element = await page.wait_for_selector(
        "#cboxContent iframe",
        timeout=15_000,
    )

    frame = await iframe_element.content_frame()

    if frame is None:

        raise RuntimeError(
            "Could not access Incoming Quotes iframe."
        )

    await frame.wait_for_selector(
        ".requestRow",
        timeout=15_000,
    )

    return frame


# ============================================================
# VIN EXTRACTION
# ============================================================

async def extract_vin_from_page(target_page):

    try:

        await target_page.wait_for_load_state(
            "domcontentloaded",
            timeout=20_000,
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # Primary VIN lookup
    # --------------------------------------------------------

    try:

        vin_title = target_page.locator(
            "div.quoteTitle"
        ).filter(
            has_text="VIN"
        ).first

        await vin_title.wait_for(
            state="visible",
            timeout=15_000,
        )

        vin_input = vin_title.locator(
            "xpath=following-sibling::"
            "div[contains(@class,"
            "'quoteTitleContent')][1]"
            "//input"
        ).first

        await vin_input.wait_for(
            state="visible",
            timeout=15_000,
        )

        value = (
            await vin_input.input_value()
        ).strip().upper()

        if VIN_REGEX.match(value):

            print(
                f"    🔎 VIN found: {value}"
            )

            return value

        print(
            f"    ⚠️ VIN field found but "
            f"value is not a valid VIN: "
            f"{value!r}"
        )

    except Exception as e:

        print(
            f"    ⚠️ Primary VIN lookup failed: "
            f"{e}"
        )

    # --------------------------------------------------------
    # Fallback: scan all inputs
    # --------------------------------------------------------

    print(
        "    🔎 Scanning all input fields "
        "for VIN..."
    )

    try:

        inputs = await target_page.locator(
            "input"
        ).all()

        for inp in inputs:

            try:

                value = (
                    await inp.input_value()
                ).strip().upper()

            except Exception:
                continue

            if VIN_REGEX.match(value):

                print(
                    f"    🔎 VIN found via fallback: "
                    f"{value}"
                )

                return value

    except Exception as e:

        print(
            f"    ⚠️ VIN fallback failed: {e}"
        )

    return None


# ============================================================
# COPY VIN + SHOW TOAST
# ============================================================

async def copy_vin_and_show_toast(
    target_page,
    vin,
):

    pyperclip.copy(vin)

    print(
        f"    📋 VIN copied to clipboard: {vin}"
    )

    try:

        await target_page.evaluate(
            """
            (vin) => {

                const oldToast =
                    document.getElementById(
                        "partscheck-vin-toast"
                    );

                if (oldToast) {
                    oldToast.remove();
                }

                const toast =
                    document.createElement("div");

                toast.id =
                    "partscheck-vin-toast";

                toast.textContent =
                    "✅ VIN copied to clipboard: "
                    + vin;

                Object.assign(
                    toast.style,
                    {
                        position: "fixed",
                        bottom: "30px",
                        right: "30px",
                        background: "#1a7f37",
                        color: "white",
                        padding: "14px 20px",
                        borderRadius: "8px",
                        fontFamily:
                            "Arial, sans-serif",
                        fontSize: "14px",
                        boxShadow:
                            "0 4px 12px "
                            + "rgba(0,0,0,0.25)",
                        zIndex: "999999"
                    }
                );

                document.body.appendChild(
                    toast
                );

                setTimeout(
                    () => {

                        if (toast) {
                            toast.remove();
                        }

                    },
                    4000
                );
            }
            """,
            vin,
        )

    except Exception as e:

        print(
            f"    ⚠️ Could not show browser toast: "
            f"{e}"
        )


# ============================================================
# MICROCAt LOGIN
# ============================================================

async def open_and_login_microcat(context, vin):

    global microcat_page

    print(
        "\n    🔧 Starting Microcat automation..."
    )

    if not MICROCAT_USERNAME or not MICROCAT_PASSWORD:

        raise RuntimeError(
            "Microcat credentials are not configured.\n"
            "Set MICROCAT_USERNAME and "
            "MICROCAT_PASSWORD environment variables."
        )

    # --------------------------------------------------------
    # If Microcat tab does not exist, create it.
    # --------------------------------------------------------

    if (
        microcat_page is None
        or microcat_page.is_closed()
    ):

        print(
            "    🌐 Opening Microcat in a new tab..."
        )

        microcat_page = await context.new_page()

        await microcat_page.goto(
            MICROCAT_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    else:

        print(
            "    🌐 Using existing Microcat tab..."
        )

    # --------------------------------------------------------
    # Check current page.
    # --------------------------------------------------------

    current_url = microcat_page.url.lower()

    # --------------------------------------------------------
    # Login page
    # --------------------------------------------------------

    if "login.superservice.com" in current_url:

        print(
            "    🔐 Microcat login page detected."
        )

        print(
            "    👤 Entering Microcat username..."
        )

        await microcat_page.wait_for_selector(
            "#username",
            state="visible",
            timeout=30_000,
        )

        await microcat_page.fill(
            "#username",
            MICROCAT_USERNAME,
        )

        print(
            "    🔑 Entering Microcat password..."
        )

        await microcat_page.fill(
            "#passwordInput",
            MICROCAT_PASSWORD,
        )

        print(
            "    🖱️ Clicking Microcat Log In..."
        )

        await microcat_page.click(
            "#loginButton"
        )

        # ----------------------------------------------------
        # Wait for Microcat navigation
        # ----------------------------------------------------

        print(
            "    ⏳ Waiting for Microcat EPC..."
        )

        try:

            await microcat_page.wait_for_url(
                "**microcat-epc/***",
                timeout=60_000,
            )

        except PlaywrightTimeoutError:

            print(
                "    ⚠️ Microcat URL was not "
                "detected within 60 seconds."
            )

            print(
                f"    🌐 Current URL: "
                f"{microcat_page.url}"
            )

            # Give Angular application additional time.
            await asyncio.sleep(5)

    # --------------------------------------------------------
    # Make sure we are on EPC.
    # --------------------------------------------------------

    if "microcat-epc" not in microcat_page.url.lower():

        print(
            "    🌐 Navigating directly to Microcat EPC..."
        )

        await microcat_page.goto(
            MICROCAT_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    # Return reused Microcat tabs to vehicle identification for the next quote.
    if not await microcat_page.locator("#genericSearchInput").is_visible():
        await microcat_page.get_by_text("Identify Vehicle", exact=True).click()

    # --------------------------------------------------------
    # Wait for VIN search input
    # --------------------------------------------------------

    print(
        "    ⏳ Waiting for Microcat VIN search field..."
    )

    await microcat_page.wait_for_selector(
        "#genericSearchInput",
        state="visible",
        timeout=60_000,
    )

    # --------------------------------------------------------
    # Fill VIN
    # --------------------------------------------------------

    print(
        f"    📋 Entering VIN into Microcat: {vin}"
    )

    search_input = microcat_page.locator(
        "#genericSearchInput"
    )

    await search_input.click()

    await search_input.fill("")

    await search_input.fill(vin)

    # --------------------------------------------------------
    # Verify value
    # --------------------------------------------------------

    entered_value = (
        await search_input.input_value()
    ).strip().upper()

    print(
        f"    🐛 Microcat search field value: "
        f"{entered_value}"
    )

    if entered_value != vin.upper():

        raise RuntimeError(
            "Microcat VIN field did not contain "
            "the expected VIN."
        )

    # --------------------------------------------------------
    # Click search
    # --------------------------------------------------------

    print(
        "    🔎 Clicking Microcat search..."
    )

    search_button = microcat_page.locator(
        "#genericSearchIcon"
    )

    await search_button.wait_for(
        state="visible",
        timeout=15_000,
    )

    await search_button.click()

    print(
        "    ✅ Microcat VIN search submitted."
    )

    # --------------------------------------------------------
    # Wait for Angular to process search
    # --------------------------------------------------------

    await asyncio.sleep(5)

    print(
        f"    🌐 Microcat current URL: "
        f"{microcat_page.url}"
    )

    print(
        "    ✅ Microcat automation completed."
    )


# ============================================================
# MICROCAt QUOTE PART DISCOVERY
# ============================================================

MICROCAT_INDEX = ".mat-content-wrapper.section-index:visible"
MICROCAT_ILLUSTRATION = ".mat-content-wrapper.section-illustration:visible"
MICROCAT_CARD = '[e2e-id="graphical-index-card"]'
MICROCAT_PART = '#partDataDialog:visible'


async def extract_quote_parts(quote_page):
    """Read the requested description/quantity/optional part number by header.

    Returns [] if the quote table structure differs, never guessed lines.
    """
    return await quote_page.evaluate("""() => {
        const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
        for (const table of document.querySelectorAll('table')) {
            const rows = Array.from(table.rows).filter(r => r.closest('table') === table);
            const header = rows.find(r => {
                const t = norm(r.innerText).toLowerCase();
                return t.includes('description') && t.includes('part number') && t.includes('qty');
            });
            if (!header) continue;
            const cells = Array.from(header.cells);
            const names = cells.map(c => norm(c.innerText).toLowerCase());
            const index = word => names.findIndex(s => s.includes(word));
            const d = index('description'), n = index('part number'), q = index('qty');
            if (d < 0 || n < 0 || q < 0) continue;
            const result = [];
            for (const row of rows.slice(rows.indexOf(header) + 1)) {
                const columns = Array.from(row.querySelectorAll(':scope > td'));
                if (columns.length <= Math.max(d,n,q)) continue;
                const value = cell => norm(cell.querySelector('input')?.value || cell.innerText);
                const description = value(columns[d]);
                if (!description) continue;
                const quantity = Number(value(columns[q]));
                if (!Number.isInteger(quantity) || quantity < 1) throw new Error('Invalid quantity for ' + description);
                result.push({description, quantity,
                             part_number: value(columns[n])});
            }
            if (result.length) return result;
        }
        return [];
    }""")


# Quote-directed Microcat selection. Unsupported/ambiguous requests fail closed.
# Extend ROUTES for additional part families; never default to the first category.
ROUTES = {
    'bumper_front': ('BODY', r'\bFRONT BUMPER\b'),
    'bumper_rear': ('BODY', r'\bREAR BUMPER\b'),
    'hood': ('BODY', r'\bHOOD\b|\bBONNET\b'),
    'fender': ('BODY', r'\bFENDER\b|\bGUARD\b'),
    'headlamp': ('ELECTRICAL', r'\bHEAD\s*LAMP\b|\bHEADLIGHT\b'),
}


def normalize_part(text):
    text = text.upper().replace('BUMBER', 'BUMPER')
    for pattern, replacement in [
        (r'\bL\s*/\s*F\b', 'LH FRONT'), (r'\bR\s*/\s*F\b', 'RH FRONT'),
        (r'\bL\s*/\s*H\b|\bLEFT(?: HAND)?\b', 'LH'),
        (r'\bR\s*/\s*H\b|\bRIGHT(?: HAND)?\b', 'RH'),
        (r'\bBONNET\b', 'HOOD'), (r'\bGUARD\b', 'FENDER'),
        (r'\bHEAD\s*LIGHT\b|\bHEAD\s+LAMP\b', 'HEADLAMP'),
    ]:
        text = re.sub(pattern, replacement, text)
    return ' '.join(re.findall(r'[A-Z0-9]+', text))


def request_family(description):
    words = set(normalize_part(description).split())
    if 'BUMPER' in words:
        if 'FRONT' in words: return 'bumper_front'
        if 'REAR' in words: return 'bumper_rear'
    for family, word in [('hood', 'HOOD'), ('headlamp', 'HEADLAMP'), ('fender', 'FENDER')]:
        if word in words: return family
    return None


def matches_requested_part(request, item):
    number = lambda s: re.sub(r'[^A-Z0-9]', '', s.upper())
    wanted_number = number(request.get('part_number', ''))
    if wanted_number:
        return wanted_number == number(item['number'])
    wanted = set(normalize_part(request['description']).split())
    actual_description = normalize_part(item['description'])
    # Toyota's trailing PAINT REQ. is a finish note on the bumper cover.
    # Remove only this exact suffix for a bumper request without finish criteria.
    # Keep HOLE, SUPPORT, colour codes and all other component/variant qualifiers.
    if request_family(request['description']) in {'bumper_front', 'bumper_rear'} and not wanted.intersection({'PAINT', 'REQ', 'REQUIRED', 'PAINTED', 'UNPAINTED'}):
        actual_description = re.sub(r'\s+PAINT REQ$', '', actual_description)
    actual = set(actual_description.split())
    family = request_family(request['description'])
    if not family: return False
    for side, opposite in [('LH', 'RH'), ('RH', 'LH')]:
        if side in wanted and (side not in actual or opposite in actual): return False
    if not wanted.intersection({'LH', 'RH'}) and actual.intersection({'LH', 'RH'}):
        return False
    # A bracket, reinforcement, bulb, etc. is not the requested main assembly.
    components = {'SUPPORT', 'REINFORCEMENT', 'STAY', 'BRACKET', 'RETAINER',
                  'CLIP', 'BOLT', 'SCREW', 'NUT', 'WASHER', 'SEAL', 'MOULDING',
                  'MOLDING', 'GRILLE', 'EXTENSION', 'ABSORBER', 'PROTECTOR',
                  'LINER', 'LINING', 'HINGE', 'LOCK', 'LATCH', 'CABLE',
                  'INSULATOR', 'BULB', 'SOCKET', 'LENS', 'GASKET', 'HARNESS',
                  'MOTOR', 'ACTUATOR', 'BEZEL', 'GROMMET', 'PAD', 'CUSHION'}
    if (actual & components) - wanted: return False
    if family.startswith('bumper_'):
        if family.split('_')[1].upper() not in actual: return False
        if 'BUMPER' not in actual: return False
        # FRONT BUMPER is treated as the outer cover, not the bumper reinforcement.
        if not wanted.intersection(components) and not actual.intersection({'COVER', 'ASSY', 'ASSEMBLY'}):
            return False
    elif family == 'hood':
        if 'HOOD' not in actual or 'COVER' in actual: return False
    elif family == 'headlamp':
        if 'HEADLAMP' not in actual: return False
        if 'UNIT' in actual and not actual.intersection({'ASSY', 'ASSEMBLY'}): return False
    elif family == 'fender':
        if 'FENDER' not in actual or 'REAR' in actual: return False
        if 'FRONT' in wanted and 'FRONT' not in actual: return False
    # A main-part request accepts only the assembly/cover/panel itself.
    # Additional unrequested words (e.g. HOLE COVER, WASHER CAP, REPAIR KIT)
    # require review even if a catalogue contains only one such row.
    if not wanted.intersection(components):
        core = {
            'bumper_front': {'COVER', 'FRONT', 'BUMPER', 'ASSY', 'ASSEMBLY', 'SUB'},
            'bumper_rear': {'COVER', 'REAR', 'BUMPER', 'ASSY', 'ASSEMBLY', 'SUB'},
            'hood': {'PANEL', 'HOOD', 'ASSY', 'ASSEMBLY', 'SUB'},
            'headlamp': {'HEADLAMP', 'UNIT', 'ASSY', 'ASSEMBLY', 'SUB', 'LH', 'RH'},
            'fender': {'PANEL', 'FENDER', 'FRONT', 'ASSY', 'ASSEMBLY', 'SUB', 'LH', 'RH'},
        }[family]
        if actual - core - wanted: return False
    # Preserve explicitly requested component, material and variant qualifiers.
    ignored = {'FRONT', 'REAR', 'LH', 'RH', 'BUMPER', 'HOOD', 'FENDER',
               'HEADLAMP', 'ASSY', 'ASSEMBLY', 'SUB', 'PANEL'}
    return wanted - ignored <= actual


async def microcat_view(page):
    for _ in range(60):
        if await page.locator(MICROCAT_ILLUSTRATION).count(): return 'illustration'
        if await page.locator(MICROCAT_INDEX).count(): return 'index'
        await asyncio.sleep(.25)
    raise RuntimeError('Microcat index/illustration not visible')


async def microcat_depth(page):
    scope = MICROCAT_ILLUSTRATION if await microcat_view(page) == 'illustration' else MICROCAT_INDEX
    return await page.locator(f'{scope} epc-section-index').first.locator(
        '[e2e-id="breadcrumb-crumb"][class*="item"]').count() - 1


async def wait_depth(page, depth):
    for _ in range(60):
        if await microcat_depth(page) == depth:
            await asyncio.sleep(.4)
            return
        await asyncio.sleep(.25)
    raise RuntimeError(f'Microcat navigation did not reach depth {depth}')


async def wait_depth_change(page, previous, direction):
    """Microcat may auto-open/collapse more than one breadcrumb level."""
    for _ in range(60):
        current = await microcat_depth(page)
        if (direction == 'down' and current > previous) or (direction == 'up' and current < previous):
            await asyncio.sleep(.4)
            return current
        await asyncio.sleep(.25)
    raise RuntimeError(f'Microcat did not navigate {direction} from depth {previous}')


async def root_index(page):
    for _ in range(10):
        depth = await microcat_depth(page)
        if depth == 0 and await microcat_view(page) == 'index': return
        if depth <= 0: break
        await page.locator('#hierarchyMoveUpButton:visible').first.click()
        await wait_depth_change(page, depth, 'up')
    raise RuntimeError('Could not return to root Illustration Index')


async def restore_index_path(page, path):
    # Replay known tile IDs instead of assuming one Up click reverses a tile click.
    await root_index(page)
    for card in path:
        await open_card(page, card)
    if await microcat_view(page) != 'index':
        raise RuntimeError('Expected parent index while returning from illustration')


# The supplied DOM uses virtual lists/grids. Scroll their actual parent instead
# of assuming the first six rendered tiles or first three rows are the full list.
async def scroll_container(scope, seed_selector, reset=False):
    return await scope.evaluate('''(scope, args) => {
        const seed = scope.querySelector(args.seed);
        if (!seed) return {bottom: true, moved: false};
        let el = seed.parentElement;
        while (el && el !== document.body) {
            const overflow = getComputedStyle(el).overflowY;
            if (/(auto|scroll)/.test(overflow) && el.scrollHeight > el.clientHeight + 2) break;
            el = el.parentElement;
        }
        if (!el || el === document.body) return {bottom: true, moved: false};
        const before = el.scrollTop;
        el.scrollTop = args.reset ? 0 : Math.min(el.scrollTop + Math.max(50, el.clientHeight * .65), el.scrollHeight);
        el.dispatchEvent(new Event('scroll', {bubbles: true}));
        return {bottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 2,
                moved: Math.abs(before - el.scrollTop) > 1};
    }''', {'seed': seed_selector, 'reset': reset})


async def index_cards(page):
    scope = page.locator(MICROCAT_INDEX)
    await scope.locator(MICROCAT_CARD).first.wait_for(state='attached', timeout=15000)
    await scroll_container(scope, MICROCAT_CARD, True)
    await asyncio.sleep(.2)
    found = {}
    for _ in range(120):
        cards = await scope.locator(MICROCAT_CARD).evaluate_all('''els => els.map(e => ({
            id: e.id, label: e.querySelector('img')?.alt || e.innerText
        }))''')
        for card in cards:
            if not card['id']: raise RuntimeError('Index tile missing stable ID')
            found[card['id']] = {'id': card['id'], 'label': ' '.join(card['label'].split())}
        result = await scroll_container(scope, MICROCAT_CARD)
        await asyncio.sleep(.2)
        if not result['moved']: return list(found.values())
    raise RuntimeError('Index scan incomplete; refusing partial candidate selection')


async def open_card(page, card):
    scope = page.locator(MICROCAT_INDEX)
    await scroll_container(scope, MICROCAT_CARD, True)
    await asyncio.sleep(.2)
    for _ in range(120):
        target = scope.locator(f'{MICROCAT_CARD}[id={json.dumps(card["id"])}]')
        if await target.count() == 1:
            depth = await microcat_depth(page)
            await target.click()
            await wait_depth_change(page, depth, 'down')
            return
        result = await scroll_container(scope, MICROCAT_CARD)
        await asyncio.sleep(.2)
        if not result['moved']: break
    raise RuntimeError(f'Cannot find tile {card["label"]}')


async def populated_grid_snapshot(scope, timeout=30):
    """Wait for actual data, not an attached AG Grid shell or aria-rowcount=1."""
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        snapshot = await scope.evaluate('''scope => {
            const grid = scope.querySelector('#partsGrid [role="grid"]');
            const rows = [...scope.querySelectorAll('#partsGrid .ag-center-cols-container .ag-row')].map(row => {
                const text = id => (row.querySelector('[col-id="'+id+'ColumnIllustration"]')?.textContent || '').trim();
                return {row_index: row.getAttribute('row-index'), number: text('partNumber'),
                    description: text('description'), pnc: text('pnc'), model_codes: text('modelCodes'),
                    selectable: !!row.querySelector('epc-cart-cell-select-component input[type=checkbox]:not(:disabled)'),
                    non_applicable: !!row.querySelector('.incorrect, .non-applicable')};
            });
            const count = Number(grid?.getAttribute('aria-rowcount') || 0);
            return {expected: count > 1 ? count - 1 : null, rows};
        }''')
        rows = snapshot['rows']
        ready = bool(rows) and all(r['number'] and r['description'] and r['pnc'] for r in rows)
        signature = json.dumps(snapshot, sort_keys=True)
        stable = stable + 1 if ready and signature == previous else 0
        previous = signature
        if ready and stable >= 3:
            return snapshot
        await asyncio.sleep(.3)
    raise RuntimeError('Parts grid did not populate with OEM numbers/descriptions; this is a loading failure, not NOT_FOUND')


async def read_illustrated_rows(page, path):
    print('    Waiting for populated illustrated parts in split view...', flush=True)
    await ensure_parts_split_view(page)
    scope = page.locator(MICROCAT_ILLUSTRATION)
    rows_selector = '#partsGrid .ag-center-cols-container .ag-row'
    # Keep identity and description columns rendered when horizontal virtualization is enabled.
    await scope.locator('#partsGrid .ag-center-cols-viewport').evaluate_all(
        "els => els.forEach(e => {e.scrollLeft=0; e.dispatchEvent(new Event('scroll'));})")
    await populated_grid_snapshot(scope)
    await scroll_container(scope, rows_selector, True)
    found, seen_rows = {}, set()
    expected_rows = None
    for _ in range(150):
        snapshot = await populated_grid_snapshot(scope)
        if expected_rows is None:
            expected_rows = snapshot['expected']
        elif snapshot['expected'] != expected_rows:
            raise RuntimeError('Parts grid changed size during scan; refusing partial matches')
        for item in snapshot['rows']:
            seen_rows.add(item['row_index'])
            found[(item['number'], item['description'], item['model_codes'])] = {**item, 'path': path}
        result = await scroll_container(scope, rows_selector)
        if not result['moved']:
            if not found:
                raise RuntimeError('Grid scan returned no populated rows')
            if expected_rows is not None and len(seen_rows) != expected_rows:
                raise RuntimeError(f'Incomplete parts scan: {len(seen_rows)}/{expected_rows} rows')
            print(f'    Read {len(seen_rows)} illustrated rows.', flush=True)
            return list(found.values())
        await asyncio.sleep(.3)
    raise RuntimeError('Parts grid scan incomplete')


async def reconcile_empty_cart_history(page, report):
    """Only an observed empty current cart permits resetting historical successes.

    A nonempty cart cannot be reconciled by count alone; preserve those entries.
    Uncertain prior writes remain for review, regardless of the counter.
    """
    confirmed = {'CART_ADDED_COUNT_CONFIRMED', 'CART_CONTROL_SELECTED'}
    if not any(e['status'] in confirmed for e in report): return
    await root_index(page)
    for _ in range(8):
        if await cart_count(page) != 0: return
        await asyncio.sleep(.5)
    for entry in report:
        if entry['status'] in confirmed:
            entry['historical_addition'] = {k: v for k, v in entry.items() if k != 'historical_addition'}
            entry.update(status='PENDING', candidates=[], cart_verified=False,
                         cart_count_verified=False, quantity_verified=False,
                         preserved_from_previous_run=False)
            print(f"    Current cart is empty: rechecking {entry['request']['description']}.", flush=True)


async def discover_microcat_parts(page, requests, *, max_views=100):
    await root_index(page)
    routes = {request_family(r['description']) for r in requests}
    routes.discard(None)
    records, views = [], 0

    async def walk(path, families):
        nonlocal views
        views += 1
        if views > max_views: raise RuntimeError('Targeted navigation limit reached')
        if await microcat_view(page) == 'illustration':
            records.extend(await read_illustrated_rows(page, path))
            return
        for card in await index_cards(page):
            label = normalize_part(card['label'])
            relevant = {family for family in families
                        if re.search(ROUTES[family][1], label)}
            if not relevant: continue
            depth = await microcat_depth(page)
            print(f'    → {card["label"]}')
            await open_card(page, card)
            await walk(path + [card], relevant)
            await restore_index_path(page, path)

    for card in await index_cards(page):
        label = normalize_part(card['label'])
        families = {family for family in routes if re.search(r'\b' + ROUTES[family][0] + r'\b', label)}
        if not families: continue
        print(f'    → {card["label"]}')
        await open_card(page, card)
        await walk([card], families)
        await root_index(page)
    return records


def compare_quote_to_microcat(requests, catalogue):
    report = []
    for request in requests:
        candidates = {}
        for item in catalogue:
            if item.get('non_applicable') or not item.get('selectable'): continue
            if matches_requested_part(request, item):
                # Repeated illustrations of the same OEM number are one candidate.
                candidates.setdefault(item['number'], item)
        values = list(candidates.values())
        status = ('UNSUPPORTED_PART' if not request_family(request['description']) else
                  'NOT_FOUND' if not values else 'AMBIGUOUS' if len(values) > 1 else 'READY')
        report.append({'request': request, 'candidates': values, 'status': status,
                       'cart_verified': False})
    return report


def parse_cart_count(label):
    match = re.fullmatch(r'\s*Cart\s*(?:\(\s*(\d+)\s*\))?\s*', label, re.I)
    if not match:
        raise RuntimeError(f'Unrecognized cart counter: {label!r}')
    return int(match.group(1) or 0)


async def cart_count(page):
    counter = page.locator('#cartTextElement:visible')
    await counter.wait_for(state='visible', timeout=10000)
    return parse_cart_count(await counter.inner_text())


async def ensure_parts_split_view(page):
    scope = page.locator(MICROCAT_ILLUSTRATION)
    # Actual DOM: topDown view retains only 1% height for the parts grid.
    button = scope.locator('#sideBySide:visible')
    await button.wait_for(state='visible', timeout=10000)
    if 'active' not in (await button.get_attribute('class') or '').split():
        await button.click()
    viewport = scope.locator('#partsGrid .ag-body-viewport')
    for _ in range(40):
        if await viewport.count() == 1 and await viewport.evaluate('(e) => e.clientHeight > 80 && e.clientWidth > 80'):
            return
        await asyncio.sleep(.25)
    raise RuntimeError('Parts grid remains collapsed after switching to split view')


async def find_part_row(page, number):
    scope = page.locator(MICROCAT_ILLUSTRATION)
    seed = '#partsGrid .ag-center-cols-container .ag-row'
    await scroll_container(scope, seed, True)
    await asyncio.sleep(.25)
    for _ in range(150):
        rows = scope.locator(seed).filter(has=page.locator(
            '[col-id="partNumberColumnIllustration"]',
            has_text=re.compile(r'^\s*' + re.escape(number) + r'\s*$')))
        if await rows.count() == 1: return rows
        if await rows.count() > 1: raise RuntimeError('Multiple rows for chosen OEM number')
        result = await scroll_container(scope, seed)
        await asyncio.sleep(.25)
        if not result['moved']: break
    raise RuntimeError(f'Chosen OEM number disappeared: {number}')


async def wait_cart_increment(page, before):
    # No retry of the double-click: a timeout may still mean the server added it.
    for _ in range(40):
        after = await cart_count(page)
        if after == before + 1: return after
        if after != before:
            raise RuntimeError(f'Unexpected cart change: {before} -> {after}')
        await asyncio.sleep(.25)
    raise RuntimeError('Cart increment not confirmed; do not automatically double-click again')


async def select_requested_parts(page, report, save_report):
    """Double-click a uniquely resolved applicable PNC and confirm its cart delta.

    Counter/row checks are recorded separately from final cart-content validation.
    No other user should edit the cart during this sequence.
    """
    for entry in report:
        if entry['status'] != 'READY': continue
        candidate = entry['candidates'][0]
        await root_index(page)
        for card in candidate['path']: await open_card(page, card)
        # Re-read all rows to ensure one PNC cannot silently add a different variant.
        fresh = await read_illustrated_rows(page, candidate['path'])
        peers = [r for r in fresh if r['pnc'] == candidate['pnc']
                 and r['selectable'] and not r['non_applicable']]
        if {r['number'] for r in peers} != {candidate['number']}:
            entry['status'] = 'PNC_VARIANTS_REVIEW'
            save_report()
            continue
        row = await find_part_row(page, candidate['number'])
        desc = await row.locator('[col-id="descriptionColumnIllustration"]').inner_text()
        if not matches_requested_part(entry['request'], {**candidate, 'description': desc}):
            raise RuntimeError('Description changed before adding the part')
        checkbox = row.locator('epc-cart-cell-select-component input[type="checkbox"]')
        quantity = row.locator('[data-cy="order-qty-input"]')
        if await checkbox.count() != 1 or await quantity.count() != 1:
            raise RuntimeError('Expected cart row controls missing')
        if await checkbox.is_checked():
            entry['status'] = 'ALREADY_SELECTED_REVIEW'
            save_report()
            continue
        pnc = candidate['pnc']
        if not re.fullmatch(r'[A-Za-z0-9-]+', pnc):
            raise RuntimeError('Invalid PNC value')
        target = page.locator(
            f'{MICROCAT_ILLUSTRATION} #illustration '
            f'g.correct-callout rect[e2e-id="parts-rect"][id="rect_{pnc}"]:visible')
        if not await target.count():
            entry['status'] = 'CALLOUT_NOT_AVAILABLE'
            save_report()
            continue
        # A repeated label in the same drawing may have several hitboxes.
        target = target.first
        before = await cart_count(page)
        entry.update(status='ADD_IN_PROGRESS', cart_count_before=before,
                     cart_count_verified=False, quantity_verified=False)
        save_report()  # Durable marker BEFORE the potentially successful action.
        try:
            print(f'    Double-click PNC {pnc}: {candidate["description"]}')
            await target.dblclick(delay=100, timeout=10000)
            after = await wait_cart_increment(page, before)
            entry.update(cart_count_after=after, cart_count_verified=True)
            save_report()
            row = await find_part_row(page, candidate['number'])
            checkbox = row.locator('epc-cart-cell-select-component input[type="checkbox"]')
            quantity = row.locator('[data-cy="order-qty-input"]')
            for _ in range(20):
                if await checkbox.is_checked(): break
                await asyncio.sleep(.25)
            else:
                raise RuntimeError('Counter changed but the matched OEM row is not selected')
            wanted = str(entry['request']['quantity'])
            if await quantity.input_value() != wanted:
                entry['status'] = 'QUANTITY_UPDATE_IN_PROGRESS'
                save_report()
                await quantity.fill(wanted)
                await quantity.press('Tab')
                await asyncio.sleep(.6)
            if not await checkbox.is_checked() or await quantity.input_value() != wanted:
                raise RuntimeError('Requested quantity was not retained')
            entry.update(status='CART_ADDED_COUNT_CONFIRMED', quantity_verified=True,
                         cart_count_final=await cart_count(page))
            save_report()
            print(f'    ✓ {entry["request"]["description"]}: {candidate["number"]}, '
                  f'qty {wanted}; Cart {before} -> {after}')
        except Exception as exc:
            entry.update(status='ADD_UNCERTAIN_REVIEW', error=str(exc))
            save_report()
            raise  # Stop this quote; never blindly retry a possibly successful add.
    await root_index(page)


async def process_requested_parts_sequentially(page, report, save_report):
    added_numbers = {c['number'] for e in report
                     if e['status'] in PRESERVED_CART_STATUSES
                     for c in e.get('candidates', [])}
    for index, entry in enumerate(report, 1):
        request = entry['request']
        print(f"    [{index}/{len(report)}] Processing {request['description']}", flush=True)
        if entry['status'] in PRESERVED_CART_STATUSES:
            print(f"    Preserving prior status {entry['status']}; no repeat double-click.", flush=True)
            continue
        entry['status'] = 'DISCOVERING'
        save_report()
        try:
            catalogue = await discover_microcat_parts(page, [request])
            result = compare_quote_to_microcat([request], catalogue)[0]
            entry.update(result)
            if entry['status'] == 'READY' and entry['candidates'][0]['number'] in added_numbers:
                entry['status'] = 'DUPLICATE_REQUEST_REVIEW'
            # Preserve examined descriptions to explain a NOT_FOUND outcome.
            entry['examined_parts'] = [
                {key: part.get(key) for key in ('pnc', 'number', 'description', 'non_applicable')}
                for part in catalogue]
            save_report()
            if entry['status'] == 'READY':
                await select_requested_parts(page, [entry], save_report)
            if entry['status'] == 'CART_ADDED_COUNT_CONFIRMED':
                added_numbers.add(entry['candidates'][0]['number'])
                print(f"    Completed {request['description']}; moving to the next quote line.", flush=True)
            else:
                print(f"    {request['description']}: {entry['status']} — no guessed part added.", flush=True)
        except Exception as exc:
            # Cart-affecting uncertainty must retain its replay-blocking status.
            if entry['status'] not in {'ADD_IN_PROGRESS', 'ADD_UNCERTAIN_REVIEW',
                                      'QUANTITY_UPDATE_IN_PROGRESS', 'CART_ADDED_COUNT_CONFIRMED'}:
                entry['status'] = 'AUTOMATION_ERROR'
            entry['error'] = str(exc)
            save_report()
            diagnostic = Path('microcat_diagnostics')
            diagnostic.mkdir(exist_ok=True)
            stamp = str(time.time_ns())
            try:
                await page.screenshot(path=str(diagnostic / f'{stamp}.png'), full_page=True)
                (diagnostic / f'{stamp}.html').write_text(await page.content(), encoding='utf-8')
                print(f'    Failure screenshot/HTML saved in {diagnostic}', flush=True)
            except Exception:
                pass
            raise


# ============================================================
# PROCESS ONE QUOTE
# ============================================================

async def process_quote(
    context,
    page,
    row,
    quote_num,
    draft_id,
):

    global STOP_AFTER_CURRENT_QUOTE
    quote_page = None
    opened_new_page = False

    try:

        print(
            f"  🖱️ Opening Direct Purchase "
            f"quote {quote_num} "
            f"(draftID={draft_id})..."
        )

        # ----------------------------------------------------
        # ORIGINAL WORKING OPEN BUTTON SELECTOR
        # ----------------------------------------------------

        open_btn = row.locator(
            "a[onclick*='onclick_request']"
        ).first

        if await open_btn.count() == 0:

            open_btn = row.locator(
                "td.bbG a[onclick]"
            ).first

        if await open_btn.count() == 0:

            print(
                f"    ❌ No Direct Purchase "
                f"open button found for "
                f"{quote_num}"
            )

            return False

        action_draft_id = draft_id_from_action(await open_btn.get_attribute('onclick'))
        if action_draft_id != str(draft_id):
            raise RuntimeError(f'Wrong quote row: expected draft {draft_id}, action opens {action_draft_id}')

        # ----------------------------------------------------
        # Debug onclick
        # ----------------------------------------------------

        try:

            onclick = await open_btn.get_attribute(
                "onclick"
            )

            print(
                f"    🐛 onclick: {onclick}"
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # Try new-tab flow
        # ----------------------------------------------------

        try:

            async with context.expect_page(
                timeout=5_000
            ) as new_page_info:

                await open_btn.click(
                    force=True
                )

            quote_page = (
                await new_page_info.value
            )

            opened_new_page = True

            print(
                "    🌐 Direct Purchase "
                "opened in a new tab."
            )

            try:

                await quote_page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=20_000,
                )

            except Exception:
                pass

        except PlaywrightTimeoutError:

            # ------------------------------------------------
            # Same-page flow
            # ------------------------------------------------

            quote_page = page

            print(
                "    🌐 Direct Purchase "
                "opened in current page."
            )

        # ----------------------------------------------------
        # Wait for price-quote.php
        # ----------------------------------------------------

        try:

            await quote_page.wait_for_url(
                "**/price-quote.php*",
                timeout=20_000,
            )

            print(
                f"    🌐 Price quote page: "
                f"{quote_page.url}"
            )

        except PlaywrightTimeoutError:

            print(
                "    ⚠️ price-quote.php URL "
                "was not detected."
            )

            print(
                f"    🌐 Current URL: "
                f"{quote_page.url}"
            )

        # ----------------------------------------------------
        # Extract VIN
        # ----------------------------------------------------

        actual_draft = parse_qs(urlparse(quote_page.url).query).get('draftID', [None])[0]
        if actual_draft != str(draft_id):
            raise RuntimeError(f'Wrong quote page: expected draft {draft_id}, opened {actual_draft}')

        vin = await extract_vin_from_page(
            quote_page
        )

        if not vin:

            print(
                f"    ❌ Could not locate VIN "
                f"for quote {quote_num}"
            )

            if opened_new_page:

                try:
                    await quote_page.close()
                except Exception:
                    pass

            return False

        # ----------------------------------------------------
        # Select only the first quote with a valid VIN for this review session.
        # Stop even if some parts remain unmatched or an error occurs.
        STOP_AFTER_CURRENT_QUOTE = True
        requests = await extract_quote_parts(quote_page)
        if not requests:
            raise RuntimeError("Could not read requested quote parts; quote remains pending")
        print(f"    📦 Quote requests: {requests}")

        # ----------------------------------------------------
        # Copy VIN
        # ----------------------------------------------------

        await copy_vin_and_show_toast(
            quote_page,
            vin,
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Microcat happens ONLY after successful VIN.
        # ----------------------------------------------------

        print(
            f"    🚀 Starting Microcat search "
            f"for VIN {vin}..."
        )

        output = Path(f"microcat_candidates_{re.sub(r'[^A-Za-z0-9_-]', '_', str(draft_id))}.json")
        previous = None
        if output.exists():
            previous_text = output.read_text(encoding='utf-8')
            previous = json.loads(previous_text)
        report = prepare_resume_report(previous, requests, vin, draft_id)
        if previous is not None:
            output.with_name(output.stem + f'.backup-{time.time_ns()}.json').write_text(
                previous_text, encoding='utf-8')
            preserved = sum(e['status'] in PRESERVED_CART_STATUSES for e in report)
            print(f'    Resuming draft {draft_id}: {preserved} prior cart action(s) preserved; '
                  f'{len(report) - preserved} line(s) to inspect.', flush=True)
        await open_and_login_microcat(context, vin)
        pill = microcat_page.locator("#globalSearchBarVehiclePillValue:visible")
        await pill.wait_for(state="visible", timeout=30000)
        if (await pill.inner_text()).strip().upper() != vin.upper():
            raise RuntimeError("Microcat selected VIN does not match the quote")
        def save_report():
            temporary = output.with_suffix('.tmp')
            temporary.write_text(json.dumps({"quote": quote_num, "draft_id": draft_id,
                "vin": vin, "parts": report}, indent=2), encoding="utf-8")
            temporary.replace(output)
        await reconcile_empty_cart_history(microcat_page, report)
        save_report()
        INSPECTED_QUOTES.add(str(draft_id))
        await process_requested_parts_sequentially(microcat_page, report, save_report)
        for item in report:
            print(f"    {item['request']['description']}: {item['status']}")
        print(f"    Report saved: {output}")
        complete = all(x['status'] == 'CART_ADDED_COUNT_CONFIRMED' for x in report)
        print("    All requested additions confirmed by cart count and row quantity." if complete
              else "    Some requested parts require review; see the report.")

        await asyncio.sleep(2)

        # ----------------------------------------------------
        # New-tab PartsCheck flow
        # ----------------------------------------------------

        if opened_new_page:

            try:

                await quote_page.close()

                print(
                    "    🔙 Closed quote tab."
                )

            except Exception as e:

                print(
                    f"    ⚠️ Could not close "
                    f"quote tab: {e}"
                )

        # ----------------------------------------------------
        # Same-page PartsCheck flow
        # ----------------------------------------------------

        else:

            try:

                await page.goto(
                    DASHBOARD_URL,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                await page.wait_for_selector(
                    ".toplisttext",
                    timeout=30_000,
                )

                print(
                    "    🔙 Returned to dashboard."
                )

            except Exception as e:

                print(
                    f"    ⚠️ Could not return "
                    f"to dashboard: {e}"
                )

        if complete:
            PROCESSED_QUOTES.add(str(draft_id))
        return complete  # Includes additions recorded in the previous run.

    except Exception as e:

        print(
            f"    ❌ Error processing quote "
            f"{quote_num}: {e}"
        )

        if (
            opened_new_page
            and quote_page is not None
        ):

            try:
                await quote_page.close()
            except Exception:
                pass

        return False


# ============================================================
# HANDLE INCOMING QUOTES
# ============================================================

async def handle_quotes_in_popup(
    context,
    page,
):
    """
    Find and process Incoming Quote requests.

    IMPORTANT:

    - Does NOT depend on "Direct Purchase" text.
    - Works with rows where the action is shown as:
          Due: Wed 9, 17:00pm
    - Status "Opened" is still processed.
    - A quote is considered valid when:
          1. It is inside .requestRow
          2. It has a quote number (.ab)
          3. It has onclick_request(...)
    - Only successfully processed quotes are skipped.
    """

    try:

        # ----------------------------------------------------
        # Get Incoming Quotes iframe
        # ----------------------------------------------------

        frame = await get_quotes_frame(
            page
        )

        rows = frame.locator(
            ".requestRow"
        )

        row_count = await rows.count()

        print(
            f"  📋 Found {row_count} "
            "quote row(s)."
        )

        # ----------------------------------------------------
        # Collect quote information
        # ----------------------------------------------------

        available_quotes = []

        for i in range(row_count):

            try:

                row = rows.nth(i)

                row_text = (
                    await row.inner_text()
                ).strip()

                row_lower = row_text.lower()

                # ------------------------------------------------
                # Find quote number
                # ------------------------------------------------

                quote_link = row.locator(
                    "a.ab"
                ).first

                if await quote_link.count() == 0:

                    print(
                        f"  ⚠️ Row {i + 1} "
                        "has no quote number. "
                        "Skipping."
                    )

                    continue

                quote_num = (
                    await quote_link.inner_text()
                ).strip()

                if not quote_num:

                    print(
                        f"  ⚠️ Row {i + 1} "
                        "has empty quote number. "
                        "Skipping."
                    )

                    continue

                # ------------------------------------------------
                # Find onclick_request action
                #
                # Your HTML contains:
                #
                # onclick="onclick_request(
                #     '17737064',
                #     '',
                #     '09/09/2026',
                #     '5:00pm',
                #     ''
                # )"
                #
                # This is now our primary identifier.
                # ------------------------------------------------

                open_action = row.locator(
                    "a[onclick*='onclick_request']"
                ).first

                if await open_action.count() == 0:

                    # Fallback: any anchor containing
                    # onclick_request.
                    open_action = row.locator(
                        "[onclick*='onclick_request']"
                    ).first

                if await open_action.count() == 0:

                    print(
                        f"  ⚠️ Quote {quote_num} "
                        "has no onclick_request action. "
                        "Skipping."
                    )

                    continue

                # ------------------------------------------------
                # Read draftID
                # ------------------------------------------------

                draft_id = await row_draft_id(row)
                if not draft_id:
                    print(f'  Cannot identify draft ID for quote {quote_num}; skipping.')
                    continue

                # ------------------------------------------------
                # Skip only already processed quotes
                # ------------------------------------------------

                if str(draft_id) in PROCESSED_QUOTES or str(draft_id) in INSPECTED_QUOTES:

                    print(
                        f"  ⏭️ Skipping quote "
                        f"{quote_num} — already "
                        "processed by this script."
                    )

                    continue

                # ------------------------------------------------
                # Determine status for diagnostics
                # ONLY.
                #
                # Status does not control processing.
                # ------------------------------------------------

                if "opened" in row_lower:

                    status_text = "Opened"

                elif "new" in row_lower:

                    status_text = "New"

                else:

                    status_text = "Other"

                # ------------------------------------------------
                # Detect Due button text
                # ------------------------------------------------

                due_text = ""

                try:

                    # First column contains the Due button
                    first_cell = row.locator(
                        "td"
                    ).first

                    if await first_cell.count() > 0:

                        due_text = (
                            await first_cell.inner_text()
                        ).strip()

                except Exception:

                    due_text = ""

                # ------------------------------------------------
                # Read onclick for debugging
                # ------------------------------------------------

                onclick = ""

                try:

                    onclick = (
                        await open_action.get_attribute(
                            "onclick"
                        )
                        or ""
                    )

                except Exception:

                    pass

                # ------------------------------------------------
                # Add eligible quote
                # ------------------------------------------------

                available_quotes.append(
                    {
                        "quote_num": quote_num,
                        "draft_id": draft_id,
                    }
                )

                print(
                    f"  🎯 Eligible quote: "
                    f"{quote_num} | "
                    f"status={status_text} | "
                    f"due={due_text} | "
                    f"draftID={draft_id}"
                )

                print(
                    f"     🐛 onclick: {onclick}"
                )

            except Exception as e:

                print(
                    f"  ⚠️ Could not inspect "
                    f"quote row {i + 1}: {e}"
                )

        # ----------------------------------------------------
        # Nothing to process
        # ----------------------------------------------------

        if not available_quotes:

            print(
                "  ℹ️ No unprocessed "
                "Incoming Quotes found."
            )

            return

        print(
            f"  🎯 {len(available_quotes)} "
            "quote(s) to process."
        )

        # ----------------------------------------------------
        # Process quotes one at a time
        # ----------------------------------------------------

        for quote_info in available_quotes:

            quote_num = quote_info[
                "quote_num"
            ]

            draft_id = quote_info[
                "draft_id"
            ]

            print(
                "\n"
                + "-" * 60
            )

            print(
                f"  🎯 Processing quote "
                f"{quote_num}"
            )

            # ------------------------------------------------
            # Make sure dashboard is active
            # ------------------------------------------------

            if (
                "dashboard.php"
                not in page.url.lower()
            ):

                try:

                    await page.goto(
                        DASHBOARD_URL,
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )

                    await page.wait_for_selector(
                        ".toplisttext",
                        timeout=30_000,
                    )

                    print(
                        "  🔙 Dashboard restored."
                    )

                except Exception as e:

                    print(
                        f"  ⚠️ Could not restore "
                        f"dashboard before "
                        f"quote {quote_num}: {e}"
                    )

                    continue

            # ------------------------------------------------
            # Make sure Incoming Quotes popup is open
            # ------------------------------------------------

            popup_visible = False

            try:

                popup_visible = (
                    await page.locator(
                        "#colorbox"
                    ).is_visible()
                )

            except Exception:

                popup_visible = False

            if not popup_visible:

                clicked = (
                    await click_new_quote_request(
                        page
                    )
                )

                if not clicked:

                    print(
                        f"  ❌ Could not reopen "
                        f"Incoming Quotes for "
                        f"quote {quote_num}"
                    )

                    continue

            # ------------------------------------------------
            # Get FRESH iframe
            # ------------------------------------------------

            try:

                frame = await get_quotes_frame(
                    page
                )

            except Exception as e:

                print(
                    f"  ❌ Could not access "
                    f"Incoming Quotes for "
                    f"{quote_num}: {e}"
                )

                continue

            # ------------------------------------------------
            # Get FRESH rows
            # ------------------------------------------------

            rows = frame.locator(
                ".requestRow"
            )

            row_count = await rows.count()

            row = None

            for i in range(row_count):

                try:

                    current_row = rows.nth(i)

                    current_draft_id = await row_draft_id(current_row)
                    if str(current_draft_id) == str(draft_id):
                        row = current_row
                        break
                    # Quote labels can be duplicated. Never fall back to the label.

                except Exception:

                    continue

            # ------------------------------------------------
            # Quote disappeared
            # ------------------------------------------------

            if row is None:

                print(
                    f"  ⚠️ Quote {quote_num} "
                    "could not be found again."
                )

                continue

            print(
                f"  ✅ Fresh row located "
                f"for quote {quote_num}"
            )

            # ------------------------------------------------
            # Process quote
            # ------------------------------------------------

            success = await process_quote(
                context,
                page,
                row,
                quote_num,
                draft_id,
            )

            if STOP_AFTER_CURRENT_QUOTE:
                print('  Single-quote review: no further quotes will be opened.')
                return

            if success:

                print(
                    f"  ✅ Finished quote "
                    f"{quote_num}"
                )

            else:

                print(
                    f"  ⚠️ Quote {quote_num} "
                    "awaits cart verification or could not be inspected."
                )

    except PlaywrightTimeoutError:

        print(
            "  ⚠️ Incoming Quotes popup "
            "did not appear in time."
        )

    except Exception as e:

        print(
            f"  ⚠️ Error in quote popup handler: "
            f"{e}"
        )


# ============================================================
# CLOSE POPUP
# ============================================================

async def close_popup(page):

    try:

        close_btn = page.locator(
            "#cboxClose"
        )

        if await close_btn.is_visible():

            await close_btn.click()

            await asyncio.sleep(0.5)

            print(
                "  ✖️ Closed Incoming Quotes popup."
            )

    except Exception:
        pass


# ============================================================
# ONE CHECK CYCLE
# ============================================================

async def run_check_cycle(
    context,
    page,
    cycle_number,
):

    print(
        "\n"
        + "=" * 65
    )

    print(
        f"🔍 [{time.strftime('%H:%M:%S')}] "
        f"Cycle #{cycle_number}"
    )

    print(
        "=" * 65
    )

    # --------------------------------------------------------
    # 1. Session
    # --------------------------------------------------------

    await ensure_logged_in(
        page
    )

    # --------------------------------------------------------
    # 2. Dashboard
    # --------------------------------------------------------

    if (
        "dashboard.php"
        not in page.url.lower()
    ):

        try:

            await page.goto(
                DASHBOARD_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            await page.wait_for_selector(
                ".toplisttext",
                timeout=30_000,
            )

        except Exception as e:

            print(
                f"  ⚠️ Could not restore dashboard: "
                f"{e}"
            )

            return

    # --------------------------------------------------------
    # 3. Handle Welcome modal if reappeared
    # --------------------------------------------------------

    welcome_frame = await find_welcome_frame(
        page,
        timeout_seconds=2,
    )

    if welcome_frame is not None:

        print(
            "  ⚠️ Welcome modal is present again."
        )

        handled = await handle_welcome_modal(
            page,
            required=True,
        )

        if not handled:

            print(
                "  ❌ Could not complete "
                "Daniel Turner selection."
            )

            return

    # --------------------------------------------------------
    # 4. Read counter only for diagnostics
    # --------------------------------------------------------

    count = await get_new_quote_count(
        page
    )

    print(
        f"  📊 New quote counter: {count}"
    )

    # --------------------------------------------------------
    # 5. ALWAYS inspect Incoming Quotes
    # --------------------------------------------------------

    print(
        "  🔎 Checking Incoming Quotes "
        "regardless of counter..."
    )

    clicked = await click_new_quote_request(
        page
    )

    if not clicked:

        print(
            "  ⚠️ Could not open Incoming Quotes "
            "this cycle."
        )

        return

    # --------------------------------------------------------
    # 6. Process Direct Purchase
    # --------------------------------------------------------

    await handle_quotes_in_popup(
        context,
        page,
    )
    if STOP_AFTER_CURRENT_QUOTE:
        return

    # --------------------------------------------------------
    # 7. Close popup
    # --------------------------------------------------------

    await asyncio.sleep(1)

    await close_popup(
        page
    )


# ============================================================
# MAIN
# ============================================================

async def hold_cart_for_review(browser, partscheck_page):
    """End automation, open Cart, and keep the session alive for manual review."""
    review_page = microcat_page if microcat_page and not microcat_page.is_closed() else partscheck_page
    closed = asyncio.Event()
    review_page.on('close', lambda *_: closed.set())
    browser.on('disconnected', lambda *_: closed.set())
    try:
        await review_page.bring_to_front()
        if review_page is microcat_page:
            await review_page.locator('#cartTextElement:visible').click(timeout=15000)
            print('    Cart opened. Automation stopped; no next quote or VIN search.', flush=True)
        else:
            print('    Microcat was not reached. Automation stopped on this quote.', flush=True)
    except Exception as exc:
        print(f'    Could not open Cart automatically: {exc}. Open it manually; automation is stopped.', flush=True)
    print('    Browser remains open for your review. Close the review tab/browser when finished.', flush=True)
    if review_page.is_closed() or not browser.is_connected():
        return
    await closed.wait()


async def main():

    global microcat_page

    async with async_playwright() as p:

        # ----------------------------------------------------
        # Visible browser
        # ----------------------------------------------------

        browser = await p.chromium.launch(
            headless=False,
            slow_mo=150,
        )

        context = await browser.new_context(
            viewport={
                "width": 1280,
                "height": 900,
            }
        )

        page = await context.new_page()

        print("=" * 65)

        print(
            "  PartsCheck Automation — "
            "Fully Automatic"
        )

        print("=" * 65)
        print(f"  Version: {SCRIPT_VERSION} | File: {Path(__file__).resolve()}")

        try:

            # =================================================
            # LOGIN
            # =================================================

            await auto_login(
                page
            )

            # =================================================
            # DANIEL TURNER
            # =================================================

            welcome_handled = (
                await handle_welcome_modal(
                    page,
                    required=True,
                )
            )

            if not welcome_handled:

                print(
                    "❌ Could not complete "
                    "Daniel Turner selection."
                )

                print(
                    "❌ Automation will NOT continue "
                    "while the Welcome modal cannot "
                    "be handled."
                )

                return

            print(
                "✅ User selection completed."
            )

            print(
                "🚀 Starting quote automation..."
            )

            # =================================================
            # CONTINUOUS LOOP
            # =================================================

            cycle = 1

            while True:

                try:

                    await run_check_cycle(
                        context,
                        page,
                        cycle,
                    )
                    if STOP_AFTER_CURRENT_QUOTE:
                        await hold_cart_for_review(browser, page)
                        return

                except Exception as e:

                    print(
                        f"  ⚠️ Unexpected error "
                        f"in cycle {cycle}: {e}"
                    )

                cycle += 1

                print(
                    f"\n  ⏱️ Next check in "
                    f"{CHECK_INTERVAL_SECONDS // 60} "
                    "minutes..."
                )

                print(
                    "  📋 Processed quotes this run: "
                    f"{len(INSPECTED_QUOTES)} inspected, {len(PROCESSED_QUOTES)} cart verified"
                )

                await asyncio.sleep(
                    CHECK_INTERVAL_SECONDS
                )

        finally:

            print(
                "\n🛑 Closing PartsCheck browser..."
            )

            await browser.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())