"""Ritmo's clock, agenda and CLI, against a throwaway HERMES_HOME and a pinned clock."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "ritmo", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import ritmo  # noqa: E402
import ritmo_core as core  # noqa: E402
import ritmo_lembretes  # noqa: E402
import ritmo_rotinas  # noqa: E402

SAO_PAULO = {"timezone": "America/Sao_Paulo", "language": "pt-BR", "name": "Ana", "onboarded": True}


class Home(unittest.TestCase):
    """Each test gets its own HERMES_HOME; `at()` pins the owner's clock."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ritmo-test-")
        self.saved = {key: os.environ.get(key) for key in ("HERMES_HOME", "RITMO_NOW")}
        os.environ["HERMES_HOME"] = self.home
        os.environ.pop("RITMO_NOW", None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.home, ignore_errors=True)

    def at(self, moment: str):
        os.environ["RITMO_NOW"] = moment

    def configure(self, **extra):
        config = dict(SAO_PAULO)
        config.update(extra)
        return core.save_config(config)

    def cli(self, *argv) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ritmo.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def reminders(self) -> str:
        return ritmo_lembretes.tick()


class TimeMath(Home):
    def test_skipped_hour_moves_forward(self):
        tz = core.ZoneInfo("America/New_York")
        moment = core.at_local(dt.date(2026, 3, 8), "02:30", tz)
        self.assertEqual(moment.strftime("%H:%M %z"), "03:30 -0400")

    def test_ambiguous_hour_takes_first(self):
        tz = core.ZoneInfo("America/New_York")
        moment = core.at_local(dt.date(2026, 11, 1), "01:30", tz)
        self.assertEqual(moment.utcoffset(), dt.timedelta(hours=-4))

    def test_quiet_window_wraps_midnight(self):
        config = self.configure()
        tz = core.zone(config)
        self.assertTrue(core.in_quiet(dt.datetime(2026, 10, 19, 23, 0, tzinfo=tz), config))
        self.assertTrue(core.in_quiet(dt.datetime(2026, 10, 19, 6, 59, tzinfo=tz), config))
        self.assertFalse(core.in_quiet(dt.datetime(2026, 10, 19, 7, 0, tzinfo=tz), config))

    def test_bad_timezone_refused(self):
        with self.assertRaises(core.RitmoError):
            self.configure(timezone="Mars/Olympus")


class Reminders(Home):
    def test_silent_before_onboarding(self):
        self.assertEqual(self.reminders(), "")

    def test_reminder_fires_once_on_its_minute(self):
        self.configure()
        self.at("2026-10-19T13:55:00-03:00")
        code, added = self.cli("add", "Estudar inglês", "--at", "2026-10-19 14:00", "--minutes", "50")
        self.assertEqual(code, 0, added)
        self.at("2026-10-19T13:59:00-03:00")
        self.assertEqual(self.reminders(), "")
        self.at("2026-10-19T14:00:00-03:00")
        message = self.reminders()
        self.assertIn("14:00 · Estudar inglês (50 min)", message)
        self.assertIn("Responda feito", message)
        self.at("2026-10-19T14:01:00-03:00")
        self.assertEqual(self.reminders(), "")

    def test_in_minutes(self):
        self.configure()
        self.at("2026-10-19T10:00:30-03:00")
        self.cli("add", "Ligar pro contador", "--in", "5")
        self.at("2026-10-19T10:05:00-03:00")
        self.assertIn("10:05 · Ligar pro contador", self.reminders())

    def test_owner_reminder_ignores_quiet_hours_but_plan_waits(self):
        self.configure()
        self.at("2026-10-19T22:00:00-03:00")
        self.cli("add", "Tomar remédio", "--at", "23:00")
        self.cli("add", "Revisar plano", "--at", "23:00", "--plan")
        self.at("2026-10-19T23:00:00-03:00")
        message = self.reminders()
        self.assertIn("Tomar remédio", message)
        self.assertNotIn("Revisar plano", message)

    def test_missed_reported_once_not_replayed(self):
        self.configure()
        self.at("2026-10-19T08:00:00-03:00")
        self.cli("add", "Reunião", "--at", "09:00")
        self.at("2026-10-19T12:00:00-03:00")
        message = self.reminders()
        self.assertIn("Perdi o horário", message)
        self.assertEqual(self.reminders(), "")

    def test_habit_every_two_hours_inside_window(self):
        self.configure()
        self.cli("habit", "add", "agua", "Beber água", "--every", "120", "--start", "08:00", "--end", "22:00")
        self.at("2026-10-19T10:00:00-03:00")
        self.assertIn("Beber água", self.reminders())
        self.at("2026-10-19T10:03:00-03:00")
        self.assertEqual(self.reminders(), "")
        self.at("2026-10-19T11:00:00-03:00")
        self.assertEqual(self.reminders(), "")
        self.at("2026-10-19T12:01:00-03:00")
        self.assertIn("Beber água", self.reminders())

    def test_habit_respects_quiet_hours(self):
        self.configure(quiet={"start": "21:00", "end": "07:00"})
        self.cli("habit", "add", "agua", "Beber água", "--every", "60", "--start", "08:00", "--end", "23:00")
        self.at("2026-10-19T22:00:00-03:00")
        self.assertEqual(self.reminders(), "")

    def test_fixed_commitment_on_its_days(self):
        self.configure()
        self.cli("fixed", "add", "academia", "Academia", "--days", "mon,wed", "--start", "18:00", "--end", "19:00")
        self.at("2026-10-19T18:00:00-03:00")  # a Monday
        self.assertIn("18:00 · Academia", self.reminders())
        self.at("2026-10-20T18:00:00-03:00")  # a Tuesday
        self.assertEqual(self.reminders(), "")

    def test_english_owner_gets_english_footer(self):
        self.configure(language="en-US")
        self.at("2026-10-19T09:00:00-03:00")
        self.cli("add", "Stand-up", "--at", "09:30")
        self.at("2026-10-19T09:30:00-03:00")
        self.assertIn("Reply done", self.reminders())

    def test_owner_overrides_text(self):
        self.configure(texts={"footer": "Bora?"})
        self.at("2026-10-19T09:00:00-03:00")
        self.cli("add", "Treino", "--at", "09:30")
        self.at("2026-10-19T09:30:00-03:00")
        self.assertTrue(self.reminders().endswith("Bora?"))

    def test_broken_config_stays_silent_and_logs(self):
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(core.path("config.json"), "w") as handle:
            handle.write("{not json")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(ritmo_lembretes.main(), 0)
        self.assertEqual(buffer.getvalue(), "")
        with open(core.path("errors.log"), encoding="utf-8") as handle:
            self.assertIn("ritmo_lembretes", handle.read())


