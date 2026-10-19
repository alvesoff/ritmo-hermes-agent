#!/usr/bin/env python3
"""Ritmo's updater: code fixes reach a running agent without recreating it.

Plow cannot update an agent in place: a new image is a new agent with an empty
home, so every fix used to cost the owner a backup, a restore and new
connections. This service (root, supervised by s6) instead installs the newest
published release of this repository over the running image's own Ritmo
files: the skills, the persona and /opt/ritmo. The owner's data, the
connections and Hermes itself are never touched.

What makes it safe to run unattended:
- only a published GitHub release of the repository named in update.json, on its
  default branch (which changes through reviewed pull requests), and only one
  AHEAD of the commit this image was built from: never a downgrade, never a fork,
  and an image that does not know its commit (a local build) never updates;
- only a release with this image's `image_api`: a change to the image itself
  (s6 services, cont-init, the Plow base) bumps it, and that release waits for
  a real redeploy instead of half-installing;
- the release's own test suite runs first, as the agent user, and must pass;
- /opt stays root-owned and read-only to the agent; inside the agent's own home
  root writes nothing by name (the skills copy is refreshed as the agent).
Trust: whoever can publish a release of that repository runs code as root on
every agent that follows it. The owner turns it off with `auto_update: false`;
RITMO_AUTO_UPDATE=off turns it off for the whole image.

  ritmo_update.py            check now and then every 30 minutes (the service)
  ritmo_update.py --once     one check, then exit
  ritmo_update.py --source D install the release unpacked at D (smoke tests)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import py_compile
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.environ.get("RITMO_UPDATE_ROOT", "/")


def at(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


RITMO_DIR = at("opt", "ritmo")
SKILLS_DIR = at("opt", "hermes", "skills")
SEED_DIR = at("opt", "hermes", "plow-seed")
HOME = at("var", "lib", "hermes")
STATUS = at("run", "ritmo", "update_status.json")
# Run after an install, in this order: refresh the agent's copy of the skills, let
# stored conversations re-read the persona, republish the cron scripts and jobs.
HOOKS = (at("etc", "cont-init.d", "40-ritmo-skills"), at("etc", "cont-init.d", "41-ritmo-refresh-persona"),
         at("etc", "s6-overlay", "scripts", "ritmo-boot.sh"))
RITMO_FILES = ("ritmo_boot.py", "ritmo_update.py", "github_app.json", "update.json")
API = "https://api.github.com"
FIRST_CHECK_S = 180
EVERY_S = 1800
# Release tag -> the installed commit it was judged against: nothing to redo until either changes.
SETTLED: dict = {}
PYTHON = "/opt/hermes/.venv/bin/python3"
AS_AGENT = ("/command/s6-setuidgid", "hermes")
IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0


class UpdateError(Exception):
    pass


def log(message: str) -> None:
    print(f"[ritmo-update] {message}", file=sys.stderr, flush=True)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_status(**fields) -> None:
    """Where `ritmo.py doctor` finds what the updater last did (tmpfs: always writable)."""
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        atomic_write(STATUS, json.dumps({"at": now(), **fields}, indent=1).encode())
    except OSError as error:
        log(f"could not write the status: {error}")


def atomic_write(path: str, data: bytes, mode: int = 0o644) -> None:
    """Mode and owner set through the open descriptor, never by name: SOUL.md is written into the
    agent's own home, where a name could be swapped for a symlink between two calls."""
    descriptor, staged = tempfile.mkstemp(prefix=".ritmo-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            if IS_ROOT:
                os.fchown(handle.fileno(), 0, 0)
        os.replace(staged, path)   # a rename moves a link itself, it never writes through one
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "ritmo-updater"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub answered {error.code} for {url}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"GitHub unreachable: {error}") from error


def get_json(path: str):
    return json.loads(get(API + path))


