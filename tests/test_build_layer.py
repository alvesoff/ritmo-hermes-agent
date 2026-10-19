"""The Docker-free build ships every file the Dockerfile ships, with the right modes."""

from __future__ import annotations

import os
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_image  # noqa: E402


class Layer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(tempfile.mkdtemp(prefix="ritmo-layer-"), "layer.tar")
        build_image.write_layer(path)
        with tarfile.open(path) as tar:
            cls.members = {member.name: member for member in tar.getmembers()}
            cls.contents = {name: tar.extractfile(member).read()
                            for name, member in cls.members.items() if member.isfile()}

    def test_every_image_and_skill_file_is_in_the_layer(self):
        expected = {"opt/hermes/plow-seed/persona.md", "opt/ritmo/skills.list"}
        for top in ("image", "skills"):
            for current, dirs, files in os.walk(os.path.join(ROOT, top)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in files:
                    if name.endswith(".pyc"):
                        continue
                    relative = os.path.relpath(os.path.join(current, name), ROOT).replace(os.sep, "/")
                    if relative.startswith("skills/"):
                        expected.add("opt/hermes/" + relative)
        shipped = {target for _, target, _ in build_image.FILES}
        image_files = []
        for current, dirs, files in os.walk(os.path.join(ROOT, "image")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            image_files += [os.path.relpath(os.path.join(current, n), ROOT).replace(os.sep, "/") for n in files]
        listed = {source for source, _, _ in build_image.FILES}
        self.assertEqual(set(image_files) - listed, set(), "a file under image/ is not in build_image.FILES")
        self.assertTrue(expected | shipped <= set(self.members))

    def test_root_owned_with_unix_modes(self):
        for name, member in self.members.items():
            self.assertEqual((member.uid, member.gid), (0, 0), name)
        executables = {"etc/s6-overlay/scripts/ritmo-boot.sh", "etc/cont-init.d/40-ritmo-skills",
                       "etc/cont-init.d/41-ritmo-refresh-persona", "etc/s6-overlay/s6-rc.d/ritmo-update/run"}
        for name in executables:
            self.assertEqual(self.members[name].mode, 0o755, name)
        self.assertEqual(self.members["opt/hermes/skills/ritmo/scripts/ritmo.py"].mode, 0o644)

    def test_no_crlf_and_no_existing_base_directories(self):
        for name, data in self.contents.items():
            self.assertNotIn(b"\r\n", data, name)
        for name in ("opt", "opt/hermes", "opt/hermes/skills", "etc", "etc/cont-init.d", "etc/s6-overlay"):
            self.assertNotIn(name, self.members, "a base directory would lose its owner and mode")

    def test_skills_list_names_every_skill(self):
        self.assertEqual(self.contents["opt/ritmo/skills.list"], b"ritmo\nritmo-equipe\n")


if __name__ == "__main__":
    unittest.main()
