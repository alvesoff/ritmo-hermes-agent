#!/usr/bin/env python3
"""The Team gate: the pre-run script of the `ritmo-equipe` agent job (every 5 minutes).

What needs no judgment happens here, in code:
- a PR that was approved but waited (checks, active hours, conflicts) is merged
  as soon as nothing blocks it;
- a due dev-to-prod promotion is opened and merged, carrying only commits Ritmo
  reviewed.

The LLM wakes only for what needs it: a PR to review, or news for the owner
(a merge or promotion that happened, a new blocker, a review that keeps failing,
a repository or connection that broke). Otherwise this prints
{"wakeAgent": false} and the run costs nothing. The same blocker is reported
once, not every five minutes.
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
# Blockers that clear by themselves within minutes: never worth a message.
TRANSIENT = ("checks are still running", "GitHub is still computing", "no checks have started yet")
RETRY_AFTER = dt.timedelta(minutes=30)
MAX_ATTEMPTS = 2


def log_error(text: str) -> None:
    try:
        with open(core.path("errors.log"), "a", encoding="utf-8") as handle:
            handle.write(f"--- {core.now_utc().isoformat(timespec='seconds')} equipe_gate\n{text}\n")
    except OSError:
        pass


def worth_telling(blockers: list[str]) -> list[str]:
    return [b for b in blockers if not b.startswith(TRANSIENT)]


def report_problems(state: dict, found: dict, notes: list) -> None:
    """A problem is told once when it appears and once when it clears."""
    for where, message in found.items():
        before = state["problems"].get(where, "")
        if message and message != before:
            notes.append({"type": "problem", "where": where, "message": message})
        elif not message and before:
            notes.append({"type": "problem_solved", "where": where})
        if message:
            state["problems"][where] = message
        else:
            state["problems"].pop(where, None)


def waiting_merges(team: dict, github: core.GitHub, state: dict, notes: list) -> None:
    for review_key, record in list(state["reviews"].items()):
        if record.get("verdict") != "ok_waiting":
            continue
        repo_name, number = review_key.rsplit("#", 1)
        repo = next((r for r in team["repos"] if r["repo"].lower() == repo_name), None)
        if repo is None:
            continue
        try:
            result = equipe.try_merge(team, repo, github, state, int(number), record["sha"])
        except core.EquipeError as error:
            result = {"merged": False, "blockers": [f"GitHub error, will retry: {error}"]}
        if result.get("merged"):
            record.update(verdict="ok_merged", blockers=[])
            notes.append({"type": "merged", "repo": repo["repo"], "number": int(number), "into": result.get("into")})
        elif any("new commits" in b for b in result["blockers"]):
            record["verdict"] = "stale"   # its new head is a new review
        elif worth_telling(result["blockers"]) != worth_telling(record.get("blockers", [])):
            record["blockers"] = result["blockers"]
            if worth_telling(result["blockers"]):
                notes.append({"type": "waiting", "repo": repo["repo"], "number": int(number),
                              "blockers": worth_telling(result["blockers"])})


def due_promotions(team: dict, github: core.GitHub, state: dict, notes: list, problems: dict) -> None:
    config = core.ritmo_config()
    for repo in team["repos"]:
        if not core.promotion_due(repo, config, state):
            continue
        try:
            result = equipe.promote(team, repo, github, state)
        except core.EquipeError as error:
            problems[repo["repo"].lower()] = str(error)
            continue
        last = state.setdefault("promotion_blockers", {})
        if result.get("promoted"):
            last.pop(repo["repo"].lower(), None)
            notes.append({"type": "promoted", "repo": repo["repo"], "into": repo["prod"], "pr": result.get("promotion_pr")})
        elif worth_telling(result.get("blockers", [])) and worth_telling(result["blockers"]) != last.get(repo["repo"].lower()):
            last[repo["repo"].lower()] = worth_telling(result["blockers"])
            notes.append({"type": "promotion_waiting", "repo": repo["repo"], "blockers": worth_telling(result["blockers"])})


def gate() -> str:
    team = core.load_team()
    if not team["enabled"] or not team["repos"]:
        return SILENT
    if team["paused_until"] and core.now_utc() < core.parse_moment(team["paused_until"], "paused_until"):
        return SILENT
    if not core.read_json("github.json", {}).get("access_token"):
        return SILENT
    notes: list[dict] = []
    problems: dict = {}
    with core.locked():
        state = core.load_state()
        try:
            github = core.GitHub(core.github_token())
            problems["github"] = ""
        except core.EquipeError as error:
            report_problems(state, {"github": str(error)}, notes)
            core.write_json("equipe_state.json", state)
            return json.dumps({"equipe": [], "notices": notes}, separators=(",", ":")) if notes else SILENT
        now = core.now_utc()
        for review_key, record in state["reviews"].items():
            if record.get("verdict") != "in_review" or now - dt.datetime.fromisoformat(record["at"]) <= RETRY_AFTER:
                continue
            if record.get("attempts", 0) < MAX_ATTEMPTS:
                record["retry"] = True
            else:
                record["verdict"] = "stuck"
                notes.append({"type": "review_failed", "pr": review_key})
        waiting_merges(team, github, state, notes)
        due_promotions(team, github, state, notes, problems)
        items = equipe.review_items(team, github, state, limit=3, problems=problems)
        for item in items:
            review_key = equipe.key(item["repo"], item["number"])
            previous = state["reviews"].get(review_key, {})
            attempts = previous.get("attempts", 0) + 1 if previous.get("retry") and previous.get("sha") == item["sha"] else 1
            state["reviews"][review_key] = {"sha": item["sha"], "verdict": "in_review", "attempts": attempts,
                                            "at": now.isoformat(timespec="seconds")}
        if items:
            state["last_handout"] = now.isoformat(timespec="seconds")
        report_problems(state, problems, notes)
        core.write_json("equipe_state.json", state)
    if not items and not notes:
        return SILENT
    return json.dumps({"equipe": items, "notices": notes}, separators=(",", ":"))


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
