"""Web app: FastAPI + a single static page.

Boot sequence runs in a background thread so the container binds $PORT and
passes Railway's healthcheck immediately; the page shows a boot loader that
polls /api/status until warm-up finishes.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from promo_finder.llm import LLM
from promo_finder.parser import load_show
from promo_finder.pipeline import run as run_pipeline
from promo_finder.signals import annotate

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.getenv("DATA_DIR", ROOT / "data"))
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
RUNS_ROOT = Path(os.getenv("RUNS_DIR", ROOT / "runs"))
BUNDLED_RUNS = ROOT / "raw_outputs"
STATIC = Path(__file__).resolve().parent / "static"
DOWNLOADABLE = {"promos.json", "promos.csv", "report.md", "quality.json", "candidates_raw.json",
                "llm_responses.json", "dropped_duplicates.json"}

app = FastAPI(title="Promo Finder")

STATE: dict = {
    "boot": {"ready": False, "error": None, "started": time.time(), "steps": [
        {"key": "config", "label": "Loading model configuration", "status": "pending", "detail": ""},
        {"key": "datasets", "label": "Discovering screenplay datasets", "status": "pending", "detail": ""},
        {"key": "parse", "label": "Parsing screenplays", "status": "pending", "detail": ""},
        {"key": "signals", "label": "Computing hook / cliffhanger signals", "status": "pending", "detail": ""},
        {"key": "runs", "label": "Indexing previous runs", "status": "pending", "detail": ""},
    ]},
    "llm": {}, "datasets": {}, "runs": [],
    "job": {"running": False, "step": "", "frac": 0.0, "log": [], "run_id": None, "error": None},
}
LOCK = threading.Lock()


def _step(key, status, detail=""):
    for s in STATE["boot"]["steps"]:
        if s["key"] == key:
            s["status"], s["detail"] = status, detail


def _scan_datasets() -> dict:
    found = {}
    for root, origin in ((DATA_ROOT, "bundled"), (UPLOAD_ROOT, "uploaded")):
        if not root.exists():
            continue
        dirs = [root] + [d for d in sorted(root.iterdir()) if d.is_dir()]
        for d in dirs:
            n = len(list(d.glob("*.txt")))
            if n:
                found[d.name] = {"path": str(d), "files": n, "origin": origin}
    return found


def _scan_runs() -> list:
    runs = []
    for root, origin in ((RUNS_ROOT, "runs"), (BUNDLED_RUNS, "bundled")):
        if not root.exists():
            continue
        for d in root.iterdir():
            f = d / "promos.json"
            if f.exists():
                try:
                    meta = json.loads(f.read_text())["meta"]
                except Exception:
                    continue
                runs.append({"run_id": d.name, "path": str(d), "origin": origin, "show": meta.get("show"),
                             "mode": meta.get("mode"), "model": meta.get("model"),
                             "created": meta.get("created", "")})
    runs.sort(key=lambda r: r["created"], reverse=True)
    return runs


def boot():
    try:
        _step("config", "running")
        try:
            llm = LLM()
            STATE["llm"] = llm.describe()
            _step("config", "done", f"{STATE['llm']['provider']} · {llm.model} · credentials "
                                    f"{'OK' if llm.available else 'missing → rules-only mode'}")
        except Exception as e:  # bad provider config shouldn't kill the app: continue in rules-only mode
            STATE["llm"] = {"provider": "config error", "model": "-", "key_configured": False}
            _step("config", "done", f"LLM config error → rules-only mode: {str(e)[:160]}")

        _step("datasets", "running")
        STATE["datasets"] = _scan_datasets()
        _step("datasets", "done", f"{len(STATE['datasets'])} dataset(s)")

        _step("parse", "running")
        total_eps = total_blocks = 0
        parsed = {}
        for name, ds in STATE["datasets"].items():
            eps = load_show(ds["path"])
            parsed[name] = eps
            ds["episodes"] = len(eps)
            ds["minutes"] = round(sum(e.duration for e in eps) / 60, 1)
            total_eps += len(eps)
            total_blocks += sum(len(e.blocks) for e in eps)
        _step("parse", "done", f"{total_eps} episodes · {total_blocks} timestamped blocks")

        _step("signals", "running")
        n = sum(len(annotate(eps)) for eps in parsed.values())
        _step("signals", "done", f"{n} blocks scored")

        _step("runs", "running")
        STATE["runs"] = _scan_runs()
        _step("runs", "done", f"{len(STATE['runs'])} run(s) available")
        STATE["boot"]["ready"] = True
    except Exception as e:  # surface boot errors in the loader instead of crashing the container
        STATE["boot"]["error"] = f"{type(e).__name__}: {e}"
        for s in STATE["boot"]["steps"]:
            if s["status"] == "running":
                s["status"] = "error"


@app.on_event("startup")
def _startup():
    for d in (UPLOAD_ROOT, RUNS_ROOT):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        boot()
    else:
        threading.Thread(target=boot, daemon=True).start()


@app.get("/health")
def health():
    # always 200 so the platform healthcheck passes while warm-up continues
    return {"ok": True, "ready": STATE["boot"]["ready"]}


@app.get("/api/status")
def status():
    if not STATE["boot"]["ready"] and STATE["boot"]["error"] is None:
        boot()
    b = STATE["boot"]
    return {"boot": {**b, "uptime": round(time.time() - b["started"], 1)}, "llm": STATE["llm"],
            "datasets": STATE["datasets"], "runs": STATE["runs"], "job": STATE["job"]}


class RunReq(BaseModel):
    dataset: str
    mode: str = "auto"


def _job(ds: dict, name: str, mode: str):
    job = STATE["job"]

    def progress(step, frac, msg):
        job["step"], job["frac"] = step, frac
        job["log"].append({"t": time.strftime("%H:%M:%S"), "step": step, "msg": msg})
        job["log"] = job["log"][-200:]

    try:
        out = run_pipeline(ds["path"], RUNS_ROOT, mode, progress, show_name=name.replace("-", " ").title())
        job["run_id"] = out.name
        STATE["runs"] = _scan_runs()
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        progress("error", job["frac"], job["error"])
    finally:
        job["running"] = False


@app.post("/api/run")
def start_run(req: RunReq):
    if not STATE["boot"]["ready"]:
        raise HTTPException(503, "Still booting")
    ds = STATE["datasets"].get(req.dataset)
    if not ds:
        raise HTTPException(404, "Unknown dataset")
    if req.mode not in ("auto", "llm", "rules"):
        raise HTTPException(400, "mode must be auto|llm|rules")
    if req.mode == "llm" and not STATE["llm"].get("key_configured"):
        raise HTTPException(400, "No LLM credentials configured on the server; use rules mode")
    with LOCK:
        if STATE["job"]["running"]:
            raise HTTPException(409, "A run is already in progress")
        STATE["job"] = {"running": True, "step": "queued", "frac": 0.0, "log": [], "run_id": None,
                        "error": None, "dataset": req.dataset, "mode": req.mode, "started": time.time()}
    threading.Thread(target=_job, args=(ds, req.dataset, req.mode), daemon=True).start()
    return {"ok": True}


@app.post("/api/upload")
async def upload(name: str = Form(...), files: list[UploadFile] = File(...)):
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:60] or "show"
    dest = UPLOAD_ROOT / slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    saved = 0
    for f in files:
        data = await f.read()
        fname = Path(f.filename or "").name
        if fname.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    base = Path(info.filename).name  # flatten: never trust paths inside the zip
                    if base.lower().endswith(".txt") and not base.startswith(".") and info.file_size < 2_000_000:
                        (dest / base).write_bytes(z.read(info))
                        saved += 1
        elif fname.lower().endswith(".txt") and len(data) < 2_000_000:
            (dest / fname).write_bytes(data)
            saved += 1
    eps = load_show(dest) if saved else []
    if not eps:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(400, "No parseable screenplay .txt files found")
    STATE["datasets"] = _scan_datasets()
    STATE["datasets"][slug].update(episodes=len(eps), minutes=round(sum(e.duration for e in eps) / 60, 1))
    return {"dataset": slug, "episodes": len(eps)}


def _run_dir(run_id: str) -> Path:
    for r in STATE["runs"]:
        if r["run_id"] == run_id:
            return Path(r["path"])
    raise HTTPException(404, "Unknown run")


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    d = _run_dir(run_id)
    data = json.loads((d / "promos.json").read_text())
    data["quality"] = json.loads((d / "quality.json").read_text())
    return JSONResponse(data)


@app.get("/api/runs/{run_id}/file/{fname}")
def get_file(run_id: str, fname: str):
    if fname not in DOWNLOADABLE:
        raise HTTPException(404)
    p = _run_dir(run_id) / fname
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=f"{run_id}-{fname}")


@app.get("/")
def index():
    html_file = STATIC / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
