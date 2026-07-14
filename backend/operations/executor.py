"""
operations/executor.py
=========================
Thin wrapper around a process-local thread pool - not a distributed
task queue. Graduate to Celery/RQ when jobs need to survive an app
restart or you run multiple app replicas needing a shared queue.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_CONCURRENT_OPERATIONS = 4

_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_OPERATIONS,
    thread_name_prefix="vendor-op",
)


def submit(fn, *args, **kwargs):
    future = _executor.submit(fn, *args, **kwargs)

    def _log_exceptions(f):
        exc = f.exception()
        if exc:
            logger.exception("Unhandled exception in background operation", exc_info=exc)

    future.add_done_callback(_log_exceptions)
    return future