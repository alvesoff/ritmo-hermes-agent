# Ritmo

A routine assistant that lives in your iMessage. Tell it your week once; it
plans with you, texts each reminder on the minute ("⏰ 14:00 · Study English"),
and re-plans when the day moves. Reply "done", "snooze 20" or "skip" and it
adjusts the rest of the day.

Built on [Plow](https://github.com/plow-pbc/plow-agents) and the
[plow-hermes-agent](https://github.com/plow-pbc/plow-hermes-agent) base.

## What it does

- **Setup by conversation**: your timezone, wake and sleep times, quiet hours,
  fixed commitments, habits ("water every 2h") and goals.
- **Reminders on time, in your timezone**, daylight saving included. They are
  sent without an LLM call, so they are instant and cost nothing.
- **Morning brief, evening close and a weekly plan** at the times you choose.
- **Quiet hours**: it never starts a conversation while you sleep, except for a
  reminder you set for that exact time.

Planned: calendar connectors, and an optional Team module that reviews and
merges your team's pull requests (see [docs/ESPECIFICACAO.md](docs/ESPECIFICACAO.md)).

## Run it

```sh
plow-agents login
plow-agents lines
plow-agents deploy ghcr.io/alvesoff/ritmo-hermes-agent@sha256:<digest> --line ln_xxx
```

Then text the line's number. The first conversation sets you up.

With Docker on your own machine instead: `plow-agents deploy --local --line ln_xxx`.

## How it works

| Piece | Where |
|---|---|
| Persona | `runtime/persona.md` |
| The skill the agent follows | `skills/ritmo/SKILL.md` |
| All time logic (owner timezone, quiet hours, what is due) | `skills/ritmo/scripts/ritmo_core.py` |
| The agent's only way to change the routine | `skills/ritmo/scripts/ritmo.py` |
| Reminder clock: a no-LLM cron job, every minute | `skills/ritmo/scripts/ritmo_lembretes.py` |
| Routine gate: wakes the LLM only when a routine is due | `skills/ritmo/scripts/ritmo_rotinas.py` |
| Boot step: Hermes settings, scripts, job registration | `image/ritmo/ritmo_boot.py` |

Tests: `python -m unittest discover -s tests`. CI also builds the image and
runs `tests/smoke_image.sh` inside it.

## License

MIT, see [LICENSE](LICENSE).
