# server.py
import asyncio
import json
import os
import time
import ctypes
import sys
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LHM_DLL = os.path.join(BASE_DIR, "lib", "LibreHardwareMonitorLib.dll")
STATIC_DIR = os.path.join(BASE_DIR, "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")


# ---------------------------------------------------------------------------
# Admin check (warn, don't block)
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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

def sample_to_dict(s: UnifiedSample) -> Dict[str, Any]:
    return {
        "timestamp": s.timestamp,
        "cpu_pct": s.cpu_pct,
        "ram_pct": s.ram_pct,
        "disk_read_mb_s": s.disk_read_mb_s,
        "disk_write_mb_s": s.disk_write_mb_s,
        "cpu_load_lhm": s.cpu_load_lhm,
        "cpu_core_max": s.cpu_core_max,
        "cpu_temp": s.cpu_temp,
        "gpu_load": s.gpu_load,
        "gpu_temp": s.gpu_temp,
        "gpu_memory_used_mb": s.gpu_memory_used_mb,
        "memory_load_lhm": s.memory_load_lhm,
        "fan_rpm_max": s.fan_rpm_max,
        # we don't ship top_processes/spawned/exited on every timeline point,
        # they're large and the chart doesn't need them. served via /processes/at
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

    # Detector configured for hackathon-demo responsiveness:
    # short baseline window, low minimum samples, short cooldown
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
    det.start()

    # Stash on app state for endpoints to use
    app.state.proc = proc
    app.state.hw = hw
    app.state.agg = agg
    app.state.det = det
    app.state.started_at = time.time()

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
    If neither start/end nor window_sec is given, defaults to last 5 minutes.
    """
    agg = app.state.agg
    now = time.time()

    if start is None and end is None:
        win = window_sec if window_sec else 300.0  # default last 5 min
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