"""
Booran Parts Platform - PartsCheck & Pentana eraPower Automation

Workflow:
1. Open PartsCheck login page and authenticate.
2. Handle user selection (Daniel Turner - ID 23915).
3. Start continuous monitoring loop for incoming quote requests.
4. Open 'New Quote Request' / Incoming Quotes popup.
5. Extract Quote ID, Purchaser/Repairer, VIN, Vehicle Details, and Parts list.
6. Connect with Pentana eraPower Web portal:
   URL: https://c2892-erapower.pentana.cloud/base/templates/vIndependent.htm
7. Retrieve customer trade account, stock across Booran branches (Dandenong, Cranbourne, Cheltenham, etc.),
   bin locations, list price, and account-specific trade price.
8. Compile quotation payload and write artifacts to era_results.json, era_customer_result.json, era_quote_result.json.
9. Prepare quotation data for PartsCheck quotation submission / controller exception queue.
"""

import sys
import os
import re
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from urllib.parse import urlparse, parse_qs

import pyperclip
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Page,
    BrowserContext,
)

# Add src to sys.path for era modules
SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

from Era_orchestrator import EraOrchestrator
from Era_power import EraPower

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PartsCheckAuto")

# ============================================================
# CONFIGURATION
# ============================================================

PARTSCHECK_LOGIN_URL = os.getenv("PARTSCHECK_LOGIN_URL", "https://www.partscheck.com.au/global/login.php")
PARTSCHECK_DASHBOARD_URL = os.getenv("PARTSCHECK_DASHBOARD_URL", "http://v1.partscheck.com.au/app/dashboard.php")
PARTSCHECK_USERNAME = os.getenv("PARTSCHECK_USERNAME", "PAK1077")
PARTSCHECK_PASSWORD = os.getenv("PARTSCHECK_PASSWORD", "Catfish0125")

DANIEL_USER_ID = 23915
DANIEL_NAME = "Daniel Turner"

CHECK_INTERVAL_SECONDS = int(os.getenv("PARTSCHECK_CHECK_INTERVAL", "120"))
VIN_REGEX = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

PROCESSED_QUOTES = set()
INSPECTED_QUOTES = set()

# Initialize ERA Orchestrator
orchestrator = EraOrchestrator()


# ============================================================
# HELPER FUNCTIONS
# ============================================================

async def safe_inner_text(locator, timeout=1500) -> str:
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception:
        return ""


