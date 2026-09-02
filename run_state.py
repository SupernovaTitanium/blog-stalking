"""Persistent run state so consecutive runs neither miss nor duplicate posts.

A run that completes successfully records its fetch time (``window_end``) and
the post keys it delivered. The next run derives its cutoff from
``max(now - window_hours, last window_end - grace)`` so scheduled-run delays
or a failed day no longer leave permanent gaps, and ``seen_posts`` suppresses
re-delivery when windows overlap (e.g. a manual run plus the nightly cron).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

DEFAULT_STATE_FILE = "state/last_run.json"
STATE_GRACE_MINUTES = 10
MAX_SEEN_POSTS = 1000


@dataclass
class RunState:
    window_end: Optional[datetime] = None
    seen_posts: List[str] = field(default_factory=list)


def load_run_state(path: Path) -> Optional[RunState]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    window_end: Optional[datetime] = None
    raw_window_end = data.get("window_end")
    if raw_window_end:
        try:
            window_end = datetime.fromisoformat(str(raw_window_end))
        except ValueError:
            window_end = None
    if window_end is not None and window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    seen = [str(key) for key in data.get("seen_posts", []) if key]
    return RunState(window_end=window_end, seen_posts=seen)


def save_run_state(
    path: Path,
    *,
    window_end: datetime,
    post_keys: List[str],
    previous: Optional[RunState] = None,
) -> None:
    seen = list(previous.seen_posts if previous else []) + list(post_keys)
    seen = seen[-MAX_SEEN_POSTS:]
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    payload = {
        "window_end": window_end.astimezone(timezone.utc).isoformat(),
        "seen_posts": seen,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def effective_cutoff(
    *,
    now: datetime,
    window_hours: int,
    state: Optional[RunState] = None,
    max_backtrack_hours: int = 72,
    grace_minutes: int = STATE_GRACE_MINUTES,
) -> datetime:
    """Cutoff combining the rolling window with the last successful run.

    ``max(candidate, floor)`` clamps the state-derived point so a long outage
    cannot produce a month-long digest; ``min(..., base)`` then only ever
    moves the cutoff *backwards* from the plain window, closing the gap a
    delayed or early schedule leaves between runs. Overlap in the normal case
    is bounded by ``grace_minutes`` and de-duplicated via ``seen_posts``.
    """
    base = now - timedelta(hours=window_hours)
    if state is None or state.window_end is None:
        return base
    floor = now - timedelta(hours=max_backtrack_hours)
    candidate = state.window_end - timedelta(minutes=grace_minutes)
    return min(base, max(candidate, floor))
