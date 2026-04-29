# detector.py
import time
import statistics
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Callable

from engine import Engine
from aggregator import MetricsAggregator, UnifiedSample


# Metrics we run anomaly detection on, with display config
METRIC_CONFIG = {
    'cpu_pct':         {'label': 'CPU %',         'unit': '%',     'min_floor': 25.0},
    'ram_pct':         {'label': 'RAM %',         'unit': '%',     'min_floor': 60.0},
    'cpu_load_lhm':    {'label': 'CPU Load',      'unit': '%',     'min_floor': 25.0},
    'cpu_core_max':    {'label': 'Hottest Core',  'unit': '%',     'min_floor': 60.0},
    'gpu_load':        {'label': 'GPU %',         'unit': '%',     'min_floor': 30.0},
    'disk_read_mb_s':  {'label': 'Disk Read',     'unit': 'MB/s',  'min_floor': 20.0},
    'disk_write_mb_s': {'label': 'Disk Write',    'unit': 'MB/s',  'min_floor': 20.0},
}

# Per-anomaly: which per-process field to use for attribution
PROC_METRIC_FOR = {
    'cpu_pct':         'cpu',
    'cpu_load_lhm':    'cpu',
    'cpu_core_max':    'cpu',
    'ram_pct':         'mem_mb',
    'gpu_load':        'cpu',  # no per-proc GPU; fall back to CPU as a proxy
    'disk_read_mb_s':  'cpu',  # no per-proc disk; fall back to CPU as a proxy
    'disk_write_mb_s': 'cpu',
}


@dataclass
class Suspect:
    pid: int
    name: str
    delta: float           # how much this process's contribution grew
    metric: str            # which per-process metric was used
    flag: Optional[str] = None  # e.g. 'just_spawned'


