"""Parallel execution for multi-symbol exports."""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from . import export
from .cancel import cancellation as _cancellation_event
from .source import Source

log = logging.getLogger(__name__)


@dataclass
class ExportTask:
    """Configuration for a single symbol export."""

    symbol: str
    start_utc: datetime
    end_utc_inclusive: datetime
    resample_rule: str | None
    cache_dir: Path | None
    out: Path
    source: Source
    commit_partial_after_days: int = 7
    timeout: tuple[float, float] | None = None
    retries: int | None = None
    keep_raw: bool = False


@dataclass
class ExportResult:
    """Result from exporting a single symbol."""

    symbol: str
    output_csvs: list[Path]
    success: bool
    error: str | None = None


def _export_worker(task: ExportTask, progress: Progress | None = None) -> ExportResult:
    """Worker function to export a single symbol."""
    # None means "the framework's default", so a task built without the
    # network settings behaves exactly as before.
    network: dict[str, object] = {}
    if task.timeout is not None:
        network["timeout"] = task.timeout
    if task.retries is not None:
        network["retries"] = task.retries

    try:
        bid_csv, ask_csv = export.export_range(
            source=task.source,
            symbol=task.symbol,
            start_utc=task.start_utc,
            end_utc_inclusive=task.end_utc_inclusive,
            resample_rule=task.resample_rule,
            cache_dir=task.cache_dir,
            commit_partial_after_days=task.commit_partial_after_days,
            out=task.out,
            progress=progress,
            keep_raw=task.keep_raw,
            **network,  # type: ignore[arg-type]
        )
        output_csvs = [p for p in (bid_csv, ask_csv) if p is not None]
        return ExportResult(symbol=task.symbol, output_csvs=output_csvs, success=True)

    except Exception as e:
        log.exception(f"Failed to export {task.symbol}")
        return ExportResult(symbol=task.symbol, output_csvs=[], success=False, error=str(e))


def run_parallel_exports(
    tasks: list[ExportTask],
    max_workers: int,
) -> list[ExportResult]:
    """Execute exports in parallel."""
    total = len(tasks)
    results = []
    use_rich = sys.stdout.isatty()

    _cancellation_event.clear()

    if not use_rich:
        log.info(f"Starting export of {total} symbols with {max_workers} workers")

    progress_ctx = (
        Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[symbol]}[/] {task.fields[phase]}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )
        if use_rich
        else nullcontext()
    )

    executor = ThreadPoolExecutor(max_workers=max_workers)

    try:
        with progress_ctx as progress:
            futures = {
                executor.submit(_export_worker, task, progress if use_rich else None): task
                for task in tasks
            }

            completed = 0
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                completed += 1

                if result.success:
                    if not use_rich:
                        log.info(f"[{completed}/{total}] ✓ {result.symbol} complete")
                else:
                    prefix = f"[{completed}/{total}] " if not use_rich else ""
                    log.error(f"{prefix}✗ {result.symbol} failed: {result.error}")

    except KeyboardInterrupt:
        _cancellation_event.set()
        log.warning(
            "Interrupted - cancelling: queued units are dropped and in-flight requests "
            "end at their next timeout; press Ctrl-C again to exit without waiting"
        )
        executor.shutdown(wait=False, cancel_futures=True)
        raise

    finally:
        if not _cancellation_event.is_set():
            executor.shutdown(wait=True)
        else:
            executor.shutdown(wait=False, cancel_futures=True)

    return results
