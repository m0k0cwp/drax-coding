"""
Drax - Single Associate Performance Automation
------------------------------------------------
Navigates directly to the associate performance page,
searches for a specific associate by name, then processes
their timeline rows following all skip/edit conditions.

Skip rules per timeline row:
  1. Green flag in Exemption   -> skip + enter exempt window
  2. Inside exempt window      -> skip
  3. Checkered flag            -> skip + exit exempt window
  4. No Idle pause icon        -> skip (no edit form available)
  5. Dept != TARGET_DEPT       -> skip
  6. Time inside blocked window-> skip
  7. User column non-empty     -> skip (already edited)
  8. No edit link              -> skip
  else                         -> EDIT
"""

import os
import sys
import time
import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Force UTF-8 output on Windows so emoji/special chars don't crash prints
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
TARGET_ASSOCIATE_NAME = "TEKINA"         # First name (or full name) — both work!
DATE             = "2026-04-06"
SHIFT            = "S1"
BASE_URL         = "https://drax.walmart.com"
MODIFIED_SC      = "Downtime Systems (900080911)"
TARGET_DEPT      = "Stationary Picking"
SKIP_TIME_START  = "13:50:00"   # skip rows at or after this time
SKIP_TIME_END    = "14:50:00"   # skip rows at or before this time

# The main performance page to land on and search from
PERF_URL = (
    f"{BASE_URL}/associateperformance/"
    f"?super_department=&current_sc_code_id=019209516"
    f"&date={DATE}&shift={SHIFT}"
)

# Broader fallback URL (no SC code filter) — used if associate not found above
PERF_URL_BROAD = (
    f"{BASE_URL}/associateperformance/"
    f"?super_department=&date={DATE}&shift={SHIFT}"
)

# Persistent browser profile (saves cookies/session between runs)
PROFILE_DIR = os.path.join(os.path.dirname(__file__), "drax_browser_profile")
# ===========================================================================

LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-infobars",
]


# ---------------------------------------------------------------------------
# Flag helpers
# ---------------------------------------------------------------------------

def _has_green_flag(html: str, text: str) -> bool:
    h = html.lower()
    return any([
        "green"             in h,
        "flag-green"        in h,
        "text-success"      in h,
        "fa-flag"           in h and "green" in h,
        "#2a8703"           in h,
        "\U0001f6a9"        in text,
        "\U0001f7e2"        in text,
    ])


def _has_checkered_flag(html: str, text: str) -> bool:
    h = html.lower()
    return any([
        "checker"           in h,
        "chequered"         in h,
        "flag-end"          in h,
        "fa-flag-checkered" in h,
        "\U0001f3c1"        in text,
    ])


def _in_skip_window(timestamp: str) -> bool:
    parts = timestamp.strip().split(" ")
    if len(parts) < 2:
        return False
    row_time = parts[-1]
    return SKIP_TIME_START <= row_time <= SKIP_TIME_END


# ---------------------------------------------------------------------------
# JS: extract rows from associate timeline table
# ---------------------------------------------------------------------------

