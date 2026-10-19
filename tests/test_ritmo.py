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
        self.assertIn("Passaram sem aviso: 09:00 Reunião", message)
        self.assertEqual(self.reminders(), "")

    def test_planned_item_in_quiet_waits_for_quiet_end_and_is_not_missed(self):
        self.configure()
        self.at("2026-10-19T20:00:00-03:00")
        self.cli("add", "Revisar", "--at", "23:00", "--plan")
        for moment in ("2026-10-19T23:00:00-03:00", "2026-10-20T01:01:00-03:00", "2026-10-20T06:59:00-03:00"):
            self.at(moment)
            self.assertEqual(self.reminders(), "", moment)
        self.at("2026-10-20T07:00:00-03:00")
        self.assertIn("23:00 · Revisar", self.reminders())

    def test_missed_notice_waits_for_quiet_to_end(self):
        self.configure()
        self.at("2026-10-19T20:00:00-03:00")
        self.cli("add", "Ligar", "--at", "21:00")
        self.at("2026-10-19T23:30:00-03:00")
        self.assertEqual(self.reminders(), "")
        self.at("2026-10-20T07:00:00-03:00")
        self.assertIn("Passaram sem aviso", self.reminders())

    def test_bad_template_refused_when_saved(self):
        code, result = self.cli("config", "set", json.dumps({
            "timezone": "America/Sao_Paulo", "texts": {"reminder": "{hora} · {texto}"}}))
        self.assertEqual(code, 1)
        self.assertIn("{text}, {time}", result["error"])

    def test_bad_template_on_disk_never_marks_a_reminder_sent(self):
        self.configure()
        self.at("2026-10-19T09:00:00-03:00")
        self.cli("add", "Treino", "--at", "09:05")
        with open(core.path("config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        config["texts"] = {"reminder": "{hora}"}
        with open(core.path("config.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        self.at("2026-10-19T09:05:00-03:00")
        self.assertEqual(ritmo_lembretes.main(), 0)
        agenda = core.load_agenda()
        self.assertIsNone(agenda["items"][0].get("reminded_at"))

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
        self.configure(quiet={"start": "21:00", "end": "07:00"}, routines={"evening": {"time": "20:00"}})
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

    def test_last_ignores_a_stale_unanswered_reminder(self):
        self.configure()
        self.at("2026-10-18T13:00:00-03:00")
        self.cli("add", "Ontem", "--at", "14:00")
        self.at("2026-10-18T14:00:00-03:00")
        self.reminders()
        self.at("2026-10-19T10:00:00-03:00")
        code, result = self.cli("done")
        self.assertEqual(code, 1)
        self.assertIn("hours ago", result["error"])

    def test_last_refuses_to_guess_between_two_items_of_one_message(self):
        self.configure()
        self.at("2026-10-19T13:00:00-03:00")
        self.cli("add", "A", "--at", "14:00")
        self.cli("add", "B", "--at", "14:00")
        self.at("2026-10-19T14:00:00-03:00")
        self.reminders()
        code, result = self.cli("done")
        self.assertEqual(code, 1)
        self.assertIn("several items", result["error"])

    def test_in_minutes_counts_real_time_across_dst_fall_back(self):
        self.configure(timezone="America/New_York")
        self.at("2026-11-01T01:50:00-04:00")
        code, result = self.cli("add", "Y", "--in", "20")
        self.assertEqual(code, 0, result)
        stored = core.load_agenda()["items"][0]["at"]
        self.assertEqual(dt.datetime.fromisoformat(stored).astimezone(dt.timezone.utc),
                         dt.datetime(2026, 11, 1, 6, 10, tzinfo=dt.timezone.utc))

    def test_timezone_change_keeps_wall_clock_time(self):
        self.configure()
        self.at("2026-10-19T09:00:00-03:00")
        self.cli("add", "W", "--at", "2026-10-20 14:00")
        code, result = self.cli("config", "set", json.dumps({"timezone": "Europe/Lisbon"}))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["reanchored_items"], 1)
        self.at("2026-10-19T13:00:00+01:00")
        code, listed = self.cli("list", "--date", "2026-10-20")
        self.assertEqual(listed["days"]["2026-10-20"]["items"][0]["time"], "14:00")

    def test_plan_and_move_refuse_the_past(self):
        self.configure()
        self.at("2026-10-19T12:00:00-03:00")
        code, result = self.cli("plan", "2025-10-19", json.dumps([{"text": "A", "time": "09:00"}]))
        self.assertEqual(code, 1)
        code, result = self.cli("plan", "2026-10-19", json.dumps([{"text": "A", "time": "09:00"}]))
        self.assertEqual(code, 1)
        self.assertIn("in the past", result["error"])
        code, added = self.cli("add", "B", "--at", "15:00")
        code, result = self.cli("move", added["added"]["id"], "--at", "2026-10-19 08:00")
        self.assertEqual(code, 1)

    def test_bad_input_answers_json_not_a_traceback(self):
        self.configure()
        self.at("2026-10-19T12:00:00-03:00")
        code, result = self.cli("add", "Z", "--at", "2026-02-30 10:00")
        self.assertEqual(code, 1)
        self.assertIn("not a real date", result["error"])
        code, result = self.cli("plan", "2026-10-20", json.dumps([{"text": "A", "time": "09:00", "minutes": "50min"}]))
        self.assertEqual(code, 1)
        self.assertIn("whole number", result["error"])

    def test_string_false_is_refused_not_read_as_true(self):
        self.configure()
        code, result = self.cli("config", "set", json.dumps({"routines": {"morning": {"enabled": "false"}}}))
        self.assertEqual(code, 1)
        self.assertIn("true or false", result["error"])

    def test_routine_inside_quiet_hours_refused_with_a_fix(self):
        self.configure()
        code, result = self.cli("config", "set", json.dumps({"quiet": {"start": "23:00", "end": "08:00"}}))
        self.assertEqual(code, 1)
        self.assertIn("08:00 or later", result["error"])
        code, result = self.cli("config", "set", json.dumps({
            "quiet": {"start": "23:00", "end": "08:00"}, "routines": {"morning": {"time": "08:00"}}}))
        self.assertEqual(code, 0, result)

    def test_broken_config_can_still_be_diagnosed_and_repaired(self):
        self.configure()
        with open(core.path("config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        config["quiet"] = "22:00"
        with open(core.path("config.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        code, report = self.cli("doctor")
        self.assertEqual(code, 0)
        self.assertFalse(report["config_valid"])
        code, result = self.cli("add", "X", "--in", "5")
        self.assertEqual(code, 1)
        self.assertIn("config unset", result["error"])
        code, result = self.cli("config", "unset", "quiet")
        self.assertEqual(code, 0, result)

    def test_unknown_config_key_refused(self):
        code, result = self.cli("config", "set", json.dumps({"timezon": "UTC"}))
        self.assertEqual(code, 1)
        self.assertIn("unknown config keys", result["error"])


class Backup(Home):
    def test_a_new_agent_gets_the_routine_back_from_the_forwarded_code(self):
        self.configure(name="Ana", wake="05:20")
        self.cli("habit", "add", "agua", "Beber água", "--every", "120", "--start", "06:00", "--end", "22:00")
        self.cli("fixed", "add", "onibus", "Pegar ônibus", "--days", "mon,tue,wed,thu,fri", "--start", "06:20", "--end", "06:50")
        self.at("2026-10-19T10:00:00-03:00")
        self.cli("add", "Dentista", "--at", "2026-10-21 15:00")
        self.cli("add", "Já passou", "--at", "10:30")
        code, exported = self.cli("export")
        self.assertEqual(code, 0, exported)
        self.assertEqual(exported["items"], 2)
        self.assertTrue(exported["code"].endswith("."))

        shutil.rmtree(os.path.join(self.home, "ritmo"))  # the new agent's empty home
        self.at("2026-10-19T11:00:00-03:00")
        forwarded = "Guarde esta mensagem:\n" + exported["code"][:40] + "\n" + exported["code"][40:] + "\nobrigado"
        code, restored = self.cli("import", forwarded)
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["restored"]["habits"], 1)
        self.assertEqual(restored["restored"]["fixed"], 1)
        self.assertEqual(restored["restored"]["items_added"], 1)
        self.assertEqual([item["text"] for item in restored["expired_during_update"]], ["Já passou"],
                         "what passed during the update is reported, not silently dropped")
        self.assertEqual(core.load_config()["wake"], "05:20")
        code, again = self.cli("import", exported["code"])
        self.assertEqual(again["restored"]["items_added"], 0, "importing twice adds nothing")

    def test_team_settings_travel_but_connections_do_not(self):
        self.configure(name="Ana")
        core.write_json("equipe.json", {"enabled": True, "repos": [{"repo": "acme/app"}]})
        core.write_json("github.json", {"access_token": "secret"})
        code, exported = self.cli("export")
        shutil.rmtree(os.path.join(self.home, "ritmo"))
        code, restored = self.cli("import", exported["code"])
        self.assertEqual(code, 0, restored)
        self.assertTrue(restored["restored"]["team"])
        self.assertIn("reconnect", restored)
        self.assertEqual(core.read_json("equipe.json", {})["repos"][0]["repo"], "acme/app")
        self.assertNotIn("secret", json.dumps(ritmo.decode_backup(exported["code"])))
        self.assertFalse(os.path.exists(core.path("github.json")))

    def test_any_words_after_the_code_are_ignored(self):
        self.configure(name="Ana")
        code, exported = self.cli("export")
        for trailer in ("ok", "oi", "bom dia", "Valeu Ritmo", "abc", "a"):
            code, result = self.cli("import", exported["code"] + "\n" + trailer)
            self.assertEqual(code, 0, (trailer, result))

    def test_damaged_code_is_refused_without_changing_anything(self):
        self.configure(name="Ana")
        code, exported = self.cli("export")
        self.configure(name="Bia", timezone="Europe/Lisbon")
        with open(core.path("config.json"), encoding="utf-8") as handle:
            before = handle.read()
        code, result = self.cli("import", exported["code"][:-21] + ".")
        self.assertEqual(code, 1)
        self.assertIn("damaged", result["error"])
        with open(core.path("config.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_send_delivers_the_code_verbatim_on_the_next_quiet_minute(self):
        self.configure(name="Ana")
        self.at("2026-10-19T10:00:00-03:00")
        self.cli("add", "Treino", "--at", "10:01")
        code, queued = self.cli("export", "--send")
        self.assertEqual(code, 0, queued)
        self.assertNotIn("code", queued, "the model never sees the code it would have to copy")
        self.at("2026-10-19T10:01:00-03:00")
        message = self.reminders()
        self.assertIn("Treino", message)
        self.assertNotIn("RITMO-BACKUP-1", message, "a reminder minute stays a reminder")
        self.at("2026-10-19T10:02:00-03:00")
        backup = self.reminders()
        self.assertTrue(backup.startswith("RITMO-BACKUP-1:") and backup.endswith("."))
        self.assertEqual(self.reminders(), "", "sent once")

    def test_keys_from_a_newer_version_are_left_behind(self):
        self.configure(name="Ana")
        code, exported = self.cli("export")
        payload = ritmo.decode_backup(exported["code"])
        payload["config"]["future_feature"] = 1
        packed = ritmo.zlib.compress(json.dumps(payload).encode())
        code_text = ritmo.BACKUP_PREFIX + ritmo.base64.urlsafe_b64encode(packed).decode().rstrip("=") + "."
        code, result = self.cli("import", code_text)
        self.assertEqual(code, 0, result)
        self.assertNotIn("future_feature", core.read_json("config.json", {}))


class RestoreFromChat(Home):
    """`import --from-chat` reads the owner's message from Hermes' state.db, never a retyped copy."""

    SESSIONS = [("s-owner", "cht_owner", "dm"), ("s-owner-before", "cht_owner", "dm"),
                ("s-other", "cht_other", "dm"), ("s-group", "cht_group", "group")]

    def setUp(self):
        super().setUp()
        names = ("HERMES_SESSION_ID", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_TYPE", "PLOW_HOME_CHANNEL")
        saved = {name: os.environ.get(name) for name in names}
        self.addCleanup(lambda: [os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)
                                 for n, v in saved.items()])
        self.in_chat("s-owner", "cht_owner", "dm")
        os.environ["PLOW_HOME_CHANNEL"] = "cht_owner"
        self.configure(name="Ana", wake="05:20")
        self.mine = self.cli("export")[1]["code"]
        self.configure(name="Intruso", wake="09:00")
        self.theirs = self.cli("export")[1]["code"]
        shutil.rmtree(os.path.join(self.home, "ritmo"))

    def in_chat(self, session, chat, chat_type):
        os.environ.update(HERMES_SESSION_ID=session, HERMES_SESSION_CHAT_ID=chat, HERMES_SESSION_CHAT_TYPE=chat_type)

    def chat_store(self, *messages):
        """Hermes' own tables, holding `(session, content, minutes_ago)` user messages."""
        import sqlite3
        import time
        connection = sqlite3.connect(os.path.join(self.home, "state.db"))
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, chat_id TEXT, chat_type TEXT)")
        connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                           "role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, api_content TEXT)")
        connection.executemany("INSERT INTO sessions VALUES (?, 'plow_chat', ?, ?)", self.SESSIONS)
        for session, content, minutes_ago in messages:
            connection.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                               (session, content, time.time() - minutes_ago * 60))
        connection.commit()
        connection.close()

    def restore(self):
        return self.cli("import", "--from-chat")

    def test_only_the_owners_own_message_is_read(self):
        self.chat_store(
            ("s-owner", "Guarde:\n" + self.mine[:50] + "\n" + self.mine[50:] + "\nvaleu", 3),
            ("s-other", self.theirs, 1),
            ("s-group", self.theirs, 1),
            ("s-owner", "[Cron delivery: ritmo-lembretes]\n" + self.theirs, 0.5),
        )
        code, restored = self.restore()
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["restored"]["name"], "Ana",
                         "another person's chat, a group and Ritmo's own mirrored export are never read")
        self.assertEqual(core.load_config()["wake"], "05:20")

    def test_a_markdown_escaped_message_still_restores(self):
        escaped = self.mine.replace("_", "\\_").replace("-", "\\-").replace(".", "\\.")
        for cut in range(40, 60):  # wherever the line breaks, even right after a backslash
            os.path.exists(os.path.join(self.home, "state.db")) and os.remove(os.path.join(self.home, "state.db"))
            self.chat_store(("s-owner", escaped[:cut] + "\n" + escaped[cut:], 1))
            code, restored = self.restore()
            self.assertEqual(code, 0, (cut, restored))
            self.assertEqual(restored["restored"]["name"], "Ana")
        os.remove(os.path.join(self.home, "state.db"))
        mirrored = "\\[Cron delivery: ritmo\\-lembretes\\]\n" + self.theirs.replace("-", "\\-")
        self.chat_store(("s-owner", mirrored, 1))
        code, result = self.restore()
        self.assertEqual(code, 1, "an escaped mirror of Ritmo's own export is still not the owner's")

    def test_a_chat_that_moved_to_a_new_session_still_counts(self):
        self.chat_store(("s-owner-before", self.mine, 2))
        code, restored = self.restore()
        self.assertEqual(code, 0, restored)

    def test_a_damaged_newest_code_is_reported_not_swapped_for_an_older_one(self):
        self.chat_store(("s-owner", self.theirs, 20), ("s-owner", self.mine[:-30] + ".", 1))
        code, result = self.restore()
        self.assertEqual(code, 1)
        self.assertIn("damaged", result["error"])
        self.assertFalse(os.path.exists(core.path("config.json")))

    def test_codes_arrive_inside_parts_or_after_a_mirrored_reminder(self):
        parts = "\x00json:" + json.dumps([{"type": "text", "text": "Guarde:\n" + self.mine[:60] + "\n" + self.mine[60:]},
                                           {"type": "image_url", "image_url": {"url": "x"}}])
        self.chat_store(("s-owner", parts, 1))
        code, restored = self.restore()
        self.assertEqual(code, 0, restored)
        os.remove(os.path.join(self.home, "state.db"))
        merged = "[Cron delivery: ritmo-lembretes]\n⏰ Beber água\n\n" + self.mine
        self.chat_store(("s-owner", merged, 1))
        code, restored = self.restore()
        self.assertEqual(code, 0, restored)

    def test_refused_outside_the_owners_one_to_one_chat(self):
        self.chat_store(("s-group", self.mine, 1), ("s-other", self.mine, 1))
        for session, chat, chat_type in (("s-group", "cht_group", "group"), ("s-other", "cht_other", "dm")):
            self.in_chat(session, chat, chat_type)
            code, result = self.restore()
            self.assertEqual(code, 1, chat)
            self.assertIn("only from the owner", result["error"])
        for name in ("HERMES_SESSION_ID", "HERMES_SESSION_CHAT_ID"):
            os.environ.pop(name)
        code, result = self.restore()
        self.assertIn("only while answering", result["error"])

    def test_nothing_to_read_asks_again_and_a_code_with_the_flag_is_ambiguous(self):
        code, result = self.restore()
        self.assertIn("missing", result["error"])
        self.chat_store(("s-owner", "oi", 1))
        code, result = self.restore()
        self.assertEqual(code, 1)
        self.assertIn("forward it again", result["error"])
        code, result = self.cli("import", "--from-chat", self.mine)
        self.assertIn("not both", result["error"])
        self.assertFalse(os.path.exists(core.path("config.json")))


class Setup(Home):
    def test_config_show_says_what_setup_still_needs(self):
        code, shown = self.cli("config", "show")
        self.assertEqual(shown["setup"], {"needed": True, "missing": ["timezone", "name", "language", "onboarding"]})
        self.cli("config", "set", json.dumps({"timezone": "America/Sao_Paulo", "name": "Ana"}))
        code, shown = self.cli("config", "show")
        self.assertEqual(shown["setup"]["missing"], ["language", "onboarding"])

    def test_a_broken_config_asks_for_repair_not_onboarding(self):
        self.configure()
        with open(core.path("config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        config["quiet"] = "22:00"
        with open(core.path("config.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        code, shown = self.cli("config", "show")
        self.assertEqual(shown["setup"], {"needed": False, "repair": True})

    def test_abbreviated_zones_without_dst_are_refused(self):
        code, result = self.cli("config", "set", json.dumps({"timezone": "EST"}))
        self.assertEqual(code, 1)
        self.assertIn("America/New_York", result["error"])

    def test_tough_tone_changes_the_reminder_footer(self):
        self.configure(tone="tough")
        self.at("2026-10-19T17:00:00-03:00")
        self.cli("add", "Estudar", "--at", "18:00", "--minutes", "50")
        self.at("2026-10-19T18:00:00-03:00")
        self.assertIn("Sem desculpa.", self.reminders())

    def test_unknown_tone_refused(self):
        code, result = self.cli("config", "set", json.dumps({"tone": "brutal"}))
        self.assertEqual(code, 1)
        self.assertIn("gentle, direct, tough", result["error"])


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
        self.at("2026-10-18T20:00:00-03:00")
        self.cli("add", "Last Sunday", "--at", "2026-10-18 21:00")
        self.at("2026-10-25T20:00:00-03:00")  # a Sunday
        context = self.gate()
        self.assertEqual(context["routine"], "weekly")
        self.assertEqual(len(context["fixed_by_day"]), 7)
        self.assertEqual(context["last_7_days"], [], "the previous Sunday belongs to the previous review")

    def test_no_routine_inside_quiet_hours(self):
        self.configure(quiet={"start": "21:30", "end": "07:00"})
        self.at("2026-10-19T21:15:00-03:00")
        self.assertEqual(self.gate()["routine"], "evening")
        self.configure(quiet={"start": "21:10", "end": "07:00"}, routines={"evening": {"time": "21:00"}})
        state = core.load_state()
        state["routines"] = {}
        core.write_json("state.json", state)
        self.at("2026-10-19T21:15:00-03:00")
        self.assertEqual(self.gate(), {"wakeAgent": False})

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
