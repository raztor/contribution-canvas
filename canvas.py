#!/usr/bin/env python3
"""An isolated, replaceable contribution artwork. Python 3.11+, no dependencies."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

UTC = dt.timezone.utc
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
FONT = {
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    " ": ["000"] * 7,
}
PALETTES = {
    "dark": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
    "light": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
}


def now():
    return dt.datetime.now(UTC).isoformat()


def git(*args, input=None, env=None):
    result = subprocess.run(
        ["git", *args], input=input, capture_output=True,
        text=not isinstance(input, bytes), env=env, check=True,
    )
    return result.stdout.strip()


class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = {}
        self.counts = {}
        self.tooltip = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-date" in attrs and "data-level" in attrs:
            self.cells[attrs["id"]] = attrs
        if tag == "tool-tip":
            self.tooltip = attrs.get("for")
            self.parts = []

    def handle_data(self, data):
        if self.tooltip:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.tooltip:
            text = "".join(self.parts).strip()
            match = re.match(r"([\d,]+) contributions? on ", text)
            if match:
                self.counts[self.tooltip] = int(match[1].replace(",", ""))
            elif text.startswith("No contributions on "):
                self.counts[self.tooltip] = 0
            self.tooltip = None


def parse_calendar(html):
    parser = CalendarParser()
    parser.feed(html)
    days = []
    for cell_id, cell in parser.cells.items():
        if cell_id not in parser.counts:
            raise ValueError("GitHub calendar format changed: missing count tooltip")
        date = dt.date.fromisoformat(cell["data-date"])
        level = int(cell["data-level"])
        count = parser.counts[cell_id]
        if not 0 <= level <= 4 or (count == 0) != (level == 0):
            raise ValueError("Invalid contribution count or level")
        days.append({"date": date.isoformat(), "count": count, "level": level})
    days.sort(key=lambda day: day["date"])
    if not 350 <= len(days) <= 371 or len({d["date"] for d in days}) != len(days):
        raise ValueError("Incomplete or duplicated contribution calendar")
    start = dt.date.fromisoformat(days[0]["date"])
    if start.weekday() != 6:
        raise ValueError("Calendar does not start on Sunday")
    if any(dt.date.fromisoformat(d["date"]) != start + dt.timedelta(days=i)
           for i, d in enumerate(days)):
        raise ValueError("Calendar has missing dates")
    return days


def fetch_calendar(username):
    url = f"https://github.com/users/{username}/contributions"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Contribution-Canvas/1.0",
        "Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return parse_calendar(response.read().decode("utf-8"))
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def validate_config(config):
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise ValueError("Invalid repository")
    if config["repository"].split("/")[0].lower() != config["username"].lower():
        raise ValueError("Canvas must belong to the contribution owner")
    if any(c not in FONT for c in config["text"]):
        raise ValueError("Unsupported character in text")
    if not config["text"].strip():
        raise ValueError("Text cannot be empty")
    if len(set(config[k] for k in ("source_branch", "canvas_branch", "state_branch"))) != 3:
        raise ValueError("Engine, canvas and state branches must be different")
    for key in ("source_branch", "canvas_branch", "state_branch"):
        git("check-ref-format", "refs/heads/" + config[key])
    if not re.fullmatch(r"[^<>\s]+@[^<>\s]+", config["email"]):
        raise ValueError("Invalid author email")
    if not 1.1 <= config["contrast_multiplier"] <= 10:
        raise ValueError("Invalid contrast multiplier")
    if not (1 <= config["max_commits_per_day"] <= 10000 and
            1 <= config["max_commits_total"] <= 1000000):
        raise ValueError("Invalid commit budget")
    if not (0 < config["minimum_edge_success"] <= 1 and
            1 <= config["minimum_level_gap"] <= 4):
        raise ValueError("Invalid contrast criteria")


def make_plan(days, config):
    start = dt.date.fromisoformat(days[0]["date"])
    last = dt.date.fromisoformat(days[-1]["date"])
    weeks = (last - start).days // 7 + 1
    width = sum(len(FONT[c][0]) for c in config["text"]) + len(config["text"]) - 1
    left = (weeks - width) // 2
    if left < 1 or left + width >= weeks:
        raise ValueError("Text does not fit with a one-week margin")
    pixels = set()
    x = left
    for char in config["text"]:
        for row, line in enumerate(FONT[char]):
            for col, value in enumerate(line):
                if value == "1":
                    date = start + dt.timedelta(days=(x + col) * 7 + row)
                    if date > last:
                        raise ValueError("Text would require future commits")
                    pixels.add(date.isoformat())
        x += len(FONT[char][0]) + 1
    # Conservative initial estimate; the public graph is measured after indexing.
    target = max(8, math.ceil(max(d["count"] for d in days) * config["contrast_multiplier"]))
    additions = {d["date"]: max(0, target - d["count"]) for d in days if d["date"] in pixels}
    if max(additions.values(), default=0) > config["max_commits_per_day"]:
        raise ValueError("Per-day commit budget exceeded; no history was changed")
    if sum(additions.values()) > config["max_commits_total"]:
        raise ValueError("Total commit budget exceeded; no history was changed")
    return {"grid_start": start.isoformat(), "grid_end": last.isoformat(),
            "weeks": weeks, "left": left, "width": width,
            "target": target, "pixels": sorted(pixels), "additions": additions,
            "total": sum(additions.values()), "baseline": days}


def contrast_metrics(days, plan, config):
    cells = {d["date"]: d for d in days}
    ink = set(plan["pixels"])
    edges = []
    start = dt.date.fromisoformat(plan["grid_start"])
    for value in ink:
        if value not in cells:
            continue
        date = dt.date.fromisoformat(value)
        offset = (date - start).days
        for delta in (-7, 7, -1, 1):
            # Vertical neighbours cannot wrap from Saturday into Sunday.
            if (delta == -1 and offset % 7 == 0) or (delta == 1 and offset % 7 == 6):
                continue
            other = (date + dt.timedelta(days=delta)).isoformat()
            if other in cells and other not in ink:
                edges.append(cells[value]["level"] - cells[other]["level"])
    success = sum(gap >= config["minimum_level_gap"] for gap in edges) / max(1, len(edges))
    return {"edge_success": round(success, 4), "edges": len(edges),
            "minimum_ink_level": min((cells[v]["level"] for v in ink if v in cells), default=0),
            "sufficient": bool(edges) and success >= config["minimum_edge_success"]}


def render_svg(days, plan=None, title="RAZTOR · contribution artwork"):
    start = dt.date.fromisoformat(days[0]["date"])
    weeks = (dt.date.fromisoformat(days[-1]["date"]) - start).days // 7 + 1
    width = weeks * 14 + 28
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="154" viewBox="0 0 {width} 154">',
             '<style>text{font-family:system-ui,sans-serif;fill:#c9d1d9}</style>',
             f'<rect width="{width}" height="154" rx="8" fill="#0d1117"/>',
             f'<text x="14" y="22" font-size="13">{escape(title)}</text>']
    for day in days:
        index = (dt.date.fromisoformat(day["date"]) - start).days
        level = day["level"]
        parts.append(f'<rect x="{14 + index // 7 * 14}" y="{34 + index % 7 * 14}" width="11" height="11" rx="2" fill="{PALETTES["dark"][level]}"><title>{day["date"]}: {day["count"]} contributions, level {level}</title></rect>')
    parts.append('<text x="14" y="145" font-size="10">Public GitHub calendar · synthetic commits are labelled as artwork</text></svg>')
    return "\n".join(parts)


def predicted_days(plan):
    maximum = max(d["count"] + plan["additions"].get(d["date"], 0) for d in plan["baseline"])
    result = []
    for day in plan["baseline"]:
        count = day["count"] + plan["additions"].get(day["date"], 0)
        result.append({"date": day["date"], "count": count,
                       "level": 0 if count == 0 else min(4, math.ceil(count / max(1, maximum) * 4))})
    return result


def removed(before, after, additions):
    old = {d["date"]: d["count"] for d in before}
    new = {d["date"]: d["count"] for d in after}
    return all(date not in new or (date in old and new[date] <= old[date] - count)
               for date, count in additions.items() if count)


def indexed(days, plan):
    counts = {d["date"]: d["count"] for d in days}
    return all(counts.get(d["date"], -1) >= d["count"] + plan["additions"].get(d["date"], 0)
               for d in plan["baseline"] if plan["additions"].get(d["date"], 0))


class Store:
    def __init__(self, config):
        self.config = config
        self.refs = {}

    def refresh(self):
        remote = git("remote", "get-url", "origin")
        expected = self.config["repository"].lower()
        if remote.lower() not in (f"https://github.com/{expected}.git", f"git@github.com:{expected}.git",
                                   f"https://github.com/{expected}"):
            raise ValueError("Origin does not match the dedicated repository")
        git("fetch", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
        self.refs = {}
        for key in ("source_branch", "canvas_branch", "state_branch"):
            ref = "refs/remotes/origin/" + self.config[key]
            try:
                self.refs[key] = git("rev-parse", "--verify", ref)
            except subprocess.CalledProcessError:
                self.refs[key] = ""
        if not self.refs["source_branch"]:
            raise ValueError("The engine branch must exist")

    def load(self):
        if not self.refs.get("state_branch"):
            return {"phase": "new"}
        state = json.loads(git("show", self.refs["state_branch"] + ":state.json"))
        if state.get("schema") != 1 or state.get("repository") != self.config["repository"]:
            raise ValueError("Unrecognized state; refusing to replace history")
        return state

    def commit_tree(self, tree, parent, message):
        env = dict(os.environ, GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL,
                   GIT_COMMITTER_NAME=BOT_NAME, GIT_COMMITTER_EMAIL=BOT_EMAIL)
        return git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message, env=env)

    def save(self, state, svg=None):
        state.update(schema=1, repository=self.config["repository"], updated_at=now())
        blob = git("hash-object", "-w", "--stdin", input=json.dumps(state, indent=2) + "\n")
        entries = f"100644 blob {blob}\tstate.json\n"
        if svg is not None:
            blob = git("hash-object", "-w", "--stdin", input=svg)
            entries += f"100644 blob {blob}\tpreview.svg\n"
        tree = git("mktree", input=entries)
        previous = self.refs.get("state_branch", "")
        sha = self.commit_tree(tree, previous, "Record contribution canvas: " + state["phase"])
        self.push("state_branch", sha, previous)

    def push(self, branch_key, sha, expected):
        branch = self.config[branch_key]
        git("push", "origin", f"--force-with-lease=refs/heads/{branch}:{expected}",
            f"{sha}:refs/heads/{branch}")
        self.refs[branch_key] = sha

    def build(self, plan):
        source = self.refs["source_branch"]
        pieces = []
        previous = source
        mark = 0
        for date, count in sorted(plan["additions"].items()):
            stamp = int(dt.datetime.fromisoformat(date + "T12:00:00+00:00").timestamp())
            for index in range(count):
                mark += 1
                message = f"Synthetic contribution artwork: {self.config['text']} | {date} | {index + 1}/{count}\n"
                identity = f"{self.config['text']} <{self.config['email']}> {stamp + index} +0000"
                pieces.append(f"commit refs/canvas/build\nmark :{mark}\nauthor {identity}\ncommitter {identity}\ndata {len(message.encode())}\n{message}from {previous}\n\n")
                previous = f":{mark}"
        if not pieces:
            raise ValueError("Plan has no synthetic commits")
        git("fast-import", "--quiet", input="".join(pieces).encode())
        head = git("rev-parse", "refs/canvas/build")
        git("update-ref", "-d", "refs/canvas/build")
        if git("rev-parse", head + "^{tree}") != git("rev-parse", source + "^{tree}"):
            raise ValueError("Synthetic commits unexpectedly modified files")
        return head


def fingerprint(config, source):
    return hashlib.sha256((json.dumps(config, sort_keys=True) + source).encode()).hexdigest()


def pending(state, config, message, field="started_at"):
    elapsed = dt.datetime.now(UTC) - dt.datetime.fromisoformat(state[field])
    if elapsed.total_seconds() > config["indexing_timeout_hours"] * 3600:
        raise ValueError(message + "; indexing timeout reached. No additional commits were generated.")
    print(message + "; retry on the next scheduled run. No additional commits were generated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview",))
    parser.add_argument("--config", default="canvas.json")
    parser.add_argument("--html", help="Read a saved public calendar instead of the network")
    parser.add_argument("--output", default=".runtime")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    validate_config(config)
    days = parse_calendar(Path(args.html).read_text()) if args.html else fetch_calendar(config["username"])
    plan = make_plan(days, config)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (out / "before.svg").write_text(render_svg(days, title="RAZTOR · actual public calendar before artwork"))
    (out / "preview.svg").write_text(render_svg(predicted_days(plan), title="RAZTOR · estimated artwork preview"))
    print(json.dumps({k: plan[k] for k in ("grid_start", "grid_end", "weeks", "left", "width", "target", "total")}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Canvas stopped: {exc}", file=sys.stderr)
        sys.exit(1)
