# server.py
import asyncio
import json
import os
import time
import ctypes
import sys
import math
from datetime import datetime
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from process_engine import ProcessEngine
from hardware_engine import HardwareEngine
from aggregator import MetricsAggregator, UnifiedSample
from detector import Detector, Anomaly

from system_info import collect_system_info, collect_hardware_inventory
from pattern_engine import compute_insights

import psutil
from pydantic import BaseModel

from fastapi.responses import JSONResponse, PlainTextResponse, Response
import json as json_module

from diagnostics import build_diagnostic, render_markdown

from settings import load_settings, save_settings, settings_status
from llm_diagnosis import diagnose

from storage import Storage
from detector import Anomaly, Suspect

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LHM_DLL = os.path.join(BASE_DIR, "lib", "LibreHardwareMonitorLib.dll")
STATIC_DIR = os.path.join(BASE_DIR, "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")



# ---------------------------------------------------------------------------
# Admin elevation — auto-relaunch via UAC if not already elevated
# ---------------------------------------------------------------------------

def _ensure_admin():
    """If we're not running as admin, relaunch the same script elevated and exit."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return  # already admin, all good
    except Exception:
        return  # not on Windows or unable to check; just proceed

    # Re-run the script with the "runas" verb (triggers UAC).
    # We pass the same arguments so any future flags are preserved.
    params = " ".join(f'"{a}"' for a in sys.argv)
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,           # parent hwnd
            "runas",        # verb — this is what triggers UAC
            sys.executable, # the python.exe to launch
            params,         # arguments (path to this script + any args)
            None,           # working directory
            1               # SW_SHOWNORMAL
        )
        if ret <= 32:  # ShellExecuteW returns >32 on success
            print("[server] UAC elevation was cancelled or failed. Exiting.")
            sys.exit(1)
    except Exception as e:
        print(f"[server] Failed to elevate: {e}")
        sys.exit(1)

    # Original (non-admin) process exits — the elevated one takes over
    sys.exit(0)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_ensure_admin()

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class WSManager:
    """Tracks connected WebSocket clients and broadcasts JSON messages."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    async def register(self, ws: WebSocket):
        self.clients.add(ws)

    def unregister(self, ws: WebSocket):
        self.clients.discard(ws)

    def broadcast(self, payload: Dict[str, Any]):
        """
        Called from background (non-async) threads — the detector.
        We schedule sends on the captured event loop using
        run_coroutine_threadsafe.
        """
        if not self.clients or self.loop is None:
            return
        msg = json.dumps(payload, default=str)
        for ws in list(self.clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), self.loop)
            except Exception:
                # Client disconnected mid-send; quiet drop
                self.unregister(ws)


ws_manager = WSManager()


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _safe_float(x):
    """Replace NaN/inf with None so JSON serialization doesn't blow up."""
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def sample_to_dict(s: UnifiedSample) -> Dict[str, Any]:
    return {
        "timestamp": _safe_float(s.timestamp),
        "cpu_pct": _safe_float(s.cpu_pct),
        "ram_pct": _safe_float(s.ram_pct),
        "disk_read_mb_s": _safe_float(s.disk_read_mb_s),
        "disk_write_mb_s": _safe_float(s.disk_write_mb_s),
        "cpu_load_lhm": _safe_float(s.cpu_load_lhm),
        "cpu_core_max": _safe_float(s.cpu_core_max),
        "cpu_temp": _safe_float(s.cpu_temp),
        "gpu_load": _safe_float(s.gpu_load),
        "gpu_temp": _safe_float(s.gpu_temp),
        "gpu_memory_used_mb": _safe_float(s.gpu_memory_used_mb),
        "memory_load_lhm": _safe_float(s.memory_load_lhm),
        "fan_rpm_max": _safe_float(s.fan_rpm_max),
    }


def anomaly_to_dict(a: Anomaly) -> Dict[str, Any]:
    return {
        "id": a.id,
        "metric": a.metric,
        "label": a.label,
        "unit": a.unit,
        "started_at": a.started_at,
        "ended_at": a.ended_at,
        "baseline": a.baseline,
        "threshold": a.threshold,
        "peak_value": a.peak_value,
        "suspects": [asdict(s) for s in a.suspects],
    }


