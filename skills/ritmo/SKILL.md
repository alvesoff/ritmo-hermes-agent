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
| See the setup | `config show` → `{"setup": {"needed", "missing"} or {"repair"}, "config": {...}}` |
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
| Backup for an update | `export --send` |
| Restore a backup | `import 'RITMO-BACKUP-1:...'` |
| Something wrong | `doctor` |

Ids for habits and fixed commitments are short lowercase words you choose
(`water`, `gym`). Days are `mon tue wed thu fri sat sun`.

Settings keys: `name`, `language`, `timezone`, `tone` (`gentle`, `direct` or
`tough`), `wake`, `sleep`,
`quiet` (`{"start","end"}`), `routines` (`morning`/`evening`: `time`, `enabled`,
`days`; `weekly`: `day`, `time`, `enabled`), `goals`, `texts`,
`reminder_grace_minutes`, `onboarded`. Booleans are JSON `true`/`false`.

`texts` overrides the wording of the automatic messages. Each one may use only
its own placeholders, spelled exactly so (in English, whatever the language):
`reminder` and `fixed`: `{time}` `{text}`; `duration`: `{minutes}`;
`habit`: `{text}`; `missed`: `{items}`; `footer`: none.

Routines never fall inside quiet hours: a `config set` that would put one
there is refused, so change `quiet` and the routine time in the same call.

## Onboarding

Ask one or two things per message, save each answer right away, and keep it
light. Skip any step whose answer `config show` already has (its
`setup.missing` lists what is left). In this order:

1. Their name and the city they live in. Never ask for a "timezone" and never
   show a zone name: turn the city into its IANA `Area/City` zone yourself
   (abbreviations like EST are refused), save it with `name` and `language`
   (the language they write in), then run `now` and confirm in plain words
   ("aí agora são 19:40, certo?"). If the hour is wrong, the city was ambiguous
   or spans several zones: ask for the state or country, or simply what time
   it is there now, and pick the zone that matches.
2. When they usually wake up and go to sleep → `wake`, `sleep`. Offer quiet
   hours from sleep to wake and save `quiet`; if the morning brief (07:30 by
   default) would fall inside them, move it to the wake time in the same call.
3. Fixed commitments in their week (work hours, classes, gym) → `fixed add`.
4. Habits they want nudges for, with how often ("water every 2h") → `habit add`.
5. What they want to make progress on, in their own words ("passar no
   concurso", "ser milionário") → `goal add`.
6. How hard you should push: gentle, direct or tough (describe each in a few
   words of their language) → `tone`.
7. When they want the morning brief, the evening close and the weekly plan.
   Defaults are 07:30, 21:00 and Sunday 20:00; say them and let them change.
   Keep them clear of fixed commitments (no evening close in the middle of a
   class).

Then `config set '{"onboarded": true}'` and send one message: what tomorrow
looks like and when they will hear from you. Skipping a question is fine;
the defaults stand and they can change anything later.

## Backup and restore

A new version of Ritmo on Plow starts as a new agent with an empty memory, so
the routine travels through the owner's chat:

- **Backup** ("backup", "vou atualizar", "me manda o backup"): run
  `export --send`. The code reaches the owner by itself within a minute, as
  its own message; you never type it. Tell them in one line to forward that
  message to you after the update.
- **Restore**, only in the owner's one-to-one chat: a message containing
  `RITMO-BACKUP-1:` is the owner bringing their setup back. Copy the code from
  `RITMO-BACKUP-1:` up to and including its final dot, nothing before or after,
  into `import 'RITMO-BACKUP-1:....'`. If `config show` says setup is not
  needed, the owner is already set up here: ask first, because the backup
  replaces the current settings. Then confirm in one or two lines what came
  back, name anything in `expired_during_update` (it passed while they were
  updating), and skip onboarding. Never repeat the code. Write that
  confirmation, and everything after it, in the restored `language`: the
  forwarded code has no words to tell you the owner's language.

## Answering a reminder

Reminders arrive as messages like `⏰ 14:00 · Study English (50 min)` and are
in this conversation's history. A reply right after one is about it:

- "done", "feito", "listo" → `done`
- "skip", "pular", "hoje não" → `skip`; offer one other slot only if the owner
  seems to want it, then `move` it
- "snooze 20", "adiar 20", "in an hour" → `snooze 20` / `snooze 60`
- "I can't today" → `skip`, then look at `list` and move what still fits

A habit nudge (`⏰ Drink water`) needs no command: just acknowledge. When
`last` is refused because the message named several items or was hours ago,
ask which one in a short line, then use its id from `list`.

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
the facts you need, including the owner's `tone`; write in that tone (see the
persona). Your final response is the message the owner receives. Answer
`[SILENT]` when there is genuinely nothing useful to say.

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
