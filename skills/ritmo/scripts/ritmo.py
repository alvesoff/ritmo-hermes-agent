#!/usr/bin/env python3
"""The one door the agent uses to read and change the owner's routine.

Every command prints JSON. A refusal prints {"error": ...} and exits 1, with a
message that says what to fix; nothing changed. Times are always the OWNER's
wall clock; the agent never converts zones itself.

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
  ritmo.py export [--send]
  ritmo.py import --from-chat | import '<RITMO-BACKUP-1:...>'
  ritmo.py doctor
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritmo_core as core  # noqa: E402
from ritmo_core import RitmoError  # noqa: E402

# A planned or moved item may start this long ago and still count as "now".
PAST_TOLERANCE = dt.timedelta(minutes=1)


def out(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def ready() -> dict:
    """The validated config of an owner whose timezone is known."""
    config = core.load_config()
    if not core.configured(config):
        raise RitmoError("no timezone yet: ask the owner where they live, then "
                         "`ritmo.py config set '{\"timezone\": \"Area/City\"}'`")
    return config


def parse_days(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [part.strip().lower()[:3] for part in text.split(",") if part.strip()]


def not_past(at: dt.datetime, config: dict) -> None:
    now = core.now_in(config)
    if core.utc(at) < core.utc(now) - PAST_TOLERANCE:
        raise RitmoError(f"{at.strftime('%Y-%m-%d %H:%M')} is in the past for the owner "
                         f"(now {now.strftime('%Y-%m-%d %H:%M')})")


def find(agenda: dict, ref: str, config: dict) -> dict:
    """An item by id, or `last`: the item the latest reminder message was about.

    Only a reminder from the last few hours counts, and only when that message
    named a single item: an old unanswered reminder, or a message listing two
    items, needs an id instead of a guess.
    """
    if ref != "last":
        for item in agenda["items"]:
            if item["id"] == ref:
                return item
        raise RitmoError(f"no agenda item with id {ref!r}; `ritmo.py list` shows the ids")
    reminded = [item for item in agenda["items"] if item.get("reminded_at") and item["status"] == "pending"]
    if not reminded:
        raise RitmoError("no reminded item is open; a habit nudge needs no answer, otherwise use an id from `list`")
    latest = max(item["reminded_at"] for item in reminded)
    if core.utc(core.now_in(config)) - core.utc(dt.datetime.fromisoformat(latest)) > core.LAST_REMINDER_WINDOW:
        raise RitmoError("the last reminder was hours ago; use an id from `ritmo.py list`")
    same = [item for item in reminded if item["reminded_at"] == latest]
    if len(same) > 1:
        options = ", ".join(f"{item['id']} ({item['text']})" for item in same)
        raise RitmoError(f"the last reminder named several items: {options}; ask which one and use its id")
    return same[0]


def resolve_at(args: argparse.Namespace, config: dict) -> dt.datetime:
    if args.in_minutes is not None:
        if args.in_minutes < 1:
            raise RitmoError("--in takes a whole number of minutes, at least 1")
        return core.plus_minutes(core.now_in(config).replace(second=0, microsecond=0), args.in_minutes)
    if not args.at:
        raise RitmoError("say when: --at 'YYYY-MM-DD HH:MM', --at HH:MM or --in MINUTES")
    return core.parse_when(args.at, config)


# --- config --------------------------------------------------------------------

def cmd_now(args):
    config = ready()
    now = core.now_in(config)
    out({"now": now.isoformat(timespec="minutes"), "weekday": core.weekday_key(now.date()),
         "timezone": config["timezone"], "quiet_now": core.in_quiet(now, config)})


def cmd_config(args):
    if args.action == "show":
        try:
            config = core.load_config()
        except RitmoError as error:
            # A set-up owner with one bad field is repaired, never re-onboarded.
            out({"setup": {"needed": False, "repair": True}, "invalid": str(error),
                 "saved": core.read_json("config.json", {})})
            return
        out({"setup": core.setup_status(config), "config": config})
        return
    with core.locked():
        # The raw file, not the validated one: these two commands are also
        # how a broken config gets repaired.
        saved = core.read_json("config.json", {})
        old_zone = saved.get("timezone") if isinstance(saved.get("timezone"), str) else ""
        if args.action == "set":
            try:
                update = json.loads(args.value or "")
            except ValueError as error:
                raise RitmoError(f"config set takes a JSON object: {error}") from error
            if not isinstance(update, dict):
                raise RitmoError("config set takes a JSON object")
            unknown = sorted(set(update) - set(core.DEFAULT_CONFIG) - {"version"})
            if unknown:
                raise RitmoError(f"unknown config keys {unknown}; valid keys: {sorted(core.DEFAULT_CONFIG)}")
            config = core.save_config(core.deep_merge(saved, update))
            result = {"saved": update}
        else:
            if args.value in core.DEFAULT_CONFIG and args.value != "version":
                saved[args.value] = json.loads(json.dumps(core.DEFAULT_CONFIG[args.value]))
            elif args.value in saved and args.value not in core.DEFAULT_CONFIG:
                del saved[args.value]  # a key this version does not know, e.g. from a newer backup
            else:
                raise RitmoError(f"unknown config key {args.value!r}")
            config = core.save_config(saved)
            result = {"reset": args.value}
        moved = core.change_timezone(old_zone, config["timezone"])
        if moved:
            result["reanchored_items"] = moved
    result["setup"] = core.setup_status(config)
    result["config"] = config
    out(result)


def edit_config(mutate) -> dict:
    with core.locked():
        config = core.load_config()
        mutate(config)
        return core.save_config(config)


def cmd_habit(args):
    def mutate(config):
        before = len(config["habits"])
        config["habits"] = [habit for habit in config["habits"] if habit["id"] != args.id]
        if args.action == "add":
            habit = {"id": args.id, "text": args.text, "every_minutes": args.every,
                     "start": args.start, "end": args.end}
            days = parse_days(args.days)
            if days:
                habit["days"] = days
            config["habits"].append(habit)
        elif len(config["habits"]) == before:
            raise RitmoError(f"no habit with id {args.id!r}")

    out({"habits": edit_config(mutate)["habits"]})


def cmd_fixed(args):
    def mutate(config):
        before = len(config["fixed"])
        config["fixed"] = [item for item in config["fixed"] if item["id"] != args.id]
        if args.action == "add":
            config["fixed"].append({"id": args.id, "text": args.text, "days": parse_days(args.days) or [],
                                    "start": args.start, "end": args.end, "remind": not args.no_remind})
        elif len(config["fixed"]) == before:
            raise RitmoError(f"no fixed commitment with id {args.id!r}")

    out({"fixed": edit_config(mutate)["fixed"]})


def cmd_goal(args):
    def mutate(config):
        if args.action == "add":
            config["goals"].append(args.value)
            return
        try:
            config["goals"].pop(int(args.value))
        except (ValueError, IndexError) as error:
            raise RitmoError("goal remove takes the goal's index from `config show`") from error

    out({"goals": edit_config(mutate)["goals"]})


# --- agenda --------------------------------------------------------------------

def cmd_add(args):
    config = ready()
    at = resolve_at(args, config)
    not_past(at, config)
    item = core.new_item(args.text, at, "plan" if args.plan else "owner", core.now_in(config),
                         minutes=args.minutes, urgent=args.urgent)
    with core.locked():
        agenda = core.load_agenda()
        agenda["items"].append(item)
        core.write_json("agenda.json", agenda)
    view = core.item_view(item, config)
    if item["source"] == "plan" and core.in_quiet(at, config) and not args.urgent:
        view["warning"] = "inside quiet hours: it will wait until they end unless marked --urgent"
    out({"added": view})


def cmd_list(args):
    config = ready()
    today = core.now_in(config).date()
    if args.date in (None, "today"):
        first = today
    elif args.date == "tomorrow":
        first = today + dt.timedelta(days=1)
    else:
        first = core.parse_date(args.date)
    last = first + dt.timedelta(days=max(args.days, 1) - 1)
    agenda = core.load_agenda()
    days = {}
    for offset in range((last - first).days + 1):
        day = first + dt.timedelta(days=offset)
        days[day.isoformat()] = {"weekday": core.weekday_key(day), "fixed": core.fixed_on(config, day),
                                 "items": core.items_between(agenda, config, day, day)}
    out({"days": days, "habits": config["habits"]})


def change(ref: str, config: dict, mutate) -> dict:
    with core.locked():
        agenda = core.load_agenda()
        item = find(agenda, ref, config)
        mutate(item)
        core.write_json("agenda.json", agenda)
    return item


def cmd_status(args):
    config = ready()
    status = {"done": "done", "skip": "skipped"}[args.command]
    item = change(args.ref, config, lambda item: item.update(status=status))
    out({status: core.item_view(item, config)})


def cmd_snooze(args):
    config = ready()
    if args.minutes < 1:
        raise RitmoError("snooze takes a whole number of minutes, at least 1")
    at = core.plus_minutes(core.now_in(config).replace(second=0, microsecond=0), args.minutes)

    def mutate(item):
        item.update(at=at.isoformat(timespec="minutes"), status="pending", reminded_at=None)

    out({"snoozed": core.item_view(change(args.ref, config, mutate), config)})


def cmd_move(args):
    config = ready()
    at = core.parse_when(args.at, config)
    not_past(at, config)

    def mutate(item):
        item.update(at=at.isoformat(timespec="minutes"), status="pending", reminded_at=None)

    out({"moved": core.item_view(change(args.id, config, mutate), config)})


def cmd_remove(args):
    config = ready()
    with core.locked():
        agenda = core.load_agenda()
        item = find(agenda, args.id, config)
        agenda["items"].remove(item)
        core.write_json("agenda.json", agenda)
    out({"removed": core.item_view(item, config)})


def cmd_plan(args):
    """Replace the planner's open items on one day; the owner's own reminders stay."""
    config = ready()
    day = core.parse_date(args.date)
    try:
        entries = json.loads(args.items)
    except ValueError as error:
        raise RitmoError(f"plan takes a JSON list: {error}") from error
    if not isinstance(entries, list):
        raise RitmoError("plan takes a JSON list of {\"text\", \"time\", \"minutes\"}")
    now = core.now_in(config)
    if day < now.date():
        raise RitmoError(f"{day.isoformat()} is in the past for the owner (today is {now.date().isoformat()})")
    tz = core.zone(config)
    fresh = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RitmoError("each plan entry is an object with text, time and optional minutes")
        at = core.at_local(day, core.check_time(entry.get("time"), "plan time"), tz)
        not_past(at, config)
        fresh.append(core.new_item(entry.get("text"), at, "plan", now,
                                   minutes=entry.get("minutes"), urgent=entry.get("urgent") is True))
    with core.locked():
        agenda = core.load_agenda()
        kept = [item for item in agenda["items"]
                if not (item.get("source") == "plan" and item["status"] == "pending"
                        and dt.datetime.fromisoformat(item["at"]).astimezone(tz).date() == day
                        and dt.datetime.fromisoformat(item["at"]) > now)]
        agenda["items"] = kept + fresh
        core.write_json("agenda.json", agenda)
    quiet = [core.item_view(item, config) for item in fresh
             if core.in_quiet(dt.datetime.fromisoformat(item["at"]).astimezone(tz), config) and not item.get("urgent")]
    result = {"planned": day.isoformat(), "items": core.items_between(agenda, config, day, day)}
    if quiet:
        result["warning"] = {"inside_quiet_hours_will_wait": quiet}
    out(result)


BACKUP_PREFIX = "RITMO-BACKUP-1:"
# The code ends with a dot, so words the owner adds after it are never read as
# part of it (a trailing "ok" would otherwise turn a valid backup into garbage).
BACKUP_PATTERN = re.compile(re.escape(BACKUP_PREFIX) + r"([A-Za-z0-9_\-\s]+)\.")


def backup_code(config: dict, agenda: dict) -> tuple[str, int]:
    """Every open item not yet reminded (a held or just-due one included), plus the settings."""
    items = [
        {key: item[key] for key in ("text", "at", "minutes", "urgent", "source") if key in item}
        for item in agenda["items"]
        if item.get("status") == "pending" and not item.get("reminded_at")
    ]
    payload = {"exported": core.now_in(config).isoformat(timespec="minutes") if core.configured(config) else "",
               "config": config, "items": items}
    # The Team module's settings travel too (people, repositories, rules); its connections never do:
    # GitHub and the board are connected again by code after an update.
    team = core.read_json("equipe.json", {})
    if team:
        payload["team"] = team
    packed = zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    return BACKUP_PREFIX + base64.urlsafe_b64encode(packed).decode().rstrip("=") + ".", len(items)


def cmd_export(args):
    """The owner's setup and open items, as one message they forward back after an update.

    A new image on Plow is a new agent with an empty home, so this is how a
    routine survives an update. With --send the code is not printed for the
    model to copy: the reminder clock delivers it verbatim within a minute,
    because one wrong character in 600 would lose the whole routine.
    """
    config = core.load_config()
    code, count = backup_code(config, core.load_agenda())
    summary = {"habits": len(config["habits"]), "fixed": len(config["fixed"]), "items": count}
    if args.send:
        with core.locked():
            with open(core.path("outbox.txt"), "w", encoding="utf-8") as handle:
                handle.write(code + "\n")
        out({"queued": "the backup message reaches the owner within a minute, on its own", **summary})
        return
    out({"code": code, **summary})


def decode_backup(text: str) -> dict:
    match = BACKUP_PATTERN.search(text)
    if not match:
        raise RitmoError(f"a backup starts with {BACKUP_PREFIX} and ends with a dot; ask the owner to forward "
                         "the whole backup message")
    body = "".join(match.group(1).split())
    try:
        payload = json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
    except (ValueError, zlib.error) as error:
        raise RitmoError("the backup is incomplete or damaged; ask the owner to forward the whole message") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict) \
            or not isinstance(payload.get("items"), list):
        raise RitmoError("that is not a Ritmo backup")
    return payload


# Hermes stores a multimodal turn (text plus an attachment) as this prefix and a JSON list of parts.
CONTENT_JSON_PREFIX = "\x00json:"
# A delivery Hermes mirrors into the chat (`cron.mirror_delivery`) is a user turn under this header.
MIRROR_HEADER = re.compile(r"\[Cron delivery: [^\]\n]*\]\n")


def message_text(content) -> str:
    """The words of one stored message, whether Hermes kept it as plain text or as parts."""
    text = str(content or "")
    if not text.startswith(CONTENT_JSON_PREFIX):
        return text
    try:
        parts = json.loads(text[len(CONTENT_JSON_PREFIX):])
    except ValueError:
        return text
    if isinstance(parts, str):
        return parts
    if isinstance(parts, dict):
        parts = [parts]
    return "\n".join(part if isinstance(part, str) else str(part.get("text") or "")
                     for part in parts if isinstance(part, (str, dict)))


def owner_words(text: str) -> str:
    """Drop what Ritmo itself delivered, so its own `export --send` is never read back as the owner's."""
    if not text.startswith("[Cron delivery: "):
        return text
    # A mirrored delivery is one segment; anything after a blank line was written by the owner.
    body = MIRROR_HEADER.sub("", text, count=1)
    return body.split("\n\n", 1)[1] if "\n\n" in body else ""