def enabled() -> bool:
    if os.environ.get("RITMO_AUTO_UPDATE", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return read_json(os.path.join(HOME, "ritmo", "config.json"), {}).get("auto_update", True) is not False


def newest_tag(repo: str) -> str:
    return get_json(f"/repos/{repo}/releases/latest")["tag_name"]


def why_not(repo: str, have: str, want: str) -> str:
    """Empty when `want` is a release to install over `have`; otherwise the reason it is not."""
    if not have:
        return "this image does not know its commit (a local build), so it never updates itself"
    if have == want:
        return "up to date"
    try:
        status = get_json(f"/repos/{repo}/compare/{have}...{want}").get("status")
        # Only what reached the default branch, which changes through reviewed pull requests.
        on_main = get_json(f"/repos/{repo}/compare/{want}...HEAD").get("status") in ("ahead", "identical")
    except UpdateError as error:
        return f"cannot compare with the installed commit ({error})"
    if status != "ahead":
        return f"the release is {status} of this image, not ahead"
    if not on_main:
        return "the release is not on the default branch"
    return ""


def unpack(archive: str, into: str) -> str:
    """Regular files and directories only, all inside `into`, with plain modes."""
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")[1:]   # GitHub's top folder is <repo>-<sha>
            if not parts or not parts[0] or any(p in ("", ".", "..") for p in parts) or member.name.startswith("/"):
                continue
            target = os.path.join(into, *parts)
            if member.isdir():
                os.makedirs(target, mode=0o755, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                with tar.extractfile(member) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                os.chmod(target, 0o644)
    return into


def download(repo: str, sha: str, work: str) -> str:
    archive = os.path.join(work, "release.tgz")
    with open(archive, "wb") as handle:
        handle.write(get(f"{API}/repos/{repo}/tarball/{sha}"))
    return unpack(archive, os.path.join(work, "src"))


def check_release(src: str, image_api: int) -> list[str]:
    """The skills to install; raises when this release must not be installed here."""
    release = read_json(os.path.join(src, "image", "ritmo", "update.json"), {})
    if release.get("image_api") != image_api:
        raise UpdateError(f"this release needs a new image (image_api {release.get('image_api')}, "
                          f"this image {image_api}); it waits for a redeploy")
    skills_root = os.path.join(src, "skills")
    skills = sorted(name for name in os.listdir(skills_root)
                    if os.path.isfile(os.path.join(skills_root, name, "SKILL.md")))
    if "ritmo" not in skills:
        raise UpdateError("the release has no ritmo skill")
    for needed in [os.path.join("runtime", "persona.md")] + [os.path.join("image", "ritmo", f) for f in RITMO_FILES]:
        if not os.path.isfile(os.path.join(src, needed)):
            raise UpdateError(f"the release is missing {needed}")
    with tempfile.TemporaryDirectory() as scratch:
        for top in (skills_root, os.path.join(src, "image", "ritmo")):
            for current, _, files in os.walk(top):
                for name in files:
                    if name.endswith(".py"):
                        try:
                            py_compile.compile(os.path.join(current, name), cfile=os.path.join(scratch, "x.pyc"), doraise=True)
                        except py_compile.PyCompileError as error:
                            raise UpdateError(f"{name} does not compile: {error.msg}") from error
    run_tests(src)
    return skills


def run_tests(src: str) -> None:
    """The release's own unit tests, on this image's Python, as the agent: they must pass."""
    command = os.environ.get("RITMO_UPDATE_TEST_CMD")
    if command:
        argv = json.loads(command)
    else:
        argv = [*AS_AGENT, "env", "HOME=/tmp", PYTHON, "-B", "-m", "unittest", "discover", "-s", "tests"]
    for current, dirs, files in os.walk(src):
        os.chmod(current, 0o755)
    try:
        result = subprocess.run(argv, cwd=src, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"the release's tests could not run: {error}") from error
    if result.returncode != 0:
        raise UpdateError("the release's tests failed:\n" + (result.stderr or result.stdout)[-1500:])


def harden(path: str) -> None:
    for current, dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in files:
            os.chmod(os.path.join(current, name), 0o644)
        if IS_ROOT:
            os.chown(current, 0, 0)
            for name in files:
                os.chown(os.path.join(current, name), 0, 0)


def swap_dir(source: str, target: str) -> None:
    staged, old = target + ".ritmo-new", target + ".ritmo-old"
    shutil.rmtree(staged, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    harden(staged)
    if os.path.exists(target):
        os.rename(target, old)
    os.rename(staged, target)
    shutil.rmtree(old, ignore_errors=True)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def compose_soul() -> None:
    """What plow-init does at boot: SOUL.md = the base persona + ours, root-owned."""
    soul = os.path.join(HOME, "SOUL.md")
    if not os.path.isdir(HOME) or not os.path.isfile(os.path.join(SEED_DIR, "SOUL.md")):
        return
    identity = read_bytes(os.path.join(SEED_DIR, "SOUL.md"))
    persona = os.path.join(SEED_DIR, "persona.md")
    if os.path.exists(persona):
        identity += b"\n" + read_bytes(persona)
    atomic_write(soul, identity)


def install(src: str, skills: list[str]) -> bool:
    """Put the release in place. True when the updater itself changed (s6 restarts it)."""
    for name in skills:
        swap_dir(os.path.join(src, "skills", name), os.path.join(SKILLS_DIR, name))
    atomic_write(os.path.join(RITMO_DIR, "skills.list"), ("\n".join(skills) + "\n").encode())
    atomic_write(os.path.join(SEED_DIR, "persona.md"), read_bytes(os.path.join(src, "runtime", "persona.md")))
    compose_soul()
    me = os.path.join(RITMO_DIR, "ritmo_update.py")
    before = read_bytes(me) if os.path.exists(me) else b""
    for name in RITMO_FILES:
        atomic_write(os.path.join(RITMO_DIR, name), read_bytes(os.path.join(src, "image", "ritmo", name)))
    return read_bytes(me) != before


def after_install() -> list[str]:
    """The boot hooks, each as the user it must run as. The skills hook works inside the agent's
    home, so it runs as the agent: root never touches a path the agent could have swapped."""
    problems = []
    for hook in HOOKS:
        if not os.path.exists(hook):
            continue
        argv = ["/bin/sh", hook]
        if hook.endswith("40-ritmo-skills") and IS_ROOT:
            argv = [*AS_AGENT, *argv]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                problems.append(f"{os.path.basename(hook)}: {(result.stderr or result.stdout)[-300:]}")
        except (OSError, subprocess.TimeoutExpired) as error:
            problems.append(f"{os.path.basename(hook)}: {error}")
    return problems


def check_once(source: str | None = None) -> bool:
    """One check. True when the updater replaced itself and should restart."""
    if source is None and not enabled():
        write_status(state="off")
        return False
    meta = read_json(os.path.join(RITMO_DIR, "update.json"), {})
    repo, image_api = meta.get("repo"), meta.get("image_api")
    if not repo or image_api is None:
        raise UpdateError("this image has no update.json")
    have = read_json(os.path.join(RITMO_DIR, "installed.json"), {}).get("commit", "")
    with tempfile.TemporaryDirectory(prefix="ritmo-update-") as work:
        os.chmod(work, 0o755)   # the agent runs the release's tests from here
        if source:
            tag, sha, src = "local", "local", source
        else:
            tag = newest_tag(repo)
            if SETTLED.get(tag) == have:
                return False   # decided already: one request per check while nothing changes
            sha = get_json(f"/repos/{repo}/commits/{tag}")["sha"]
            reason = why_not(repo, have, sha)
            if reason:
                write_status(state="up_to_date" if reason == "up to date" else "skipped",
                             commit=have, latest=tag, **({} if reason == "up to date" else {"reason": reason}))
                if not reason.startswith("cannot compare"):
                    SETTLED[tag] = have
                return False
            log(f"installing {tag} ({sha[:12]})")
            src = download(repo, sha, work)
        try:
            skills = check_release(src, image_api)
        except UpdateError as error:
            write_status(state="skipped", commit=have, latest=tag, reason=str(error)[:1500])
            log(f"{tag} not installed: {error}")
            SETTLED[tag] = have
            return False
        restart = install(src, skills)
    problems = after_install()
    if problems:
        # Not recorded as installed: the next check installs it again and reruns the hooks.
        write_status(state="installed_with_problems", commit=have, latest=tag, problems=problems)
        log(f"{tag} installed, hooks failed (retrying next check): {problems}")
        return restart
    atomic_write(os.path.join(RITMO_DIR, "installed.json"), json.dumps({"commit": sha, "tag": tag, "at": now()}).encode())
    write_status(state="installed", commit=sha, latest=tag, problems=[])
    log(f"{tag} installed")
    return restart


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ritmo_update.py")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    if args.once or args.source:
        try:
            check_once(args.source)
        except Exception as error:  # noqa: BLE001
            write_status(state="error", reason=str(error)[:1500])
            log(traceback.format_exc())
            return 1
        return 0
    # Spread out: agents on one address share GitHub's 60 unauthenticated requests an hour.
    time.sleep(FIRST_CHECK_S + random.randint(0, 600))
    while True:
        try:
            if check_once():
                return 0   # s6 starts the new version of this file
        except Exception as error:  # noqa: BLE001 -- the service never dies; it tries again later
            write_status(state="error", reason=str(error)[:1500])
            log(traceback.format_exc())
        time.sleep(EVERY_S + random.randint(-300, 300))


if __name__ == "__main__":
    sys.exit(main())
