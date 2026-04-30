# diagnostics.py
"""
Generates diagnostic reports — JSON and Markdown — bundling system info,
hardware inventory, anomaly history, pattern insights, and recent metrics
into a single artifact ready for human review or LLM analysis.
"""
import time
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import asdict


def build_diagnostic(
    system_info: Dict[str, Any],
    hardware_inventory: Dict[str, Any],
    anomalies: List,
    recent_samples: List,
    insights: Dict[str, Any],
    active_anomaly_metrics: List[str],
    server_uptime_sec: float,
) -> Dict[str, Any]:
    """Build the canonical diagnostic dict that both JSON and Markdown use."""
    now = time.time()
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_at_unix": now,
            "tool": "ProcessLens",
            "tool_version": "1.0",
            "server_uptime_sec": server_uptime_sec,
        },
        "system": system_info,
        "hardware": hardware_inventory,
        "metrics_summary": _summarize_metrics(recent_samples),
        "anomalies": {
            "total": len(anomalies),
            "active_now": len(active_anomaly_metrics),
            "active_metrics": active_anomaly_metrics,
            "list": [_anomaly_to_dict(a) for a in anomalies],
        },
        "insights": insights,
        "recent_top_processes": _recent_top_processes(recent_samples),
    }


def _anomaly_to_dict(a) -> Dict[str, Any]:
    return {
        "id": a.id,
        "metric": a.metric,
        "label": a.label,
        "unit": a.unit,
        "started_at": a.started_at,
        "ended_at": a.ended_at,
        "duration_sec": (a.ended_at - a.started_at) if a.ended_at else None,
        "baseline": round(a.baseline, 2),
        "threshold": round(a.threshold, 2),
        "peak_value": round(a.peak_value, 2),
        "severity_ratio": round((a.peak_value - a.baseline) / max(a.baseline, 1e-6), 2),
        "suspects": [
            {
                "pid": s.pid,
                "name": s.name,
                "delta": round(s.delta, 2),
                "metric": s.metric,
                "flag": s.flag,
            } for s in a.suspects
        ],
    }


def _summarize_metrics(samples: List) -> Dict[str, Any]:
    """Compute min/avg/max/p95 for each metric from the sample list."""
    if not samples:
        return {}

    metrics = ['cpu_pct', 'ram_pct', 'cpu_load_lhm', 'cpu_core_max',
               'gpu_load', 'cpu_temp', 'gpu_temp', 'disk_read_mb_s',
               'disk_write_mb_s', 'fan_rpm_max']

    summary = {}
    for m in metrics:
        values = [getattr(s, m) for s in samples if getattr(s, m, None) is not None]
        if not values:
            continue
        sorted_vals = sorted(values)
        p95_idx = int(0.95 * (len(sorted_vals) - 1))
        summary[m] = {
            "samples": len(values),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(statistics.mean(values), 2),
            "stddev": round(statistics.stdev(values), 2) if len(values) > 1 else 0,
            "p95": round(sorted_vals[p95_idx], 2),
        }
    return summary