def backup_from_chat() -> dict:
    """The backup code in the owner's newest message that holds one, read as it arrived.

    Copying 600 random characters into a command is where a restore broke in a real test: one
    wrong character and the whole routine is lost. Read from Hermes' state.db, the code is never
    retyped. Only the chat this command runs in is read, and only when it is the owner's
    one-to-one chat: Hermes hands every command its session and chat, and Plow names the owner's
    chat in PLOW_HOME_CHANNEL. Only the newest message with a code counts: when it is damaged the
    owner hears so, instead of an older backup coming back in its place.
    """
    import pathlib
    import sqlite3
    session = os.environ.get("HERMES_SESSION_ID", "")
    chat = os.environ.get("HERMES_SESSION_CHAT_ID", "")
    home_chat = os.environ.get("PLOW_HOME_CHANNEL", "")
    if not (session or chat):
        raise RitmoError("restore runs only while answering the owner's message in their own chat")
    if os.environ.get("HERMES_SESSION_CHAT_TYPE", "dm") != "dm" or (home_chat and chat and chat != home_chat):
        raise RitmoError("a backup is restored only from the owner's one-to-one chat; ignore this one")
    database = pathlib.Path(core.home(), "state.db")
    if not database.exists():
        raise RitmoError("the conversation store is missing; tell the owner the restore failed and to "
                         "try again later. Never retype the code")
    rows = None
    for attempt in range(3):
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                # Session and chat both: a long chat may have moved to a new session since. Compared
                # as bytes: LIKE stops at the NUL that starts a stored list of parts.
                rows = connection.execute(
                    """SELECT m.content FROM messages m JOIN sessions s ON s.id = m.session_id
                       WHERE m.role = 'user' AND instr(CAST(m.content AS BLOB), CAST('RITMO-BACKUP-1:' AS BLOB)) > 0
                         AND (m.session_id = ? OR (? != '' AND s.chat_id = ? AND s.chat_type = 'dm'))
                       ORDER BY m.timestamp DESC, m.id DESC LIMIT 20""", (session, chat, chat)).fetchall()
            finally:
                connection.close()
            break
        except sqlite3.OperationalError as error:
            # A reader can catch the writer mid-checkpoint for a moment ("disk I/O error").
            if attempt == 2:
                raise RitmoError(f"could not read the conversation store ({error}); tell the owner the "
                                 "restore failed and to try again in a minute. Never retype the code") from error
            import time
            time.sleep(0.2)
    for (content,) in rows or []:
        words = owner_words(message_text(content))
        if BACKUP_PREFIX in words:
            # The last code in the message: anything before it is quoted or older.
            codes = [m.group(0) for m in BACKUP_PATTERN.finditer(words)]
            return decode_backup(codes[-1] if codes else words)
    raise RitmoError("no backup message from the owner in this chat; ask them to forward it again")


