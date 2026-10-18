#!/usr/bin/env python3
"""The reminder clock: a `no_agent` cron job that runs every minute.

Whatever this prints is delivered to the owner verbatim, with no LLM in the
loop; printing nothing is a silent tick. So a reminder costs no tokens and
arrives on the minute it was set for.

It never fails loudly. A non-zero exit would put a technical error alert in
the owner's chat every minute, which is worse than a missed reminder; errors
go to ritmo/errors.log instead, where `ritmo.py doctor` shows them.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritmo_core as core  # noqa: E402


def compose(config: dict, items: list[dict], habits: list, fixed: list, missed: list[dict]) -> str:
    words = core.texts(config)
    tz = core.zone(config)
    lines = []
    for item, start in fixed:
        lines.append(words["fixed"].format(time=start.strftime("%H:%M"), text=item["text"]))
    for item in items:
        at = dt.datetime.fromisoformat(item["at"]).astimezone(tz)
        line = words["reminder"].format(time=at.strftime("%H:%M"), text=item["text"])
        if item.get("minutes"):
            line += words["duration"].format(minutes=item["minutes"])
        lines.append(line)
    for habit, _slot in habits:
        lines.append(words["habit"].format(text=habit["text"]))
    if missed:
        names = ", ".join(
            f"{dt.datetime.fromisoformat(item['at']).astimezone(tz).strftime('%H:%M')} {item['text']}"
            for item in missed)
        lines.append(words["missed"].format(items=names))
    if items:
        lines.append(words["footer"])
    return "\n".join(lines)


def tick() -> str:
    config = core.load_config()
    if not core.configured(config):
        return ""
    with core.locked():
        agenda = core.load_agenda()
        state = core.load_state()
        now = core.now_in(config)
        items, missed = core.due_items(config, agenda, now)
        habits = core.due_habits(config, state, now)
        fixed = core.due_fixed(config, state, now)
        if not (items or missed or habits or fixed):
            return ""
        stamp = now.isoformat(timespec="seconds")
        for item in items:
            item["reminded_at"] = stamp
        for item in missed:
            item["status"] = "missed"
        for habit, slot in habits:
            state["habits"][habit["id"]] = slot.isoformat(timespec="seconds")
        for item, start in fixed:
            state["fixed"][item["id"]] = start.isoformat(timespec="seconds")
        # State first: a crash between the two writes then loses a reminder
        # instead of sending it twice.
        core.write_json("state.json", state)
        core.write_json("agenda.json", agenda)
    return compose(config, items, habits, fixed, missed)


def main() -> int:
    try:
        message = tick()
    except Exception:  # noqa: BLE001 -- see the module docstring
        core.log_error("ritmo_lembretes", traceback.format_exc())
        return 0
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
