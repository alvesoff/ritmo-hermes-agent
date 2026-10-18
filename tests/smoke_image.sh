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
for f in /opt/hermes/plow-seed/persona.md /opt/ritmo/ritmo_boot.py /opt/ritmo/skills.list \
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

echo "== an empty agent home"
rm -rf /tmp/rh && mkdir -p /tmp/rh && cp /opt/hermes/plow-seed/config.yaml /tmp/rh/config.yaml
chown -R hermes:hermes /tmp/rh

echo "== hermes cron create --help (for the log)"
$AS_AGENT /opt/hermes/bin/hermes cron create --help | head -40 || true

echo "== boot step"
$AS_AGENT RITMO_SCRIPTS_SOURCE=/opt/hermes/skills/ritmo/scripts $PY /opt/ritmo/ritmo_boot.py
$AS_AGENT $PY - <<'PY' || fail "jobs.json is not what the boot step should register"
import json, yaml
jobs = {j["name"]: j for j in json.load(open("/tmp/rh/cron/jobs.json"))["jobs"]}
print(json.dumps(jobs, indent=1, default=str)[:4000])
assert set(jobs) >= {"ritmo-lembretes", "ritmo-rotinas"}, jobs.keys()
lemb, rot = jobs["ritmo-lembretes"], jobs["ritmo-rotinas"]
assert lemb.get("no_agent") is True, lemb
assert lemb.get("script", "").endswith("ritmo_lembretes.py"), lemb
assert rot.get("script", "").endswith("ritmo_rotinas.py"), rot
assert not rot.get("no_agent"), rot
skills = rot.get("skills") or [rot.get("skill")]
assert "ritmo" in skills, rot
for job in (lemb, rot):
    assert job.get("deliver") == "plow_chat", job.get("deliver")
config = yaml.safe_load(open("/tmp/rh/config.yaml"))
assert config["cron"]["wrap_response"] is False
assert config["cron"]["mirror_delivery"] is True
print("jobs and config OK")
PY
for f in ritmo_core.py ritmo_lembretes.py ritmo_rotinas.py; do
  [ -f "/tmp/rh/scripts/$f" ] || fail "boot step did not publish $f"
done

echo "== boot step again: idempotent"
before=$(cat /tmp/rh/cron/jobs.json | md5sum)
$AS_AGENT $PY /opt/ritmo/ritmo_boot.py
after=$(cat /tmp/rh/cron/jobs.json | md5sum)
[ "$before" = "$after" ] || fail "a second boot changed jobs.json"

echo "== the scripts on the image's Python"
R="$AS_AGENT $PY /opt/hermes/skills/ritmo/scripts/ritmo.py"
$R config set '{"timezone": "America/Sao_Paulo", "language": "pt-BR", "onboarded": true}' >/dev/null
$AS_AGENT RITMO_NOW=2026-10-19T09:00:00-03:00 $PY /opt/hermes/skills/ritmo/scripts/ritmo.py add "Teste" --at 09:05 >/dev/null
out=$($AS_AGENT RITMO_NOW=2026-10-19T09:05:00-03:00 $PY /tmp/rh/scripts/ritmo_lembretes.py)
echo "$out"
echo "$out" | grep -q "09:05 · Teste" || fail "reminder did not fire from the published script"
gate=$($AS_AGENT RITMO_NOW=2026-10-19T07:30:00-03:00 $PY /tmp/rh/scripts/ritmo_rotinas.py | tail -1)
echo "$gate" | grep -q '"routine":"morning"' || fail "routine gate did not wake for the morning"
quiet=$($AS_AGENT RITMO_NOW=2026-10-19T07:31:00-03:00 $PY /tmp/rh/scripts/ritmo_rotinas.py | tail -1)
[ "$quiet" = '{"wakeAgent": false}' ] || fail "routine gate woke twice: $quiet"

echo "SMOKE OK"