class Replies(Home):
    def test_done_snooze_skip_on_last_reminded(self):
        self.configure()
        self.at("2026-10-19T13:00:00-03:00")
        self.cli("add", "Estudar", "--at", "14:00")
        self.at("2026-10-19T14:00:00-03:00")
        self.reminders()
        code, snoozed = self.cli("snooze", "30")
        self.assertEqual(code, 0, snoozed)
        self.assertEqual(snoozed["snoozed"]["time"], "14:30")
        self.at("2026-10-19T14:30:00-03:00")
        self.assertIn("14:30 · Estudar", self.reminders())
        code, done = self.cli("done")
        self.assertEqual(done["done"]["status"], "done")

    def test_last_without_reminder_is_refused(self):
        self.configure()
        code, result = self.cli("done")
        self.assertEqual(code, 1)
        self.assertIn("no reminded item", result["error"])

    def test_past_time_refused(self):
        self.configure()
        self.at("2026-10-19T15:00:00-03:00")
        code, result = self.cli("add", "Algo", "--at", "14:00")
        self.assertEqual(code, 1)
        self.assertIn("in the past", result["error"])

    def test_add_without_timezone_asks_for_it(self):
        code, result = self.cli("add", "Algo", "--in", "5")
        self.assertEqual(code, 1)
        self.assertIn("timezone", result["error"])

    def test_plan_replaces_only_future_planned_items(self):
        self.configure()
        self.at("2026-10-19T08:00:00-03:00")
        self.cli("add", "Dentista", "--at", "2026-10-20 10:00")
        self.cli("plan", "2026-10-20", json.dumps([{"text": "Inglês", "time": "09:00", "minutes": 50}]))
        code, result = self.cli("plan", "2026-10-20", json.dumps([{"text": "Oracle", "time": "15:00"}]))
        self.assertEqual(code, 0, result)
        texts = [item["text"] for item in result["items"]]
        self.assertEqual(texts, ["Dentista", "Oracle"])

    def test_unknown_config_key_refused(self):
        code, result = self.cli("config", "set", json.dumps({"timezon": "UTC"}))
        self.assertEqual(code, 1)
        self.assertIn("unknown config keys", result["error"])


class Routines(Home):
    def gate(self) -> dict:
        return json.loads(ritmo_rotinas.gate().splitlines()[-1])

    def test_silent_until_onboarded(self):
        self.configure(onboarded=False)
        self.at("2026-10-19T07:30:00-03:00")
        self.assertEqual(self.gate(), {"wakeAgent": False})

    def test_morning_wakes_once_per_day(self):
        self.configure()
        self.at("2026-10-19T07:29:00-03:00")
        self.assertEqual(self.gate(), {"wakeAgent": False})
        self.at("2026-10-19T07:30:00-03:00")
        context = self.gate()
        self.assertEqual(context["routine"], "morning")
        self.assertIn("today", context)
        self.at("2026-10-19T07:31:00-03:00")
        self.assertEqual(self.gate(), {"wakeAgent": False})
        self.at("2026-10-20T07:31:00-03:00")
        self.assertEqual(self.gate()["routine"], "morning")

    def test_weekly_plan_on_its_day(self):
        self.configure()
        self.at("2026-10-25T20:00:00-03:00")  # a Sunday
        context = self.gate()
        self.assertEqual(context["routine"], "weekly")
        self.assertEqual(len(context["fixed_by_day"]), 7)

    def test_routine_follows_owner_zone_not_server(self):
        self.configure(timezone="Asia/Tokyo")
        self.at("2026-10-19T07:30:00+09:00")
        self.assertEqual(self.gate()["routine"], "morning")

    def test_disabled_routine_never_wakes(self):
        self.configure(routines={"morning": {"enabled": False}})
        self.at("2026-10-19T07:30:00-03:00")
        self.assertEqual(self.gate(), {"wakeAgent": False})


if __name__ == "__main__":
    unittest.main()
