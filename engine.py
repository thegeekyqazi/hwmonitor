import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Optional, List


class Engine(ABC):
    """Base polling engine. Subclass and implement poll() (and optionally setup/teardown)."""

    def __init__(self, name: str, interval: float = 1.0, history_size: int = 600):
        self.name = name
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=history_size)

    @abstractmethod
    def poll(self) -> Any:
        """Collect one round of data."""

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def snapshot(self) -> Any:
        """Return the latest poll, or None."""
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self, start_ts: Optional[float] = None, end_ts: Optional[float] = None) -> List[Any]:
        """Return snapshots within timestamp range. Each snapshot must have a `.timestamp` attr."""
        with self._lock:
            items = list(self._history)
        if start_ts is None and end_ts is None:
            return items
        lo = start_ts if start_ts is not None else 0.0
        hi = end_ts if end_ts is not None else float('inf')
        return [x for x in items if hasattr(x, 'timestamp') and lo <= x.timestamp <= hi]

    def _run(self) -> None:
        try:
            self.setup()
        except Exception as e:
            print(f"[{self.name}] setup failed: {e}")
            return
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    data = self.poll()
                    if data is not None:
                        with self._lock:
                            self._history.append(data)
                except Exception as e:
                    print(f"[{self.name}] poll error: {e}")
                elapsed = time.monotonic() - t0
                self._stop.wait(max(0.0, self.interval - elapsed))
        finally:
            try:
                self.teardown()
            except Exception:
                pass