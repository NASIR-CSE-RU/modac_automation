from __future__ import annotations
import asyncio
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Pattern, Tuple, Sequence

from playwright.async_api import async_playwright, Page, BrowserContext

# =============================================================================
# MDAC Automation — ultra-verbose debug build
# - Console-log everything (steps, events, selectors, values, errors)
# - Never crash on minor DOM differences; keep going and log why
# =============================================================================

# ===== Config =====
BASE = "https://imigresen-online.imi.gov.my/mdac/main"
HEADLESS_DEFAULT = os.getenv("HEADLESS", "1") == "1"            # headless by default in Docker
LOG_NETWORK = os.getenv("LOG_NETWORK", "1") == "1"              # <— default ON for deep debug
GATE_WAIT_SECONDS = int(os.getenv("GATE_WAIT_SECONDS", "120"))
RECORD_TRACE = os.getenv("RECORD_TRACE", "1") == "1"            # keep traces for debugging

def log(msg: str) -> None:
    print(f"[MDAC] {msg}", flush=True)

def log_ok(step: str) -> None:
    log(f"✅ {step}")

def log_warn(step: str) -> None:
    log(f"⚠️  {step}")

def log_err(step: str, e: BaseException | None = None) -> None:
    if e:
        tb = "".join(traceback.format_exception_only(type(e), e)).strip()
        log(f"❌ {step} -> {tb}")
    else:
        log(f"❌ {step}")

def log_exc(step: str, e: BaseException) -> None:
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__)).rstrip()
    log(f"💥 {step}\n{tb}")

async def log_exists(page: Page, sel: str, timeout=1500) -> bool:
    try:
        await page.wait_for_selector(sel, state="attached", timeout=timeout)
        log_ok(f"Selector attached: {sel}")
        return True
    except Exception as e:
        log_warn(f"Selector NOT attached (skip): {sel} ({e})")
        return False


# ===== Gate =====
class ManualGate:
    def __init__(self):
        self._events: dict[str, asyncio.Event] = {}
    def create(self, token: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._events[token] = ev
        return ev
    def resume(self, token: str) -> bool:
        ev = self._events.get(token)
        if not ev:
            return False
        ev.set()
        del self._events[token]
        return True

GATE = ManualGate()


# ===== Artifacts =====
@dataclass
class ContextArtifacts:
    video_path: Optional[Path] = None
    trace_path: Optional[Path] = None
    screenshots_dir: Optional[Path] = None


# ===== Listeners / screenshots =====
async def _attach_listeners(page: Page) -> None:
    def safe(fn):
        async def wrap(*args, **kwargs):
            try:
                res = fn(*args, **kwargs)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                log_exc("Listener error", e)
        return wrap

    @safe
    def on_console(m):
        t = ""
        try:
            t = getattr(m, "type", "") or ""
        except Exception:
            pass
        try:
            txt = m.text()
        except Exception:
            txt = "<console message unavailable>"
        log(f"BROWSER {str(t).upper()}: {txt}")

    @safe
    def on_pageerror(e):
        log_err("BROWSER pageerror", e)

    @safe
    def on_dialog(dlg):
        log(f"Dialog: {dlg.type} {dlg.message}")
        try:
            dlg.dismiss()
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("dialog", on_dialog)

    if LOG_NETWORK:
        @safe
        def on_request(r):
            try:
                log(f"REQ {r.method} {r.url}")
            except Exception as e:
                log_err("REQ log failed", e)

        @safe
        def on_response(r):
            try:
                log(f"RES {r.status} {r.url}")
            except Exception as e:
                log_err("RES log failed", e)

        @safe
        def on_request_failed(r):
            try:
                log(f"REQ-FAILED {r.method} {r.url} -> {r.failure.error_text if r.failure else '?'}")
            except Exception as e:
                log_err("REQ-FAILED log failed", e)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    @safe
    def on_framenav(frame):
        try:
            log(f"FRAME NAV -> {frame.url}")
        except Exception:
            pass

    page.on("framenavigated", on_framenav)


def _get_screens_dir(page: Page) -> Optional[Path]:
    return getattr(page, "_mdac_screens", None)


async def _screenshot(page: Page, name: str) -> None:
    target_dir = _get_screens_dir(page)
    if not target_dir:
        return
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        log_ok(f"Screenshot: {path}")
    except Exception as e:
        log_err(f"screenshot failed ({name})", e)


# ===== Browser / context =====
async def open_context(
    download_dir: Optional[Path] = None,
    headless: Optional[bool] = None,
    record_video_dir: Optional[Path] = None,
) -> Tuple[BrowserContext, Page, ContextArtifacts]:
    """
    Create a fresh context/page. If record_video_dir is set, we record video and store
    screenshots under <record_video_dir>/screens. Also starts Playwright trace if enabled.
    """
    if headless is None:
        headless = HEADLESS_DEFAULT

    pw = await async_playwright().start()
    log(f"Launching Chromium headless={headless}")
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
    )

    ctx_kwargs = {
        "viewport": {"width": 1280, "height": 900},
        "accept_downloads": True,
    }

    screenshots_dir: Optional[Path] = None
    if record_video_dir is not None:
        try:
            record_video_dir.mkdir(parents=True, exist_ok=True)
            ctx_kwargs["record_video_dir"] = str(record_video_dir)
            ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}
            screenshots_dir = record_video_dir / "screens"
            log_ok(f"Video+Screens dir ready: {record_video_dir}")
        except Exception as e:
            log_err("Failed to prepare record_video_dir", e)

    context = await browser.new_context(**ctx_kwargs)
    log_ok("New browser context created")

    if RECORD_TRACE and record_video_dir is not None:
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            log_ok("Tracing started")
        except Exception as e:
            log_err("Tracing start failed", e)

    page = await context.new_page()
    log_ok("New page created")
    await _attach_listeners(page)

    # Attach screenshots dir to page for convenience
    if screenshots_dir:
        setattr(page, "_mdac_screens", screenshots_dir)
        log_ok(f"Attached screenshots dir to page: {screenshots_dir}")

    if download_dir:
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
            log_ok(f"Download dir ready: {download_dir}")
        except Exception as e:
            log_err("Failed to prepare download_dir", e)

    return context, page, ContextArtifacts(
        video_path=None,
        trace_path=None,
        screenshots_dir=screenshots_dir,
    )


