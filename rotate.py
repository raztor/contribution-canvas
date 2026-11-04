#!/usr/bin/env python3
"""Recreate one explicitly authorized, disposable repository to replace artwork."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import canvas

# Deletion is intentionally NOT configurable through arbitrary repository input.
ALLOWED_SOURCE = "raztor/contribution-canvas"
ALLOWED_PIXELS = "raztor/contribution-canvas-pixels"


class GitHub:
    def __init__(self, token):
        if not token:
            raise ValueError("Missing CANVAS_ADMIN_TOKEN (repo + delete_repo); no repository was changed")
        self.token = token

    def api(self, path, method="GET", body=None, missing=False):
        base = "/repos/" + ALLOWED_PIXELS
        allowed_read = method == "GET" and (path == "/user" or path == base or path.startswith(base + "/"))
        allowed_create = method == "POST" and path == "/user/repos" and body and body.get("name") == ALLOWED_PIXELS.split("/")[1]
        allowed_change = method in ("DELETE", "PATCH") and path == base
        if not (allowed_read or allowed_create or allowed_change):
            raise ValueError("Administrative API operation outside the permitted repository")
        if method == "DELETE" and path != "/repos/" + ALLOWED_PIXELS:
            raise ValueError("Only the disposable pixel repository may be deleted")
        req = urllib.request.Request("https://api.github.com" + path, method=method,
            data=json.dumps(body).encode() if body is not None else None, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json", "User-Agent": "Contribution-Canvas/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            # Never print request headers or credential values.
            raise ValueError(f"GitHub API {method} {path} returned HTTP {exc.code}") from None


def validate(config):
    canvas.validate_config(config)
    if config["repository"] != ALLOWED_SOURCE or config["pixels_repository"] != ALLOWED_PIXELS:
        raise ValueError("Repository names differ from the explicitly authorized source and disposable canvas")
    if config["username"] != "raztor":
        raise ValueError("Unexpected profile owner")
    if not isinstance(config["auto_calibration_attempts"], int) or not 0 <= config["auto_calibration_attempts"] <= 4:
        raise ValueError("Invalid automatic calibration limit")


def marker(nonce):
    return {"schema": 1, "disposable": True, "engine": ALLOWED_SOURCE,
            "repository": ALLOWED_PIXELS, "nonce": nonce}


def description(nonce):
    return f"Disposable synthetic artwork | {ALLOWED_SOURCE} | {nonce}"


def safe_to_delete(api, state):
    repo = api.api("/repos/" + ALLOWED_PIXELS, missing=True)
    if repo is None:
        return False
    if (repo["id"] != state["repository_id"] or repo["full_name"] != ALLOWED_PIXELS
            or repo["owner"]["login"] != "raztor" or repo["fork"] or repo["archived"]
            or repo["private"] or repo["description"] != description(state["nonce"])):
        raise ValueError("Disposable repository identity changed; deletion refused")
    branches = api.api("/repos/" + ALLOWED_PIXELS + "/branches?per_page=100")
    if [b["name"] for b in branches] != ["main"] or branches[0]["commit"]["sha"] != state["head"]:
        raise ValueError("Unexpected branch or commit in the pixel repository; deletion refused")
    for suffix in ("/tags?per_page=1", "/releases?per_page=1", "/issues?state=all&per_page=1", "/pulls?state=all&per_page=1"):
        if api.api("/repos/" + ALLOWED_PIXELS + suffix):
            raise ValueError("Non-artwork content found in the pixel repository; deletion refused")
    content = api.api("/repos/" + ALLOWED_PIXELS + "/contents/.canvas-owner.json?ref=main")
    if json.loads(base64.b64decode(content["content"])) != marker(state["nonce"]):
        raise ValueError("Disposable marker changed; deletion refused")
    return True


def bootstrap(store, state):
    files = {
        ".canvas-owner.json": json.dumps(marker(state["nonce"]), sort_keys=True) + "\n",
        "README.md": "# RAZTOR · disposable contribution artwork\n\n"
            "Empty commits in this repository are synthetic artwork, not development activity.\n\n"
            "**This repository is automatically deleted and recreated each week. Do not store work here.**\n\n"
            "Source, configuration, receipts and controls: https://github.com/raztor/contribution-canvas\n",
    }
    entries = ""
    for name, content in sorted(files.items()):
        blob = canvas.git("hash-object", "-w", "--stdin", input=content)
        entries += f"100644 blob {blob}\t{name}\n"
    tree = canvas.git("mktree", input=entries)
    env = dict(os.environ, GIT_AUTHOR_NAME=canvas.BOT_NAME, GIT_AUTHOR_EMAIL=canvas.BOT_EMAIL,
        GIT_COMMITTER_NAME=canvas.BOT_NAME, GIT_COMMITTER_EMAIL=canvas.BOT_EMAIL,
        GIT_AUTHOR_DATE=state["started_at"], GIT_COMMITTER_DATE=state["started_at"])
    base = canvas.git("commit-tree", tree, "-m", "Initialize explicitly disposable artwork", env=env)
    source = store.refs["source_branch"]
    try:
        store.refs["source_branch"] = base
        return store.build(state["plan"])
    finally:
        store.refs["source_branch"] = source


def push_pixels(head, token):
    # Override checkout's source-only credential without exposing secrets in argv or URLs.
    basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
        GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + basic)
    canvas.git("push", "https://github.com/" + ALLOWED_PIXELS + ".git",
               "--force-with-lease=refs/heads/main:", head + ":refs/heads/main", env=env)


def key(config, source):
    return canvas.fingerprint(config, source)


def calibrated(config, round):
    if not 0 <= round <= config["auto_calibration_attempts"]:
        raise ValueError("Invalid calibration round")
    return dict(config, contrast_multiplier=config["contrast_multiplier"] * 1.5 ** round)


def step(config, api, mode="auto"):
    store = canvas.Store(config)
    store.refresh()
    state = store.load()
    days = canvas.fetch_calendar(config["username"])
    phase = state["phase"]
    source = store.refs["source_branch"]
    if phase == "disabled" and mode not in ("resume", "refresh"):
        return state
    if phase in ("published", "ready", "contrast_insufficient"):
        if not canvas.indexed(days, state["plan"]):
            canvas.pending(state, config, "Artwork is still being indexed")
            return state
        metrics = canvas.contrast_metrics(days, state["plan"], config)
        state["metrics"] = metrics
        same_week = days[0]["date"] == state["plan"]["grid_start"]
        unchanged = state["fingerprint"] == key(config, source)
        next_round = 0
        if mode == "auto" and same_week and unchanged and metrics["sufficient"]:
            state["phase"] = "ready"
            store.save(state, canvas.render_svg(days, title="RAZTOR · measured public GitHub calendar"))
            return state
        if mode == "auto" and same_week and unchanged and not metrics["sufficient"]:
            next_round = state.get("calibration_round", 0) + 1
            if next_round > config["auto_calibration_attempts"]:
                state["phase"] = "contrast_insufficient"
                store.save(state, canvas.render_svg(days, title="RAZTOR · contrast below configured minimum"))
                raise ValueError("Bounded automatic contrast calibration exhausted; review target/budget")
            print(f"Measured contrast requires calibration {next_round}; confirming full cleanup first.", flush=True)
        if mode != "clear":
            # Preflight the next budget before removing a working picture.
            baseline = [{**d, "count": d["count"] - state["plan"]["additions"].get(d["date"], 0)} for d in days]
            if any(d["count"] < 0 for d in baseline):
                raise ValueError("Cannot infer a safe preflight baseline")
            canvas.make_plan(baseline, calibrated(config, next_round))
        safe_to_delete(api, state)
        state.update(phase="deleting", before=days, clearing_started_at=canvas.now(), stop=mode == "clear",
                     next_calibration_round=next_round)
        store.save(state, canvas.render_svg(days, title="RAZTOR · replacement requested"))
        phase = "deleting"
    if phase == "deleting":
        if safe_to_delete(api, state):
            api.api("/repos/" + ALLOWED_PIXELS, method="DELETE")
            print("Deleted the confirmed disposable canvas; waiting for public counts to clear.", flush=True)
            return state
        if not canvas.removed(state["before"], days, state["plan"]["additions"]):
            canvas.pending(state, config, "The deleted artwork is still cached", "clearing_started_at")
            return state
        stop = state.get("stop", False) or mode == "clear"
        state = {"phase": "disabled" if stop else "new",
                 "calibration_round": state.get("next_calibration_round", 0)}
        store.save(state, canvas.render_svg(days, title="RAZTOR · previous artwork removed"))
        if stop:
            return state
        phase = "new"
    if phase in ("new", "disabled"):
        if mode == "clear":
            state = {"phase": "disabled"}
            store.save(state, canvas.render_svg(days))
            return state
        if api.api("/repos/" + ALLOWED_PIXELS, missing=True) is not None:
            raise ValueError("A pixel repository exists without an ownership receipt; refusing to modify it")
        round = state.get("calibration_round", 0)
        plan = canvas.make_plan(days, calibrated(config, round))
        state = {"phase": "creating", "plan": plan, "nonce": uuid.uuid4().hex,
                 "started_at": canvas.now(), "fingerprint": key(config, source), "calibration_round": round}
        state["head"] = bootstrap(store, state)
        store.save(state, canvas.render_svg(canvas.predicted_days(plan), title="RAZTOR · estimated preview, awaiting indexing"))
        phase = "creating"
    if phase == "creating":
        repo = api.api("/repos/" + ALLOWED_PIXELS, missing=True)
        if repo is None:
            repo = api.api("/user/repos", method="POST", body={
                "name": ALLOWED_PIXELS.split("/")[1], "private": False,
                "description": description(state["nonce"]),
                "has_issues": True, "has_projects": False, "has_wiki": False,
                "auto_init": False})
        if repo["full_name"] != ALLOWED_PIXELS or repo["description"] != description(state["nonce"]):
            raise ValueError("A different repository appeared during creation; refusing to overwrite it")
        if state.get("repository_id", repo["id"]) != repo["id"]:
            raise ValueError("Repository identity changed during creation")
        state["repository_id"] = repo["id"]
        store.save(state, canvas.render_svg(canvas.predicted_days(state["plan"]), title="RAZTOR · estimated preview, awaiting indexing"))
        # Rebuild the same objects after runner loss; every timestamp is in the receipt.
        head = bootstrap(store, state)
        if head != state["head"]:
            raise ValueError("Prepared artwork did not reproduce deterministically")
        ref = api.api("/repos/" + ALLOWED_PIXELS + "/git/ref/heads/main", missing=True)
        if ref is None:
            push_pixels(head, api.token)
        elif ref["object"]["sha"] != head:
            raise ValueError("Unexpected history appeared during publication")
        api.api("/repos/" + ALLOWED_PIXELS, method="PATCH", body={"default_branch": "main"})
        state["phase"] = "published"
        store.save(state, canvas.render_svg(canvas.predicted_days(state["plan"]), title="RAZTOR · estimated preview, awaiting indexing"))
        print(json.dumps({"phase": "published", "empty_commits": state["plan"]["total"],
                          "repository_id": state["repository_id"]}), flush=True)
        return state
    raise ValueError("Unknown receipt phase: " + phase)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "refresh", "clear", "resume"), default="auto")
    parser.add_argument("--config", default="canvas.json")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--use-gh-session", action="store_true", help="Local use only: use the active GitHub CLI credential")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    validate(config)
    token = os.environ.get("CANVAS_ADMIN_TOKEN")
    if args.use_gh_session:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    api = GitHub(token)
    if api.api("/user")["login"] != config["username"]:
        raise ValueError("The administrative credential belongs to a different owner")
    deadline = time.monotonic() + min(max(args.wait_seconds, 0), 1800)
    mode = args.mode
    while True:
        state = step(config, api, mode)
        mode = "auto"
        Path(".runtime").mkdir(exist_ok=True)
        Path(".runtime/state.json").write_text(json.dumps(state, indent=2) + "\n")
        print("Canvas phase: " + state["phase"], flush=True)
        if state["phase"] in ("ready", "disabled") or time.monotonic() >= deadline:
            break
        time.sleep(min(30, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("Canvas stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
