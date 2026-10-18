---
name: ritmo
description: Read and change the owner's routine (timezone, quiet hours, habits, fixed commitments, goals, reminders and the daily plan) and run the morning brief, evening close and weekly plan. Use for every reminder, schedule question, reply to a reminder ("done", "snooze 20", "skip"), setup step, and whenever a ritmo routine job fires.
---

# Ritmo

```sh
python3 /opt/hermes/skills/ritmo/scripts/ritmo.py <command>
```

The table below lists only the `<command>` part; always run it with the full
line above.

Every command prints JSON. `{"error": ...}` means nothing changed: fix what it
says or ask the owner. Times are the owner's wall clock (`HH:MM`, 24h) and dates
are `YYYY-MM-DD`; the script handles the timezone and daylight saving.

| Need | Command |
|---|---|
| Owner's current time | `now` |
| See the setup | `config show` |
| Change settings | `config set '{"timezone": "America/Sao_Paulo", "language": "pt-BR"}'` |
| Reminder at a time | `add "Call the accountant" --at "2026-10-20 10:00"` (or `--at 15:30` for today) |
| Reminder in N minutes | `add "Take the cake out" --in 20` |
| With a duration | `add "Study English" --at 14:00 --minutes 50` |
| Agenda | `list` (today), `list --date tomorrow`, `list --date 2026-10-22 --days 7` |
| Answer the last reminder | `done`, `skip`, `snooze 30` (or give an item id) |
| Reschedule / delete | `move <id> --at "2026-10-21 09:00"`, `remove <id>` |
| Habit | `habit add water "Drink water" --every 120 --start 08:00 --end 22:00 [--days mon,tue,...]`; `habit remove water` |
| Fixed commitment | `fixed add gym "Gym" --days mon,wed,fri --start 18:00 --end 19:00`; `fixed remove gym` |
| Goal | `goal add "Study 5h of English a week"`; `goal remove 0` |
| Plan one day | `plan 2026-10-21 '[{"text": "Study English", "time": "09:00", "minutes": 50}]'` |
| Something wrong | `doctor` |

Ids for habits and fixed commitments are short lowercase words you choose
(`water`, `gym`). Days are `mon tue wed thu fri sat sun`.

Settings keys: `name`, `language`, `timezone`, `wake`, `sleep`,
`quiet` (`{"start","end"}`), `routines` (`morning`/`evening`: `time`, `enabled`,
`days`; `weekly`: `day`, `time`, `enabled`), `goals`, `texts` (overrides for the
reminder wording: `reminder`, `habit`, `fixed`, `footer`, `missed`),
`reminder_grace_minutes`, `onboarded`.

## Onboarding

Ask one or two things per message, save each answer right away, and keep it
light. In this order:

1. Their name and where they live. Turn the place into an IANA zone yourself
   ("São Paulo" → `America/Sao_Paulo`) and save it with `language` (the
   language they write in).
2. When they usually wake up and go to sleep → `wake`, `sleep`. Offer quiet
   hours from sleep to wake and save `quiet`.
3. Fixed commitments in their week (work hours, classes, gym) → `fixed add`.
4. Habits they want nudges for, with how often ("water every 2h") → `habit add`.
5. What they want to make progress on this week → `goal add`.
6. When they want the morning brief, the evening close and the weekly plan.
   Defaults are 07:30, 21:00 and Sunday 20:00; say them and let them change.

Then `config set '{"onboarded": true}'` and send one message: what tomorrow
looks like and when they will hear from you. Skipping a question is fine;
the defaults stand and they can change anything later.

## Answering a reminder

Reminders arrive as messages like `⏰ 14:00 · Study English (50 min)` and are
in this conversation's history. A reply right after one is about it:

- "done", "feito", "listo" → `done`
- "skip", "pular", "hoje não" → `skip`; offer one other slot only if the owner
  seems to want it, then `move` it
- "snooze 20", "adiar 20", "in an hour" → `snooze 20` / `snooze 60`
- "I can't today" → `skip`, then look at `list` and move what still fits

When a change pushes later items, `move` them so nothing overlaps and nothing
lands in quiet hours. Reply with the result in one or two lines.

## Planning

Plans come from what the owner told you, never invented. A planned block:
fits between wake and sleep, avoids fixed commitments with 15 minutes around
them, leaves real gaps (no back-to-back day), and serves a goal or something
they asked for. Use `plan <date> '<json>'` per day; it replaces only the
planner's future items on that day and keeps the owner's own reminders.

## Routines

The `ritmo-rotinas` job wakes you with a JSON block naming the `routine` and
the facts you need. Your final response is the message the owner receives.
Answer `[SILENT]` when there is genuinely nothing useful to say.

**morning**: the day in at most 6 lines: fixed commitments and items in time
order, then one line on what matters most today given their goals. If the day
is empty, offer to plan it in one line.

**evening**: what got done (`status: done`), what is still open or `missed`
and where it could go tomorrow, then tomorrow's first commitment. Ask one
question at most ("move English to 9am tomorrow?"). Do not move anything
without a yes.

**weekly**: look at `last_7_days` (done vs skipped/missed) and the goals, then
propose next week day by day in one message: fixed commitments are already
there; add focused blocks for the goals. On "ok", write it with `plan`, one
day at a time, and confirm with the count of blocks per day. On changes,
adjust and show only the changed days.

## Secrets

If the owner pastes a calendar address, token or password, never repeat it
back. Confirm by what it is ("saved your Google calendar"), not by its value.
