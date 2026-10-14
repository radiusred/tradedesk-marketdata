"""The export's bad-tick rule: drop a one-side price its own tick and its neighbours contradict.

A source can deliver a tick whose price on one side is corrupt while the other
side is correct, for example an ask with one extra digit (ten times too large)
or one digit missing (about ten times too small). Copied into the candles, such
a price makes a high or a low that never traded. The export applies
:class:`BadTickRule` to each unit's decoded ticks before it builds candles.

Rule ``one_side_outlier``. For tick *i* and side *S* (``bid`` or ``ask``), with
*O* the other side of the same tick:

- the references are the medians of *S* over up to ``window`` ticks before *i*
  and up to ``window`` ticks after *i* in the same frame (*i* itself excluded);
- *S* is **isolated** when it differs by more than ``max_jump`` (relative) from
  every reference it has; a tick with no neighbour is never isolated;
- *S* is **implausible against its other side** when ``|S / O - 1|`` is more
  than ``max_jump`` and *O* is not isolated (it agrees with its own neighbours).

When *S* is isolated and implausible against its other side, the tick's *S*
price is dropped: it goes into neither that side's candles nor its volume. The
tick's *O* price is kept. A fast move on both sides isolates both sides, so
neither is dropped; a step that the next ticks follow is not isolated; both are
kept. A tick whose two sides are both corrupt
is kept too: the rule only removes a side its own tick contradicts.

The rule holds no value per instrument: ``max_jump`` is relative, ``window`` is
a count of ticks, and both are defaults a caller can set. It is deterministic:
the same frame gives the same result, and a frame with nothing to drop is
returned unchanged (the same object), so its candles are unchanged too.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Default relative distance beyond which a one-side price is implausible.
DEFAULT_MAX_JUMP = 0.1
#: Default number of neighbouring ticks on each side used for the references.
DEFAULT_WINDOW = 5

RULE_ONE_SIDE_OUTLIER = "one_side_outlier"

_SIDES = (("bid", "ask"), ("ask", "bid"))


@dataclass(frozen=True)
class DroppedTick:
    """One side of one tick the rule removed, as recorded in ``_sources.jsonl``."""

    ts: pd.Timestamp
    side: str
    price: float
    rule: str

    def record(self) -> dict[str, object]:
        """The JSON object of the day's ``dropped_ticks`` list."""
        return {
            "ts": self.ts.isoformat(),
            "side": self.side,
            "price": self.price,
            "rule": self.rule,
        }


def _neighbour_median(values: pd.Series, window: int, *, after: bool) -> np.ndarray:
    """Per position, the median of up to ``window`` values before (or after) it; NaN at an edge."""
    s = values[::-1] if after else values
    med = s.shift(1).rolling(window, min_periods=1).median().to_numpy()
    return med[::-1] if after else med


def _beyond(price: np.ndarray, ref: np.ndarray, max_jump: float) -> np.ndarray:
    """Where ``price`` differs from ``ref`` by more than ``max_jump`` (relative); False on NaN."""
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(price / ref - 1.0)
    return np.asarray(rel > max_jump)


@dataclass(frozen=True)
class BadTickRule:
    """The export's bad-tick rule (see the module docstring)."""

    max_jump: float = DEFAULT_MAX_JUMP
    window: int = DEFAULT_WINDOW

    def __post_init__(self) -> None:
        if not self.max_jump > 0:
            raise ValueError(f"max_jump must be > 0, not {self.max_jump}")
        if self.window < 1:
            raise ValueError(f"window must be >= 1, not {self.window}")

    def apply(self, ticks: pd.DataFrame) -> tuple[pd.DataFrame, list[DroppedTick]]:
        """The ticks with the bad one-side prices removed, and those prices in time order.

        ``ticks`` is a tick frame in time order (see :mod:`tradedesk_marketdata.source`).
        A removed price becomes NaN and its side's volume 0.0. When nothing is
        removed, ``ticks`` itself is returned.
        """
        if len(ticks) < 2:
            return ticks, []
        isolated: dict[str, np.ndarray] = {}
        for side, _ in _SIDES:
            values = ticks[side].reset_index(drop=True)
            price = values.to_numpy(dtype=float)
            beyond_all = np.ones(len(price), dtype=bool)
            has_ref = np.zeros(len(price), dtype=bool)
            for after in (False, True):
                ref = _neighbour_median(values, self.window, after=after)
                present = ~np.isnan(ref)
                has_ref |= present
                beyond_all &= ~present | _beyond(price, ref, self.max_jump)
            isolated[side] = beyond_all & has_ref
        drop: dict[str, np.ndarray] = {}
        for side, other in _SIDES:
            spread = _beyond(
                ticks[side].to_numpy(dtype=float),
                ticks[other].to_numpy(dtype=float),
                self.max_jump,
            )
            drop[side] = isolated[side] & ~isolated[other] & spread
        if not (drop["bid"].any() or drop["ask"].any()):
            return ticks, []

        index = pd.DatetimeIndex(ticks.index)
        prices = {side: ticks[side].to_numpy(dtype=float) for side, _ in _SIDES}
        dropped = [
            DroppedTick(
                ts=index[i],
                side=side,
                price=float(prices[side][i]),
                rule=RULE_ONE_SIDE_OUTLIER,
            )
            for i in range(len(ticks))
            for side, _ in _SIDES
            if drop[side][i]
        ]
        # By position: two ticks can share a timestamp.
        columns: dict[str, np.ndarray] = {}
        for side, _ in _SIDES:
            if drop[side].any():
                columns[side] = np.where(drop[side], np.nan, prices[side])
                vol = ticks[f"{side}_vol"].to_numpy(dtype=float)
                columns[f"{side}_vol"] = np.where(drop[side], 0.0, vol)
        clean = ticks.assign(**columns)
        return clean, dropped