def cmd_import(args):
    """Restore an exported setup. Everything is checked first; nothing is written on a refusal."""
    if args.from_chat and args.code:
        raise RitmoError("give either --from-chat or a code, not both; --from-chat is the safe one")
    if args.from_chat:
        payload = backup_from_chat()
    elif args.code:
        payload = decode_backup(args.code)
    else:
        raise RitmoError("use `import --from-chat` (reads the owner's message as it arrived)")
    # Keys a newer Ritmo knows and this one does not are left behind.
    settings = {key: value for key, value in payload["config"].items() if key in core.DEFAULT_CONFIG}
    config = core.validate_config(settings)
    if not core.configured(config):
        raise RitmoError("the backup has no timezone; set the owner up instead")
    now = core.now_in(config)
    restored, expired, seen = [], [], set()
    for entry in payload["items"]:
        if not isinstance(entry, dict):
            continue
        try:
            at = dt.datetime.fromisoformat(str(entry.get("at")))
        except ValueError:
            continue
        if at.tzinfo is None:
            continue
        try:
            item = core.new_item(entry.get("text"), at.astimezone(core.zone(config)), entry.get("source", "owner"),
                                 now, minutes=entry.get("minutes"), urgent=entry.get("urgent") is True)
        except RitmoError:
            continue
        key = (item["text"], core.utc(at))
        if key in seen:
            continue
        seen.add(key)
        if core.utc(at) <= core.utc(now) - PAST_TOLERANCE:
            expired.append({"text": item["text"], "time": at.astimezone(core.zone(config)).strftime("%Y-%m-%d %H:%M")})
            continue
        restored.append(item)
    with core.locked():
        previous = core.read_json("config.json", {})
        agenda = core.load_agenda()
        existing = {(item["text"], core.utc(dt.datetime.fromisoformat(item["at"]))) for item in agenda["items"]}
        added = [item for item in restored
                 if (item["text"], core.utc(dt.datetime.fromisoformat(item["at"]))) not in existing]
        agenda["items"].extend(added)
        core.write_json("agenda.json", agenda)
        old_zone = previous.get("timezone") if isinstance(previous.get("timezone"), str) else ""
        config = core.save_config(settings)
        if isinstance(payload.get("team"), dict):
            core.write_json("equipe.json", payload["team"])
        # Items the new agent already had follow the restored zone, like any zone change.
        core.change_timezone(old_zone, config["timezone"])
    out({"restored": {"name": config["name"], "language": config["language"], "timezone": config["timezone"],
                      "tone": config["tone"],
                      "habits": len(config["habits"]), "fixed": len(config["fixed"]), "goals": len(config["goals"]),
                      "items_added": len(added), "team": isinstance(payload.get("team"), dict)},
         **({"reconnect": "the team settings came back; connect GitHub and the board again (equipe skill)"}
            if isinstance(payload.get("team"), dict) else {}),
         "replaced_settings": bool(previous),
         "expired_during_update": expired,
         "backup_made": payload.get("exported", "")})


