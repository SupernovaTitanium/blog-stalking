from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from run_state import (
    RunState,
    effective_cutoff,
    load_run_state,
    save_run_state,
)


class RunStateRoundTripTest(unittest.TestCase):
    def test_save_and_load_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "last_run.json"
            window_end = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)
            save_run_state(
                path,
                window_end=window_end,
                post_keys=["source:a", "source:b"],
                previous=None,
            )

            loaded = load_run_state(path)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.window_end, window_end)
        self.assertEqual(loaded.seen_posts, ["source:a", "source:b"])

    def test_load_returns_none_for_missing_or_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_run_state(Path(tmp) / "absent.json"))
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_run_state(corrupt))

    def test_save_merges_and_caps_seen_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            previous = RunState(
                window_end=datetime(2026, 9, 1, tzinfo=timezone.utc),
                seen_posts=[f"old:{i}" for i in range(999)],
            )
            save_run_state(
                path,
                window_end=datetime(2026, 9, 2, tzinfo=timezone.utc),
                post_keys=["new:1", "new:2"],
                previous=previous,
            )

            loaded = load_run_state(path)

        self.assertEqual(len(loaded.seen_posts), 1000)
        self.assertEqual(loaded.seen_posts[-2:], ["new:1", "new:2"])


class EffectiveCutoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)

    def test_uses_rolling_window_without_state(self) -> None:
        cutoff = effective_cutoff(now=self.now, window_hours=24, state=None)
        self.assertEqual(cutoff, self.now - timedelta(hours=24))

    def test_narrows_window_after_recent_run(self) -> None:
        # Last run 2h ago: everything it covered is inside the plain window,
        # so the cutoff stays at the window (re-delivery is blocked by
        # seen_posts, not by narrowing).
        state = RunState(window_end=self.now - timedelta(hours=2))
        cutoff = effective_cutoff(now=self.now, window_hours=24, state=state)
        self.assertEqual(cutoff, self.now - timedelta(hours=24))

    def test_extends_window_when_today_runs_late(self) -> None:
        # Yesterday's run was punctual (fetched at 22:00); today's run is
        # delayed to 23:30, so the plain window would only start at 23:30 and
        # skip posts published shortly after 22:00. The cutoff reaches back
        # to the recorded window end minus grace.
        now = datetime(2026, 9, 2, 23, 30, tzinfo=timezone.utc)
        window_end = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)
        state = RunState(window_end=window_end)
        cutoff = effective_cutoff(now=now, window_hours=24, state=state)
        self.assertEqual(cutoff, window_end - timedelta(minutes=10))

    def test_clamps_very_old_state_to_backtrack_cap(self) -> None:
        state = RunState(window_end=self.now - timedelta(days=30))
        cutoff = effective_cutoff(
            now=self.now, window_hours=24, state=state, max_backtrack_hours=72
        )
        self.assertEqual(cutoff, self.now - timedelta(hours=72))


if __name__ == "__main__":
    unittest.main()