def _recent_top_processes(samples: List, top_n: int = 10) -> List[Dict[str, Any]]:
    """Find processes that consistently appeared in the top-N during the window."""
    if not samples:
        return []
    by_pid_cpu = {}
    by_pid_ram = {}
    name_for_pid = {}
    appearances = {}

    for s in samples:
        for p in s.top_processes:
            pid = p['pid']
            by_pid_cpu[pid] = by_pid_cpu.get(pid, 0) + p.get('cpu', 0)
            by_pid_ram[pid] = by_pid_ram.get(pid, 0) + p.get('mem_mb', 0)
            appearances[pid] = appearances.get(pid, 0) + 1
            name_for_pid[pid] = p['name']

    rows = []
    for pid in by_pid_cpu:
        n = appearances[pid]
        rows.append({
            "pid": pid,
            "name": name_for_pid.get(pid, "unknown"),
            "avg_cpu": round(by_pid_cpu[pid] / n, 2),
            "avg_ram_mb": round(by_pid_ram[pid] / n, 1),
            "appearances": n,
        })
    rows.sort(key=lambda r: r['avg_cpu'], reverse=True)
    return rows[:top_n]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(diagnostic: Dict[str, Any]) -> str:
    """Render the diagnostic dict as a markdown report optimized for LLMs."""
    out = []
    meta = diagnostic["meta"]
    sys = diagnostic["system"]
    hw = diagnostic["hardware"]

    out.append(f"# ProcessLens Diagnostic Report")
    out.append("")
    out.append(f"**Generated:** {meta['generated_at']}  ")
    out.append(f"**Monitoring duration:** {_fmt_duration(meta['server_uptime_sec'])}")
    out.append("")

    out.append("## System Summary")
    out.append("")
    out.append(f"- **Hostname:** {sys['hostname']}")
    out.append(f"- **OS:** {sys['os']['system']} {sys['os']['release']} ({sys['os']['version']})")
    out.append(f"- **CPU:** {sys['cpu']['model']} — {sys['cpu']['physical_cores']} cores / {sys['cpu']['logical_cores']} threads")
    out.append(f"- **RAM:** {sys['memory']['total_gb']} GB total ({sys['memory']['available_gb']} GB available at report time)")
    out.append("")

    # Hardware
    out.append("## Hardware Inventory")
    out.append("")

    if hw.get('memory_modules'):
        out.append("**Memory modules:**")
        for m in hw['memory_modules']:
            slot = m.get('slot') or 'Unknown slot'
            cap = f"{m['capacity_gb']} GB" if m.get('capacity_gb') else "?"
            mtype = m.get('memory_type', '')
            speed = f"{m.get('configured_speed_mhz', '?')} MHz"
            mfg = m.get('manufacturer', 'Unknown')
            out.append(f"- {slot}: {mfg} {cap} {mtype} @ {speed}")
        out.append("")

    if hw.get('graphics'):
        out.append("**Graphics:**")
        for g in hw['graphics']:
            details = [g.get('memory_mb') and f"{g['memory_mb']} MB VRAM",
                       g.get('current_resolution') and f"{g['current_resolution']} @ {g.get('current_refresh_hz', '?')}Hz",
                       g.get('driver_version') and f"driver {g['driver_version']}"]
            d = " · ".join(filter(None, details))
            out.append(f"- {g['name']}" + (f" ({d})" if d else ""))
        out.append("")

    if hw.get('monitors'):
        out.append("**Monitors:**")
        for m in hw['monitors']:
            name = f"{m.get('manufacturer', '')} {m.get('user_friendly_name') or m.get('product_code', '')}".strip()
            year = m.get('year_of_manufacture')
            extras = [year and f"made {year}", m.get('serial') and f"S/N {m['serial']}"]
            d = " · ".join(filter(None, extras))
            out.append(f"- {name}" + (f" ({d})" if d else ""))
        out.append("")

    if hw.get('storage', {}).get('physical'):
        out.append("**Storage drives:**")
        for d in hw['storage']['physical']:
            warn = " ⚠ **PREDICTED FAILURE**" if d.get('smart_predicted_failure') else ""
            extras = [
                d.get('size_gb') and f"{d['size_gb']} GB",
                d.get('media_type'), d.get('interface'),
                d.get('firmware_revision') and f"firmware {d['firmware_revision']}",
                f"SMART: {d.get('smart_status', 'unknown')}",
            ]
            ds = " · ".join(filter(None, extras))
            out.append(f"- {d.get('model', 'Unknown')}{warn} ({ds})")
        out.append("")

    if hw.get('battery'):
        b = hw['battery']
        line = f"**Battery:** {b.get('name', 'Unknown')} — {b.get('chemistry', '')}"
        details = [
            b.get('estimated_charge_pct') is not None and f"charge {b['estimated_charge_pct']}%",
            b.get('health_percent') is not None and f"health {b['health_percent']}%",
            b.get('wear_percent') is not None and f"wear {b['wear_percent']}%",
            b.get('status'),
        ]
        d = " · ".join(filter(None, details))
        out.append(line + (f" ({d})" if d else ""))
        if b.get('wear_percent', 0) > 20:
            out.append(f"  > ⚠ Significant battery wear detected.")
        out.append("")

    if hw.get('peripherals'):
        p = hw['peripherals']
        if p.get('pointing'):
            out.append(f"**Pointing devices:** " + ", ".join(d['name'] for d in p['pointing']))
        if p.get('keyboards'):
            out.append(f"**Keyboards:** " + ", ".join(d['name'] for d in p['keyboards']))
        if p.get('cameras'):
            out.append(f"**Cameras:** " + ", ".join(d['name'] for d in p['cameras']))
        out.append("")

    if hw.get('motherboard'):
        m = hw['motherboard']
        out.append(f"**Motherboard:** {m.get('system_manufacturer') or m.get('manufacturer')} {m.get('system_model') or m.get('product')}  ")
        if m.get('bios_version'):
            out.append(f"**BIOS:** {m['bios_version']} ({m.get('bios_release_date', '?')})")
        out.append("")

    # Metrics summary
    out.append("## Recent Metrics Summary")
    out.append("")
    out.append("Statistics over the most recent monitoring window:")
    out.append("")
    out.append("| Metric | Min | Avg | P95 | Max | StdDev | Samples |")
    out.append("|---|---|---|---|---|---|---|")
    metric_labels = {
        'cpu_pct': 'CPU %', 'ram_pct': 'RAM %',
        'cpu_load_lhm': 'CPU Load (LHM)', 'cpu_core_max': 'Hottest Core %',
        'gpu_load': 'GPU %', 'cpu_temp': 'CPU °C', 'gpu_temp': 'GPU °C',
        'disk_read_mb_s': 'Disk Read MB/s', 'disk_write_mb_s': 'Disk Write MB/s',
        'fan_rpm_max': 'Fan RPM',
    }
    for key, stats in diagnostic['metrics_summary'].items():
        label = metric_labels.get(key, key)
        out.append(f"| {label} | {stats['min']} | {stats['avg']} | {stats['p95']} | {stats['max']} | {stats['stddev']} | {stats['samples']} |")
    out.append("")

    # Anomalies
    a = diagnostic['anomalies']
    out.append(f"## Anomalies Detected ({a['total']} total, {a['active_now']} currently active)")
    out.append("")
    if a['list']:
        for an in a['list']:
            ts = datetime.fromtimestamp(an['started_at']).strftime('%H:%M:%S')
            duration = _fmt_duration(an['duration_sec']) if an['duration_sec'] else "ongoing"
            out.append(f"### {an['label']} at {ts} ({duration})")
            out.append("")
            out.append(f"- **Peak:** {an['peak_value']}{an['unit']}")
            out.append(f"- **Baseline:** {an['baseline']}{an['unit']}")
            out.append(f"- **Threshold:** {an['threshold']}{an['unit']}")
            out.append(f"- **Severity ratio:** {an['severity_ratio']}× over baseline")
            if an['suspects']:
                out.append(f"- **Top suspects:**")
                for s in an['suspects'][:5]:
                    flag = f" [{s['flag']}]" if s.get('flag') else ""
                    out.append(f"  - {s['name']} (PID {s['pid']}) — Δ +{s['delta']}{flag}")
            out.append("")
    else:
        out.append("_No anomalies detected during this monitoring window._")
        out.append("")

    # Insights
    ins = diagnostic['insights']
    if ins.get('total'):
        out.append("## Pattern Analysis")
        out.append("")
        if ins.get('repeat_offenders'):
            out.append("**Repeat offender processes:**")
            for o in ins['repeat_offenders']:
                out.append(f"- {o['name']}: appeared in {o['anomaly_count']} anomalies")
            out.append("")
        if ins.get('metric_distribution'):
            out.append("**Anomalies by metric:**")
            for m in ins['metric_distribution']:
                out.append(f"- {m['label']}: {m['count']} ({m['percent']}%)")
            out.append("")
        if ins.get('most_severe'):
            ms = ins['most_severe']
            out.append(f"**Most severe anomaly:** {ms['label']} — peaked at {ms['peak']:.1f} (baseline {ms['baseline']:.1f}, ratio {ms['ratio']:.1f}×)")
            out.append("")

    # Recent processes
    if diagnostic['recent_top_processes']:
        out.append("## Top Processes Recently")
        out.append("")
        out.append("| Process | Avg CPU% | Avg RAM (MB) | Appearances |")
        out.append("|---|---|---|---|")
        for p in diagnostic['recent_top_processes']:
            out.append(f"| {p['name']} (PID {p['pid']}) | {p['avg_cpu']} | {p['avg_ram_mb']} | {p['appearances']} |")
        out.append("")

    # LLM prompt prefix at the end
    out.append("---")
    out.append("")
    out.append("## Diagnostic Question")
    out.append("")
    out.append("Based on this report, please:")
    out.append("1. Summarize the **overall system health** in 2-3 sentences.")
    out.append("2. Identify any **likely problems** — both software (rogue processes, memory leaks, runaway tasks) and hardware (failing storage, battery wear, thermal issues).")
    out.append("3. Distinguish whether issues are likely **hardware vs software** in origin.")
    out.append("4. Provide **specific, prioritized recommendations** for the user — what to check, what to change, what can wait.")
    out.append("5. Flag anything **unusual or suspicious** that warrants further investigation.")
    out.append("")

    return "\n".join(out)


def _fmt_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"