def cmd_doctor(args):
    """What is wrong, without needing anything to be right."""
    report = {"data_dir": core.data_dir()}
    try:
        config = core.load_config()
        report.update(config_valid=True, configured=core.configured(config), onboarded=config["onboarded"])
    except RitmoError as error:
        report.update(config_valid=False, config_error=str(error))
    try:
        agenda = core.load_agenda()
        report["agenda_items"] = len(agenda["items"])
        report["pending"] = sum(1 for item in agenda["items"] if item.get("status") == "pending")
    except RitmoError as error:
        report["agenda_error"] = str(error)
    try:
        with open(core.path("errors.log"), encoding="utf-8", errors="replace") as handle:
            report["recent_errors"] = handle.read()[-2000:]
    except FileNotFoundError:
        report["recent_errors"] = ""
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

    export = sub.add_parser("export")
    export.add_argument("--send", action="store_true", help="deliver the code to the owner verbatim, no copying")
    restore = sub.add_parser("import")
    restore.add_argument("code", nargs="?")
    restore.add_argument("--from-chat", action="store_true", help="read the code from the owner's message as it arrived")
    return root


COMMANDS = {
    "now": cmd_now, "config": cmd_config, "habit": cmd_habit, "fixed": cmd_fixed, "goal": cmd_goal,
    "add": cmd_add, "list": cmd_list, "done": cmd_status, "skip": cmd_status, "snooze": cmd_snooze,
    "move": cmd_move, "remove": cmd_remove, "plan": cmd_plan, "doctor": cmd_doctor,
    "export": cmd_export, "import": cmd_import,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        COMMANDS[args.command](args)
    except RitmoError as error:
        out({"error": str(error)})
        return 1
    except Exception as error:  # noqa: BLE001 -- the agent reads JSON, never a traceback
        core.log_error(f"ritmo.py {args.command}", repr(error))
        out({"error": f"unexpected {error.__class__.__name__}: {error}. Check `ritmo.py list` "
                      "or `ritmo.py doctor` before telling the owner anything changed"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