JS_EXTRACT_ROWS = """
() => {
    const tables = Array.from(document.querySelectorAll('table'));
    if (!tables.length) return { headers: [], rows: [] };

    const mainTable = tables.reduce(
        (best, t) => t.querySelectorAll('tbody tr').length >
                     best.querySelectorAll('tbody tr').length ? t : best,
        tables[0]
    );

    const headers = Array.from(mainTable.querySelectorAll('thead th'))
        .map(th => th.innerText.trim().toLowerCase());

    const idx  = name => headers.findIndex(h => h.includes(name));
    const uIdx = idx('user');
    const eIdx = idx('exemption');
    const aIdx = idx('action');
    const iIdx = idx('idle');
    const dIdx = idx('department');

    // Only scan VISIBLE rows so hidden/filtered-out rows are ignored
    const allRows = Array.from(mainTable.querySelectorAll('tbody tr'));
    const visibleRows = allRows.filter(row => {
        if (row.offsetParent === null) return false;
        const style = window.getComputedStyle(row);
        return style.display !== 'none' && style.visibility !== 'hidden';
    });

    const rows = visibleRows.map(row => {
        const cells  = Array.from(row.querySelectorAll('td'));
        const cell   = i => (i >= 0 && cells[i]) ? cells[i] : null;
        const editEl = cell(aIdx) ? cell(aIdx).querySelector('a, button') : null;

        const idleCell    = cell(iIdx);
        const idleHtml    = idleCell ? idleCell.innerHTML.trim() : '';
        const hasIdleIcon = idleCell
            ? idleCell.children.length > 0 || idleHtml.length > 0
            : false;

        return {
            timestamp:    cell(1)    ? cell(1).innerText.trim()    : '',
            department:   cell(dIdx) ? cell(dIdx).innerText.trim() : '',
            userText:     cell(uIdx) ? cell(uIdx).innerText.trim() : '',
            exemptHtml:   cell(eIdx) ? cell(eIdx).innerHTML        : '',
            exemptText:   cell(eIdx) ? cell(eIdx).innerText.trim() : '',
            editHref:     editEl     ? editEl.href                 : null,
            hasIdleIcon:  hasIdleIcon,
        };
    });

    return { headers, rows };
}
"""


# ---------------------------------------------------------------------------
# Build the correct shift-specific associate URL from any associate href
# ---------------------------------------------------------------------------

def _build_shift_url(href: str) -> str:
    """
    Given any href containing '/associates/{id}', extract the numeric ID
    and build the proper date+shift URL.
    e.g. '/associates/300065/performance/daily'
      -> 'https://drax.walmart.com/associates/300065/?date=2026-04-01&shift=S1'
    """
    match = re.search(r"/associates/(\d+)", href)
    if match:
        assoc_id = match.group(1)
        url = f"{BASE_URL}/associates/{assoc_id}/?date={DATE}&shift={SHIFT}"
        print(f"    Built shift URL: {url}")
        return url
    # Fallback: return as-is if we can't parse it
    return href if href.startswith("http") else BASE_URL + href


# ---------------------------------------------------------------------------
# Find the associate's detail page href by searching on the perf page
# ---------------------------------------------------------------------------

def find_associate_href(page, name: str) -> str | None:
    """
    On the associate performance page, use the DataTables search box
    to filter by name, then grab the matching associate's href.
    Falls back to paginating through the table if search doesn't work.
    """
    target_lower = name.strip().lower()
    first_name   = name.strip().split()[0].lower()   # e.g. 'tekina'
    search_term  = name.strip().split()[0].upper()   # always search by first name only

    # Wait for the table to be ready
    try:
        page.wait_for_selector("table tbody tr", timeout=15_000)
    except Exception:
        pass

    # Take a pre-search screenshot for debugging
    page.screenshot(path="debug_pre_search.png", full_page=True)

    # ---- Try DataTables JS API first (fastest) — search by first name ----
    try:
        found_via_js = page.evaluate("""
            (searchTerm) => {
                // Try DataTables API via jQuery
                if (typeof $ !== 'undefined' && $.fn && $.fn.dataTable) {
                    const tables = $.fn.dataTable.tables();
                    if (tables.length > 0) {
                        $(tables[0]).DataTable().search(searchTerm).draw();
                        return 'datatable-api';
                    }
                }
                // Target the specific table-search input by ID first
                const byId = document.querySelector('#table-search');
                if (byId) {
                    byId.value = searchTerm;
                    byId.dispatchEvent(new Event('input',  { bubbles: true }));
                    byId.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                    return 'by-id';
                }
                // Fallback: any search-type input (NOT datetimepicker)
                const sel = [
                    "div.dataTables_filter input",
                    ".dt-search input",
                    "input[type='search']",
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el) {
                        el.value = searchTerm;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                        return s;
                    }
                }
                return null;
            }
        """, search_term)
        if found_via_js:
            print(f"    Search triggered via JS ({found_via_js}) for '{search_term}'")
            page.wait_for_timeout(2_000)
    except Exception as e:
        print(f"    [WARN] JS search failed: {e}")

    # ---- Also try Playwright click-and-type on #table-search directly ----
    try:
        inp = page.locator("#table-search").first
        if inp.is_visible(timeout=3_000):
            inp.triple_click()
            inp.fill("")                        # clear first
            inp.type(search_term, delay=80)     # type first name only
            page.wait_for_timeout(2_000)
            print(f"    Playwright typed into #table-search for '{search_term}'")
    except Exception:
        pass

    page.screenshot(path="debug_after_search.png", full_page=True)

    # ---- Scan the (hopefully filtered) table rows ----
    def scan_table_for_href() -> str | None:
        try:
            rows = page.locator("table tbody tr").all()
            for row in rows:
                try:
                    row_text = row.inner_text(timeout=2_000).strip().lower()
                    if first_name in row_text:
                        link = row.locator("a").first
                        href = link.get_attribute("href") or ""
                        if href:
                            return href if href.startswith("http") else BASE_URL + href
                except Exception:
                    continue
        except Exception:
            pass
        return None

    result = scan_table_for_href()
    if result:
        return _build_shift_url(result)

    # ---- Last resort: paginate through the table (up to 3 pages) ----
    print("    [INFO] Not found on current view — paging through table...")
    for page_num in range(2, 4):   # try pages 2 and 3
        try:
            next_btn = page.locator(f"a.paginate_button:has-text('{page_num}'), "
                                    f"a[data-dt-idx='{page_num}']").first
            if next_btn.is_visible(timeout=3_000):
                next_btn.click()
                page.wait_for_timeout(1_500)
                result = scan_table_for_href()
                if result:
                    print(f"    Found on page {page_num}!")
                    return _build_shift_url(result)
        except Exception:
            break

    return None


