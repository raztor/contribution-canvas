import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import canvas
import rotate
from test_canvas import calendar, CONFIG


class FakeAPI:
    token = "test-only-value"

    def __init__(self, state, existing=True):
        self.state = state
        self.existing = existing
        self.calls = []
        self.repo = {"id": state.get("repository_id", 99), "full_name": rotate.ALLOWED_PIXELS,
                     "owner": {"login": "raztor"}, "fork": False, "archived": False,
                     "private": False, "description": rotate.description(state.get("nonce", "nonce"))}
        self.extra = []

    def api(self, path, method="GET", body=None, missing=False):
        self.calls.append((method, path))
        if method == "DELETE":
            self.existing = False
            return None
        if method == "POST":
            self.existing = True
            self.repo["description"] = body["description"]
            return self.repo
        if method == "PATCH":
            return self.repo
        if path == "/repos/" + rotate.ALLOWED_PIXELS:
            return self.repo if self.existing else None
        if "/branches?" in path:
            return [{"name": "main", "commit": {"sha": self.state["head"]}}]
        if "/contents/" in path:
            import base64
            return {"content": base64.b64encode(json.dumps(rotate.marker(self.state["nonce"])).encode()).decode()}
        if "/git/ref/" in path:
            return None
        return self.extra


class FakeStore:
    receipt = {"phase": "new"}

    def __init__(self, config):
        self.refs = {"source_branch": "source"}

    def refresh(self):
        pass

    def load(self):
        return copy.deepcopy(type(self).receipt)

    def save(self, state, svg=None):
        type(self).receipt = copy.deepcopy(state)


def ready_state():
    plan = canvas.make_plan(calendar(), CONFIG)
    return {"phase": "ready", "plan": plan, "nonce": "nonce", "repository_id": 99,
            "head": "pixels", "started_at": canvas.now(), "calibration_round": 0,
            "fingerprint": rotate.key(CONFIG, "source")}


