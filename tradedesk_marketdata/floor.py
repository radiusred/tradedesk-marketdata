"""Set, lower or remove a symbol's earliest-date floor in a cache.

A floor retires a symbol's oldest history: the operator names the first day
the store should hold, and every day before it leaves the live store. Nothing
is deleted. The day files move to an operator-named backup directory, in the
store's own layout::

    {backup}/{SYMBOL}/{YYYY}/{MM0}/{DD}_{bid,ask}.csv.zst

and the ``_sources.jsonl`` and ``_partial_days.jsonl`` lines of those days move
to the same file names under ``{backup}/{SYMBOL}/``.

The floor itself is ``{cache}/{SYMBOL}/_floor.json``::

    {"floor": "YYYY-MM-DD", "set_at": "<UTC ISO time>", "tool_version": "<version>"}

The export does not write a day before it and verify does not check one (see
:func:`tradedesk_marketdata.export.read_floor`). Every change appends one
record to the live ``_sources.jsonl``::

    {"status": "floor", "action": "set" | "lower" | "remove", "floor": "YYYY-MM-DD" | null,
     "previous": "YYYY-MM-DD" | null, "days_moved": N, "backup_dir": "<absolute path>" | null,
     "tool_version": "<version>", "decided_at": "<UTC ISO time>"}

A record with a ``status`` is not a commit, so the manifest stays a valid
manifest of the days that remain.

Order of work: copy each file to the backup and compare its bytes, append the
moved manifest lines to the backup, write the floor, rewrite the live
manifests, and only then remove the live day files. A run stopped part way is
completed by running it again. The same floor a second time, with no day
before it left in the store, writes nothing.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__
from .export import (
    FloorError,
    _floor_path,
    _parse_cache_day,
    _partial_day_manifest_path,
    _source_manifest_path,
    read_floor,
)

__all__ = ["FloorError", "FloorResult", "remove_floor", "set_floor"]

log = logging.getLogger(__name__)

_SIDES = ("bid", "ask")


@dataclass
class FloorResult:
    """What a floor change did, or would do with ``dry_run``.

    ``action`` is ``set``, ``lower``, ``remove`` or ``none`` (nothing to change).
    """

    symbol: str
    action: str
    floor: date | None
    previous: date | None
    days_moved: list[date] = field(default_factory=list)
    manifest_lines_moved: int = 0

    @property
    def changed(self) -> bool:
        return self.action != "none"


def _day_files_before(sym_dir: Path, floor: date) -> dict[date, list[Path]]:
    """Every day file of the symbol dated before ``floor``, by day."""
    out: dict[date, list[Path]] = {}
    if not sym_dir.is_dir():
        return out
    for year_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        for month_dir in sorted(p for p in year_dir.iterdir() if p.is_dir() and p.name.isdigit()):
            for side in _SIDES:
                for path in sorted(month_dir.glob(f"*_{side}.csv.zst")):
                    day = _parse_cache_day(
                        year_dir.name, month_dir.name, path.name.split("_", 1)[0]
                    )
                    if day is not None and day < floor:
                        out.setdefault(day, []).append(path)
    return dict(sorted(out.items()))


def _split_manifest(path: Path, floor: date) -> tuple[list[str], list[str]]:
    """A manifest's lines as (kept, moved): moved are the day records dated before ``floor``."""
    if not path.exists():
        return [], []
    kept: list[str] = []
    moved: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            day = date.fromisoformat(json.loads(line)["day"])
        except (ValueError, KeyError, TypeError):
            kept.append(line)  # not a day record (a floor record, say): it stays
            continue
        (moved if day < floor else kept).append(line)
    return kept, moved


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append_new_lines(path: Path, lines: list[str]) -> None:
    """Append the lines ``path`` does not hold yet (a re-run after a stop adds none twice)."""
    have = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
    new = [line for line in lines if line not in have]
    if not new:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in new))


