"""Ritmo's state and clock: the owner's config, their agenda, and what is due now.

Everything here is deterministic and owner-timezone aware. The LLM never does
time arithmetic itself: it calls `ritmo.py`, which calls this module, and the
two cron scripts (`ritmo_lembretes.py`, `ritmo_rotinas.py`) read the same files.

Times are stored as ISO 8601 with the owner's UTC offset, and every "is it time
yet" question is asked in the owner's zone through zoneinfo, so daylight saving
and a change of zone are handled here and nowhere else. Hermes' own clock (which
caches its zone until the gateway restarts) is never consulted.

Standard library only: the same file runs in the image's Python and in the
tests on any machine.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import os
import re
import secrets
import tempfile
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_CONFIG: dict = {
    "version": SCHEMA_VERSION,
    "name": "",
    "language": "en",
    "timezone": "",
    "wake": "07:00",
    "sleep": "23:00",
    "quiet": {"start": "22:00", "end": "07:00"},
    "routines": {
        "morning": {"time": "07:30", "enabled": True, "days": list(WEEKDAYS)},
        "evening": {"time": "21:00", "enabled": True, "days": list(WEEKDAYS)},
        "weekly": {"day": "sun", "time": "20:00", "enabled": True},
    },
    "habits": [],
    "fixed": [],
    "goals": [],
    "texts": {},
    "reminder_grace_minutes": 120,
    "onboarded": False,
}

# The fixed wording of the no-LLM messages. An owner can override any key
# through config["texts"]; a language missing here falls back to English.
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "reminder": "⏰ {time} · {text}",
        "duration": " ({minutes} min)",
        "habit": "⏰ {text}",
        "fixed": "📅 {time} · {text}",
        "footer": "Reply done, snooze 15 or skip.",
        "missed": "Missed while I was offline: {items}",
    },
    "pt": {
        "reminder": "⏰ {time} · {text}",
        "duration": " ({minutes} min)",
        "habit": "⏰ {text}",
        "fixed": "📅 {time} · {text}",
        "footer": "Responda feito, adiar 15 ou pular.",
        "missed": "Perdi o horário destes enquanto estava fora do ar: {items}",
    },
    "es": {
        "reminder": "⏰ {time} · {text}",
        "duration": " ({minutes} min)",
        "habit": "⏰ {text}",
        "fixed": "📅 {time} · {text}",
        "footer": "Responde hecho, posponer 15 u omitir.",
        "missed": "Se me pasó la hora de estos mientras estaba sin conexión: {items}",
    },
}

HABIT_WINDOW = dt.timedelta(minutes=10)
FIXED_WINDOW = dt.timedelta(minutes=10)
ROUTINE_WINDOW = dt.timedelta(minutes=30)

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class RitmoError(ValueError):
    """A refusal worth showing the model verbatim: what was wrong and how to fix it."""


# --- paths and files ---------------------------------------------------------

def home() -> str:
    return os.environ.get("HERMES_HOME") or "/var/lib/hermes"


def data_dir() -> str:
    return os.path.join(home(), "ritmo")


def path(name: str) -> str:
    return os.path.join(data_dir(), name)


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Serialise every read-modify-write of the agenda and state.

    The reminder job, the routine gate and a conversation turn can all touch
    the same files within one minute. fcntl exists in the image; the fallback
    only matters for running the tests on Windows.
    """
    os.makedirs(data_dir(), exist_ok=True)
    handle = open(path(".lock"), "a+")
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


ERROR_LOG_LIMIT = 256 * 1024


def log_error(source: str, text: str) -> None:
    """Append to ritmo/errors.log, keeping only the newest half once it grows past the limit."""
    try:
        os.makedirs(data_dir(), exist_ok=True)
        log = path("errors.log")
        with contextlib.suppress(FileNotFoundError):
            if os.path.getsize(log) > ERROR_LOG_LIMIT:
                with open(log, encoding="utf-8", errors="replace") as handle:
                    tail = handle.read()[-ERROR_LOG_LIMIT // 2:]
                with open(log, "w", encoding="utf-8") as handle:
                    handle.write(tail)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"--- {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {source}\n{text}\n")
    except OSError:
        pass