async def _finalize_artifacts(
    context: BrowserContext,
    page: Page,
    artifacts: ContextArtifacts,
    record_dir: Optional[Path],
) -> ContextArtifacts:
    # Stop trace BEFORE closing context
    if RECORD_TRACE and record_dir is not None:
        trace_zip = record_dir / "trace.zip"
        try:
            await context.tracing.stop(path=str(trace_zip))
            artifacts.trace_path = trace_zip
            log_ok(f"Trace saved: {trace_zip}")
        except Exception as e:
            log_err("Trace stop failed", e)

    # Close context to flush video file to disk
    try:
        await context.close()
        log_ok("Context closed")
    except Exception as e:
        log_err("Context close failed", e)

    # Only after context closed, the video path becomes available
    try:
        if getattr(page, "video", None):
            vp = await page.video.path()
            artifacts.video_path = Path(vp)
            log_ok(f"Video saved: {vp}")
    except Exception as e:
        log_err("Video path fetch failed", e)

    return artifacts


# ===== Generic actions (used by download flow only) =====
async def navigate_safe(page: Page, url: str) -> None:
    try:
        log(f"Navigate -> {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        log_ok(f"Navigated: {page.url}")
    except Exception as e:
        log_err(f"Navigate failed ({url}), retrying BASE", e)
        try:
            await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            log_ok(f"Fallback navigate -> {BASE}")
        except Exception as e2:
            log_exc("Fallback navigate failed", e2)


async def click_if_exists(page: Page, text_regex: Pattern[str]) -> bool:
    log(f"Try click-if-exists: /{text_regex.pattern}/")
    btn = page.get_by_role("button", name=text_regex)
    try:
        if await btn.count():
            log_ok(f"Button match found: /{text_regex.pattern}/")
            await btn.first().click()
            log_ok("Button clicked")
            return True
    except Exception as e:
        log_err("Button click failed", e)

    link = page.get_by_role("link", name=text_regex)
    try:
        if await link.count():
            log_ok(f"Link match found: /{text_regex.pattern}/")
            await link.first().click()
            log_ok("Link clicked")
            return True
    except Exception as e:
        log_err("Link click failed", e)

    log_warn(f"No button/link matched: /{text_regex.pattern}/")
    return False


# ===== Helpers tailored to the provided HTML =====
def _map_gender(g: Optional[str]) -> str:
    if not g: return ""
    g = g.strip().lower()
    if g.startswith("m"): return "1"  # MALE
    if g.startswith("f"): return "2"  # FEMALE
    return ""


def _map_mode(m: Optional[str]) -> str:
    if not m: return ""
    m = m.strip().lower()
    if m.startswith("air"):  return "1"
    if m.startswith("land"): return "2"
    if m.startswith("sea"):  return "3"
    return ""


def _extract_region_code(phone: Optional[str]) -> str:
    """
    Try to extract a country calling code for #region select, e.g., +8801... -> '880'.
    Very naive, but good enough for auto-selecting the Region Code.
    """
    if not phone:
        return ""
    s = re.sub(r"[^\d+]", "", phone)
    m = re.match(r"^\+?0{0,2}(\d{1,3})", s)
    code = m.group(1) if m else ""
    log(f"Extracted region code from '{phone}': '{code}'")
    return code


async def set_date_by_id(page: Page, input_id: str, date_str: str) -> None:
    """
    Robustly set a Bootstrap/jQuery datepicker (or plain readonly input).
    Accepts: 'YYYY-MM-DD', 'DD/MM/YYYY', 'DD-MM-YYYY'
    Writes:  'DD/MM/YYYY'
    Logs every step.
    """
    sel = f"#{input_id}"
    # --- Normalize to DD/MM/YYYY ---
    s = (date_str or "").strip()
    m1 = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)             # YYYY-MM-DD
    m2 = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)             # DD-MM-YYYY
    if m1:
        dd, mm, yyyy = m1.group(3), m1.group(2), m1.group(1)
    elif m2:
        dd, mm, yyyy = m2.group(1), m2.group(2), m2.group(3)
    else:
        # assume already DD/MM/YYYY (light validation)
        mmddyyyy = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
        if not mmddyyyy:
            print(f"[MDAC] ❌ set_date_by_id('{input_id}'): unsupported format '{date_str}'", flush=True)
            return
        dd, mm, yyyy = mmddyyyy.group(1), mmddyyyy.group(2), mmddyyyy.group(3)
    ddmmyyyy = f"{dd}/{mm}/{yyyy}"

    print(f"[MDAC] 📅 set_date_by_id('{input_id}'): input='{date_str}' -> normalized='{ddmmyyyy}'", flush=True)

    # Ensure element exists
    try:
        await page.wait_for_selector(sel, timeout=15000)
    except Exception as e:
        print(f"[MDAC] ❌ date field not found: {sel} ({e})", flush=True)
        return

    # Try plugin path first (Bootstrap Datepicker / jQuery UI Datepicker / Tempus)
    js = """
    async (cfg) => {
      const { sel, val, dd, mm, yyyy } = cfg;
      const el = document.querySelector(sel);
      if (!el) return { ok:false, mode:'none', reason:'no element' };

      const toDate = () => {
        const y = parseInt(yyyy, 10), m = parseInt(mm, 10)-1, d = parseInt(dd, 10);
        const dt = new Date(y, m, d);
        // guard invalid
        return isNaN(dt.getTime()) ? null : dt;
      };
      const dt = toDate();

      const fire = (name) => {
        try { el.dispatchEvent(new Event(name, { bubbles:true })); } catch (_) {}
      };

      // ---- Bootstrap Datepicker (eternicode) ----
      try {
        const $ = window.jQuery || window.$;
        if ($ && $(el).datepicker) {
          if (dt) {
            $(el).datepicker('setDate', dt);
          } else {
            $(el).datepicker('setDate', val);
          }
          try { $(el).datepicker('update'); } catch(_) {}
          try { $(el).trigger('changeDate'); } catch(_) {}
          // Some implementations mirror to a hidden alt field via data-link-field
          const linkId = el.getAttribute('data-link-field');
          if (linkId) {
            const alt = document.getElementById(linkId);
            if (alt) alt.value = val;
          }
          el.value = val;     // ensure visible value matches
          fire('input'); fire('change'); el.blur();
          return { ok:true, mode:'bootstrap-datepicker' };
        }
      } catch (e) {
        return { ok:false, mode:'bootstrap-datepicker', reason:String(e) };
      }

      // ---- jQuery UI Datepicker ----
      try {
        const $ = window.jQuery || window.$;
        if ($ && $.datepicker && $.isFunction($.datepicker._selectDate)) {
          if (dt) {
            // _setDate formats automatically per widget options
            $(el).datepicker('setDate', dt);
          } else {
            $(el).val(val);
            $(el).datepicker('setDate', $(el).val());
          }
          try { $(el).datepicker('refresh'); } catch(_) {}
          fire('input'); fire('change'); el.blur();
          return { ok:true, mode:'jquery-ui' };
        }
      } catch (e) {
        return { ok:false, mode:'jquery-ui', reason:String(e) };
      }

      // ---- Tempus Dominus / Bootstrap 4/5 datetimepicker (common APIs) ----
      try {
        const $ = window.jQuery || window.$;
        if ($ && $(el).data && ($(el).data('DateTimePicker') || $(el).data('datetimepicker'))) {
          const w = $(el).data('DateTimePicker') || $(el).data('datetimepicker');
          if (w && w.date) {
            if (dt) w.date(dt); else w.date(val);
            fire('input'); fire('change'); el.blur();
            return { ok:true, mode:'tempus' };
          }
        }
      } catch (e) {
        return { ok:false, mode:'tempus', reason:String(e) };
      }

      // ---- Inputmask-aware plain input ----
      try {
        // If Inputmask is attached, prefer its API so masks/validators run
        if (el.inputmask && typeof el.inputmask.setValue === 'function') {
          el.inputmask.setValue(val);
          fire('input'); fire('change'); el.blur();
          return { ok:true, mode:'inputmask' };
        }
      } catch (e) {
        return { ok:false, mode:'inputmask', reason:String(e) };
      }

      // ---- Plain fallback ----
      try {
        el.removeAttribute('readonly');
        el.value = val;
        fire('input'); fire('change'); el.blur();
        return { ok:true, mode:'plain' };
      } catch (e) {
        return { ok:false, mode:'plain', reason:String(e) };
      }
    }
    """

    try:
        res = await page.evaluate(js, {"sel": sel, "val": ddmmyyyy, "dd": dd, "mm": mm, "yyyy": yyyy})
        print(f"[MDAC] 📅 set_date_by_id('{input_id}'): mode={res.get('mode')} ok={res.get('ok')} reason={res.get('reason')}", flush=True)
    except Exception as e:
        print(f"[MDAC] ❌ set_date_by_id('{input_id}') JS failed: {e}", flush=True)

    # Verify what the field has now (after a brief tick so masks/formatters run)
    try:
        await page.wait_for_timeout(50)
        got = await page.eval_on_selector(sel, "el => el.value")
        print(f"[MDAC] 📅 verify {input_id} -> '{got}'", flush=True)
    except Exception as e:
        print(f"[MDAC] ⚠️  verify failed for {input_id}: {e}", flush=True)


