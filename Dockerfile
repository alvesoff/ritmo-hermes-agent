# Ritmo: the Plow Hermes base, plus a persona, one skill, a boot step and the
# two cron scripts that make it speak first.
#
# The base is pinned by immutable tag AND digest: an agent holding a live Plow
# credential must not have code swapped underneath it by a moved tag.
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-7422f99a12b15489408cacfbf884d745d4eccab5@sha256:efe3b0fdf97601c8a1b14b3faf31d0c919116c40687c5252cf501e5f6ff19898

# Who this agent is on the Agent Index. Baked in because a cloud deploy passes
# no environment; a fork with its own listing overrides it.
ENV AGENT_ID=ritmo

# plow-init composes SOUL.md on every boot as the base persona plus this file.
COPY runtime/persona.md /opt/hermes/plow-seed/persona.md
RUN chmod 0644 /opt/hermes/plow-seed/persona.md

# Skills live outside every home, root-owned: the agent runs ritmo.py from
# here and cannot edit it. The list of OUR skills comes from the build
# context, so a new skill is never forgotten by the boot step that restores them.
COPY skills/ /opt/hermes/skills/
COPY skills/ /tmp/ritmo-skills/
RUN mkdir -p /opt/ritmo \
 && ls -1 /tmp/ritmo-skills > /opt/ritmo/skills.list \
 && for name in $(cat /opt/ritmo/skills.list); do \
      find "/opt/hermes/skills/$name" -type d -exec chmod 0755 {} + ; \
      find "/opt/hermes/skills/$name" -type f -exec chmod 0644 {} + ; \
    done \
 && rm -rf /tmp/ritmo-skills

COPY image/ritmo/ritmo_boot.py /opt/ritmo/ritmo_boot.py
COPY image/s6-overlay/ /etc/s6-overlay/
COPY image/cont-init.d/40-ritmo-skills image/cont-init.d/41-ritmo-refresh-persona /etc/cont-init.d/
RUN chown -R root:root /opt/ritmo \
 && chmod 0644 /opt/ritmo/ritmo_boot.py /opt/ritmo/skills.list \
 && chmod 0755 /etc/s6-overlay/scripts/ritmo-boot.sh \
               /etc/cont-init.d/40-ritmo-skills \
               /etc/cont-init.d/41-ritmo-refresh-persona
