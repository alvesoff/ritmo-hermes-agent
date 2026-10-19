"""The updater installs a newer release over a running image, and refuses everything else."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = json.dumps([sys.executable, "-c", "pass"])
FAIL = json.dumps([sys.executable, "-c", "raise SystemExit('a test failed')"])


def load_updater(root: str):
    os.environ["RITMO_UPDATE_ROOT"] = root
    spec = importlib.util.spec_from_file_location("ritmo_update_under_test",
                                                  os.path.join(REPO, "image", "ritmo", "ritmo_update.py"))
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode, before = True, sys.dont_write_bytecode
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def tarball(prefix: str, files: dict[str, bytes], extra=()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info)
    return buffer.getvalue()


def release_files(image_api: int = 1) -> dict[str, bytes]:
    """This repository as a release: what the updater installs, plus the tests it runs."""
    files = {}
    for top in ("skills", "image", "runtime"):
        for current, dirs, names in os.walk(os.path.join(REPO, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(current, name)
                with open(path, "rb") as handle:
                    files[os.path.relpath(path, REPO).replace(os.sep, "/")] = handle.read()
    files["image/ritmo/update.json"] = json.dumps({"repo": "acme/ritmo", "image_api": image_api}).encode()
    return files


class Updater(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ritmo-update-test-")
        self.saved = {k: os.environ.get(k) for k in ("RITMO_UPDATE_ROOT", "RITMO_UPDATE_TEST_CMD", "RITMO_AUTO_UPDATE")}
        os.environ["RITMO_UPDATE_TEST_CMD"] = PASS
        os.environ.pop("RITMO_AUTO_UPDATE", None)
        for directory in ("opt/ritmo", "opt/hermes/skills/ritmo", "opt/hermes/plow-seed", "var/lib/hermes/ritmo"):
            os.makedirs(os.path.join(self.root, directory))
        self.write("opt/ritmo/update.json", json.dumps({"repo": "acme/ritmo", "image_api": 1}))
        self.write("opt/ritmo/installed.json", json.dumps({"commit": "old", "tag": "image"}))
        self.write("opt/hermes/skills/ritmo/SKILL.md", "old skill")
        self.write("opt/hermes/plow-seed/SOUL.md", "BASE")
        self.write("opt/hermes/plow-seed/persona.md", "old persona")
        self.updater = load_updater(self.root)
        self.calls = []

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative: str, text: str):
        with open(os.path.join(self.root, relative), "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self, relative: str) -> str:
        with open(os.path.join(self.root, relative), encoding="utf-8") as handle:
            return handle.read()

    def status(self) -> dict:
        return json.loads(self.read("run/ritmo/update_status.json"))

    def github(self, compare="ahead", files=None):
        """A fake GitHub serving one release, `v2` at commit `new`."""
        archive = tarball("acme-ritmo-new", files if files is not None else release_files())

        def get(url):
            self.calls.append(url)
            path = url.replace(self.updater.API, "")
            answers = {"/repos/acme/ritmo/releases/latest": {"tag_name": "v2"},
                       "/repos/acme/ritmo/commits/v2": {"sha": "new"},
                       "/repos/acme/ritmo/compare/old...new": {"status": compare}}
            if path == "/repos/acme/ritmo/tarball/new":
                return archive
            return json.dumps(answers[path]).encode()
        self.updater.get = get

    def test_a_newer_release_is_installed_over_the_image(self):
        self.github()
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "installed", self.status())
        with open(os.path.join(REPO, "skills", "ritmo", "SKILL.md"), encoding="utf-8") as handle:
            self.assertEqual(self.read("opt/hermes/skills/ritmo/SKILL.md"), handle.read())
        self.assertTrue(os.path.isfile(os.path.join(self.root, "opt/hermes/skills/ritmo-equipe/scripts/equipe.py")))
        self.assertEqual(self.read("opt/ritmo/skills.list"), "ritmo\nritmo-equipe\n")
        self.assertEqual(json.loads(self.read("opt/ritmo/installed.json"))["commit"], "new")
        soul = self.read("var/lib/hermes/SOUL.md")
        self.assertTrue(soul.startswith("BASE\n") and "Never edit, patch or copy" in soul, "SOUL.md recomposed")
        self.assertFalse(any(name.endswith((".ritmo-new", ".ritmo-old")) for name in os.listdir(
            os.path.join(self.root, "opt/hermes/skills"))))

    def test_the_same_or_an_older_release_changes_nothing(self):
        for compare in ("identical", "behind", "diverged"):
            self.github(compare=compare)
            self.updater.check_once()
            self.assertEqual(self.status()["state"], "up_to_date", compare)
            self.assertEqual(self.read("opt/hermes/skills/ritmo/SKILL.md"), "old skill")
        self.assertFalse(any("tarball" in url for url in self.calls), "nothing is downloaded")

    def test_a_release_for_another_image_waits_for_a_redeploy(self):
        self.github(files=release_files(image_api=2))
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "skipped")
        self.assertIn("new image", self.status()["reason"])
        self.assertEqual(self.read("opt/hermes/skills/ritmo/SKILL.md"), "old skill")
        self.assertEqual(json.loads(self.read("opt/ritmo/installed.json"))["commit"], "old")

    def test_a_release_whose_tests_fail_is_not_installed(self):
        os.environ["RITMO_UPDATE_TEST_CMD"] = FAIL
        self.github()
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "skipped")
        self.assertIn("tests failed", self.status()["reason"])
        self.assertEqual(self.read("opt/hermes/plow-seed/persona.md"), "old persona")

    def test_a_release_that_does_not_compile_is_not_installed(self):
        files = release_files()
        files["skills/ritmo/scripts/ritmo.py"] = b"def broken(:\n"
        self.github(files=files)
        self.updater.check_once()
        self.assertIn("does not compile", self.status()["reason"])

    def test_the_owner_or_the_image_can_turn_it_off(self):
        self.github()
        self.write("var/lib/hermes/ritmo/config.json", json.dumps({"auto_update": False}))
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "off")
        os.remove(os.path.join(self.root, "var/lib/hermes/ritmo/config.json"))
        os.environ["RITMO_AUTO_UPDATE"] = "off"
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "off")
        self.assertEqual(self.calls, [], "GitHub is never asked")

    def test_an_archive_cannot_write_outside_the_work_folder(self):
        link = tarfile.TarInfo("acme-ritmo-new/skills/ritmo/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive = os.path.join(self.root, "a.tgz")
        with open(archive, "wb") as handle:
            handle.write(tarball("acme-ritmo-new", {"../escape": b"x", "skills/ritmo/SKILL.md": b"ok"}, [link]))
        into = os.path.join(self.root, "src")
        self.updater.unpack(archive, into)
        self.assertTrue(os.path.isfile(os.path.join(into, "skills/ritmo/SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape")))
        self.assertFalse(os.path.lexists(os.path.join(into, "skills/ritmo/evil")))

    def test_an_image_without_a_commit_takes_the_release(self):
        self.write("opt/ritmo/installed.json", json.dumps({"commit": "", "tag": "image"}))
        self.github()
        self.updater.check_once()
        self.assertEqual(self.status()["state"], "installed")
        self.assertFalse(any("compare" in url for url in self.calls))


if __name__ == "__main__":
    unittest.main()