def downsample(samples: List[UnifiedSample], target: int = 500) -> List[UnifiedSample]:
    """Reduce a long sample list to ~target points by stride sampling."""
    if len(samples) <= target:
        return samples
    step = max(1, len(samples) // target)
    return samples[::step]


# ---------------------------------------------------------------------------
# Lifespan: start engines on app boot, stop on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not is_admin():
        print("[server] WARNING: not running as admin — some hardware sensors may be unavailable.")
    print("[server] Starting engines...")

    proc = ProcessEngine(interval=1.0, top_n=20)
    hw = HardwareEngine(lhm_dll_path=LHM_DLL, interval=2.0)
    agg = MetricsAggregator(proc, hw, interval=1.0)

    proc.start()
    hw.start()
    # Give the engines a moment so the aggregator has data to read on first tick
    await asyncio.sleep(1.5)
    agg.start()

    # Cache static system info — collected once at startup
    print("[server] Collecting system info & hardware inventory...")
    app.state.system_info = collect_system_info(hw)
    app.state.hardware_inventory = collect_hardware_inventory(hw)

    # Detector configured for hackathon-demo responsiveness
    det = Detector(
        agg,
        interval=3.0,
        baseline_window_sec=120.0,
        recent_window_sec=8.0,
        min_baseline_samples=20,
        sigma=3.0,
        consecutive_required=3,
        cooldown_sec=30.0,
    )

    # Capture the running event loop so the detector (background thread)
    # can schedule WebSocket sends on it.
    ws_manager.attach_loop(asyncio.get_running_loop())

    def on_anomaly_event(event_type: str, anomaly: Anomaly):
        ws_manager.broadcast({
            "event": event_type,  # "started" | "ended"
            "anomaly": anomaly_to_dict(anomaly),
        })
    det.on_event = on_anomaly_event

    # Persistent storage
    print("[server] Initializing storage...")
    app.state.storage = Storage()

    # Hydrate anomalies from disk into the detector's deque (before starting it)
    hydrated = 0
    for row in app.state.storage.load_recent_anomalies(hours=24):
        suspects = [Suspect(**s) for s in row["suspects"]]
        anomaly = Anomaly(
            id=row["id"], metric=row["metric"], label=row["label"],
            unit=row["unit"], started_at=row["started_at"], ended_at=row["ended_at"],
            baseline=row["baseline"], threshold=row["threshold"],
            peak_value=row["peak_value"], suspects=suspects,
        )
        det._anomalies.append(anomaly)
        hydrated += 1
    print(f"[server] Hydrated {hydrated} anomalies from previous sessions.")

    # Wrap the websocket callback with a persistence layer
    storage_ref = app.state.storage
    original_on_event = det.on_event
    def persistent_on_event(event_type: str, anomaly: Anomaly):
        try:
            storage_ref.upsert_anomaly(anomaly)
        except Exception as e:
            print(f"[storage] Failed to persist anomaly: {e}")
        if original_on_event:
            original_on_event(event_type, anomaly)
    det.on_event = persistent_on_event

    # Now start the detector — after storage hydration and on_event wrapping
    det.start()

    # Stash on app state for endpoints
    app.state.proc = proc
    app.state.hw = hw
    app.state.agg = agg
    app.state.det = det
    app.state.started_at = time.time()

    # Background task: evict old data every hour
    async def eviction_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                storage_ref.evict_old()
            except Exception as e:
                print(f"[storage] eviction failed: {e}")

    asyncio.create_task(eviction_loop())

    print("[server] Engines started. Listening on http://127.0.0.1:8000")
    try:
        yield
    finally:
        print("[server] Shutting down engines...")
        try: det.stop()
        except Exception: pass
        try: agg.stop()
        except Exception: pass
        try: hw.stop()
        except Exception: pass
        try: proc.stop()
        except Exception: pass
        try: app.state.storage.close()
        except Exception: pass
        print("[server] Goodbye.")
# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ProcessLens", lifespan=lifespan)


# ---- HTML ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    if os.path.exists(INDEX_HTML):
        return FileResponse(INDEX_HTML)
    # Helpful fallback if the frontend isn't built yet
    return HTMLResponse(
        "<h1>ProcessLens backend running</h1>"
        "<p>Frontend not yet present. Try <a href='/api/anomalies'>/api/anomalies</a> "
        "or <a href='/api/health'>/api/health</a>.</p>"
    )


# ---- API -------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Quick status check — useful from the frontend on load."""
    agg = app.state.agg
    det = app.state.det
    sample = agg.snapshot()
    return {
        "ok": True,
        "is_admin": is_admin(),
        "uptime_sec": time.time() - app.state.started_at,
        "history_samples": len(agg.history()),
        "anomaly_count": len(det.anomalies()),
        "active_anomalies": [a.metric for a in det.active_anomalies()],
        "latest_sample_ts": sample.timestamp if sample else None,
        "available_metrics": _available_metrics(sample),
        "storage": app.state.storage.stats(),
    }


def _available_metrics(sample: Optional[UnifiedSample]) -> Dict[str, bool]:
    """Tell the frontend which metrics actually have data on this hardware."""
    if sample is None:
        return {}
    return {
        "cpu_pct": True,
        "ram_pct": True,
        "disk_read_mb_s": True,
        "disk_write_mb_s": True,
        "cpu_load_lhm": sample.cpu_load_lhm is not None,
        "cpu_core_max": sample.cpu_core_max is not None,
        "cpu_temp": sample.cpu_temp is not None,
        "gpu_load": sample.gpu_load is not None,
        "gpu_temp": sample.gpu_temp is not None,
        "memory_load_lhm": sample.memory_load_lhm is not None,
        "fan_rpm_max": sample.fan_rpm_max is not None,
    }




@app.get("/api/timeline")
def timeline(start: Optional[float] = None, end: Optional[float] = None,
             window_sec: Optional[float] = None):
    """
    Return downsampled samples for a time range.
    Defaults to last 5 minutes if no params given.
    """
    # Defensive: reject NaN/inf which break JSON serialization downstream
    for name, val in [('start', start), ('end', end), ('window_sec', window_sec)]:
        if val is not None and (math.isnan(val) or math.isinf(val)):
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number")

    agg = app.state.agg
    now = time.time()

    if start is None and end is None:
        win = window_sec if window_sec else 300.0
        start, end = now - win, now
    elif start is None:
        start = (end or now) - 300.0
    elif end is None:
        end = now

    samples = agg.history(start, end)
    samples = downsample(samples, target=500)
    return {
        "start": start,
        "end": end,
        "count": len(samples),
        "samples": [sample_to_dict(s) for s in samples],
    }

@app.get("/api/anomalies")
def anomalies(limit: int = 100):
    det = app.state.det
    items = det.anomalies(limit=limit)
    return {
        "count": len(items),
        "anomalies": [anomaly_to_dict(a) for a in items],
    }


@app.get("/api/anomalies/{anomaly_id}")
def anomaly_detail(anomaly_id: str):
    det = app.state.det
    for a in det.anomalies():
        if a.id == anomaly_id:
            return anomaly_to_dict(a)
    raise HTTPException(status_code=404, detail="Anomaly not found")


@app.get("/api/processes/at")
def processes_at(ts: float):
    """
    Returns the top processes that were running closest to the given timestamp.
    Uses nearest-sample lookup rather than exact match.
    """
    agg = app.state.agg
    history = agg.history()
    if not history:
        return {"timestamp": ts, "processes": []}

    nearest = min(history, key=lambda s: abs(s.timestamp - ts))
    return {
        "timestamp": nearest.timestamp,
        "requested_ts": ts,
        "processes": nearest.top_processes,
        "spawned": nearest.spawned,
        "exited": nearest.exited,
    }


@app.get("/api/processes/now")
def processes_now():
    """Latest top processes — for the live table at the bottom of the dashboard."""
    agg = app.state.agg
    s = agg.snapshot()
    if s is None:
        return {"timestamp": None, "processes": []}
    return {
        "timestamp": s.timestamp,
        "processes": s.top_processes,
    }

@app.get("/api/system_info")
def system_info():
    return app.state.system_info


@app.get("/api/hardware_inventory")
def hardware_inventory():
    return app.state.hardware_inventory


@app.get("/api/insights")
def insights(hours: Optional[float] = None):
    """Pattern-engine insights over the anomaly history."""
    det = app.state.det
    return compute_insights(det.anomalies(), hours_window=hours)


# ---- Process control ------------------------------------------------------

# Kernel pseudo-processes that should never be killable
PROTECTED_PIDS = {0, 4}

# Critical Windows processes — killing these BSODs the machine. We block by name.
PROTECTED_NAMES = {
    "system", "system idle process", "registry", "memory compression",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "dwm.exe", "fontdrvhost.exe", "secure system",
}


class KillRequest(BaseModel):
    force: bool = False  # if True, use kill() instead of terminate()


@app.post("/api/processes/{pid}/kill")
def kill_process(pid: int, body: KillRequest = None):
    """Terminate a process by PID. Refuses critical system processes."""
    if pid in PROTECTED_PIDS:
        raise HTTPException(status_code=403, detail="Cannot terminate kernel processes")

    try:
        proc = psutil.Process(pid)
        name = proc.name().lower()
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail=f"PID {pid} no longer exists")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied")

    if name in PROTECTED_NAMES:
        raise HTTPException(
            status_code=403,
            detail=f"'{name}' is a protected system process and cannot be terminated"
        )

    force = body.force if body else False
    try:
        if force:
            proc.kill()
            method = "kill"
        else:
            proc.terminate()
            method = "terminate"

        # Wait briefly for the process to actually exit
        try:
            proc.wait(timeout=2)
            still_running = False
        except psutil.TimeoutExpired:
            still_running = proc.is_running()

        return {
            "ok": True,
            "pid": pid,
            "name": name,
            "method": method,
            "still_running": still_running,
        }
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied — try running as admin")
    except psutil.NoSuchProcess:
        return {"ok": True, "pid": pid, "name": name, "method": method, "still_running": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kill failed: {e}")

def _build_current_diagnostic():
    """Helper: pull all the live data and build the diagnostic dict."""
    agg = app.state.agg
    det = app.state.det

    # Recent samples — last hour or whatever's available
    now = time.time()
    recent_samples = agg.history(now - 3600, now)

    return build_diagnostic(
        system_info=app.state.system_info,
        hardware_inventory=app.state.hardware_inventory,
        anomalies=det.anomalies(),
        recent_samples=recent_samples,
        insights=compute_insights(det.anomalies()),
        active_anomaly_metrics=[a.metric for a in det.active_anomalies()],
        server_uptime_sec=now - app.state.started_at,
    )


@app.get("/api/diagnostic.json")
def diagnostic_json():
    """Full diagnostic as JSON — for programmatic consumption / LLM API."""
    return _build_current_diagnostic()


@app.get("/api/diagnostic.md")
def diagnostic_md():
    """Full diagnostic as Markdown — paste into ChatGPT/Claude/Gemini."""
    diagnostic = _build_current_diagnostic()
    md = render_markdown(diagnostic)
    return PlainTextResponse(content=md, media_type="text/markdown; charset=utf-8")


@app.get("/api/diagnostic.md/download")
def diagnostic_md_download():
    """Same as /diagnostic.md but with attachment header for browser download."""
    diagnostic = _build_current_diagnostic()
    md = render_markdown(diagnostic)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="processlens-diagnostic-{timestamp}.md"'}
    )

class SettingsUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    preferred_provider: Optional[str] = None

@app.get("/api/settings/status")
def get_settings_status():
    """Returns which LLM providers are configured (without exposing keys)."""
    return settings_status()


@app.post("/api/settings")
def update_settings(body: SettingsUpdate):
    current = load_settings()

    if body.anthropic_api_key is not None:
        if body.anthropic_api_key == "":
            current.pop("anthropic_api_key", None)
        else:
            current["anthropic_api_key"] = body.anthropic_api_key

    if body.openai_api_key is not None:
        if body.openai_api_key == "":
            current.pop("openai_api_key", None)
        else:
            current["openai_api_key"] = body.openai_api_key

    if body.gemini_api_key is not None:
        if body.gemini_api_key == "":
            current.pop("gemini_api_key", None)
        else:
            current["gemini_api_key"] = body.gemini_api_key

    if body.preferred_provider is not None:
        if body.preferred_provider not in ("claude", "openai", "gemini"):
            raise HTTPException(status_code=400, detail="Provider must be 'claude', 'openai', or 'gemini'")
        current["preferred_provider"] = body.preferred_provider

    save_settings(current)
    return settings_status()

class DiagnoseRequest(BaseModel):
    provider: Optional[str] = None  # if None, use preferred from settings
    model: Optional[str] = None