async def _select_if_value(page: Page, css: str, value: Optional[str]) -> None:
    if not value:
        log_warn(f"_select_if_value: No value for {css}")
        return
    try:
        await page.wait_for_selector(css, timeout=4000)
        await page.select_option(css, str(value))
        log_ok(f"Select {css} = {value}")
    except Exception as e:
        log_err(f"Select failed {css} = {value}", e)


async def _fill_if_value(page: Page, css: str, value: Optional[str]) -> None:
    if value is None:
        log_warn(f"_fill_if_value: No value for {css}")
        return
    try:
        await page.wait_for_selector(css, timeout=4000)
        await page.fill(css, str(value))
        log_ok(f"Fill {css} = '{value}'")
    except Exception as e:
        log_err(f"Fill failed {css} = '{value}'", e)


# ===== High-level flows =====
async def register_one(page: Page, row: "RegisterRow", gate_token: Optional[str] = None, pause: bool = True) -> str:
    log("=== register_one: START ===")
    # Go straight to the registration page
    await navigate_safe(page, f"{BASE}?registerMain")
    await _screenshot(page, "01_register_main")

    # Ensure accordion open
    try:
        if not await page.locator("#name").is_visible():
            log_warn("#name not visible; try opening 'Personal Information' accordion")
            await page.get_by_role("link", name=re.compile(r"personal information", re.I)).click(timeout=4000)
        await page.locator("#passNo").click()
        log_ok("Personal Information section visible")
    except Exception as e:
        log_err("Opening Personal Information section failed (will proceed)", e)

    await _screenshot(page, "02_form_visible")

    # ---- Personal Information ----
    await _fill_if_value(page, "#name", getattr(row, "fullName", None))
    await _fill_if_value(page, "#passNo", getattr(row, "passport", None))
    await _fill_if_value(page, "#email", getattr(row, "email", None))
    await _fill_if_value(page, "#confirmEmail", getattr(row, "email", None))

    # DOB may not be #dob; try several selectors and normalize formats
    await set_date_by_id(page, 'dob', getattr(row, "dateOfBirth", None))  # DD/MM/YYYY expected

    # Nationality (3-letter code, e.g. 'BGD')
    await _select_if_value(page, "#nationality", getattr(row, "nationality", None))

    # Sex (1=MALE, 2=FEMALE)
    mapped_sex = _map_gender(getattr(row, "gender", None))
    log(f"Map gender '{getattr(row, 'gender', None)}' -> '{mapped_sex}'")
    await _select_if_value(page, "#sex", mapped_sex)

    # Passport Expiry (optional field in your model)
    pass_exp = getattr(row, "passportExpiryDate", None)
    await set_date_by_id(page, 'passExpDte', getattr(row, "passportExpiryDate", None))

    # Country / Region code + Mobile
    region_code = getattr(row, "regionCode", None) or _extract_region_code(getattr(row, "phone", None))
    await _select_if_value(page, "#region", region_code)

    mobile = getattr(row, "mobile", None)
    if not mobile and getattr(row, "phone", None):
        mobile = re.sub(r"^\+?\d{1,3}\s*[-]?\s*", "", getattr(row, "phone"))
        log(f"Derived mobile from phone: '{getattr(row, 'phone')}' -> '{mobile}'")
    await _fill_if_value(page, "#mobile", mobile)

    # ---- Traveling Information ----
    try:
        if not await page.locator("#arrDt").is_visible():
            log_warn("#arrDt not visible; open 'Travel' accordion")
            await page.get_by_role("link", name=re.compile(r"travel", re.I)).click(timeout=4000)
    except Exception as e:
        log_err("Opening Travel accordion failed (will proceed)", e)

    await set_date_by_id(page, 'arrDt', getattr(row, "arrivalDate", None))  # must be within 3 days
    await set_date_by_id(page, 'depDt', getattr(row, "departureDate", None))

    await _fill_if_value(page, "#vesselNm", getattr(row, "flightNo", None))  # Flight / Vessel No.

    # Mode of Travel (1=AIR, 2=LAND, 3=SEA)
    mapped_mode = _map_mode(getattr(row, "arrivalMode", None))
    log(f"Map mode '{getattr(row, 'arrivalMode', None)}' -> '{mapped_mode}'")
    await _select_if_value(page, "#trvlMode", mapped_mode)

    # Last port of embarkation before Malaysia (3-letter code, e.g. 'BGD')
    await _select_if_value(page, "#embark", 'BGD - BANGLADESH')

    # ---- Accommodation ----
    acc_type = getattr(row, "accommodationStay", None) or "01"  # default to Hotel
    await _select_if_value(page, "#accommodationStay", acc_type)

    addr1 = getattr(row, "addressInMalaysia", None) or getattr(row, "accommodationAddress1", None)
    await _fill_if_value(page, "#accommodationAddress1", addr1)
    await _fill_if_value(page, "#accommodationAddress2", getattr(row, "accommodationAddress2", ""))

    state_code = getattr(row, "accommodationState", None) or getattr(row, "stateCode", None) or "14"  # WP Kuala Lumpur
    await _select_if_value(page, "#accommodationState", state_code)

    # Wait for city list to populate after state change
    city_code = getattr(row, "accommodationCity", None) or getattr(row, "cityCode", None)
    try:
        log("Wait for city list to populate (#accommodationCity)")
        await page.wait_for_function(
            "() => { const sel = document.querySelector('#accommodationCity'); return sel && sel.options && sel.options.length > 1; }",
            timeout=10000,
        )
        log_ok("City list populated")
        if city_code:
            await page.select_option("#accommodationCity", city_code)
            log_ok(f"City selected: {city_code}")
        else:
            first_val = await page.evaluate("""
                () => {
                  const sel = document.querySelector('#accommodationCity');
                  if (!sel) return '';
                  for (const opt of sel.options) { if (opt.value) return opt.value; }
                  return '';
                }
            """)
            if first_val:
                await page.select_option("#accommodationCity", first_val)
                log_ok(f"City selected (first non-empty): {first_val}")
            else:
                log_warn("No non-empty city option found")
    except Exception as e:
        log_err("City list populate/select failed", e)

    postcode = getattr(row, "accommodationPostcode", None) or getattr(row, "postcode", None) or "50050"
    await _fill_if_value(page, "#accommodationPostcode", postcode)

    # Pause for manual CAPTCHA/OTP if requested
    if pause and gate_token:
        log(f"PAUSE gate_token={gate_token} (waiting up to {GATE_WAIT_SECONDS}s)")
        try:
            await page.evaluate(f"console.log('Solve CAPTCHA/OTP, then POST /resume/{gate_token}')")
        except Exception:
            pass
        ev = GATE.create(gate_token)
        try:
            await asyncio.wait_for(ev.wait(), timeout=GATE_WAIT_SECONDS)
            log_ok("Gate resumed")
        except asyncio.TimeoutError:
            log_warn("Gate timed out; continuing")

    # Submit
    log("Try submit form (#submit or Enter)")
    try:
        await page.click("#submit", timeout=5000)
        log_ok("Submit button clicked")
    except Exception as e:
        log_warn(f"#submit click failed ({e}); try Enter")
        try:
            await page.keyboard.press("Enter")
            log_ok("Enter pressed for submit")
        except Exception as e2:
            log_err("Submit via Enter failed", e2)

    await page.wait_for_timeout(1500)
    await _screenshot(page, "03_after_submit")

    try:
        body_txt = await page.inner_text("body")
    except Exception:
        body_txt = "submitted"
    summary = body_txt[:500]
    safe_excerpt = summary[:160].replace(os.linesep, " ")
    log(f"Register result (excerpt): {safe_excerpt}")
    log("=== register_one: END ===")
    return summary


