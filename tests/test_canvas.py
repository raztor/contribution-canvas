import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import canvas

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "canvas.json").read_text())


def calendar(start="2025-11-02", count=20, length=368):
    date = dt.date.fromisoformat(start)
    return [{"date": (date + dt.timedelta(days=i)).isoformat(), "count": count,
             "level": 1 if count else 0} for i in range(length)]


def html_calendar(days):
    return "".join(
        f'<td id="d{i}" data-date="{d["date"]}" data-level="{d["level"]}"></td>'
        f'<tool-tip for="d{i}">{str(d["count"]) + " contributions" if d["count"] else "No contributions"} on January 1st.</tool-tip>'
        for i, d in enumerate(days)
    )


class CalendarTests(unittest.TestCase):
    def test_parses_published_calendar(self):
        days = calendar()
        self.assertEqual(canvas.parse_calendar(html_calendar(days)), days)

    def test_rejects_missing_tooltips_and_dates(self):
        with self.assertRaises(ValueError):
            canvas.parse_calendar('<td id="d" data-date="2026-01-01" data-level="1"/>')
        days = calendar()
        del days[100]
        with self.assertRaises(ValueError):
            canvas.parse_calendar(html_calendar(days))

    def test_zero_and_comma_counts(self):
        days = calendar(count=0)
        html = html_calendar(days).replace("No contributions", "1,234 contributions", 1).replace('data-level="0"', 'data-level="4"', 1)
        self.assertEqual(canvas.parse_calendar(html)[0]["count"], 1234)

    def test_rejects_inconsistent_level(self):
        days = calendar(count=0)
        days[1]["level"] = 2
        with self.assertRaises(ValueError):
            canvas.parse_calendar(html_calendar(days))


class PlanningTests(unittest.TestCase):
    def test_centered_in_53_weeks_with_no_future_commits(self):
        days = calendar()
        plan = canvas.make_plan(days, CONFIG)
        self.assertEqual((plan["left"], plan["width"], plan["weeks"]), (9, 35, 53))
        self.assertTrue(all(days[0]["date"] <= d <= days[-1]["date"] for d in plan["pixels"]))
        self.assertTrue(all(plan["additions"][d] == 24 for d in plan["pixels"]))
        self.assertEqual(set(plan["additions"]), set(plan["pixels"]))

    def test_recenters_by_one_week_and_handles_leap_year(self):
        first = canvas.make_plan(calendar("2023-03-05", length=367), CONFIG)
        second = canvas.make_plan(calendar("2023-03-12", length=367), CONFIG)
        shifted = {(dt.date.fromisoformat(d) + dt.timedelta(days=7)).isoformat() for d in first["pixels"]}
        self.assertEqual(shifted, set(second["pixels"]))

    def test_commit_budgets_fail_before_mutation(self):
        for key in ("max_commits_total", "max_commits_per_day"):
            config = dict(CONFIG, **{key: 1})
            with self.assertRaises(ValueError):
                canvas.make_plan(calendar(), config)

    def test_empty_calendar_uses_nonzero_artwork(self):
        plan = canvas.make_plan(calendar(count=0), CONFIG)
        self.assertEqual(plan["target"], 8)
        self.assertGreater(plan["total"], 0)

    def test_oversize_and_unknown_characters_fail(self):
        with self.assertRaises(ValueError):
            canvas.make_plan(calendar(), dict(CONFIG, text="RAZTOR" * 3))
        with self.assertRaises(ValueError):
            canvas.validate_config(dict(CONFIG, text="X"))

    def test_actual_level_contrast(self):
        days = calendar()
        plan = canvas.make_plan(days, CONFIG)
        actual = [{**d, "level": 4 if d["date"] in plan["pixels"] else 1} for d in days]
        self.assertTrue(canvas.contrast_metrics(actual, plan, CONFIG)["sufficient"])
        self.assertFalse(canvas.contrast_metrics(days, plan, CONFIG)["sufficient"])

    def test_removal_does_not_subtract_twice_or_accept_stale_cache(self):
        before = calendar(count=30)
        after = calendar(count=20)
        additions = {before[100]["date"]: 10}
        self.assertFalse(canvas.removed(before, before, additions))
        self.assertTrue(canvas.removed(before, after, additions))
        after[100]["count"] = 25
        self.assertFalse(canvas.removed(before, after, additions))

    def test_indexing_checks_every_modified_day(self):
        days = calendar()
        plan = canvas.make_plan(days, CONFIG)
        self.assertFalse(canvas.indexed(days, plan))
        predicted = canvas.predicted_days(plan)
        self.assertTrue(canvas.indexed(predicted, plan))
        next(d for d in predicted if d["date"] in plan["pixels"])["count"] -= 1
        self.assertFalse(canvas.indexed(predicted, plan))


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        canvas.git("init", "-b", "engine")
        Path("README.md").write_text("artwork engine\n")
        canvas.git("add", "README.md")
        env = dict(os.environ, GIT_AUTHOR_NAME=canvas.BOT_NAME, GIT_AUTHOR_EMAIL=canvas.BOT_EMAIL,
                   GIT_COMMITTER_NAME=canvas.BOT_NAME, GIT_COMMITTER_EMAIL=canvas.BOT_EMAIL)
        canvas.git("commit", "-m", "engine", env=env)

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def test_empty_commits_are_deterministic_and_keep_source_tree(self):
        store = canvas.Store(CONFIG)
        base = canvas.git("rev-parse", "engine")
        store.refs["source_branch"] = base
        plan = {"additions": {"2026-02-11": 3, "2026-03-04": 2}}
        head = store.build(plan)
        self.assertEqual(head, store.build(plan))
        self.assertEqual(canvas.git("rev-list", "--count", f"{base}..{head}"), "5")
        self.assertEqual(canvas.git("rev-parse", base + "^{tree}"), canvas.git("rev-parse", head + "^{tree}"))
        self.assertEqual(canvas.git("log", "-1", "--format=%ae", head), CONFIG["email"])
        self.assertEqual(dt.datetime.fromisoformat(canvas.git("log", "-1", "--format=%aI", head)),
                         dt.datetime(2026, 3, 4, 12, 0, 1, tzinfo=dt.timezone.utc))

    def test_publishing_uses_expected_remote_head(self):
        store = canvas.Store(CONFIG)
        with patch.object(canvas, "git", return_value="") as run:
            store.push("canvas_branch", "new-sha", "old-sha")
        self.assertIn("--force-with-lease=refs/heads/main:old-sha", run.call_args.args)

    def test_source_and_canvas_cannot_be_same_branch(self):
        with self.assertRaises(ValueError):
            canvas.validate_config(dict(CONFIG, source_branch="main"))


if __name__ == "__main__":
    unittest.main()
