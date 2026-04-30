# pattern_engine.py
"""
Computes pattern insights from the detector's anomaly history.
All computations are stateless — pure aggregations over the anomaly list.
Called on demand from the API, not running continuously.
"""
from collections import Counter, defaultdict
from typing import Any, Dict, List
import time


def compute_insights(anomalies: List, hours_window: float = None) -> Dict[str, Any]:
    """
    Returns a dict of insights computable from the anomaly history.

    anomalies: list of Anomaly objects from Detector.anomalies()
    hours_window: if given, only consider anomalies started within last N hours
    """
    if hours_window is not None:
        cutoff = time.time() - (hours_window * 3600)
        anomalies = [a for a in anomalies if a.started_at >= cutoff]

    if not anomalies:
        return {
            "total": 0,
            "repeat_offenders": [],
            "metric_distribution": [],
            "time_of_day": [],
            "active_now": 0,
            "avg_duration_sec": None,
        }

    return {
        "total": len(anomalies),
        "active_now": sum(1 for a in anomalies if a.ended_at is None),
        "repeat_offenders": _repeat_offenders(anomalies),
        "metric_distribution": _metric_distribution(anomalies),
        "time_of_day": _time_of_day_pattern(anomalies),
        "avg_duration_sec": _avg_duration(anomalies),
        "longest_anomaly": _longest_anomaly(anomalies),
        "most_severe": _most_severe(anomalies),
    }


def _repeat_offenders(anomalies, top_n: int = 5) -> List[Dict[str, Any]]:
    """Process names that appear most often as suspects."""
    counter = Counter()
    for a in anomalies:
        seen_in_this = set()
        for s in a.suspects:
            # Count each process name once per anomaly (don't double-count suspects)
            if s.name not in seen_in_this:
                counter[s.name] += 1
                seen_in_this.add(s.name)

    return [
        {"name": name, "anomaly_count": count}
        for name, count in counter.most_common(top_n)
    ]


def _metric_distribution(anomalies) -> List[Dict[str, Any]]:
    """How often does each metric fire?"""
    counter = Counter(a.metric for a in anomalies)
    total = sum(counter.values())
    return [
        {
            "metric": metric,
            "label": _metric_label(metric, anomalies),
            "count": count,
            "percent": round(100 * count / total, 1) if total else 0,
        }
        for metric, count in counter.most_common()
    ]


def _metric_label(metric_key: str, anomalies) -> str:
    """Find the human-readable label for a metric key from any anomaly."""
    for a in anomalies:
        if a.metric == metric_key:
            return a.label
    return metric_key


def _time_of_day_pattern(anomalies) -> List[Dict[str, Any]]:
    """Hourly distribution of anomaly start times."""
    hours = defaultdict(int)
    for a in anomalies:
        hour = time.localtime(a.started_at).tm_hour
        hours[hour] += 1

    return [
        {"hour": h, "count": hours.get(h, 0)}
        for h in range(24)
    ]


def _avg_duration(anomalies) -> float:
    """Average duration in seconds for closed anomalies."""
    durations = [
        a.ended_at - a.started_at
        for a in anomalies if a.ended_at is not None
    ]
    return sum(durations) / len(durations) if durations else None


def _longest_anomaly(anomalies):
    """The single longest-lived anomaly."""
    closed = [a for a in anomalies if a.ended_at is not None]
    if not closed:
        return None
    longest = max(closed, key=lambda a: a.ended_at - a.started_at)
    return {
        "metric": longest.metric,
        "label": longest.label,
        "duration_sec": longest.ended_at - longest.started_at,
        "started_at": longest.started_at,
    }


def _most_severe(anomalies):
    """Anomaly with the largest peak relative to its baseline."""
    if not anomalies:
        return None
    severity = lambda a: (a.peak_value - a.baseline) / max(a.baseline, 1e-6)
    most = max(anomalies, key=severity)
    return {
        "metric": most.metric,
        "label": most.label,
        "peak": most.peak_value,
        "baseline": most.baseline,
        "ratio": severity(most),
        "started_at": most.started_at,
    }