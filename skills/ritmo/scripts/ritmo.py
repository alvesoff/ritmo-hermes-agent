#!/usr/bin/env python3
"""The one door the agent uses to read and change the owner's routine.

Every command prints JSON. A refusal prints {"error": ...} and exits 1, with a
message that says what to fix. Times are always the OWNER's wall clock; the
agent never converts zones itself.

  ritmo.py now
  ritmo.py config show | set '<json>' | unset <key>
  ritmo.py habit add <id> <text> --every MIN --start HH:MM --end HH:MM [--days mon,tue]
  ritmo.py habit remove <id>
  ritmo.py fixed add <id> <text> --days mon,wed --start HH:MM --end HH:MM [--no-remind]
  ritmo.py fixed remove <id>
  ritmo.py goal add <text> | goal remove <index>
  ritmo.py add <text> (--at 'YYYY-MM-DD HH:MM' | --at HH:MM | --in MIN) [--minutes N] [--urgent] [--plan]
  ritmo.py list [--date YYYY-MM-DD | --date today | --date tomorrow] [--days N]
  ritmo.py done|skip [<id>|last]
  ritmo.py snooze [<id>|last] <minutes>
  ritmo.py move <id> --at 'YYYY-MM-DD HH:MM'
  ritmo.py remove <id>
  ritmo.py plan <YYYY-MM-DD> '<json list of {"text","time","minutes"}>'
  ritmo.py doctor
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritmo_core as core  # noqa: E402
from ritmo_core import RitmoError  # noqa: E402


def out(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def require_timezone(config: dict) -> None:
    if not core.configured(config):
        raise RitmoError("no timezone yet: ask the owner where they live, then "
                         "`ritmo.py config set '{\"timezone\": \"Area/City\"}'`")


def parse_days(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [part.strip().lower()[:3] for part in text.split(",") if part.strip()]


def find(agenda: dict, ref: str) -> dict:
    """An item by id, or `last`: the most recently reminded item still open."""
    if ref == "last":
        reminded = [item for item in agenda["items"] if item.get("reminded_at") and item["status"] == "pending"]
        if not reminded:
            raise RitmoError("no reminded item is open; list the agenda and use an id")
        return max(reminded, key=lambda item: item["reminded_at"])
    for item in agenda["items"]:
        if item["id"] == ref:
            return item
    raise RitmoError(f"no agenda item with id {ref!r}; `ritmo.py list` shows the ids")


def resolve_at(args: argparse.Namespace, config: dict) -> dt.datetime:
    if getattr(args, "in_minutes", None) is not None:
        if args.in_minutes < 1:
            raise RitmoError("--in takes a whole number of minutes, at least 1")
        return core.now_in(config).replace(second=0, microsecond=0) + dt.timedelta(minutes=args.in_minutes)
    if not args.at:
        raise RitmoError("say when: --at 'YYYY-MM-DD HH:MM', --at HH:MM or --in MINUTES")
    return core.parse_when(args.at, config)


# --- config --------------------------------------------------------------------

def cmd_now(args, config):
    require_timezone(config)
    now = core.now_in(config)
    out({"now": now.isoformat(timespec="minutes"), "weekday": core.weekday_key(now.date()),
         "timezone": config["timezone"], "quiet_now": core.in_quiet(now, config)})


def cmd_config(args, config):
    if args.action == "show":
        out(config)
        return
    if args.action == "set":
        try:
            update = json.loads(args.value or "")
        except ValueError as error:
            raise RitmoError(f"config set takes a JSON object: {error}") from error
        if not isinstance(update, dict):
            raise RitmoError("config set takes a JSON object")
        unknown = sorted(set(update) - set(core.DEFAULT_CONFIG))
        if unknown:
            raise RitmoError(f"unknown config keys {unknown}; valid keys: {sorted(core.DEFAULT_CONFIG)}")
        saved = core.save_config(core.deep_merge(config, update))
        out({"saved": update, "config": saved})
        return
    if args.action == "unset":
        if args.value not in core.DEFAULT_CONFIG or args.value == "version":
            raise RitmoError(f"unknown config key {args.value!r}")
        config[args.value] = core.DEFAULT_CONFIG[args.value]
        out({"reset": args.value, "config": core.save_config(config)})


def cmd_habit(args, config):
    habits = [habit for habit in config["habits"] if habit["id"] != args.id]
    if args.action == "add":
        habit = {"id": args.id, "text": args.text, "every_minutes": args.every,
                 "start": args.start, "end": args.end}
        days = parse_days(args.days)
        if days:
            habit["days"] = days
        habits.append(habit)
    elif len(habits) == len(config["habits"]):
        raise RitmoError(f"no habit with id {args.id!r}")
    config["habits"] = habits
    saved = core.save_config(config)
    out({"habits": saved["habits"]})


def cmd_fixed(args, config):
    fixed = [item for item in config["fixed"] if item["id"] != args.id]
    if args.action == "add":
        fixed.append({"id": args.id, "text": args.text, "days": parse_days(args.days) or [],
                      "start": args.start, "end": args.end, "remind": not args.no_remind})
    elif len(fixed) == len(config["fixed"]):
        raise RitmoError(f"no fixed commitment with id {args.id!r}")
    config["fixed"] = fixed
    saved = core.save_config(config)
    out({"fixed": saved["fixed"]})


def cmd_goal(args, config):
    if args.action == "add":
        config["goals"].append(args.value)
    else:
        try:
            config["goals"].pop(int(args.value))
        except (ValueError, IndexError) as error:
            raise RitmoError("goal remove takes the goal's index from `config show`") from error
    out({"goals": core.save_config(config)["goals"]})


# --- agenda --------------------------------------------------------------------

def cmd_add(args, config):
    require_timezone(config)
    at = resolve_at(args, config)
    if at < core.now_in(config) - dt.timedelta(minutes=1):
        raise RitmoError(f"{at.strftime('%Y-%m-%d %H:%M')} is in the past for the owner "
                         f"(now {core.now_in(config).strftime('%Y-%m-%d %H:%M')})")
    item = {"id": core.new_id(), "text": args.text.strip(), "at": at.isoformat(timespec="minutes"),
            "status": "pending", "source": "plan" if args.plan else "owner",
            "created": core.now_in(config).isoformat(timespec="seconds")}
    if not item["text"]:
        raise RitmoError("the reminder needs a text")
    if args.minutes:
        item["minutes"] = args.minutes
    if args.urgent:
        item["urgent"] = True
    with core.locked():
        agenda = core.load_agenda()
        agenda["items"].append(item)
        core.write_json("agenda.json", agenda)
    view = core.item_view(item, config)
    if item["source"] == "plan" and core.in_quiet(at, config) and not args.urgent:
        view["warning"] = "inside quiet hours: it will wait until they end unless marked --urgent"
    out({"added": view})


def cmd_list(args, config):
    require_timezone(config)
    today = core.now_in(config).date()
    if args.date in (None, "today"):
        first = today
    elif args.date == "tomorrow":
        first = today + dt.timedelta(days=1)
    else:
        try:
            first = dt.date.fromisoformat(args.date)
        except ValueError as error:
            raise RitmoError("--date takes YYYY-MM-DD, today or tomorrow") from error
    last = first + dt.timedelta(days=max(args.days, 1) - 1)
    agenda = core.load_agenda()
    days = {}
    for offset in range((last - first).days + 1):
        day = first + dt.timedelta(days=offset)
        days[day.isoformat()] = {"weekday": core.weekday_key(day), "fixed": core.fixed_on(config, day),
                                 "items": core.items_between(agenda, config, day, day)}
    out({"days": days, "habits": config["habits"]})


def change(ref: str, mutate) -> dict:
    with core.locked():
        agenda = core.load_agenda()
        item = find(agenda, ref)
        mutate(item)
        core.write_json("agenda.json", agenda)
    return item


def cmd_status(args, config):
    status = {"done": "done", "skip": "skipped"}[args.command]
    item = change(args.ref, lambda item: item.update(status=status))
    out({status: core.item_view(item, config)})


def cmd_snooze(args, config):
    require_timezone(config)
    if args.minutes < 1:
        raise RitmoError("snooze takes a whole number of minutes, at least 1")
    at = core.now_in(config).replace(second=0, microsecond=0) + dt.timedelta(minutes=args.minutes)

    def mutate(item):
        item.update(at=at.isoformat(timespec="minutes"), status="pending", reminded_at=None)

    out({"snoozed": core.item_view(change(args.ref, mutate), config)})


def cmd_move(args, config):
    require_timezone(config)
    at = core.parse_when(args.at, config)

    def mutate(item):
        item.update(at=at.isoformat(timespec="minutes"), status="pending", reminded_at=None)

    out({"moved": core.item_view(change(args.id, mutate), config)})


def cmd_remove(args, config):
    with core.locked():
        agenda = core.load_agenda()
        item = find(agenda, args.id)
        agenda["items"].remove(item)
        core.write_json("agenda.json", agenda)
    out({"removed": core.item_view(item, config)})


def cmd_plan(args, config):
    """Replace the planner's open items on one day; the owner's own reminders stay."""
    require_timezone(config)
    try:
        day = dt.date.fromisoformat(args.date)
        entries = json.loads(args.items)
    except ValueError as error:
        raise RitmoError(f"plan takes a date and a JSON list: {error}") from error
    if not isinstance(entries, list):
        raise RitmoError("plan takes a JSON list of {\"text\", \"time\", \"minutes\"}")
    now = core.now_in(config)
    tz = core.zone(config)
    fresh = []
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
            raise RitmoError("each plan entry needs a text")
        at = core.at_local(day, core.check_time(entry.get("time"), "plan time"), tz)
        item = {"id": core.new_id(), "text": str(entry["text"]).strip(), "at": at.isoformat(timespec="minutes"),
                "status": "pending", "source": "plan", "created": now.isoformat(timespec="seconds")}
        if entry.get("minutes"):
            item["minutes"] = int(entry["minutes"])
        if entry.get("urgent"):
            item["urgent"] = True
        fresh.append(item)
    with core.locked():
        agenda = core.load_agenda()
        kept = [item for item in agenda["items"]
                if not (item.get("source") == "plan" and item["status"] == "pending"
                        and dt.datetime.fromisoformat(item["at"]).astimezone(tz).date() == day
                        and dt.datetime.fromisoformat(item["at"]) > now)]
        agenda["items"] = kept + fresh
        core.write_json("agenda.json", agenda)
    quiet = [core.item_view(item, config) for item in fresh
             if core.in_quiet(dt.datetime.fromisoformat(item["at"]), config) and not item.get("urgent")]
    result = {"planned": day.isoformat(), "items": core.items_between(agenda, config, day, day)}
    if quiet:
        result["warning"] = {"inside_quiet_hours": quiet}
    out(result)


def cmd_doctor(args, config):
    report = {"configured": core.configured(config), "onboarded": config["onboarded"],
              "data_dir": core.data_dir(), "errors": ""}
    try:
        with open(core.path("errors.log"), encoding="utf-8", errors="replace") as handle:
            report["errors"] = handle.read()[-2000:]
    except FileNotFoundError:
        pass
    agenda = core.load_agenda()
    report["agenda_items"] = len(agenda["items"])
    report["pending"] = sum(1 for item in agenda["items"] if item["status"] == "pending")
    out(report)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ritmo.py", description="Read and change the owner's routine.")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("now")
    sub.add_parser("doctor")

    config = sub.add_parser("config")
    config.add_argument("action", choices=["show", "set", "unset"])
    config.add_argument("value", nargs="?")

    habit = sub.add_parser("habit")
    habit.add_argument("action", choices=["add", "remove"])
    habit.add_argument("id")
    habit.add_argument("text", nargs="?", default="")
    habit.add_argument("--every", type=int)
    habit.add_argument("--start")
    habit.add_argument("--end")
    habit.add_argument("--days")

    fixed = sub.add_parser("fixed")
    fixed.add_argument("action", choices=["add", "remove"])
    fixed.add_argument("id")
    fixed.add_argument("text", nargs="?", default="")
    fixed.add_argument("--days")
    fixed.add_argument("--start")
    fixed.add_argument("--end")
    fixed.add_argument("--no-remind", action="store_true")

    goal = sub.add_parser("goal")
    goal.add_argument("action", choices=["add", "remove"])
    goal.add_argument("value")

    add = sub.add_parser("add")
    add.add_argument("text")
    add.add_argument("--at")
    add.add_argument("--in", dest="in_minutes", type=int)
    add.add_argument("--minutes", type=int)
    add.add_argument("--urgent", action="store_true")
    add.add_argument("--plan", action="store_true", help="placed by the planner, not asked for by the owner")

    listing = sub.add_parser("list")
    listing.add_argument("--date")
    listing.add_argument("--days", type=int, default=1)

    for name in ("done", "skip"):
        status = sub.add_parser(name)
        status.add_argument("ref", nargs="?", default="last")

    snooze = sub.add_parser("snooze")
    snooze.add_argument("ref", nargs="?", default="last")
    snooze.add_argument("minutes", type=int)

    move = sub.add_parser("move")
    move.add_argument("id")
    move.add_argument("--at", required=True)

    remove = sub.add_parser("remove")
    remove.add_argument("id")

    plan = sub.add_parser("plan")
    plan.add_argument("date")
    plan.add_argument("items")
    return root


COMMANDS = {
    "now": cmd_now, "config": cmd_config, "habit": cmd_habit, "fixed": cmd_fixed, "goal": cmd_goal,
    "add": cmd_add, "list": cmd_list, "done": cmd_status, "skip": cmd_status, "snooze": cmd_snooze,
    "move": cmd_move, "remove": cmd_remove, "plan": cmd_plan, "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = core.load_config()
        COMMANDS[args.command](args, config)
    except RitmoError as error:
        out({"error": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
