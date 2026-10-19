#!/bin/sh
# Runs INSIDE the built image (CI: docker run --entrypoint /bin/sh). It checks
# what the unit tests cannot: that the image's own Python runs the scripts and
# that this pinned Hermes accepts the exact `hermes cron create` calls the boot
# step makes, before any of it reaches a phone.
set -eu

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
PY=/opt/hermes/.venv/bin/python3
AS_AGENT="/command/s6-setuidgid hermes env HOME=/tmp/rh HERMES_HOME=/tmp/rh"

echo "== files"
for f in /opt/hermes/plow-seed/persona.md /opt/ritmo/ritmo_boot.py /opt/ritmo/skills.list /opt/ritmo/github_app.json \
         /opt/hermes/skills/ritmo/SKILL.md /opt/hermes/skills/ritmo/scripts/ritmo.py \
         /etc/s6-overlay/scripts/ritmo-boot.sh /etc/cont-init.d/40-ritmo-skills \
         /etc/s6-overlay/s6-rc.d/ritmo-boot/up /etc/s6-overlay/s6-rc.d/hermes-gateway/dependencies.d/ritmo-boot; do
  [ -e "$f" ] || fail "missing $f"
done
[ -x /etc/s6-overlay/scripts/ritmo-boot.sh ] || fail "ritmo-boot.sh not executable"
grep -qx ritmo /opt/ritmo/skills.list || fail "skills.list does not name ritmo"
[ "$(printenv AGENT_ID)" = "ritmo" ] || fail "AGENT_ID is not baked in"
command -v timeout >/dev/null || fail "no timeout(1) in the image"
for f in /etc/s6-overlay/scripts/ritmo-boot.sh /etc/cont-init.d/40-ritmo-skills; do
  ! grep -q "$(printf '\r')" "$f" || fail "$f has CRLF line endings"
done
echo "python: $($PY --version)"

echo "== the python3 the agent's terminal resolves, with a timezone database"
$AS_AGENT sh -c 'command -v python3 && python3 -c "import zoneinfo; zoneinfo.ZoneInfo(\"America/Sao_Paulo\"); print(\"zoneinfo OK\")"' \
  || fail "python3 on PATH cannot resolve IANA zones"
echo "upstream skill files still executable: $(find /opt/hermes/skills -path /opt/hermes/skills/ritmo -prune -o -type f -perm -u+x -print | wc -l)"

echo "== an empty agent home"
rm -rf /tmp/rh && mkdir -p /tmp/rh && cp /opt/hermes/plow-seed/config.yaml /tmp/rh/config.yaml
chown -R hermes:hermes /tmp/rh && chmod 0640 /tmp/rh/config.yaml

echo "== hermes cron create --help (for the log)"
$AS_AGENT /opt/hermes/bin/hermes cron create --help | head -40 || true

echo "== boot step"
$AS_AGENT $PY /opt/ritmo/ritmo_boot.py
$AS_AGENT $PY - <<'PY' || fail "jobs.json is not what the boot step should register"
import json, yaml
jobs = {j["name"]: j for j in json.load(open("/tmp/rh/cron/jobs.json"))["jobs"]}
print(json.dumps(jobs, indent=1, default=str)[:4000])
assert set(jobs) >= {"ritmo-lembretes", "ritmo-rotinas", "ritmo-equipe"}, jobs.keys()
lemb, rot, eq = jobs["ritmo-lembretes"], jobs["ritmo-rotinas"], jobs["ritmo-equipe"]
assert eq.get("script", "").endswith("equipe_gate.py") and "ritmo-equipe" in (eq.get("skills") or []), eq
assert eq.get("model") == "anthropic/claude-sonnet-5", {k: eq.get(k) for k in ("model", "provider", "model_snapshot")}
assert eq["schedule"]["expr"] == "*/5 * * * *", eq["schedule"]
assert lemb.get("no_agent") is True, lemb
assert lemb.get("script", "").endswith("ritmo_lembretes.py"), lemb
assert rot.get("script", "").endswith("ritmo_rotinas.py"), rot
assert not rot.get("no_agent"), rot
skills = rot.get("skills") or [rot.get("skill")]
assert "ritmo" in skills, rot
for job in (lemb, rot):
    assert job.get("deliver") == "plow_chat", job.get("deliver")
    assert job.get("failure_deliver") == "local", {k: v for k, v in job.items() if "deliver" in k}
config = yaml.safe_load(open("/tmp/rh/config.yaml"))
assert config["cron"]["wrap_response"] is False
assert config["cron"]["mirror_delivery"] is True
print("jobs and config OK")
PY
for f in ritmo_core.py ritmo_lembretes.py ritmo_rotinas.py equipe_core.py equipe.py equipe_gate.py; do
  [ -f "/tmp/rh/scripts/$f" ] || fail "boot step did not publish $f"
