"""Background jobs for the web interface (a search can take minutes).

One job runs at a time. A job collects log lines the page polls for, can be asked to
stop, and runs its asynchronous work in its own event loop on its own thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any


class JobBusyError(RuntimeError):
    """Another job is still running."""


class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"  # running, done, failed, cancelled
        self.lines: list[str] = []
        self.error: str | None = None
        self.result: dict[str, Any] = {}
        self.run_id: int | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel_requested = False
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    def log(self, message: str) -> None:
        with self._lock:
            self.lines.append(message)

    def run_async(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` to completion in a private event loop (cancellable)."""
        loop = asyncio.new_event_loop()

        async def main() -> Any:
            with self._lock:
                self._loop, self._task = loop, asyncio.current_task()
                already = self.cancel_requested
            if already:
                raise asyncio.CancelledError
            return await coro

        try:
            return loop.run_until_complete(main())
        finally:
            with self._lock:
                self._loop = self._task = None
            loop.close()

    def cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "lines": self.lines[since:],
                "next": len(self.lines),
                "error": self.error,
                "result": dict(self.result),
                "run_id": self.run_id,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.status == "running"), None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, kind: str, work: Callable[[Job], None]) -> Job:
        """Run ``work(job)`` on a new thread; ``JobBusyError`` if one is running."""
        with self._lock:
            if any(j.status == "running" for j in self._jobs.values()):
                raise JobBusyError("another job is still running")
            job = Job(kind)
            self._jobs[job.id] = job
            # keep only a few finished jobs
            finished = [j for j in self._jobs.values() if j.status != "running"]
            for old in sorted(finished, key=lambda j: j.started)[:-5]:
                self._jobs.pop(old.id, None)

        def target() -> None:
            try:
                work(job)
                job.status = "cancelled" if job.cancel_requested else "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.log("Stopped. What was fetched so far is saved.")
            except Exception as exc:  # noqa: BLE001 - a job must never kill the server
                job.status = "failed"
                job.error = str(exc) or type(exc).__name__
                job.log(f"Failed: {job.error}")
            finally:
                job.finished = time.time()

        threading.Thread(target=target, name=f"job-{job.id}", daemon=True).start()
        return job