def _copy_verified(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` and check the bytes; a ``dest`` with the same bytes is kept."""
    data = src.read_bytes()
    if dest.exists() and dest.read_bytes() == data:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    if dest.read_bytes() != data:
        raise FloorError(f"{dest}: the copy of {src} does not match; nothing removed")


def _floor_record(
    action: str,
    floor: date | None,
    previous: date | None,
    days_moved: int,
    backup_dir: Path | None,
) -> str:
    return json.dumps(
        {
            "status": "floor",
            "action": action,
            "floor": floor.isoformat() if floor else None,
            "previous": previous.isoformat() if previous else None,
            "days_moved": days_moved,
            "backup_dir": str(backup_dir) if backup_dir is not None else None,
            "tool_version": __version__,
            "decided_at": datetime.now(UTC).isoformat(),
        }
    )


def _remove_empty_dirs(paths: list[Path], sym_dir: Path) -> None:
    for d in sorted({p.parent for p in paths} | {p.parent.parent for p in paths}, reverse=True):
        if d != sym_dir and d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def set_floor(
    cache_dir: Path,
    symbol: str,
    floor: date,
    backup_dir: Path,
    *,
    lower: bool = False,
    dry_run: bool = False,
) -> FloorResult:
    """Make ``floor`` the symbol's earliest day; move every day before it to ``backup_dir``.

    A floor below the current one is refused unless ``lower`` is given. A
    backup directory inside ``cache_dir`` is refused, and so is a backup file
    that exists with other bytes than the file it would back up. Raises
    :class:`FloorError` on a refusal, before anything is written.
    """
    sym_dir = cache_dir / symbol
    if not sym_dir.is_dir():
        raise FloorError(f"{sym_dir}: no such symbol in the cache")
    cache_root, backup_root = cache_dir.resolve(), backup_dir.resolve()
    if backup_root == cache_root or cache_root in backup_root.parents:
        raise FloorError(f"--backup-dir {backup_dir} is inside the cache {cache_dir}")
    previous = read_floor(cache_dir, symbol)
    if previous is not None and floor < previous and not lower:
        raise FloorError(
            f"{symbol}: {floor.isoformat()} is below the current floor {previous.isoformat()}; "
            "lowering a floor needs --lower"
        )

    files = _day_files_before(sym_dir, floor)
    sources = _source_manifest_path(cache_dir, symbol)
    partials = _partial_day_manifest_path(cache_dir, symbol)
    manifests = {p: _split_manifest(p, floor) for p in (sources, partials)}
    lines_moved = sum(len(moved) for _, moved in manifests.values())
    if previous == floor and not files and not lines_moved:
        return FloorResult(symbol, "none", floor, previous)
    action = "lower" if previous is not None and floor < previous else "set"
    result = FloorResult(symbol, action, floor, previous, list(files), lines_moved)

    backup_sym = backup_dir / symbol
    moves = [(p, backup_sym / p.relative_to(sym_dir)) for paths in files.values() for p in paths]
    for src, dest in moves:
        if dest.exists() and dest.read_bytes() != src.read_bytes():
            raise FloorError(f"{dest} exists with other bytes than {src}; nothing moved")
    if dry_run:
        return result

    for src, dest in moves:
        _copy_verified(src, dest)
    for path, (_, moved) in manifests.items():
        if moved:
            _append_new_lines(backup_sym / path.name, moved)
    _write_atomic(
        _floor_path(cache_dir, symbol),
        json.dumps(
            {
                "floor": floor.isoformat(),
                "set_at": datetime.now(UTC).isoformat(),
                "tool_version": __version__,
            }
        )
        + "\n",
    )
    record = _floor_record(action, floor, previous, len(files), backup_dir.absolute())
    for path, (kept, moved) in manifests.items():
        if path == sources:
            kept = [*kept, record]
        elif not moved:
            continue
        _write_atomic(path, "".join(line + "\n" for line in kept))
    for src, _ in moves:
        src.unlink()
    _remove_empty_dirs([src for src, _ in moves], sym_dir)
    log.info(
        f"{symbol}: floor {floor.isoformat()} ({action}); {len(files)} day(s) and "
        f"{lines_moved} manifest line(s) moved to {backup_sym}"
    )
    return result


def remove_floor(cache_dir: Path, symbol: str, *, dry_run: bool = False) -> FloorResult:
    """Remove the symbol's floor and record it; the days moved out stay in their backup."""
    previous = read_floor(cache_dir, symbol)
    if previous is None:
        return FloorResult(symbol, "none", None, None)
    result = FloorResult(symbol, "remove", None, previous)
    if dry_run:
        return result
    manifest = _source_manifest_path(cache_dir, symbol)
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(_floor_record("remove", None, previous, 0, None) + "\n")
    _floor_path(cache_dir, symbol).unlink()
    log.info(f"{symbol}: floor {previous.isoformat()} removed")
    return result
