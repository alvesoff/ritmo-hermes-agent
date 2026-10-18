#!/usr/bin/env python3
"""The routine gate: the pre-run script of the `ritmo-rotinas` agent job.

The job runs every minute, but the LLM only wakes when this script says a
routine is due in the OWNER's timezone: the morning brief, the evening close or
the weekly plan. Otherwise it prints `{"wakeAgent": false}` and the run costs
nothing. Deciding here, with zoneinfo, is what keeps the routines on the
owner's wall clock regardless of the zone Hermes itself runs in.

When a routine is due, the period is marked fired BEFORE the agent runs, so a
crash mid-run skips that brief rather than sending it twice. The JSON printed
is the context the agent turns into the message.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritmo_core as core  # noqa: E402

SILENT = json.dumps({"wakeAgent": False})


def context(config: dict, agenda: dict, routine: str, now: dt.datetime) -> dict:
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    payload = {
        "routine": routine,
        "now": now.isoformat(timespec="minutes"),
        "weekday": core.weekday_key(today),
        "owner": {"name": config["name"], "language": config["language"], "timezone": config["timezone"]},
        "wake": config["wake"],
        "sleep": config["sleep"],
        "quiet": config["quiet"],
        "goals": config["goals"],
        "habits": [
            {"id": h["id"], "text": h["text"], "every_minutes": h["every_minutes"], "start": h["start"], "end": h["end"]}
            for h in config["habits"]
        ],
    }
    if routine == "morning":
        payload["today"] = core.items_between(agenda, config, today, today)
        payload["fixed_today"] = core.fixed_on(config, today)
    elif routine == "evening":
        payload["today"] = core.items_between(agenda, config, today, today)
        payload["tomorrow"] = core.items_between(agenda, config, tomorrow, tomorrow)
        payload["fixed_tomorrow"] = core.fixed_on(config, tomorrow)
    else:
        last_week = today - dt.timedelta(days=7)
        payload["last_7_days"] = core.items_between(agenda, config, last_week, today)
        payload["next_7_days"] = core.items_between(agenda, config, tomorrow, today + dt.timedelta(days=7))
        payload["fixed_by_day"] = {
            (today + dt.timedelta(days=offset)).isoformat(): core.fixed_on(config, today + dt.timedelta(days=offset))
            for offset in range(1, 8)
        }
    return payload


def gate() -> str:
    config = core.load_config()
    if not core.configured(config) or not config["onboarded"]:
        return SILENT
    with core.locked():
        state = core.load_state()
        now = core.now_in(config)
        due = core.routine_due(config, state, now)
        if due is None:
            return SILENT
        routine, period = due
        state["routines"][routine] = period
        core.write_json("state.json", state)
        agenda = core.load_agenda()
    # Compact: Hermes caps the injected script output, and the weekly context is the largest.
    return json.dumps(context(config, agenda, routine, now), ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    try:
        output = gate()
    except Exception:  # noqa: BLE001 -- a broken gate stays quiet; see ritmo_lembretes.py
        core.log_error("ritmo_rotinas", traceback.format_exc())
        output = SILENT
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