class GuardTests(unittest.TestCase):
    def test_other_repositories_cannot_be_configured_or_deleted(self):
        with self.assertRaises(ValueError):
            rotate.validate(dict(CONFIG, pixels_repository="raztor/real-work"))
        api = rotate.GitHub("test-only-value")
        with self.assertRaises(ValueError):
            api.api("/repos/raztor/real-work", method="DELETE")
        with self.assertRaises(ValueError):
            api.api("/user/repos", method="POST", body={"name": "real-work"})

    def test_missing_token_fails_before_any_operation(self):
        with self.assertRaises(ValueError):
            rotate.GitHub(None)

    def test_empty_repository_ref_409_is_recoverable(self):
        import urllib.error
        path = "/repos/" + rotate.ALLOWED_PIXELS + "/git/ref/heads/main"
        error = urllib.error.HTTPError("https://api.github.com" + path, 409, "Git Repository is empty", {}, None)
        with patch.object(rotate.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(rotate.GitHub("test-only-value").api(path, missing=True))
        with patch.object(rotate.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ValueError):
                rotate.GitHub("test-only-value").api("/repos/" + rotate.ALLOWED_PIXELS, missing=True)

    def test_push_diagnostics_do_not_expose_credentials(self):
        import base64, subprocess
        token = "test-only-private-credential"
        basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
        error = subprocess.CalledProcessError(128, ["git", "push"], stderr=f"Authorization: basic {basic}\nfailed with {token}")
        with patch.object(canvas, "git", side_effect=error):
            with self.assertRaises(ValueError) as result:
                rotate.push_pixels("head", token)
        self.assertNotIn(token, str(result.exception))
        self.assertNotIn(basic, str(result.exception))

    def test_deletion_requires_exact_numeric_identity_and_head(self):
        state = ready_state()
        api = FakeAPI(state)
        self.assertTrue(rotate.safe_to_delete(api, state))
        api.repo["id"] = 100
        with self.assertRaises(ValueError):
            rotate.safe_to_delete(api, state)
        api.repo["id"] = 99
        api.state = dict(state, head="unexpected")
        with self.assertRaises(ValueError):
            rotate.safe_to_delete(api, state)

    def test_user_content_blocks_deletion(self):
        state = ready_state()
        api = FakeAPI(state)
        api.extra = [{"name": "preserve-me"}]
        with self.assertRaises(ValueError):
            rotate.safe_to_delete(api, state)
        self.assertFalse(any(method == "DELETE" for method, path in api.calls))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        FakeStore.receipt = ready_state()
        self.store_patch = patch.object(canvas, "Store", FakeStore)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()

    def test_stale_index_never_creates_another_generation(self):
        FakeStore.receipt["phase"] = "published"
        api = FakeAPI(FakeStore.receipt)
        with patch.object(canvas, "fetch_calendar", return_value=calendar()):
            state = rotate.step(CONFIG, api)
        self.assertEqual(state["phase"], "published")
        self.assertEqual(api.calls, [])

    def test_clear_request_survives_an_unindexed_picture(self):
        state = ready_state()
        state["phase"] = "published"
        FakeStore.receipt = state
        api = FakeAPI(state)
        with patch.object(canvas, "fetch_calendar", return_value=calendar()):
            result = rotate.step(CONFIG, api, "clear")
        self.assertEqual(result["requested_mode"], "clear")
        with patch.object(canvas, "fetch_calendar", return_value=canvas.predicted_days(state["plan"])):
            result = rotate.step(CONFIG, api)
        self.assertEqual(result["phase"], "deleting")
        self.assertTrue(result["stop"])
        self.assertFalse(api.existing)

    def test_resume_does_not_redraw_an_already_active_picture(self):
        state = ready_state()
        api = FakeAPI(state)
        with patch.object(canvas, "fetch_calendar", return_value=canvas.predicted_days(state["plan"])):
            result = rotate.step(CONFIG, api, "resume")
        self.assertEqual(result["phase"], "ready")
        self.assertEqual(api.calls, [])

    def test_clear_waits_for_actual_removal_then_stays_suspended(self):
        state = ready_state()
        actual = canvas.predicted_days(state["plan"])
        api = FakeAPI(state)
        with patch.object(canvas, "fetch_calendar", return_value=actual):
            state = rotate.step(CONFIG, api, "clear")
            self.assertEqual(state["phase"], "deleting")
            self.assertFalse(api.existing)
            state = rotate.step(CONFIG, api)
            self.assertEqual(state["phase"], "deleting")
        with patch.object(canvas, "fetch_calendar", return_value=calendar()):
            state = rotate.step(CONFIG, api)
            self.assertEqual(state["phase"], "disabled")
            state = rotate.step(CONFIG, api)
            self.assertEqual(state["phase"], "disabled")
        self.assertEqual(sum(method == "DELETE" for method, path in api.calls), 1)

    def test_foreign_existing_repository_is_not_adopted(self):
        FakeStore.receipt = {"phase": "new"}
        api = FakeAPI({})
        with patch.object(canvas, "fetch_calendar", return_value=calendar()):
            with self.assertRaises(ValueError):
                rotate.step(CONFIG, api)
        self.assertFalse(any(method in ("DELETE", "POST", "PATCH") for method, path in api.calls))

    def test_budget_failure_preserves_existing_picture(self):
        state = ready_state()
        api = FakeAPI(state)
        config = dict(CONFIG, max_commits_total=1)
        with patch.object(canvas, "fetch_calendar", return_value=canvas.predicted_days(state["plan"])):
            with self.assertRaises(ValueError):
                rotate.step(config, api, "refresh")
        self.assertTrue(api.existing)
        self.assertEqual(FakeStore.receipt["phase"], "ready")

    def test_low_contrast_calibration_is_bounded(self):
        state = ready_state()
        state["calibration_round"] = CONFIG["auto_calibration_attempts"]
        FakeStore.receipt = state
        actual = canvas.predicted_days(state["plan"])
        for day in actual:
            day["level"] = 4
        api = FakeAPI(state)
        with patch.object(canvas, "fetch_calendar", return_value=actual):
            with self.assertRaises(ValueError):
                rotate.step(CONFIG, api)
        self.assertTrue(api.existing)
        self.assertEqual(FakeStore.receipt["phase"], "contrast_insufficient")


if __name__ == "__main__":
    unittest.main()
