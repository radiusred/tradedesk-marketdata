"""Cooperative cancellation shared by the export and parallel modules.

The main thread sets ``cancellation`` on the first Ctrl-C. Download code checks
it before every request and during every backoff sleep and raises
``KeyboardInterrupt`` when it is set, so worker threads stop within one HTTP
timeout instead of draining their queues.
"""

import threading

cancellation = threading.Event()


def check_cancelled() -> None:
    """Raise ``KeyboardInterrupt`` in the calling thread if a cancel is pending."""
    if cancellation.is_set():
        raise KeyboardInterrupt()
