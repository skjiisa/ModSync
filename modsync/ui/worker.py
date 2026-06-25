"""Run blocking work (daemon start, REST calls) off the UI thread.

``run_async(fn, *args, on_done=, on_failed=, **kwargs)`` runs ``fn`` on a thread
pool and delivers the result (or error string) back on the UI thread via signals.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # surface the message to the UI thread
            self.signals.failed.emit(str(exc))
        else:
            self.signals.done.emit(result)


# Hold a strong Python reference to every in-flight job. Without this the _Job /
# _Signals Python objects can be garbage-collected while the pool thread is still
# running (or a queued signal is still undelivered), which segfaults inside
# shiboken. Jobs retire themselves on completion.
_active_jobs: set[_Job] = set()


def run_async(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_failed: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> _Job:
    job = _Job(fn, args, kwargs)
    _active_jobs.add(job)

    def _retire(*_: Any) -> None:
        _active_jobs.discard(job)

    if on_done is not None:
        job.signals.done.connect(on_done)
    if on_failed is not None:
        job.signals.failed.connect(on_failed)
    job.signals.done.connect(_retire)
    job.signals.failed.connect(_retire)

    QThreadPool.globalInstance().start(job)
    return job