done
gate=$($AS_AGENT $PY /tmp/rh/scripts/equipe_gate.py | tail -1)
[ "$gate" = '{"wakeAgent": false}' ] || fail "the team gate is not silent while the module is off: $gate"
$AS_AGENT python3 /opt/hermes/skills/ritmo-equipe/scripts/equipe.py team show | grep -q '"setup_missing"' \
  || fail "equipe.py does not run on the agent's python3"
[ "$(stat -c %a /tmp/rh/config.yaml)" = "640" ] || fail "boot step changed config.yaml's mode to $(stat -c %a /tmp/rh/config.yaml)"

echo "== boot step again: idempotent"
before=$(cat /tmp/rh/cron/jobs.json | md5sum)
$AS_AGENT $PY /opt/ritmo/ritmo_boot.py
after=$(cat /tmp/rh/cron/jobs.json | md5sum)
[ "$before" = "$after" ] || fail "a second boot changed jobs.json"

echo "== the scripts on the image's Python"
$AS_AGENT python3 /opt/hermes/skills/ritmo/scripts/ritmo.py config set \
  '{"timezone": "America/Sao_Paulo", "language": "pt-BR", "onboarded": true}' >/dev/null \
  || fail "ritmo.py config set failed on the agent's python3"
$AS_AGENT RITMO_NOW=2026-10-19T09:00:00-03:00 $PY /opt/hermes/skills/ritmo/scripts/ritmo.py add "Teste" --at 09:05 >/dev/null
out=$($AS_AGENT RITMO_NOW=2026-10-19T09:05:00-03:00 $PY /tmp/rh/scripts/ritmo_lembretes.py)
echo "$out"
echo "$out" | grep -q "09:05 · Teste" || fail "reminder did not fire from the published script"
gate=$($AS_AGENT RITMO_NOW=2026-10-19T07:30:00-03:00 $PY /tmp/rh/scripts/ritmo_rotinas.py | tail -1)
echo "$gate" | grep -q '"routine":"morning"' || fail "routine gate did not wake for the morning"
quiet=$($AS_AGENT RITMO_NOW=2026-10-19T07:31:00-03:00 $PY /tmp/rh/scripts/ritmo_rotinas.py | tail -1)
[ "$quiet" = '{"wakeAgent": false}' ] || fail "routine gate woke twice: $quiet"

echo "== restore reads the owner's message from Hermes' own store"
code=$($AS_AGENT python3 /opt/hermes/skills/ritmo/scripts/ritmo.py export | $PY -c 'import json, sys; print(json.load(sys.stdin)["code"])')
$AS_AGENT RITMO_CODE="$code" $PY - <<'PY' || fail "Hermes' SessionDB did not take the test messages"
import os, sys
sys.path.insert(0, "/opt/hermes")
from hermes_state import SessionDB
code = os.environ["RITMO_CODE"]
db = SessionDB()
db.create_session("s-smoke", "plow_chat", chat_id="cht_smoke", chat_type="dm")
db.create_session("s-stranger", "plow_chat", chat_id="cht_stranger", chat_type="dm")
db.append_message("s-smoke", "user", content="[Cron delivery: ritmo-lembretes]\n" + code)
db.append_message("s-smoke", "user", content=[{"type": "text", "text": "Guarde:\n" + code[:60] + "\n" + code[60:]}])
db.append_message("s-stranger", "user", content="RITMO-BACKUP-1:AAAA.")
db.close()
print("stored")
PY
restored=$($AS_AGENT HERMES_SESSION_ID=s-smoke HERMES_SESSION_CHAT_ID=cht_smoke HERMES_SESSION_CHAT_TYPE=dm \
  PLOW_HOME_CHANNEL=cht_smoke python3 /opt/hermes/skills/ritmo/scripts/ritmo.py import --from-chat) \
  || fail "import --from-chat failed against Hermes' real state.db: $restored"
echo "$restored" | grep -q '"timezone": "America/Sao_Paulo"' || fail "import --from-chat restored something else: $restored"

echo "== the home copy of the skills is read-only for the agent"
mkdir -p /var/lib/hermes/skills && chown hermes:hermes /var/lib/hermes/skills
/etc/cont-init.d/40-ritmo-skills
[ -f /var/lib/hermes/skills/ritmo/scripts/ritmo.py ] || fail "40-ritmo-skills did not copy the skill"
! /command/s6-setuidgid hermes sh -c 'echo x >> /var/lib/hermes/skills/ritmo/scripts/ritmo.py' 2>/dev/null \
  || fail "the agent can still patch its own ritmo.py"
/command/s6-setuidgid hermes cat /var/lib/hermes/skills/ritmo/SKILL.md >/dev/null || fail "the agent cannot read its skill"
/etc/cont-init.d/40-ritmo-skills  # a second boot leaves it alone and still read-only

echo "SMOKE OK"