def read_json(name: str, default: dict) -> dict:
    try:
        with open(path(name), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return copy.deepcopy(default)
    if not isinstance(data, dict):
        raise RitmoError(f"{path(name)} should hold a JSON object")
    return data


def write_json(name: str, data: dict) -> None:
    """Write beside and rename over, so a crash never leaves half a file."""
    os.makedirs(data_dir(), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=data_dir(), prefix=f".{name}.", suffix=".new")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path(name))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- validation ----------------------------------------------------------------

def check_time(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TIME.match(value):
        raise RitmoError(f"{field} must be HH:MM in 24h time, got {value!r}")
    return value


def check_days(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value or any(day not in WEEKDAYS for day in value):
        raise RitmoError(f"{field} must be a non-empty list drawn from {', '.join(WEEKDAYS)}")
    return [day for day in WEEKDAYS if day in value]


def check_timezone(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RitmoError("timezone must be an IANA name such as America/Sao_Paulo")
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise RitmoError(f"unknown timezone {value!r}: use an IANA name such as Europe/Lisbon") from error
    return value.strip()


def check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise RitmoError(f"{field} must be lowercase letters, digits, '-' or '_' (max 40), got {value!r}")
    return value


def validate_config(config: dict) -> dict:
    """Fill defaults, then refuse anything the clock could misread."""
    merged = deep_merge(copy.deepcopy(DEFAULT_CONFIG), config)
    merged["version"] = SCHEMA_VERSION
    if merged["timezone"]:
        merged["timezone"] = check_timezone(merged["timezone"])
    if not isinstance(merged["language"], str) or not merged["language"].strip():
        raise RitmoError("language must be a language tag such as pt-BR or en")
    check_time(merged["wake"], "wake")
    check_time(merged["sleep"], "sleep")
    check_time(merged["quiet"].get("start"), "quiet.start")
    check_time(merged["quiet"].get("end"), "quiet.end")
    for name in ("morning", "evening"):
        routine = merged["routines"][name]
        check_time(routine.get("time"), f"routines.{name}.time")
        routine["days"] = check_days(routine.get("days", list(WEEKDAYS)), f"routines.{name}.days")
        routine["enabled"] = bool(routine.get("enabled", True))
    weekly = merged["routines"]["weekly"]
    if weekly.get("day") not in WEEKDAYS:
        raise RitmoError(f"routines.weekly.day must be one of {', '.join(WEEKDAYS)}")
    check_time(weekly.get("time"), "routines.weekly.time")
    weekly["enabled"] = bool(weekly.get("enabled", True))

    seen: set[str] = set()
    for habit in merged["habits"]:
        check_id(habit.get("id"), "habit id")
        if habit["id"] in seen:
            raise RitmoError(f"two habits share the id {habit['id']!r}")
        seen.add(habit["id"])
        if not str(habit.get("text", "")).strip():
            raise RitmoError(f"habit {habit['id']!r} needs a text")
        every = habit.get("every_minutes")
        if not isinstance(every, int) or every < 15:
            raise RitmoError(f"habit {habit['id']!r}: every_minutes must be a whole number of at least 15")
        check_time(habit.get("start"), f"habit {habit['id']!r} start")
        check_time(habit.get("end"), f"habit {habit['id']!r} end")
        habit["days"] = check_days(habit.get("days", list(WEEKDAYS)), f"habit {habit['id']!r} days")

    seen = set()
    for item in merged["fixed"]:
        check_id(item.get("id"), "fixed id")
        if item["id"] in seen:
            raise RitmoError(f"two fixed commitments share the id {item['id']!r}")
        seen.add(item["id"])
        if not str(item.get("text", "")).strip():
            raise RitmoError(f"fixed commitment {item['id']!r} needs a text")
        check_time(item.get("start"), f"fixed {item['id']!r} start")
        check_time(item.get("end"), f"fixed {item['id']!r} end")
        item["days"] = check_days(item.get("days"), f"fixed {item['id']!r} days")
        item["remind"] = bool(item.get("remind", True))

    if not isinstance(merged["goals"], list) or not all(isinstance(goal, str) for goal in merged["goals"]):
        raise RitmoError("goals must be a list of strings")
    if not isinstance(merged["texts"], dict):
        raise RitmoError("texts must be an object of message templates")
    grace = merged["reminder_grace_minutes"]
    if not isinstance(grace, int) or grace < 5:
        raise RitmoError("reminder_grace_minutes must be a whole number of at least 5")
    merged["onboarded"] = bool(merged["onboarded"])
    return merged


def deep_merge(base: dict, update: dict) -> dict:
    """Objects merge key by key; lists and scalars are replaced whole."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


# --- config, agenda, state ---------------------------------------------------

def load_config() -> dict:
    return validate_config(read_json("config.json", {}))


def save_config(config: dict) -> dict:
    valid = validate_config(config)
    write_json("config.json", valid)
    return valid


def configured(config: dict) -> bool:
    """The clocks stay silent until the owner has said where they live."""
    return bool(config.get("timezone"))


def load_agenda() -> dict:
    agenda = read_json("agenda.json", {"version": SCHEMA_VERSION, "items": []})
    agenda.setdefault("items", [])
    agenda["version"] = SCHEMA_VERSION
    return agenda


def load_state() -> dict:
    state = read_json("state.json", {"version": SCHEMA_VERSION})
    state.setdefault("habits", {})
    state.setdefault("fixed", {})
    state.setdefault("routines", {})
    state["version"] = SCHEMA_VERSION
    return state


def new_id() -> str:
    return secrets.token_hex(3)


# --- time --------------------------------------------------------------------

def zone(config: dict) -> ZoneInfo:
    return ZoneInfo(config["timezone"])


def now_in(config: dict) -> dt.datetime:
    """The owner's wall clock. RITMO_NOW pins it for tests and support."""
    pinned = os.environ.get("RITMO_NOW")
    if pinned:
        moment = dt.datetime.fromisoformat(pinned)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone(config))
        return moment.astimezone(zone(config))
    return dt.datetime.now(zone(config))


def at_local(day: dt.date, hhmm: str, tz: ZoneInfo) -> dt.datetime:
    """A wall-clock time on a date, resolved in the owner's zone.

    A time that does not exist (skipped by daylight saving) moves forward with
    the clock; an ambiguous one takes its first occurrence. Both are what a
    person means by "at 2:30".
    """
    hour, minute = (int(part) for part in hhmm.split(":"))
    naive = dt.datetime.combine(day, dt.time(hour, minute))
    moment = naive.replace(tzinfo=tz, fold=0)
    # Round-tripping through UTC normalises a non-existent time onto the real clock.
    return moment.astimezone(dt.timezone.utc).astimezone(tz)


def parse_when(text: str, config: dict) -> dt.datetime:
    """`YYYY-MM-DD HH:MM` or `HH:MM` (today), in the owner's zone."""
    text = text.strip()
    tz = zone(config)
    if _TIME.match(text):
        return at_local(now_in(config).date(), text, tz)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]([0-2]\d:[0-5]\d)$", text)
    if not match:
        raise RitmoError(f"use 'YYYY-MM-DD HH:MM' or 'HH:MM' for times, got {text!r}")
    day = dt.date.fromisoformat(match.group(1))
    return at_local(day, check_time(match.group(2), "time"), tz)


def weekday_key(day: dt.date) -> str:
    return WEEKDAYS[day.weekday()]


def in_window(moment: dt.datetime, start: str, end: str) -> bool:
    """Whether a wall-clock time falls in [start, end), wrapping past midnight."""
    minute = moment.hour * 60 + moment.minute
    first = int(start[:2]) * 60 + int(start[3:])
    last = int(end[:2]) * 60 + int(end[3:])
    if first == last:
        return False
    if first < last:
        return first <= minute < last
    return minute >= first or minute < last


def in_quiet(moment: dt.datetime, config: dict) -> bool:
    return in_window(moment, config["quiet"]["start"], config["quiet"]["end"])


# --- what is due ---------------------------------------------------------------

def habit_slots(habit: dict, day: dt.date, tz: ZoneInfo) -> list[dt.datetime]:
    """Every reminder time of a habit on one day: start, start+every, ... before end."""
    if weekday_key(day) not in habit["days"]:
        return []
    start = at_local(day, habit["start"], tz)
    end = at_local(day, habit["end"], tz)
    if end <= start:
        end += dt.timedelta(days=1)
    slots, slot = [], start
    step = dt.timedelta(minutes=habit["every_minutes"])
    while slot <= end:
        slots.append(slot)
        slot += step
    return slots


def due_habits(config: dict, state: dict, now: dt.datetime) -> list[tuple[dict, dt.datetime]]:
    """Habits whose latest slot passed in the last few minutes and was not sent.

    Missed slots are not replayed: "drink water" three times at once after an
    outage is noise, so only a slot inside HABIT_WINDOW counts.
    """
    tz = zone(config)
    due = []
    for habit in config["habits"]:
        candidates = habit_slots(habit, now.date(), tz) + habit_slots(habit, now.date() - dt.timedelta(days=1), tz)
        recent = [slot for slot in candidates if slot <= now and now - slot < HABIT_WINDOW]
        if not recent:
            continue
        slot = max(recent)
        if in_quiet(slot, config):
            continue
        last = state["habits"].get(habit["id"])
        if last and dt.datetime.fromisoformat(last) >= slot:
            continue
        due.append((habit, slot))
    return due


def due_fixed(config: dict, state: dict, now: dt.datetime) -> list[tuple[dict, dt.datetime]]:
    """Fixed commitments starting now. The owner set these times, so quiet hours do not apply."""
    tz = zone(config)
    due = []
    for item in config["fixed"]:
        if not item.get("remind", True) or weekday_key(now.date()) not in item["days"]:
            continue
        start = at_local(now.date(), item["start"], tz)
        if not (start <= now and now - start < FIXED_WINDOW):
            continue
        last = state["fixed"].get(item["id"])
        if last and dt.datetime.fromisoformat(last) >= start:
            continue
        due.append((item, start))
    return due


def due_items(config: dict, agenda: dict, now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """(remind now, missed) among pending agenda items.

    An item the planner placed is held while quiet hours last; one the owner
    asked for explicitly, or marked urgent, fires at the time they chose.
    Anything older than the grace period was missed (the agent was down), and
    is reported once instead of arriving hours late as if it were on time.
    """
    grace = dt.timedelta(minutes=config["reminder_grace_minutes"])
    remind, missed = [], []
    for item in agenda["items"]:
        if item.get("status") != "pending" or item.get("reminded_at"):
            continue
        at = dt.datetime.fromisoformat(item["at"])
        if at > now:
            continue
        if now - at > grace:
            missed.append(item)
            continue
        if item.get("source") == "plan" and not item.get("urgent") and in_quiet(now, config):
            continue
        remind.append(item)
    return remind, missed


def routine_due(config: dict, state: dict, now: dt.datetime) -> tuple[str, str] | None:
    """(routine, period key) for the first routine due and not yet fired in its period."""
    tz = zone(config)
    routines = config["routines"]
    for name in ("morning", "evening"):
        routine = routines[name]
        if not routine["enabled"] or weekday_key(now.date()) not in routine["days"]:
            continue
        start = at_local(now.date(), routine["time"], tz)
        period = now.date().isoformat()
        if start <= now < start + ROUTINE_WINDOW and state["routines"].get(name) != period:
            return name, period
    weekly = routines["weekly"]
    if weekly["enabled"] and weekday_key(now.date()) == weekly["day"]:
        start = at_local(now.date(), weekly["time"], tz)
        year, week, _ = now.isocalendar()
        period = f"{year}-W{week:02d}"
        if start <= now < start + ROUTINE_WINDOW and state["routines"].get("weekly") != period:
            return "weekly", period
    return None


# --- views -------------------------------------------------------------------

def item_view(item: dict, config: dict) -> dict:
    at = dt.datetime.fromisoformat(item["at"]).astimezone(zone(config))
    view = {
        "id": item["id"],
        "text": item["text"],
        "date": at.date().isoformat(),
        "time": at.strftime("%H:%M"),
        "status": item["status"],
    }
    for key in ("minutes", "urgent", "source", "reminded_at"):
        if item.get(key):
            view[key] = item[key]
    return view


def items_between(agenda: dict, config: dict, first: dt.date, last: dt.date) -> list[dict]:
    tz = zone(config)
    chosen = []
    for item in agenda["items"]:
        day = dt.datetime.fromisoformat(item["at"]).astimezone(tz).date()
        if first <= day <= last:
            chosen.append(item)
    chosen.sort(key=lambda item: item["at"])
    return [item_view(item, config) for item in chosen]


def fixed_on(config: dict, day: dt.date) -> list[dict]:
    key = weekday_key(day)
    return [
        {"id": item["id"], "text": item["text"], "start": item["start"], "end": item["end"]}
        for item in config["fixed"] if key in item["days"]
    ]


def texts(config: dict) -> dict[str, str]:
    language = config["language"].split("-")[0].lower()
    merged = dict(TEXTS["en"])
    merged.update(TEXTS.get(language, {}))
    merged.update({key: value for key, value in config["texts"].items() if isinstance(value, str)})
    return merged