# ---------------------------------------------------------------------------
# Collect eligible rows for the associate
# ---------------------------------------------------------------------------

def apply_time_filter(page, target_mmdd: str) -> None:
    """
    Fill the 'Filter by Time' from/to inputs with the target date (full day),
    click Apply Filters, then wait until the table shows target_mmdd rows.
    """
    from_val = f"{DATE} 00:00"
    to_val   = f"{DATE} 23:59"

    try:
        # Set from/to via JS to bypass datetimepicker widget quirks
        page.evaluate(f"""
            () => {{
                const setVal = (id, val) => {{
                    const el = document.getElementById(id);
                    if (!el) return;
                    const nativeInput = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeInput.call(el, val);
                    el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }};
                setVal('id_from_time', '{from_val}');
                setVal('id_to_time',   '{to_val}');
            }}
        """)
        print(f"    [INFO] Set filter range: {from_val} → {to_val}")
    except Exception as e:
        print(f"    [WARN] Could not set date range inputs: {e}")

    try:
        btn = page.locator("#filterButton, button:has-text('Apply Filters'), input[value='Apply Filters']").first
        btn.wait_for(state="visible", timeout=8_000)
        btn.click()
        page.wait_for_function(
            f"""() => {{
                const cells = Array.from(document.querySelectorAll('table tbody td'));
                return cells.some(c => c.innerText.trim().startsWith('{target_mmdd}'));
            }}""",
            timeout=20_000,
        )
        print(f"    [INFO] Filter applied — table now shows {target_mmdd} rows.")
    except Exception as e:
        print(f"    [WARN] Could not confirm filter applied: {e}")


