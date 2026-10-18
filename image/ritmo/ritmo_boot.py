#!/usr/bin/env python3
"""Ritmo's boot step, run as the agent after plow-init and before the gateway.

Three things, each idempotent, each unable to stop the agent from starting:

1. Hermes settings this agent depends on, written into the agent's own
   config.yaml (plow-init keeps keys it does not manage):
   - cron.wrap_response: false -- otherwise every reminder arrives wrapped in
     "Cronjob Response: ... (job_id: ...)" plumbing.
   - cron.mirror_delivery: true -- reminders land in the conversation history,
     so "done" or "snooze 20" right after one is understood.
   - platforms.plow_chat.gateway_restart_notification: false -- an update must
     not text the owner "Gateway shutting down".
   - timezone -- the owner's zone from Ritmo's config, so the agent's own clock
     and message stamps match theirs from the next boot on.
2. The cron scripts, copied into $HERMES_HOME/scripts (the only place Hermes
   runs job scripts from) from the root-owned copy in the image, every boot, so
   an edit made by a turn never outlives a restart.
3. The two jobs, registered once and re-registered only when their spec in this
   file changes:
   - ritmo-lembretes: no_agent, every minute; prints due reminders or nothing.
   - ritmo-rotinas: agent job, every minute, gated by a script that wakes the
     LLM only when a routine is due in the owner's timezone.

Both deliver to the bare `plow_chat` target, which Hermes resolves to the
owner's home chat at delivery time: a re-minted credential or a new chat id
needs no re-registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

import yaml

HOME = os.environ.get("HERMES_HOME") or "/var/lib/hermes"
CONFIG = os.path.join(HOME, "config.yaml")
JOBS = os.path.join(HOME, "cron", "jobs.json")
SCRIPTS = os.path.join(HOME, "scripts")
FINGERPRINT = os.path.join(HOME, "ritmo", "jobs.fingerprint")
SOURCE = os.environ.get("RITMO_SCRIPTS_SOURCE", "/opt/hermes/skills/ritmo/scripts")
HERMES = os.environ.get("RITMO_HERMES_BIN", "/opt/hermes/bin/hermes")
PUBLISHED = ("ritmo_core.py", "ritmo_lembretes.py", "ritmo_rotinas.py")

ROUTINE_PROMPT = (
    "A Ritmo routine is due. The JSON block above names it in `routine` (morning, evening or weekly) "
    "and carries the owner's facts for it. Follow the ritmo skill's Routines section and write the "
    "message the owner will receive, in their language. If there is nothing useful to say, answer "
    "exactly [SILENT]."
)

# `--failure-deliver local`: a failed run (a provider outage during the morning
# brief, say) stays in `hermes cron list` instead of texting the owner a
# technical error. The scripts themselves never fail; see ritmo_lembretes.py.
JOB_SPECS = (
    {
        "name": "ritmo-lembretes",
        "args": ["* * * * *", "--no-agent", "--script", "ritmo_lembretes.py",
                 "--deliver", "plow_chat", "--failure-deliver", "local", "--name", "ritmo-lembretes"],
    },
    {
        "name": "ritmo-rotinas",
        "args": ["* * * * *", ROUTINE_PROMPT, "--script", "ritmo_rotinas.py", "--skill", "ritmo",
                 "--deliver", "plow_chat", "--failure-deliver", "local", "--name", "ritmo-rotinas"],
    },
)


def log(message: str) -> None:
    print(f"[ritmo-boot] {message}", file=sys.stderr, flush=True)


def owner_timezone() -> str:
    try:
        with open(os.path.join(HOME, "ritmo", "config.json"), encoding="utf-8") as handle:
            zone = json.load(handle).get("timezone", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return zone if isinstance(zone, str) else ""


def configure() -> None:
    with open(CONFIG, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    before = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)

    if not isinstance(config.get("cron"), dict):
        config["cron"] = {}
    cron = config["cron"]
    cron["wrap_response"] = False
    cron["mirror_delivery"] = True
    plow_chat = (config.get("platforms") or {}).get("plow_chat")
    if isinstance(plow_chat, dict):
        # Only beside what the chat plugin already wrote: creating the section
        # ourselves before its first connect is not ours to decide.
        plow_chat["gateway_restart_notification"] = False
    zone = owner_timezone()
    if zone:
        config["timezone"] = zone

    after = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    if after == before:
        log("config.yaml already as Ritmo needs it")
        return
    # Same mode as the file it replaces: a new file would get the umask's
    # default and leave the agent's config readable by everyone.
    mode = os.stat(CONFIG).st_mode & 0o777
    temporary = CONFIG + ".ritmo-new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(after)
    os.chmod(temporary, mode)
    os.replace(temporary, CONFIG)
    log("config.yaml updated (cron delivery, restart notice" + (f", timezone {zone})" if zone else ")"))


def publish_scripts() -> None:
    os.makedirs(SCRIPTS, exist_ok=True)
    for name in PUBLISHED:
        shutil.copyfile(os.path.join(SOURCE, name), os.path.join(SCRIPTS, name))
    log(f"published {len(PUBLISHED)} scripts to {SCRIPTS}")


def registered() -> list[dict]:
    """Hermes' own job store. Absent means empty; unreadable raises, because
    reading "could not tell" as "nothing there" would register duplicates."""
    try:
        with open(JOBS, encoding="utf-8") as handle:
            return json.load(handle).get("jobs", [])
    except FileNotFoundError:
        return []


def hermes(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HERMES, "cron", *args], capture_output=True, text=True, timeout=90)


def fingerprint() -> str:
    spec = json.dumps([job["args"] for job in JOB_SPECS], sort_keys=True)
    return hashlib.sha256(spec.encode()).hexdigest()


def register_jobs() -> None:
    wanted = fingerprint()
    try:
        with open(FINGERPRINT, encoding="utf-8") as handle:
            current = handle.read().strip()
    except FileNotFoundError:
        current = ""
    jobs = registered()
    names = {job.get("name") for job in jobs}
    if current == wanted and all(spec["name"] in names for spec in JOB_SPECS):
        log("jobs already registered")
        return
    for job in jobs:
        if str(job.get("name", "")).startswith("ritmo-"):
            result = hermes("remove", str(job["id"]))
            log(f"removed old {job.get('name')}: rc={result.returncode}")
    failed = False
    for spec in JOB_SPECS:
        result = hermes("create", *spec["args"])
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            failed = True
            log(f"could not register {spec['name']}: {output}")
        else:
            log(f"registered {spec['name']}")
    if not failed:
        os.makedirs(os.path.dirname(FINGERPRINT), exist_ok=True)
        with open(FINGERPRINT, "w", encoding="utf-8") as handle:
            handle.write(wanted + "\n")


def main() -> int:
    for step in (configure, publish_scripts, register_jobs):
        try:
            step()
        except Exception as error:  # noqa: BLE001 -- a boot step never stops the agent from starting
            log(f"{step.__name__} failed: {error.__class__.__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