async def download_one(page: Page, row: "PinRow", download_dir: Path) -> Optional[Path]:
    log("=== download_one: START ===")
    await navigate_safe(page, f"{BASE}?checkMain")
    await _screenshot(page, "10_check_main")

    await click_if_exists(page, re.compile(r"(check registration|check|retrieve)", re.I))

    # The check/retrieve screen typically uses labeled inputs; keep generic helpers if IDs differ
    # Try common patterns first:
    try:
        await page.fill("input[name*='pass']", row.passport)
        log_ok("Filled passport on check page")
    except Exception as e:
        log_err("Fill passport on check page failed", e)
    try:
        await page.select_option("select[name*='nation'], select[name*='nationality']", row.nationality)
        log_ok("Selected nationality on check page")
    except Exception as e:
        log_err("Select nationality on check page failed", e)
    try:
        await page.fill("input[name*='pin']", row.pin)
        log_ok("Filled pin on check page")
    except Exception as e:
        log_err("Fill pin on check page failed", e)

    if not await click_if_exists(page, re.compile(r"(submit|check|search)", re.I)):
        try:
            await page.keyboard.press("Enter")
            log_ok("Pressed Enter on check page")
        except Exception as e:
            log_err("Enter press on check page failed", e)

    await page.wait_for_timeout(1200)

    # Direct download
    try:
        btn = page.get_by_role("button", name=re.compile(r"(download|print)", re.I))
        lnk = page.get_by_role("link", name=re.compile(r"(download|print)", re.I))
        if await btn.count():
            await btn.first().click()
            log_ok("Clicked download/print button")
        elif await lnk.count():
            await lnk.first().click()
            log_ok("Clicked download/print link")
        download = await page.wait_for_event("download", timeout=25000)
        suggested = download.suggested_filename
        out = download_dir / f"{row.passport}_{suggested}"
        await download.save_as(str(out))
        log_ok(f"PDF saved: {out}")
        log("=== download_one: END (direct) ===")
        return out
    except Exception as e:
        log_err("Direct download failed", e)

    # Popup PDF
    try:
        async with page.expect_popup(timeout=25000) as pop_wait:
            await click_if_exists(page, re.compile(r"(download|print|pdf)", re.I))
        popup = await pop_wait.value
        log_ok(f"Popup opened: {popup.url}")
        resp = await popup.wait_for_event(
            "response",
            predicate=lambda r: "application/pdf" in (r.headers.get("content-type", "")),
            timeout=25000,
        )
        content = await resp.body()
        out = download_dir / f"{row.passport}_{int(asyncio.get_event_loop().time()*1000)}.pdf"
        out.write_bytes(content)
        await popup.close()
        log_ok(f"PDF saved (popup): {out}")
        log("=== download_one: END (popup) ===")
        return out
    except Exception as e:
        log_err("Popup download failed", e)
        log("=== download_one: END (failed) ===")
        return None


# expose finalize for main.py
__all__ = ["open_context", "_finalize_artifacts", "register_one", "download_one", "GATE"]
