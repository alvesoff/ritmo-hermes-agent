#!/usr/bin/env python3
"""The Team gate: the pre-run script of the `ritmo-equipe` agent job (every 5 minutes).

It wakes the LLM only when there is work: a pull request whose current commit
has not been reviewed and whose checks have finished, a merge that was waiting
(checks, active hours) and may go now, or a dev-to-prod promotion that is due.
Otherwise it prints {"wakeAgent": false} and the run costs nothing.

A review handed out is marked `in_review`; if the agent never records a
verdict, the PR is handed out once more after 30 minutes and then left for
the owner, so a failing review cannot wake the LLM every five minutes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import equipe  # noqa: E402
import equipe_core as core  # noqa: E402

SILENT = json.dumps({"wakeAgent": False})
RETRY_AFTER = dt.timedelta(minutes=30)
MAX_ATTEMPTS = 2


def log_error(text: str) -> None:
    try:
        with open(core.path("errors.log"), "a", encoding="utf-8") as handle:
            handle.write(f"--- {core.now_utc().isoformat(timespec='seconds')} equipe_gate\n{text}\n")
    except OSError:
        pass


def gate() -> str:
    team = core.load_team()
    if not team["enabled"] or not team["repos"]:
        return SILENT
    if team["paused_until"] and core.now_utc() < dt.datetime.fromisoformat(team["paused_until"]):
        return SILENT
    if not core.read_json("github.json", {}).get("access_token"):
        return SILENT
    github = core.GitHub(core.github_token())
    with core.locked():
        state = core.load_state()
        now = core.now_utc()
        for record in state["reviews"].values():
            if record.get("verdict") == "in_review" and record.get("attempts", 0) < MAX_ATTEMPTS \
                    and now - dt.datetime.fromisoformat(record["at"]) > RETRY_AFTER:
                record["retry_sha"], record["sha"] = record["sha"], ""
        work = equipe.pending_work(team, github, state, limit=3)
        for item in work:
            if item["type"] != "review":
                continue
            k = equipe.key(item["repo"], item["number"])
            previous = state["reviews"].get(k, {})
            attempts = previous.get("attempts", 0) + 1 if previous.get("retry_sha") == item["sha"] else 1
            state["reviews"][k] = {"sha": item["sha"], "verdict": "in_review", "attempts": attempts,
                                   "at": now.isoformat(timespec="seconds")}
        core.write_json("equipe_state.json", state)
    if not work:
        return SILENT
    return json.dumps({"equipe": work}, separators=(",", ":"))


def main() -> int:
    try:
        output = gate()
    except Exception:  # noqa: BLE001 -- a broken gate stays quiet; errors.log and `team show` tell the owner
        log_error(traceback.format_exc())
        output = SILENT
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