@app.post("/api/diagnose")
def diagnose_with_llm(body: DiagnoseRequest):
    settings = load_settings()
    provider = body.provider or settings.get("preferred_provider", "claude")

    if provider == "claude":
        api_key = settings.get("anthropic_api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="Anthropic API key not configured. Set it in Settings.")
    elif provider == "openai":
        api_key = settings.get("openai_api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="OpenAI API key not configured. Set it in Settings.")
    elif provider == "gemini":
        api_key = settings.get("gemini_api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="Gemini API key not configured. Set it in Settings.")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    diagnostic = _build_current_diagnostic()
    from diagnostics import render_markdown
    md_report = render_markdown(diagnostic)

    try:
        result = diagnose(md_report, provider=provider, api_key=api_key, model=body.model)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM call failed: {str(e)}")

    return result
# ---- WebSocket -------------------------------------------------------------

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    await ws_manager.register(websocket)
    try:
        # send a hello so the client knows the connection is live
        await websocket.send_text(json.dumps({
            "event": "connected",
            "ts": time.time(),
        }))
        # passive loop — we mostly broadcast; client pings to keep alive
        while True:
            # If client sends anything, just echo a pong
            msg = await websocket.receive_text()
            if msg:
                await websocket.send_text(json.dumps({"event": "pong", "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws] error: {e}")
    finally:
        ws_manager.unregister(websocket)


# ---- Static files ----------------------------------------------------------

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")