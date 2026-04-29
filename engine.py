# engine.py
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional


class Engine(ABC):
    """Base polling engine. Subclass and implement poll() (and optionally setup/teardown)."""

    def __init__(self, name: str, interval: float = 1.0):
        self.name = name
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._snapshot: Any = None

    @abstractmethod
    def poll(self) -> Any:
        """Collect one round of data. Called repeatedly on the worker thread."""

    def setup(self) -> None:
        """One-time init that runs on the worker thread before the loop starts."""

    def teardown(self) -> None:
        """Cleanup that runs on the worker thread after the loop exits."""

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
        with self._lock:
            return self._snapshot

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
                    with self._lock:
                        self._snapshot = data
                except Exception as e:
                    print(f"[{self.name}] poll error: {e}")
                # sleep what's left of the interval; wakes early on stop()
                elapsed = time.monotonic() - t0
                self._stop.wait(max(0.0, self.interval - elapsed))
        finally:
            try:
                self.teardown()
            except Exception:
                pass