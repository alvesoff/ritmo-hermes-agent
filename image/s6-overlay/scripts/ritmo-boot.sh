#!/bin/sh
# Ritmo's boot step: after plow-init, before the gateway (which depends on it).
#
# As the agent, never as root: a home file written by root stops the agent
# from writing its own config. And it always exits 0, because the gateway
# waits on this oneshot and a failure here would leave the agent mute.
# ritmo_boot.py logs what went wrong; the agent still starts.

/command/s6-setuidgid hermes \
  env HOME=/var/lib/hermes HERMES_HOME=/var/lib/hermes \
  timeout 240 /opt/hermes/.venv/bin/python3 /opt/ritmo/ritmo_boot.py \
  || echo "[ritmo-boot] did not finish cleanly; the agent starts anyway" >&2

exit 0