async def auto_login(page: Page):
    """Login to PartsCheck and wait until dashboard is ready."""
    logger.info(f"Navigating to PartsCheck login page: {PARTSCHECK_LOGIN_URL}")
    await page.goto(PARTSCHECK_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

    remember_me_btn = await page.query_selector("#loginRememberMeButton")
    if remember_me_btn and await remember_me_btn.is_visible():
        text = await remember_me_btn.inner_text()
        logger.info(f"Found saved session: '{text.strip()}' — clicking...")
        await remember_me_btn.click()
    else:
        logger.info("Entering credentials for PartsCheck...")
        login_area = await page.query_selector("#loginArea")
        if login_area:
            style = (await login_area.get_attribute("style")) or ""
            if "display: none" in style:
                await page.evaluate("() => { const el = document.getElementById('loginArea'); if (el) el.style.display = 'block'; }")

        await page.fill("#myuser", PARTSCHECK_USERNAME)
        await page.fill("#mypass", PARTSCHECK_PASSWORD)

        remember_checkbox = await page.query_selector("#rememberMe")
        if remember_checkbox:
            try:
                if not await remember_checkbox.is_checked():
                    await remember_checkbox.check()
            except Exception:
                pass

        await page.click("#loginButton")
        logger.info("Submitted PartsCheck login form.")

    logger.info("Waiting for dashboard to load...")
    await page.wait_for_selector(".toplisttext", timeout=60000)
    logger.info(f"Logged in successfully. Current URL: {page.url}")


async def find_welcome_frame(page: Page, timeout_seconds=60):
    """Search frames for the PartsCheck Welcome / User Selection modal."""
    logger.info("Waiting for 'Welcome to PartsCheck' modal...")
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                body = frame.locator("body")
                if await body.count() == 0:
                    continue
                text = await body.inner_text(timeout=1200)
                if "Welcome to PartsCheck" in text or "Please fill in details or select your name" in text:
                    logger.info(f"Welcome modal detected in frame: {frame.url}")
                    return frame
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return None


async def handle_welcome_modal(page: Page, required=True) -> bool:
    """Select Daniel Turner (ID: 23915) from the Welcome modal."""
    frame = await find_welcome_frame(page, timeout_seconds=60 if required else 10)
    if frame is None:
        return not required

    logger.info(f"Searching for {DANIEL_NAME} in modal...")
    deadline = time.monotonic() + 30
    daniel_row = None

    while time.monotonic() < deadline:
        try:
            rows = frame.locator("div.userRow")
            count = await rows.count()
            for i in range(count):
                row = rows.nth(i)
                text = await safe_inner_text(row, timeout=1200)
                if DANIEL_NAME.lower() in text.lower():
                    daniel_row = row
                    break
            if daniel_row is not None:
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    if daniel_row is None:
        logger.warning(f"{DANIEL_NAME} was not found in modal.")
        return False

    logger.info(f"Selecting {DANIEL_NAME}...")
    try:
        await daniel_row.evaluate("(el) => el.click()")
    except Exception:
        try:
            await frame.evaluate(f"(uid) => window.changeFingerPrint({DANIEL_USER_ID})")
        except Exception as e:
            logger.error(f"Failed invoking changeFingerPrint: {e}")
            return False

    await asyncio.sleep(2)
    logger.info(f"User {DANIEL_NAME} selected successfully.")
    return True


async def click_new_quote_request(page: Page) -> bool:
    """Open Incoming Quotes popup via New Quote Request."""
    try:
        colorbox = page.locator("#colorbox")
        if await colorbox.is_visible():
            return True

        elements = page.locator(".toplist.cboxlink")
        count = await elements.count()
        for i in range(count):
            el = elements.nth(i)
            text = await safe_inner_text(el)
            if "New Quote Request" in text:
                logger.info("Found 'New Quote Request' - clicking...")
                await el.evaluate("(el) => el.click()")
                await page.wait_for_selector("#colorbox", state="visible", timeout=15000)
                return True
        return False
    except Exception as e:
        logger.error(f"Error opening New Quote Request: {e}")
        return False


async def get_quotes_frame(page: Page):
    """Wait for and retrieve the Incoming Quotes iframe."""
    await page.wait_for_selector("#colorbox", state="visible", timeout=15000)
    iframe_el = await page.wait_for_selector("#cboxContent iframe", timeout=15000)
    frame = await iframe_el.content_frame()
    if not frame:
        raise RuntimeError("Could not access Incoming Quotes iframe.")
    await frame.wait_for_selector(".requestRow", timeout=15000)
    return frame


async def extract_vin_from_quote_page(target_page: Page) -> Optional[str]:
    """Extract 17-character VIN from the quote page."""
    try:
        await target_page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass

    try:
        vin_title = target_page.locator("div.quoteTitle").filter(has_text="VIN").first
        if await vin_title.count():
            vin_input = vin_title.locator("xpath=following-sibling::div[contains(@class,'quoteTitleContent')][1]//input").first
            val = (await vin_input.input_value()).strip().upper()
            if VIN_REGEX.match(val):
                return val
    except Exception:
        pass

    # Fallback search across all inputs
    inputs = await target_page.locator("input").all()
    for inp in inputs:
        try:
            val = (await inp.input_value()).strip().upper()
            if VIN_REGEX.match(val):
                return val
        except Exception:
            continue
    return None


async def extract_quote_parts(quote_page: Page) -> List[Dict[str, Any]]:
    """Extract required parts, quantities, and descriptions from quote page."""
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
                if (!Number.isInteger(quantity) || quantity < 1) continue;
                result.push({
                    description: description,
                    quantity: quantity,
                    part_number: value(columns[n])
                });
            }
            if (result.length) return result;
        }
        return [];
    }""")


# ============================================================
# MAIN AUTOMATION CYCLE
# ============================================================

async def process_quote_row(context: BrowserContext, frame, row):
    """Process a single incoming quote row from PartsCheck with Pentana eraPower."""
    row_text = await safe_inner_text(row)
    logger.info(f"Inspecting incoming quote row: {row_text[:80]}...")

    # Extract quote ID & Repairer
    quote_link = row.locator("[onclick*='onclick_request']").first
    onclick_attr = await quote_link.get_attribute("onclick") if await quote_link.count() else ""
    match = re.search(r"onclick_request\s*\(\s*['\"](\d+)['\"]", onclick_attr or "")
    quote_id = match.group(1) if match else f"Q-{int(time.time())}"

    if quote_id in PROCESSED_QUOTES:
        logger.info(f"Quote #{quote_id} already processed. Skipping.")
        return

    # Extract Repairer / Purchaser Name
    repairer = "Trade Customer"
    cols = row.locator("td")
    if await cols.count() >= 3:
        repairer = (await safe_inner_text(cols.nth(1))) or "Trade Customer"

    logger.info(f"Opening details for Quote #{quote_id} (Repairer: {repairer})...")

    # Click quote row and handle opened page
    async with context.expect_page() as page_info:
        await quote_link.click()
    
    quote_page = await page_info.value
    await quote_page.wait_for_load_state("domcontentloaded")

    # Extract VIN & Parts
    vin = await extract_vin_from_quote_page(quote_page) or "UNKNOWN_VIN"
    parts = await extract_quote_parts(quote_page)

    logger.info(f"Extracted for Quote #{quote_id}: VIN={vin}, Parts={len(parts)} items")

    if not parts:
        logger.warning(f"No specific parts table detected for Quote #{quote_id}. Checking fallback structure...")
        parts = [{"description": "General Parts Request", "quantity": 1, "part_number": ""}]

    # Copy VIN to clipboard
    if vin != "UNKNOWN_VIN":
        pyperclip.copy(vin)

    # Process through Pentana eraPower
    logger.info(f"Resolving Quote #{quote_id} against Pentana eraPower...")
    quotation_result = await orchestrator.process_partscheck_rfq(
        quote_id=quote_id,
        vin=vin,
        repairer=repairer,
        parts=parts
    )

    PROCESSED_QUOTES.add(quote_id)
    INSPECTED_QUOTES.add(quote_id)

    logger.info(f"Successfully processed Quote #{quote_id}. Output saved to era_results.json.")
    await quote_page.close()


async def run_partscheck_erapower_pipeline():
    """Main continuous monitoring loop for PartsCheck and Pentana eraPower."""
    logger.info("Starting Booran Parts Platform Automation Pipeline...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Step 1: PartsCheck Login & User Fingerprint
            await auto_login(page)
            await handle_welcome_modal(page, required=True)

            cycle = 1
            while True:
                logger.info(f"\n--- [Cycle {cycle}] Checking Incoming Quotes in PartsCheck ---")
                
                try:
                    # Step 2: Open Incoming Quotes
                    opened = await click_new_quote_request(page)
                    if opened:
                        quotes_frame = await get_quotes_frame(page)
                        rows = quotes_frame.locator(".requestRow")
                        row_count = await rows.count()
                        logger.info(f"Found {row_count} incoming quote row(s).")

                        for idx in range(row_count):
                            row = rows.nth(idx)
                            await process_quote_row(context, quotes_frame, row)
                    else:
                        logger.warning("Could not open Incoming Quotes popup.")

                except Exception as e:
                    logger.error(f"Error during cycle {cycle}: {e}")

                cycle += 1
                logger.info(f"Next check in {CHECK_INTERVAL_SECONDS // 60} minutes. (Processed: {len(PROCESSED_QUOTES)} quotes)")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        finally:
            logger.info("Closing browser...")
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run_partscheck_erapower_pipeline())