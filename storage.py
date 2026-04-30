# storage.py
"""
SQLite persistence for anomalies and unified samples.
- Anomalies: kept long-term (30 days)
- UnifiedSamples: rolling window (last 24h, evicted nightly)
"""
import sqlite3
import json
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict


_DB_PATH = Path.home() / ".processlens" / "processlens.db"

SAMPLE_RETENTION_SEC = 24 * 3600       # 24 hours
ANOMALY_RETENTION_SEC = 30 * 24 * 3600 # 30 days


class Storage:
    """Thread-safe SQLite wrapper."""

    def __init__(self, db_path: Path = _DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        # check_same_thread=False because we access from multiple threads;
        # we serialize via our own lock.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS anomalies (
                    id TEXT PRIMARY KEY,
                    metric TEXT NOT NULL,
                    label TEXT NOT NULL,
                    unit TEXT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    baseline REAL,
                    threshold REAL,
                    peak_value REAL,
                    suspects_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_anomalies_started ON anomalies(started_at);

                CREATE TABLE IF NOT EXISTS samples (
                    timestamp REAL PRIMARY KEY,
                    cpu_pct REAL,
                    ram_pct REAL,
                    disk_read_mb_s REAL,
                    disk_write_mb_s REAL,
                    cpu_load_lhm REAL,
                    cpu_core_max REAL,
                    cpu_temp REAL,
                    gpu_load REAL,
                    gpu_temp REAL,
                    gpu_memory_used_mb REAL,
                    memory_load_lhm REAL,
                    fan_rpm_max REAL,
                    top_processes_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(timestamp);
            """)

    # ---- Anomalies ----------------------------------------------------------

    def upsert_anomaly(self, anomaly):
        """Insert new or update existing anomaly (by id)."""
        suspects = [asdict(s) for s in anomaly.suspects]
        with self.lock:
            self.conn.execute("""
                INSERT INTO anomalies (id, metric, label, unit, started_at, ended_at,
                                      baseline, threshold, peak_value, suspects_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ended_at=excluded.ended_at,
                    suspects_json=excluded.suspects_json
            """, (
                anomaly.id, anomaly.metric, anomaly.label, anomaly.unit,
                anomaly.started_at, anomaly.ended_at,
                anomaly.baseline, anomaly.threshold, anomaly.peak_value,
                json.dumps(suspects),
            ))

    def load_recent_anomalies(self, hours: float = 24) -> List[Dict[str, Any]]:
        """Load anomalies from the last N hours (for hydration on boot)."""
        cutoff = time.time() - hours * 3600
        with self.lock:
            rows = self.conn.execute("""
                SELECT id, metric, label, unit, started_at, ended_at,
                       baseline, threshold, peak_value, suspects_json
                FROM anomalies
                WHERE started_at >= ?
                ORDER BY started_at ASC
            """, (cutoff,)).fetchall()
        return [self._row_to_anomaly_dict(r) for r in rows]

    @staticmethod
    def _row_to_anomaly_dict(row) -> Dict[str, Any]:
        return {
            "id": row[0], "metric": row[1], "label": row[2], "unit": row[3],
            "started_at": row[4], "ended_at": row[5],
            "baseline": row[6], "threshold": row[7], "peak_value": row[8],
            "suspects": json.loads(row[9]) if row[9] else [],
        }

    # ---- Samples ------------------------------------------------------------

    def insert_sample(self, sample):
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO samples
                (timestamp, cpu_pct, ram_pct, disk_read_mb_s, disk_write_mb_s,
                 cpu_load_lhm, cpu_core_max, cpu_temp, gpu_load, gpu_temp,
                 gpu_memory_used_mb, memory_load_lhm, fan_rpm_max, top_processes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sample.timestamp, sample.cpu_pct, sample.ram_pct,
                sample.disk_read_mb_s, sample.disk_write_mb_s,
                sample.cpu_load_lhm, sample.cpu_core_max, sample.cpu_temp,
                sample.gpu_load, sample.gpu_temp, sample.gpu_memory_used_mb,
                sample.memory_load_lhm, sample.fan_rpm_max,
                json.dumps(sample.top_processes),
            ))

    def load_samples_in_range(self, start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("""
                SELECT * FROM samples
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (start_ts, end_ts)).fetchall()
        cols = ['timestamp', 'cpu_pct', 'ram_pct', 'disk_read_mb_s', 'disk_write_mb_s',
                'cpu_load_lhm', 'cpu_core_max', 'cpu_temp', 'gpu_load', 'gpu_temp',
                'gpu_memory_used_mb', 'memory_load_lhm', 'fan_rpm_max', 'top_processes_json']
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            d['top_processes'] = json.loads(d.pop('top_processes_json') or '[]')
            d['spawned'] = []
            d['exited'] = []
            result.append(d)
        return result

    # ---- Maintenance --------------------------------------------------------

    def evict_old(self):
        now = time.time()
        with self.lock:
            self.conn.execute("DELETE FROM samples WHERE timestamp < ?",
                              (now - SAMPLE_RETENTION_SEC,))
            self.conn.execute("DELETE FROM anomalies WHERE started_at < ? AND ended_at IS NOT NULL",
                              (now - ANOMALY_RETENTION_SEC,))

    def stats(self) -> Dict[str, int]:
        with self.lock:
            n_anom = self.conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
            n_samp = self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        return {
            "anomalies_stored": n_anom,
            "samples_stored": n_samp,
            "db_path": self.db_path,
            "db_size_mb": round(os.path.getsize(self.db_path) / (1024**2), 2) if os.path.exists(self.db_path) else 0,
        }

    def close(self):
        with self.lock:
            self.conn.close()