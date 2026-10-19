#!/usr/bin/env python3
"""Build and push the Ritmo image without Docker.

The image is the Plow base plus one layer of our files and one environment
variable, so it does not need a container engine: this script writes that
layer as a tarball (root-owned, Unix modes, LF line endings) and `crane`
appends it to the pinned base and pushes the result. It runs on Windows,
macOS or Linux; the Dockerfile builds the same image where Docker exists.

  python tools/build_image.py                 # push ghcr.io/<owner>/ritmo-hermes-agent:sha-<commit>
  python tools/build_image.py --tag test-1    # another tag

Needs `crane` (go-containerregistry) and `gh` logged in with write:packages.
Prints the digest reference that `plow-agents deploy` takes.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "ghcr.io/alvesoff/ritmo-hermes-agent"
SOURCE_URL = "https://github.com/alvesoff/ritmo-hermes-agent"

# (path in the repo, path in the image, mode). Directories that already exist
# in the base are never written: a directory entry in a layer would replace
# their owner and mode.
FILES = [
    ("runtime/persona.md", "opt/hermes/plow-seed/persona.md", 0o644),
    ("image/ritmo/ritmo_boot.py", "opt/ritmo/ritmo_boot.py", 0o644),
    # The GitHub App the Team module connects through (public client id; no secret anywhere).
    ("image/ritmo/github_app.json", "opt/ritmo/github_app.json", 0o644),
    ("image/s6-overlay/scripts/ritmo-boot.sh", "etc/s6-overlay/scripts/ritmo-boot.sh", 0o755),
    ("image/s6-overlay/s6-rc.d/ritmo-boot/type", "etc/s6-overlay/s6-rc.d/ritmo-boot/type", 0o644),
    ("image/s6-overlay/s6-rc.d/ritmo-boot/up", "etc/s6-overlay/s6-rc.d/ritmo-boot/up", 0o644),
    ("image/s6-overlay/s6-rc.d/ritmo-boot/dependencies.d/plow-init",
     "etc/s6-overlay/s6-rc.d/ritmo-boot/dependencies.d/plow-init", 0o644),
    ("image/s6-overlay/s6-rc.d/hermes-gateway/dependencies.d/ritmo-boot",
     "etc/s6-overlay/s6-rc.d/hermes-gateway/dependencies.d/ritmo-boot", 0o644),
    ("image/s6-overlay/s6-rc.d/user/contents.d/ritmo-boot",
     "etc/s6-overlay/s6-rc.d/user/contents.d/ritmo-boot", 0o644),
    ("image/cont-init.d/40-ritmo-skills", "etc/cont-init.d/40-ritmo-skills", 0o755),
    ("image/cont-init.d/41-ritmo-refresh-persona", "etc/cont-init.d/41-ritmo-refresh-persona", 0o755),
]
NEW_DIRS = [
    "opt/ritmo",
    "etc/s6-overlay/s6-rc.d/ritmo-boot",
    "etc/s6-overlay/s6-rc.d/ritmo-boot/dependencies.d",
]
TEXT_SUFFIXES = (".md", ".py", ".sh", ".list", "")


def run(*args: str, capture: bool = False, stdin: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(args, capture_output=capture, text=True, input=stdin, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        sys.exit(f"build_image: `{' '.join(args[:3])} ...` failed {detail}")
    return (result.stdout or "").strip() if capture else ""


def base_reference() -> str:
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as handle:
        dockerfile = handle.read()
    args = dict(re.findall(r"^ARG (BASE_\w+)=(\S+)$", dockerfile, re.M))
    return f"{args['BASE_REGISTRY']}:{args['BASE_TAG']}@{args['BASE_DIGEST']}"


def read(path: str) -> bytes:
    with open(os.path.join(ROOT, path), "rb") as handle:
        data = handle.read()
    # The working tree may hold CRLF on Windows; a CRLF shebang does not run.
    return data.replace(b"\r\n", b"\n")


def add_dir(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info)


def add_file(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def skill_entries() -> list[tuple[str, str]]:
    """Every file of every skill in skills/, as (repo path, image path)."""
    entries = []
    for current, dirs, files in os.walk(os.path.join(ROOT, "skills")):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            repo_path = os.path.relpath(os.path.join(current, name), ROOT).replace(os.sep, "/")
            entries.append((repo_path, "opt/hermes/" + repo_path))
    return entries


def write_layer(path: str) -> None:
    skills = sorted(d for d in os.listdir(os.path.join(ROOT, "skills"))
                    if os.path.isdir(os.path.join(ROOT, "skills", d)))
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for directory in NEW_DIRS:
            add_dir(tar, directory)
        made = set()
        for repo_path, image_path in skill_entries():
            parent = os.path.dirname(image_path)
            parts = parent.split("/")
            # opt/hermes/skills exists in the base; everything under it is ours.
            for depth in range(4, len(parts) + 1):
                directory = "/".join(parts[:depth])
                if directory not in made:
                    add_dir(tar, directory)
                    made.add(directory)
            add_file(tar, image_path, read(repo_path), 0o644)
        for repo_path, image_path, mode in FILES:
            add_file(tar, image_path, read(repo_path), mode)
        add_file(tar, "opt/ritmo/skills.list", ("\n".join(skills) + "\n").encode(), 0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="image tag (default sha-<commit>)")
    parser.add_argument("--crane", default=os.environ.get("CRANE", "crane"))
    args = parser.parse_args()

    commit = run("git", "-C", ROOT, "rev-parse", "HEAD", capture=True)
    if run("git", "-C", ROOT, "status", "--porcelain", capture=True):
        print("build_image: the working tree has uncommitted changes; the image will not match a commit",
              file=sys.stderr)
    tag = args.tag or f"sha-{commit[:12]}"
    crane = shutil.which(args.crane) or args.crane
    workdir = tempfile.mkdtemp(prefix="ritmo-build-")
    try:
        env = dict(os.environ, DOCKER_CONFIG=os.path.join(workdir, "docker"))
        os.makedirs(env["DOCKER_CONFIG"])
        user = run("gh", "api", "user", "--jq", ".login", capture=True)
        token = run("gh", "auth", "token", capture=True)
        run(crane, "auth", "login", "ghcr.io", "-u", user, "--password-stdin", stdin=token, capture=True, env=env)

        layer = os.path.join(workdir, "ritmo-layer.tar")
        write_layer(layer)
        staging = f"{IMAGE}:build-{commit[:12]}"
        print(f"build_image: appending {os.path.getsize(layer)} bytes to {base_reference()}", file=sys.stderr)
        run(crane, "append", "--platform", "linux/amd64", "-b", base_reference(), "-f", layer, "-t", staging, env=env)
        final = f"{IMAGE}:{tag}"
        run(crane, "mutate", staging, "-t", final,
            "--env", "AGENT_ID=ritmo",
            "-l", f"org.opencontainers.image.source={SOURCE_URL}",
            "-l", f"org.opencontainers.image.revision={commit}",
            "-l", "org.opencontainers.image.title=Ritmo",
            capture=True, env=env)
        digest = run(crane, "digest", final, capture=True, env=env)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"{IMAGE}@{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