@dataclass
class Anomaly:
    id: str                              # e.g. "cpu_pct-1714409123"
    metric: str                          # canonical metric key
    label: str                           # human-readable
    unit: str
    started_at: float
    ended_at: Optional[float]
    baseline: float                      # mean during baseline window
    threshold: float                     # mean + 3σ (with floor)
    peak_value: float
    suspects: List[Suspect] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class Detector(Engine):
    """
    Reads UnifiedSamples from the aggregator's history, applies a
    rolling 3σ threshold per metric, and fires anomalies with
    attributed suspect processes.
    """

    def __init__(self, aggregator: MetricsAggregator,
                 interval: float = 3.0,
                 baseline_window_sec: float = 300.0,
                 recent_window_sec: float = 8.0,
                 min_baseline_samples: int = 30,
                 sigma: float = 3.0,
                 consecutive_required: int = 4,
                 cooldown_sec: float = 60.0,
                 history_size: int = 200):
        super().__init__("Detector", interval, history_size=1)  # _history unused here
        self.agg = aggregator
        self.baseline_window = baseline_window_sec
        self.recent_window = recent_window_sec
        self.min_baseline = min_baseline_samples
        self.sigma = sigma
        self.consecutive_required = consecutive_required
        self.cooldown = cooldown_sec

        # State
        self._active: Dict[str, float] = {}        # metric -> started_at timestamp
        self._cooldowns: Dict[str, float] = {}     # metric -> until timestamp
        self._anomalies: deque = deque(maxlen=history_size)

        # Callback the API/UI registers to get push notifications.
        # Signature: callback(event_type: str, anomaly: Anomaly)
        # event_type is "started" or "ended"
        self.on_event: Optional[Callable[[str, Anomaly], None]] = None

    def poll(self):
        """Called every `interval` seconds by Engine._run."""
        now = time.time()

        baseline_samples = self.agg.history(now - self.baseline_window, now - self.recent_window)
        recent_samples = self.agg.history(now - self.recent_window, now)

        if len(baseline_samples) < self.min_baseline:
            return None  # not enough history yet

        for metric in METRIC_CONFIG:
            self._check_metric(metric, baseline_samples, recent_samples, now)
        return None

    def _check_metric(self, metric: str, baseline: List[UnifiedSample],
                      recent: List[UnifiedSample], now: float):
        # In cooldown? skip
        if self._cooldowns.get(metric, 0) > now:
            return

        b_vals = [getattr(s, metric) for s in baseline if getattr(s, metric, None) is not None]
        r_vals = [getattr(s, metric) for s in recent   if getattr(s, metric, None) is not None]

        if len(b_vals) < self.min_baseline or not r_vals:
            return

        mean = statistics.mean(b_vals)
        sd = statistics.stdev(b_vals) if len(b_vals) > 1 else 0.0

        # 3σ threshold, with a floor so noisy idle metrics don't trip
        # at low absolute values (e.g. 0.5% CPU jumping to 2% is not interesting)
        floor = METRIC_CONFIG[metric]['min_floor']
        threshold = max(mean + self.sigma * sd, floor)

        consecutive_high = sum(1 for v in r_vals if v > threshold)

        if consecutive_high >= self.consecutive_required:
            if metric not in self._active:
                self._active[metric] = now - self.recent_window  # rough start time
                self._fire(metric, mean, threshold, max(r_vals), now)
        else:
            if metric in self._active:
                self._close(metric, now)

    def _fire(self, metric: str, baseline: float, threshold: float,
              peak: float, now: float):
        cfg = METRIC_CONFIG[metric]
        suspects = self._attribute(metric, self._active[metric], now)

        anomaly = Anomaly(
            id=f"{metric}-{int(self._active[metric])}",
            metric=metric,
            label=cfg['label'],
            unit=cfg['unit'],
            started_at=self._active[metric],
            ended_at=None,
            baseline=baseline,
            threshold=threshold,
            peak_value=peak,
            suspects=suspects,
        )

        with self._lock:
            self._anomalies.append(anomaly)
        self._cooldowns[metric] = now + self.cooldown

        if self.on_event:
            try:
                self.on_event("started", anomaly)
            except Exception as e:
                print(f"[Detector] on_event(started) raised: {e}")

    def _close(self, metric: str, now: float):
        started_at = self._active.pop(metric, None)
        if started_at is None:
            return
        # Find the matching anomaly and update ended_at
        with self._lock:
            for a in reversed(self._anomalies):
                if a.metric == metric and a.ended_at is None and a.started_at == started_at:
                    a.ended_at = now
                    matched = a
                    break
            else:
                matched = None
        if matched and self.on_event:
            try:
                self.on_event("ended", matched)
            except Exception as e:
                print(f"[Detector] on_event(ended) raised: {e}")

    def _attribute(self, metric: str, anomaly_start: float, now: float) -> List[Suspect]:
        """Rank top suspect processes by delta contribution before vs during."""
        proc_metric = PROC_METRIC_FOR.get(metric, 'cpu')

        # Windows: before (10s before anomaly) vs during (anomaly to now)
        before = self.agg.history(anomaly_start - 10, anomaly_start)
        during = self.agg.history(anomaly_start, now)

        before_avg = self._avg_proc_metric(before, proc_metric)
        during_avg = self._avg_proc_metric(during, proc_metric)

        deltas: List[Suspect] = []
        all_pids = set(before_avg.keys()) | set(during_avg.keys())
        for pid in all_pids:
            d = during_avg.get(pid, 0.0) - before_avg.get(pid, 0.0)
            if d <= 0:
                continue
            name = self._name_for_pid(during, pid) or self._name_for_pid(before, pid) or "unknown"
            deltas.append(Suspect(pid=pid, name=name, delta=d, metric=proc_metric))

        # Add temporal-correlation suspects: anything spawned within ±5s of onset
        seen_pids = {s.pid for s in deltas}
        for sample in during:
            for spawn in sample.spawned:
                pid = spawn.get('pid')
                if pid is None or pid in seen_pids:
                    continue
                if abs(spawn.get('started_at', sample.timestamp) - anomaly_start) <= 5:
                    deltas.append(Suspect(
                        pid=pid,
                        name=spawn.get('name', 'unknown'),
                        delta=0.0,
                        metric=proc_metric,
                        flag='just_spawned',
                    ))
                    seen_pids.add(pid)

        deltas.sort(key=lambda s: s.delta, reverse=True)
        return deltas[:5]

    @staticmethod
    def _avg_proc_metric(samples: List[UnifiedSample], key: str) -> Dict[int, float]:
        sums: Dict[int, float] = {}
        counts: Dict[int, int] = {}
        for s in samples:
            for p in s.top_processes:
                pid = p['pid']
                v = p.get(key, 0) or 0
                sums[pid] = sums.get(pid, 0.0) + v
                counts[pid] = counts.get(pid, 0) + 1
        return {pid: sums[pid] / counts[pid] for pid in sums}

    @staticmethod
    def _name_for_pid(samples: List[UnifiedSample], pid: int) -> Optional[str]:
        for s in reversed(samples):
            for p in s.top_processes:
                if p['pid'] == pid:
                    return p['name']
        return None

    def anomalies(self, limit: Optional[int] = None) -> List[Anomaly]:
        with self._lock:
            items = list(self._anomalies)
        if limit:
            return items[-limit:]
        return items

    def active_anomalies(self) -> List[Anomaly]:
        with self._lock:
            return [a for a in self._anomalies if a.ended_at is None]