def collect_eligible_rows(page) -> list[dict]:
    """Returns list of {timestamp, href} for rows that pass all skip rules."""
    target_mmdd = f"{DATE[5:7]}/{DATE[8:10]}"  # e.g. '2026-04-01' -> '04/01'
    apply_time_filter(page, target_mmdd)
    page.wait_for_selector("table", timeout=60_000)
    result   = page.evaluate(JS_EXTRACT_ROWS)
    row_data = result.get("rows", [])

    eligible  = []
    skip_mode = False

    for row in row_data:
        ts            = row.get("timestamp",   "?")
        dept          = row.get("department",  "")
        user          = row.get("userText",    "")
        ex_html       = row.get("exemptHtml",  "")
        ex_text       = row.get("exemptText",  "")
        href          = row.get("editHref")
        has_idle_icon = row.get("hasIdleIcon", False)

        if _has_green_flag(ex_html, ex_text):
            print(f"    [SKIP] {ts}  (green flag -> skip-mode ON)")
            skip_mode = True
            continue

        if skip_mode:
            if _has_checkered_flag(ex_html, ex_text):
                print(f"    [SKIP] {ts}  (checkered flag -> skip-mode OFF)")
                skip_mode = False
            else:
                print(f"    [SKIP] {ts}  (inside exempt window)")
            continue

        if not has_idle_icon:
            print(f"    [SKIP] {ts}  (no idle event)")
            continue

        # Only process rows matching our target date (MM/DD format)
        if not ts.startswith(target_mmdd):
            print(f"    [SKIP] {ts}  (wrong date, want {target_mmdd})")
            continue

        if TARGET_DEPT.lower() not in dept.lower():
            print(f"    [SKIP] {ts}  (dept: '{dept}')")
            continue

        if _in_skip_window(ts):
            print(f"    [SKIP] {ts}  (blocked window {SKIP_TIME_START}-{SKIP_TIME_END})")
            continue

        if user:
            print(f"    [SKIP] {ts}  (already edited by: {user})")
            continue

        if not href:
            print(f"    [SKIP] {ts}  (no edit link)")
            continue

        print(f"    [EDIT] {ts}")
        eligible.append({"timestamp": ts, "href": href})

    return eligible


# ---------------------------------------------------------------------------
# Edit one row
# ---------------------------------------------------------------------------

