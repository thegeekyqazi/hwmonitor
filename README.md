# ProcessLens

> A dashcam for your operating system.

ProcessLens is a real-time anomaly forensics tool for Windows. It continuously records process and hardware activity, automatically detects unusual events using rolling statistical analysis, and identifies which processes were responsible — all served through a clean local web dashboard.

When something weird happens on your computer — a fan ramps up, RAM suddenly spikes, the system stutters — by the time you open Task Manager, the moment has passed. ProcessLens is the rewind button. It builds a continuous history of what your machine has been doing and surfaces anomalies the instant they occur, with attributed suspect processes and a scrubbable view of the moments around the event.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Demo Flow](#demo-flow)
- [Architecture](#architecture)
- [The Math Behind It](#the-math-behind-it)
- [File-by-File Breakdown](#file-by-file-breakdown)
- [Setup](#setup)
- [Running It](#running-it)
- [API Reference](#api-reference)
- [Known Limitations](#known-limitations)
- [Tech Stack](#tech-stack)

---

## What It Does

ProcessLens does four things continuously, in the background:

1. **Samples your system** every second — running processes, CPU usage, RAM, disk I/O, GPU load, hardware sensors
2. **Detects anomalies** in real time using a rolling 3-sigma threshold, with safeguards against false positives on idle systems
3. **Attributes suspects** by computing which processes' resource contributions actually grew during the anomaly window — not just which ones happened to be biggest
4. **Renders it all** in a live web dashboard with streaming charts, anomaly cards, and a process table

Everything runs locally. No telemetry, no cloud, no data leaves your machine.

---

## Demo Flow

1. Run `python server.py`. The app elevates via UAC, starts engines, and serves the dashboard at `http://127.0.0.1:8000`.
2. The dashboard immediately begins charting CPU, RAM, GPU, and CPU load, with a top-processes table refreshing every two seconds.
3. After about 30–60 seconds (long enough to build a baseline), the detector arms.
4. Trigger something — open many browser tabs, allocate a few GB of memory in Python, run a build. An anomaly fires within ~10 seconds:
   - A toast notification slides in
   - A new card appears in the right sidebar with the metric name, peak value, baseline, and ranked suspect processes
   - A red-shaded band marks the anomaly's time window on the chart
5. Click the card. The chart zooms to the anomaly. A modal opens showing peak vs. baseline vs. threshold, plus a ranked list of suspect processes with their delta contributions.

---

## Architecture

ProcessLens has a layered architecture. Each layer reads from the one below and provides a clean abstraction to the one above.

┌──────────────────────────────────────────────┐
│  Frontend (vanilla JS + Plotly)              │
│  index.html, app.js, styles.css              │
└──────────────────────────────────────────────┘
▲
│ HTTP + WebSocket
▼
┌──────────────────────────────────────────────┐
│  FastAPI Server (server.py)                  │
│  REST endpoints + live anomaly broadcasting  │
└──────────────────────────────────────────────┘
▲
│ reads from
▼
┌──────────────────────────────────────────────┐
│  Detector (detector.py)                      │
│  3σ anomaly detection + suspect attribution  │
└──────────────────────────────────────────────┘
▲
│ reads history()
▼
┌──────────────────────────────────────────────┐
│  MetricsAggregator (aggregator.py)           │
│  Unifies engine snapshots into a single      │
│  time-series with a 2-hour rolling buffer    │
└──────────────────────────────────────────────┘
▲
│ reads snapshots from
▼
┌──────────────────────────────────────────────┐
│  Engines                                     │
│  ProcessEngine, HardwareEngine               │
│  Each polls on its own background thread     │
└──────────────────────────────────────────────┘
▲
│ samples
▼
┌──────────────────────────────────────────────┐
│  Operating System                            │
│  psutil (processes) + LibreHardwareMonitor   │
│  via pythonnet (hardware sensors)            │
└──────────────────────────────────────────────┘
Each layer runs at its own pace:
- **ProcessEngine** ticks every 1s
- **HardwareEngine** ticks every 2s (sensor reads are slow)
- **MetricsAggregator** ticks every 1s, snapshotting both engines
- **Detector** ticks every 3s, scanning the aggregator's history for anomalies

All four engines run as daemon threads. The FastAPI server runs on the main thread and reads from the aggregator and detector when serving requests.

---

## The Math Behind It

### Rolling 3-sigma anomaly detection

For each metric (CPU%, RAM%, GPU load, etc.), the detector maintains a sliding window of recent samples and computes a statistical model of "normal" behavior.

The mean of the baseline window:

$$\mu = \frac{1}{n}\sum_{i=1}^{n} x_i$$

The standard deviation:

$$\sigma = \sqrt{\frac{1}{n-1}\sum_{i=1}^{n} (x_i - \mu)^2}$$

These two values describe what "normal" looks like *for this machine, right now*. Standard deviation captures the typical spread of the data — high σ means the metric is naturally volatile, low σ means it's tightly clustered.

The threshold for declaring a value anomalous:

$$T = \max(\mu + 3\sigma, \; \text{floor})$$

If the data is roughly normally distributed, only **0.27%** of values naturally fall above $\mu + 3\sigma$ — about 1 in 370 samples. That's the statistical justification for "3-sigma is unusual enough to alert on."

The `floor` is a hard minimum specific to each metric (25% for CPU, 60% for RAM, etc.). This prevents the detector from firing on mathematically-rare-but-practically-uninteresting events. If your machine has been at 1% CPU for five minutes, σ is tiny, and pure 3σ would fire on a jump to 1.5%. The floor says "don't bother me unless CPU is at least 25% in absolute terms."

The detector also requires multiple consecutive samples above threshold (three by default) before firing. Single-sample blips — interrupts, scheduler noise — are filtered out. Real anomalies last seconds; noise lasts one tick.

### Why "rolling" matters

The baseline window slides forward in time on every detector poll. This means the model of normal *adapts to whatever the machine is currently doing*. If you start a long compile, after a couple of minutes the baseline incorporates the elevated CPU, σ stabilizes, and the threshold rises to match. The detector won't keep alerting about an ongoing condition you've apparently accepted — but it *will* still fire if something even more unusual happens *on top of* the compile.

This is the core conceptual difference from a fixed-threshold alerting system. A fixed threshold answers "is the metric high?". A rolling-baseline detector answers "is the metric doing something different from what it's been doing recently?". The latter is almost always the more useful question.

### Suspect attribution: delta contribution

When an anomaly fires, the detector needs to identify *which* processes caused it. The naive answer — "show the top processes by CPU/RAM right now" — fails because it points at incumbents (Chrome is always huge in RAM) rather than change agents (the 50MB Python process that just grew to 1.1GB).

ProcessLens computes per-process **delta contribution** instead. For each process $p$ and the relevant resource metric $m$:

$$\Delta_p = \overline{m_{\text{during}}}(p) - \overline{m_{\text{before}}}(p)$$

In words: the average value of $m$ for $p$ during the anomaly window, minus the average for that same process in the 10 seconds immediately before. A positive delta means "this process's contribution grew." A large positive delta means "this process is likely responsible."

Sorted descending by delta, the top 5 are surfaced as suspects. Brand-new processes (which have no "before" average) get full credit for their during-average — newcomers are inherently high-value suspects.

### Temporal correlation suspects

Pure delta attribution misses one important case: processes that *trigger* an anomaly without *consuming* much themselves. A scheduler kicks off a backup; the backup eats RAM but the scheduler is tiny. Pure delta would correctly point at the backup, but miss the trigger.

Fix: any process that **spawns** within ±5 seconds of the anomaly onset is added to the suspect list with a `just_spawned` flag, regardless of its own resource usage. These are sorted to the bottom (delta=0) but visually marked, so the user sees both consumers and triggers.

---

## File-by-File Breakdown

### `engine.py` — The base class

`Engine` is an abstract polling thread. Subclasses implement `poll()` and the base class handles all the threading, timing, and history management.

Key properties:
- **Background daemon thread**: started via `start()`, stopped via `stop()`, doesn't block process exit
- **Monotonic-clock interval timing**: uses `time.monotonic()` so wall-clock changes don't break timing
- **Drift compensation**: `sleep(max(0, interval - elapsed))` — if a `poll()` call takes 0.3s, sleeps only 0.7s next, keeping the loop on a steady cadence
- **Bounded history deque**: `collections.deque(maxlen=N)` — every successful poll appends a snapshot; old entries automatically evicted
- **Thread-safe access**: `snapshot()` returns the latest poll result, `history(start, end)` returns a time-filtered slice, both protected by an `RLock`

This base class is the single most important piece of infrastructure in the project. Every other engine inherits from it.

### `process_engine.py` — Process & system metrics

A `ProcessEngine` polls `psutil` once per second and produces a `ProcessSnapshot` containing:
- System-wide CPU%, RAM%, disk read/write rates
- A list of top-N processes by CPU usage, with PID, name, user, CPU%, RAM in MB
- A list of newly-spawned processes (PID + name + cmdline + start time)
- A list of exited PIDs

Spawn/exit detection works by maintaining a `_known_pids: set` and diffing against the current PID set on each poll. New PIDs are spawns; missing PIDs are exits.

CPU% is normalized to a 0–100 scale by dividing each process's `cpu_percent()` by `psutil.cpu_count()`. Without this, multi-core processes can report >100% (one process pinned to 4 cores would report 400%), which breaks downstream comparisons against system metrics.

Disk I/O rates are computed from the deltas of psutil's cumulative byte counters — `(current - last) / elapsed_seconds`.

### `hardware_engine.py` — Hardware sensors

A `HardwareEngine` reads CPU/GPU/memory/motherboard sensors via the **LibreHardwareMonitorLib** .NET library, accessed through `pythonnet`. This gives access to data the OS doesn't normally expose:
- Per-core CPU loads
- CPU temperatures (where the chipset exposes them)
- GPU load, memory used, temperature
- RAM voltages, motherboard temps, fan RPMs (where supported)

The setup phase pre-loads required .NET dependencies (`System.Memory`, `System.Numerics.Vectors`, `System.Runtime.CompilerServices.Unsafe`) before initializing `Computer()`. Storage and Controller groups are deliberately disabled to avoid a known dependency conflict with newer LHM versions.

`HardwareSnapshot.sensors` is a nested dict: `{hardware_name: {sensor_name: (sensor_type, value)}}`. The aggregator extracts canonical fields from this structure.

### `aggregator.py` — Unified time-series

A `MetricsAggregator` is also an `Engine` subclass. Every second, it reads the latest `ProcessSnapshot` and `HardwareSnapshot` and produces a single `UnifiedSample` containing canonical fields:

- `timestamp`, `cpu_pct`, `ram_pct`, `disk_read_mb_s`, `disk_write_mb_s`
- `cpu_load_lhm`, `cpu_core_max`, `cpu_temp`, `cpu_power`
- `gpu_load`, `gpu_temp`, `gpu_memory_used_mb`
- `memory_load_lhm`, `fan_rpm_max`
- `top_processes`, `spawned`, `exited`
- `raw_sensors` (full LHM dict for the detail view)

Hardware sensor extraction uses a **scanning approach** rather than hardcoded paths — it walks the sensors dict and matches by hardware-name keywords (`"ryzen"`, `"radeon"`, `"memory"`) and sensor type (`"Load"`, `"Temperature"`, etc.). This makes the aggregator portable across different CPU/GPU vendors.

Fields that aren't available on a given machine are set to `None` — never zeros — so downstream code can distinguish "sensor unavailable" from "sensor reads zero."

History size is 7,200 samples — exactly two hours at 1Hz. This is the canonical buffer everything downstream reads from.

### `detector.py` — Anomaly detection + suspect attribution

The `Detector` is also an `Engine` subclass, ticking every 3 seconds. On each tick:

1. Pulls baseline samples (last 2 minutes minus the recent 8 seconds) and recent samples (last 8 seconds) from the aggregator
2. For each tracked metric, computes mean and stddev of the baseline, derives threshold
3. Counts how many recent samples exceed threshold
4. If at least 3 do and we're not already tracking this metric, fires an anomaly
5. If fewer do and we *were* tracking this metric, closes the anomaly with `ended_at = now`

Each metric is tracked independently and has its own cooldown timer (default 60s) preventing rapid re-firing.

When firing, the detector calls `_attribute()` to compute suspects:
- Pulls per-process samples in the 10-second `before` window and the entire `during` window
- Averages each PID's resource usage in each window
- Computes delta = during_avg - before_avg
- Discards negative deltas (processes that *shrank* didn't cause the anomaly)
- Adds temporal-correlation suspects (any process spawned within ±5s of onset)
- Sorts by delta descending, returns top 5

PIDs 0 (System Idle) and 4 (System) are filtered out — they're kernel-level pseudo-processes, not actionable suspects.

The `on_event` callback fires on both `started` and `ended` events. The FastAPI server registers this callback to broadcast over WebSocket.

### `server.py` — FastAPI server

Boots all four engines via FastAPI's `lifespan` context manager. Engines stop cleanly on shutdown.

Auto-elevates to admin via UAC if not already running elevated. Some hardware sensors and `psutil.net_connections()` require admin on Windows.

Endpoints:
- `GET /` — serves the frontend
- `GET /api/health` — uptime, sample count, anomaly count, available metrics on this hardware
- `GET /api/timeline?window_sec=300` — time-series of UnifiedSamples, downsampled to ~500 points
- `GET /api/anomalies` — list of all anomalies, newest first
- `GET /api/anomalies/{id}` — single anomaly detail
- `GET /api/processes/now` — current top processes
- `GET /api/processes/at?ts=...` — top processes nearest to a given timestamp
- `WS /ws/live` — push-based event stream for new anomalies

The WebSocket flow handles a tricky cross-thread coordination problem. The detector runs on a background thread; the WebSocket sends are async coroutines that need to run on the main event loop. The `WSManager` captures the running loop on startup and uses `asyncio.run_coroutine_threadsafe` to schedule sends from the detector's thread.

Server binds to `127.0.0.1` only — never exposed to the network.

### `monitor.py` — CLI debugging tool

A standalone CLI version that wires the engines together and prints unified state to the terminal every two seconds. Used during development to verify the engine pipeline works without involving the web server. Not needed for normal use, but kept in the repo as a diagnostic tool — if the dashboard misbehaves, run `monitor.py` to confirm the data layer is healthy.

### `static/index.html` — Frontend layout

A single-page dashboard with four regions:

1. **Header**: brand, WebSocket status indicator (green dot when connected), uptime, sample count, anomaly count
2. **Chart panel**: Plotly multi-line chart of CPU%, RAM%, CPU Load, Hottest Core, GPU% on a shared time axis. Window pills (1m / 5m / 15m / 1h) control the visible range. Anomalies appear as red-shaded vertical bands.
3. **Anomaly sidebar**: list of detected anomalies, newest first. Each card shows metric, peak vs. baseline, top 3 suspects.
4. **Top processes table**: live list of current top-N processes with CPU% bars and RAM usage.

A toast notification system slides in alerts when new anomalies arrive via WebSocket. A modal opens with a detail view when an anomaly card is clicked.

### `static/app.js` — Frontend logic

Vanilla JavaScript, no build step.

Responsibilities:
- On load: fetch `/api/health` to learn which metrics are available on this hardware, then init the chart with only those traces
- Polling: refresh chart and processes table every 2s, refresh status bar every 3s
- WebSocket: connect to `/ws/live`, handle `started`/`ended` events, auto-reconnect on disconnect
- Window control: clicking 1m/5m/15m/1h re-fetches and re-renders the chart with the new window
- Click handling: anomaly cards zoom the chart and open the detail modal

The chart uses Plotly's streaming pattern: `Plotly.restyle` for trace data updates and `Plotly.relayout` for axis range and shape updates. The X-axis range is pinned explicitly to `[now - window, now]` on each refresh so the chart scrolls forward in real time rather than drifting.

### `static/styles.css` — Visual design

Light theme tuned for readability in bright demo environments. CSS variables for the color palette, no preprocessor required. Main layout uses CSS grid; cards and panels use a soft shadow + 1px border for an enterprise-tool feel.

### `lib/`

Bundled .NET assemblies required by `LibreHardwareMonitorLib.dll`:
- `LibreHardwareMonitorLib.dll` — main library
- `HidSharp.dll` — USB device support
- `System.Memory.dll`, `System.Numerics.Vectors.dll`, `System.Runtime.CompilerServices.Unsafe.dll` — .NET Standard polyfills

These DLLs ship with the repo. On first checkout, run `Get-ChildItem -Path lib -Recurse | Unblock-File` in PowerShell to remove the "downloaded from internet" zone identifier — Windows blocks .NET assemblies from loading otherwise.

---

## Setup

### Prerequisites

- **Windows 10 or 11**
- **Python 3.10+**
- **Administrator privileges** (the app self-elevates via UAC, but you'll need an admin account)

### Installation

```powershell
# Clone the repo
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>

# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Unblock the bundled .NET assemblies
Get-ChildItem -Path lib -Recurse | Unblock-File
```

### Dependencies

- `fastapi` — web framework
- `uvicorn[standard]` — ASGI server
- `psutil` — process and system metrics
- `pythonnet` — Python ↔ .NET interop for LibreHardwareMonitor

A `requirements.txt` is included.

---

## Running It

```powershell
python server.py
```

The first launch triggers a UAC prompt for admin elevation. After accepting, a new console window opens with the elevated process. Open `http://127.0.0.1:8000` in any browser.

The dashboard starts immediately, but the detector requires ~30–60 seconds to build a baseline before it can fire anomalies. Status bar shows live sample count and uptime.

To trigger a test anomaly:

```powershell
python -c "x = bytearray(2_000_000_000); input('press enter to release')"
```

Within ~10 seconds, a RAM anomaly should fire. Press Enter in the python window to release the memory.

---

## API Reference

All endpoints are JSON unless otherwise noted. CORS is unrestricted on `127.0.0.1`.

### `GET /api/health`

```json
{
  "ok": true,
  "is_admin": true,
  "uptime_sec": 142.3,
  "history_samples": 142,
  "anomaly_count": 3,
  "active_anomalies": ["ram_pct"],
  "latest_sample_ts": 1714409123.45,
  "available_metrics": {
    "cpu_pct": true,
    "ram_pct": true,
    "cpu_load_lhm": true,
    "cpu_temp": false,
    "fan_rpm_max": false
  }
}
```

### `GET /api/timeline?window_sec=300`

Returns up to ~500 unified samples within the requested window.

```json
{
  "start": 1714408823.0,
  "end": 1714409123.0,
  "count": 300,
  "samples": [
    {
      "timestamp": 1714408823.0,
      "cpu_pct": 4.2,
      "ram_pct": 51.3,
      "cpu_load_lhm": 3.1,
      "cpu_core_max": 12.5,
      "gpu_load": 1.0
    }
  ]
}
```

### `GET /api/anomalies`

```json
{
  "count": 3,
  "anomalies": [
    {
      "id": "ram_pct-1714409100",
      "metric": "ram_pct",
      "label": "RAM %",
      "unit": "%",
      "started_at": 1714409100.0,
      "ended_at": null,
      "baseline": 51.0,
      "threshold": 60.0,
      "peak_value": 70.5,
      "suspects": [
        {"pid": 12345, "name": "python.exe", "delta": 1097.5, "metric": "mem_mb"}
      ]
    }
  ]
}
```

### `WebSocket /ws/live`

Push events sent by the server:

```json
{"event": "connected", "ts": 1714409100.0}
{"event": "started", "anomaly": { ... }}
{"event": "ended", "anomaly": { ... }}
```

---

## Known Limitations

- **Windows-only.** LibreHardwareMonitor is a Windows-targeted .NET library. macOS/Linux ports would need a different sensor source.
- **Some hardware exposes fewer sensors than others.** AMD Ryzen Mobile chips, for example, often don't expose CPU temperature or fan RPM via LHM. The dashboard gracefully omits unavailable metrics.
- **No persistence.** All data is in-memory; restarting the server clears history.
- **Process attribution requires the process to appear in top-N samples.** Very-short-lived processes (under 1 second) may be missed by the spawn detector. ETW would catch them but is out of scope.
- **No per-process disk or network attribution.** Suspect ranking for disk I/O and GPU anomalies falls back to CPU% as a proxy. Direct attribution would require ETW (Windows) or eBPF (Linux).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, FastAPI, uvicorn |
| System metrics | psutil |
| Hardware sensors | LibreHardwareMonitorLib via pythonnet |
| Frontend | Vanilla JavaScript, Plotly.js |
| Real-time push | WebSocket |
| Storage | In-memory `collections.deque` (no database) |

---

## Project Structure
processlens/
├── engine.py             # Polling thread base class
├── process_engine.py     # Process / system metrics via psutil
├── hardware_engine.py    # Hardware sensors via LibreHardwareMonitor
├── aggregator.py         # Unifies engine snapshots into time-series
├── detector.py           # 3σ anomaly detection + suspect attribution
├── server.py             # FastAPI server + WebSocket + UAC elevation
├── monitor.py            # CLI debugging tool (dev use)
├── static/
│   ├── index.html        # Dashboard layout
│   ├── app.js            # Frontend logic
│   └── styles.css        # Visual design
├── lib/
│   ├── LibreHardwareMonitorLib.dll
│   ├── HidSharp.dll
│   ├── System.Memory.dll
│   ├── System.Numerics.Vectors.dll
│   └── System.Runtime.CompilerServices.Unsafe.dll
├── requirements.txt
└── README.md

