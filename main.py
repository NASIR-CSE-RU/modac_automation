from __future__ import annotations
import csv
import io
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from models import RegisterRow, PinRow
from mdac_automation import (
    open_context,
    register_one,
    download_one,
    GATE,
    _finalize_artifacts,   # finalize: stop trace, close context, resolve video path
)

app = FastAPI(title="MDAC Automation API", version="1.0.0")

DOWNLOAD_DIR = Path("./downloads")
VIDEOS_DIR = Path("./videos")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

def log(msg: str) -> None:
    print(f"[API] {msg}", flush=True)

def default_pause(headless: Optional[bool]) -> bool:
    """Pause only when running headful (so you can solve CAPTCHA)."""
    if headless is None:
        headless = os.getenv("HEADLESS", "1") == "1"
    return not headless


# ========== CSV parsers ==========

async def parse_csv_register(f: UploadFile) -> List[RegisterRow]:
    raw = await f.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[RegisterRow] = [RegisterRow(**r) for r in reader]
    if not rows:
        raise HTTPException(status_code=400, detail="register.csv is empty or has no valid rows.")
    return rows

async def parse_csv_pins(f: UploadFile) -> List[PinRow]:
    raw = await f.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[PinRow] = [PinRow(**r) for r in reader]
    if not rows:
        raise HTTPException(status_code=400, detail="pins.csv is empty or has no valid rows.")
    return rows

# ========== Endpoints ==========

@app.post("/register")
async def register_rows(
    rows: List[RegisterRow] = Body(..., description="Array of traveler objects"),
    record: bool = False,
    headless: Optional[bool] = None,
    pause: Optional[bool] = None,
):
    """
    JSON body-based registration. For CSV, use /register-csv.
    Use query params: ?record=1&headless=0&pause=1 for desktop/headful with CAPTCHA solving.
    """
    if pause is None:
        pause = default_pause(headless)

    results = []
    total = len(rows)
    for idx, row in enumerate(rows, 1):
        log(f"Register [{idx}/{total}] {row.passport} headless={headless} record={record} pause={pause}")
        video_dir = (VIDEOS_DIR / row.passport) if record else None

        # Create a fresh context/page (returns 3 values)
        context, page, artifacts = await open_context(
            headless=headless,
            record_video_dir=video_dir,
        )
        try:
            token = uuid.uuid4().hex[:8] if pause else None
            info = await register_one(page, row, gate_token=token, pause=pause)

            # Finalize artifacts: stop trace, close context, resolve video path
            artifacts = await _finalize_artifacts(context, page, artifacts, video_dir)

            results.append({
                "passport": row.passport,
                "gate_token": token,
                "paused": pause,
                "info": info,
                "video": str(artifacts.video_path) if artifacts.video_path else None,
                "trace": str(artifacts.trace_path) if artifacts.trace_path else None,
            })
        except Exception as e:
            # Try to finalize artifacts even on error
            try:
                await _finalize_artifacts(context, page, artifacts, video_dir)
            except Exception:
                pass
            results.append({"passport": row.passport, "error": str(e)})

    return {"ok": True, "count": len(results), "rows": results}

@app.post("/register-csv")
async def register_csv(
    file: UploadFile = File(...),
    record: bool = False,
    headless: Optional[bool] = None,
    pause: Optional[bool] = None,
):
    """CSV upload-based registration."""
    rows = await parse_csv_register(file)
    return await register_rows(rows=rows, record=record, headless=headless, pause=pause)

@app.post("/resume/{token}")
async def resume(token: str):
    """Resume a paused traveler after human solved CAPTCHA/OTP."""
    if not GATE.resume(token):
        raise HTTPException(status_code=404, detail="Unknown or already-resumed token")
    return {"ok": True, "resumed": token}

@app.post("/download")
async def download_rows(
    rows: List[PinRow] = Body(..., description="Array of {passport,nationality,pin} objects"),
    record: bool = False,
    headless: Optional[bool] = None,
):
    out = []
    total = len(rows)
    for idx, row in enumerate(rows, 1):
        log(f"Download [{idx}/{total}] {row.passport} headless={headless} record={record}")
        video_dir = (VIDEOS_DIR / f"{row.passport}_download") if record else None

        context, page, artifacts = await open_context(
            download_dir=DOWNLOAD_DIR,
            headless=headless,
            record_video_dir=video_dir,
        )
        try:
            pdf_path = await download_one(page, row, DOWNLOAD_DIR)

            artifacts = await _finalize_artifacts(context, page, artifacts, video_dir)

            out.append({
                "passport": row.passport,
                "saved": bool(pdf_path),
                "file": str(pdf_path) if pdf_path else None,
                "video": str(artifacts.video_path) if artifacts.video_path else None,
                "trace": str(artifacts.trace_path) if artifacts.trace_path else None,
            })
        except Exception as e:
            try:
                await _finalize_artifacts(context, page, artifacts, video_dir)
            except Exception:
                pass
            out.append({"passport": row.passport, "error": str(e)})

    return {"ok": True, "count": len(out), "rows": out}

@app.post("/download-csv")
async def download_csv(
    file: UploadFile = File(...),
    record: bool = False,
    headless: Optional[bool] = None,
):
    """CSV upload-based download (pins.csv)."""
    rows = await parse_csv_pins(file)
    return await download_rows(rows=rows, record=record, headless=headless)

@app.get("/health")
async def health():
    return {"ok": True}