def edit_row(page, href: str, timestamp: str) -> bool:
    """Navigate to edit URL, select Modified SC Code, submit. Returns True on success."""
    page.goto(href, wait_until="networkidle", timeout=60_000)

    if "assign_code" not in page.url:
        print(f"      [WARN] Redirected away from edit form - skipping {timestamp}")
        return False

    selected = False
    try:
        sc_select = page.locator("select").filter(
            has=page.locator("option", has_text=MODIFIED_SC)
        ).first
        sc_select.wait_for(state="visible", timeout=10_000)
        sc_select.select_option(label=MODIFIED_SC)
        selected = True
    except PlaywrightTimeoutError:
        pass

    if not selected:
        try:
            label = page.locator(
                "label:has-text('Modified SC Code'), "
                "*:has-text('Modified SC Code')"
            ).first
            label.wait_for(state="visible", timeout=10_000)
            label.locator("..").locator(
                "input, button, [role='combobox']"
            ).first.click()
            page.wait_for_timeout(1_000)
            page.locator(f"text={MODIFIED_SC}").first.click()
        except PlaywrightTimeoutError:
            print(f"      [WARN] Modified SC Code field not found - skipping {timestamp}")
            return False

    SUBMIT_SEL = (
        "button:has-text('Submit Event'), "
        "input[value='Submit Event'], "
        "button:has-text('Submit')"
    )
    try:
        page.wait_for_selector(SUBMIT_SEL, state="visible", timeout=30_000)
        page.evaluate("""
            const all = [...document.querySelectorAll('button, input[type="submit"]')];
            const btn = all.find(el =>
                el.innerText?.includes('Submit') || el.value?.includes('Submit')
            );
            if (btn) btn.click();
            else throw new Error('Submit button not found');
        """)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass
        print(f"      [OK] Submitted: {timestamp}")
        return True
    except Exception as exc:
        print(f"      [WARN] Submit failed for {timestamp}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:

        # [1] Launch persistent browser
        print("[1/4] Launching browser with persistent session...")
        context = None
        for channel in ("msedge", "chrome"):
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    channel=channel,
                    headless=False,
                    slow_mo=0,
                    args=LAUNCH_ARGS,
                    timeout=30_000,
                )
                print(f"    Using {channel}  |  Profile: {PROFILE_DIR}")
                break
            except Exception as exc:
                print(f"    {channel} unavailable: {exc}")

        if context is None:
            raise RuntimeError("Could not launch Chrome or Edge.")

        # [2] Verify session / wait for login
        print("\n[2/4] Checking DRAX session...")
        main_page = context.new_page()
        print("    " + "*" * 56)
        print("    * DO NOT CLOSE THIS BROWSER WINDOW while the    *")
        print("    * script is running!                             *")
        print("    " + "*" * 56)
        try:
            main_page.goto(PERF_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            pass

        logged_in = False
        for attempt in range(60):  # wait up to 5 minutes
            current_url = main_page.url
            if "drax.walmart.com" in current_url and "associateperformance" in current_url:
                logged_in = True
                print("    Session valid - proceeding!")
                break
            if attempt == 0:
                print("\n    ACTION REQUIRED: Log in to DRAX in the browser")
                print("    window that just opened, then the script will")
                print("    continue automatically.\n")
            print(f"    Waiting for login... ({attempt * 5}s elapsed)")
            main_page.wait_for_timeout(5_000)

        if not logged_in:
            context.close()
            raise RuntimeError("Login not completed within 5 minutes. Aborting.")

        try:
            main_page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        # [3] Search for the associate
        print(f"\n[3/4] Searching for '{TARGET_ASSOCIATE_NAME}' on performance page...")
        assoc_href = find_associate_href(main_page, TARGET_ASSOCIATE_NAME)

        if not assoc_href:
            # Fallback: try the broader URL without SC code filter
            print(f"    Not found on filtered page. Trying broader search...")
            try:
                main_page.goto(PERF_URL_BROAD, wait_until="domcontentloaded", timeout=60_000)
                main_page.wait_for_timeout(2_000)
            except PlaywrightTimeoutError:
                pass
            assoc_href = find_associate_href(main_page, TARGET_ASSOCIATE_NAME)

        if not assoc_href:
            # Take a debug screenshot so we can see what the page looks like
            main_page.screenshot(path="debug_search_fail.png", full_page=True)
            context.close()
            raise RuntimeError(
                f"Could not find '{TARGET_ASSOCIATE_NAME}' on the page. "
                "See debug_search_fail.png for the current page state."
            )

        print(f"    Found! Navigating to: {assoc_href}")
        main_page.close()

        # [4] Process the associate's timeline
        print(f"\n[4/4] Processing '{TARGET_ASSOCIATE_NAME}'...")
        start_time = time.time()

        assoc_page = context.new_page()
        edit_page  = context.new_page()

        assoc_page.goto(assoc_href, wait_until="domcontentloaded", timeout=60_000)
        assoc_page.screenshot(path="debug_associate_found.png", full_page=True)

        # Guard against redirect to login
        if "drax.walmart.com" not in assoc_page.url or "associates" not in assoc_page.url:
            context.close()
            raise RuntimeError(
                f"Session expired or wrong redirect: {assoc_page.url[:100]}"
            )

        eligible = collect_eligible_rows(assoc_page)

        if not eligible:
            print("    Nothing to edit — all rows skipped by conditions.")
        else:
            print(f"    {len(eligible)} row(s) to edit:")
            edited = 0
            for i, row in enumerate(eligible, 1):
                print(f"\n    [{i}/{len(eligible)}] {row['timestamp']}")
                success = edit_row(edit_page, row["href"], row["timestamp"])
                if success:
                    edited += 1
                edit_page.wait_for_timeout(1_500)

            elapsed = round(time.time() - start_time)
            print(f"\n{'='*60}")
            print(f"  DONE — {edited}/{len(eligible)} row(s) edited for {TARGET_ASSOCIATE_NAME}")
            print(f"  Time elapsed: {elapsed}s")
            print(f"{'='*60}")

        try:
            edit_page.screenshot(path="debug_final_single.png", full_page=True)
            print("\n  Screenshot saved: debug_final_single.png")
        except Exception:
            pass

        edit_page.wait_for_timeout(2_000)
        context.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(0)
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)
