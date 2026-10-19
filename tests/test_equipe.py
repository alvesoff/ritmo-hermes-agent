"""The Team module against a fake GitHub and a fake board: merge rules, demand links, connections, the gate."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "ritmo-equipe", "scripts"))

import equipe  # noqa: E402
import equipe_core as core  # noqa: E402
import equipe_gate  # noqa: E402

REPO = "acme/app"
SHA = "a" * 40


class FakeGitHub:
    """Routes (method, url regex) to handlers; records every call."""

    def __init__(self):
        self.calls = []
        self.checks = {SHA: ("completed", "success")}
        self.statuses = {}
        self.files = [{"filename": "calc.py", "status": "modified", "additions": 3, "deletions": 0, "patch": "+x"}]
        self.workflows = 1
        self.pr = {"number": 7, "state": "open", "merged": False, "draft": False, "mergeable": True,
                   "html_url": f"https://github.com/{REPO}/pull/7", "title": "Subtração", "body": "Closes #3",
                   "user": {"login": "ana"},
                   "head": {"sha": SHA, "ref": "feat/sub", "repo": {"full_name": REPO}},
                   "base": {"ref": "develop", "repo": {"full_name": REPO}}}
        self.merge_answers = [(200, {"merged": True, "sha": "m" * 40}, {})]
        self.deleted = []

    def __call__(self, method, url, token="", body=None, form=None, accept="", timeout=30):
        self.calls.append((method, url, body or form))
        path = re.sub(r"^https://api\.github\.com", "", url)
        if method == "GET" and re.match(rf"/repos/{REPO}/pulls/7$", path):
            return 200, self.pr, {}
        if method == "GET" and re.match(rf"/repos/{REPO}/pulls/7/files", path):
            return 200, self.files, {}
        if method == "GET" and "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            state = self.checks.get(sha)
            runs = [{"name": "ci", "status": state[0], "conclusion": state[1]}] if state else []
            return 200, {"check_runs": runs}, {}
        if method == "GET" and path.endswith("/status"):
            sha = path.split("/commits/")[1].split("/")[0]
            return 200, {"statuses": self.statuses.get(sha, [])}, {}
        if method == "GET" and "/actions/workflows" in path:
            return 200, {"total_count": self.workflows}, {}
        if method == "PUT" and path.endswith("/merge"):
            return self.merge_answers.pop(0)
        if method == "GET" and "/pulls?state=open" in path:
            return 200, [], {}
        if method == "DELETE" and "/git/refs/heads/" in path:
            self.deleted.append(path.split("/heads/")[1])
            return 204, None, {}
        if method == "GET" and re.match(rf"/repos/{REPO}/issues/3$", path):
            return 200, {"number": 3, "html_url": f"https://github.com/{REPO}/issues/3", "title": "Subtração",
                         "body": "- [ ] subtract\n- [ ] teste", "state": "open", "assignees": [{"login": "ana"}]}, {}
        raise AssertionError(f"unexpected call {method} {url}")


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="equipe-test-")
        self.saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "RITMO_NOW", "RITMO_GITHUB_APP_FILE")}
        os.environ["HERMES_HOME"] = self.home
        os.environ.pop("RITMO_NOW", None)
        app = os.path.join(self.home, "github_app.json")
        with open(app, "w") as handle:
            json.dump({"slug": "ritmo", "client_id": "Iv1.test"}, handle)
        os.environ["RITMO_GITHUB_APP_FILE"] = app
        core.APP_FILE = app
        self.gh = FakeGitHub()
        self.team = core.validate_team({"enabled": True, "repos": [{"repo": REPO}],
                                        "people": [{"name": "Ana", "github": "ana", "level": "junior"}]})
        self.repo = self.team["repos"][0]

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.home, ignore_errors=True)

    def blockers(self, sha=SHA, production=False, owner_ok=False, ritmo_config=None):
        github = core.GitHub("t", self.gh)
        return core.merge_blockers(github, self.team, self.repo, self.gh.pr, sha, production=production,
                                   ritmo_config=ritmo_config or {}, owner_ok=owner_ok)


class MergeRules(Home):
    def test_green_pr_on_the_reviewed_commit_may_merge(self):
        self.assertEqual(self.blockers(), [])

    def test_a_commit_after_the_review_stops_everything(self):
        found = self.blockers(sha="b" * 40)
        self.assertEqual(len(found), 1)
        self.assertIn("new commits", found[0])
        self.assertFalse(any("/check-runs" in url for _, url, _ in self.gh.calls), "no checks asked for a stale commit")

    def test_running_and_failed_checks_block(self):
        self.gh.checks[SHA] = ("in_progress", None)
        self.assertIn("checks are still running", self.blockers())
        self.gh.checks[SHA] = ("completed", "failure")
        self.assertTrue(any("checks failed: ci" in b for b in self.blockers()))

    def test_neutral_and_skipped_checks_count_as_green(self):
        self.gh.checks[SHA] = ("completed", "skipped")
        self.assertEqual(self.blockers(), [])

    def test_a_pending_commit_status_blocks(self):
        self.gh.statuses[SHA] = [{"context": "vercel", "state": "pending"}]
        self.assertIn("checks are still running", self.blockers())

    def test_no_checks_yet_on_a_repo_with_ci_waits(self):
        del self.gh.checks[SHA]
        self.assertIn("no checks have started yet for this commit", self.blockers())

    def test_repo_without_ci_needs_the_owner_unless_allowed(self):
        del self.gh.checks[SHA]
        self.gh.workflows = 0
        self.assertTrue(any("no CI" in b for b in self.blockers()))
        self.repo["allow_no_ci"] = True
        self.assertEqual(self.blockers(), [])

    def test_changing_ci_workflows_needs_the_owner(self):
        self.gh.files.append({"filename": ".github/workflows/ci.yml", "status": "modified", "additions": 1,
                              "deletions": 1, "patch": ""})
        self.assertTrue(any("workflows" in b for b in self.blockers()))
        self.assertEqual(self.blockers(owner_ok=True), [])

    def test_forks_drafts_conflicts_and_other_bases_block(self):
        self.gh.pr["head"]["repo"]["full_name"] = "stranger/app"
        self.assertTrue(any("fork" in b for b in self.blockers()))
        self.gh.pr["head"]["repo"]["full_name"] = REPO
        self.gh.pr["draft"] = True
        self.assertTrue(any("draft" in b for b in self.blockers()))
        self.gh.pr["draft"] = False
        self.gh.pr["mergeable"] = False
        self.assertTrue(any("conflicts" in b for b in self.blockers()))
        self.gh.pr["mergeable"] = True
        self.gh.pr["base"]["ref"] = "experiment"
        self.assertTrue(any("targets experiment" in b for b in self.blockers()))

    def test_production_waits_for_the_owner_active_hours(self):
        os.environ["RITMO_NOW"] = "2026-10-19T02:00:00+00:00"   # 23:00 in São Paulo
        config = {"timezone": "America/Sao_Paulo", "quiet": {"start": "22:00", "end": "07:00"}}
        self.assertTrue(any("active hours" in b for b in self.blockers(production=True, ritmo_config=config)))
        self.assertEqual(self.blockers(production=False, ritmo_config=config), [], "develop merges at any hour")

    def test_manual_production_needs_the_owner_ok(self):
        self.repo["promote"] = "manual"
        self.assertTrue(any("owner's ok" in b for b in self.blockers(production=True)))
        self.assertEqual(self.blockers(production=True, owner_ok=True), [])

    def test_merge_pins_the_sha_and_falls_back_to_an_allowed_method(self):
        self.gh.merge_answers = [(405, {"message": "Merge commits are not allowed"}, {}), (200, {"merged": True, "sha": "s"}, {})]
        result = core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA)
        self.assertEqual(result, {"merged": True, "method": "squash", "sha": "s"})
        bodies = [body for method, _, body in self.gh.calls if method == "PUT"]
        self.assertTrue(all(body["sha"] == SHA for body in bodies))

    def test_a_moved_head_at_merge_time_is_not_merged(self):
        self.gh.merge_answers = [(409, {"message": "Head branch was modified"}, {})]
        result = core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA)
        self.assertFalse(result["merged"])

    def test_long_lived_branches_are_never_deleted(self):
        github = core.GitHub("t", self.gh)
        self.gh.pr["head"]["ref"] = "release/2.0"
        self.assertFalse(core.delete_branch_if_safe(github, self.team, self.repo, self.gh.pr))
        self.gh.pr["head"]["ref"] = "feat/sub"
        self.assertTrue(core.delete_branch_if_safe(github, self.team, self.repo, self.gh.pr))
        self.assertEqual(self.gh.deleted, ["feat/sub"])


class Promotion(Home):
    def test_policies(self):
        config = {"timezone": "America/Sao_Paulo", "quiet": {"start": "22:00", "end": "07:00"}}
        state = core.load_state()
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"   # 12:00 in São Paulo
        self.assertTrue(core.promotion_due(self.repo, config, state))
        self.repo["promote"] = "manual"
        self.assertFalse(core.promotion_due(self.repo, config, state))
        self.repo["promote"] = "18:00"
        self.assertFalse(core.promotion_due(self.repo, config, state))
        os.environ["RITMO_NOW"] = "2026-10-19T21:30:00+00:00"   # 18:30
        self.assertTrue(core.promotion_due(self.repo, config, state))
        state["promotions"][REPO] = "2026-10-19"
        self.assertFalse(core.promotion_due(self.repo, config, state), "once a day")
        self.repo["promote"] = "now"
        os.environ["RITMO_NOW"] = "2026-10-19T03:00:00+00:00"   # 00:00, quiet
        self.assertFalse(core.promotion_due(self.repo, config, state))


class DemandLinks(Home):
    def pr(self, title="", body="", branch="feat/x"):
        return {"title": title, "body": body, "head": {"ref": branch}, "base": {"repo": {"full_name": REPO}}}

    def test_github_issue_references(self):
        self.assertEqual(core.demand_refs(self.team, self.pr(body="Closes #12, refs #3")), ["12", "3"])
        self.assertEqual(core.demand_refs(self.team, self.pr(body=f"see https://github.com/{REPO}/issues/9")), ["9"])
        self.assertEqual(core.demand_refs(self.team, self.pr(body="see https://github.com/other/repo/issues/9")), [])

    def test_board_references(self):
        team = core.validate_team({"board": {"type": "ritmo", "url": "https://painel.example.com"}})
        body = "Entrega da https://painel.example.com/tarefas?t=24"
        self.assertEqual(core.demand_refs(team, self.pr(body=body)), ["24"])
        self.assertEqual(core.demand_refs(team, self.pr(title="Demanda #31: login")), ["31"])
        self.assertEqual(core.demand_refs(team, self.pr(branch="feat/demanda-42-tela")), ["42"])
        self.assertEqual(core.demand_refs(team, self.pr(body="https://evil.example/tarefas?t=5")), [],
                         "a link to another system is not this board's demand")


class Connections(Home):
    def test_device_flow_connects_and_refreshes_without_a_secret(self):
        answers = iter([
            (200, {"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
                   "interval": 5, "expires_in": 900}, {}),
            (200, {"error": "authorization_pending"}, {}),
            (200, {"access_token": "t1", "refresh_token": "r1", "expires_in": 28800,
                   "refresh_token_expires_in": 15897600}, {}),
            (200, {"access_token": "t2", "refresh_token": "r2", "expires_in": 28800}, {}),
        ])
        sent = []

        def fake(method, url, **kw):
            sent.append(kw.get("form"))
            return next(answers)

        started = core.github_connect_start(fake)
        self.assertEqual(started["code"], "ABCD-1234")
        self.assertIn("/apps/ritmo/installations/new", started["install_url"])
        self.assertEqual(core.github_connect_poll(fake)["status"], "pending")
        self.assertEqual(core.github_connect_poll(fake)["status"], "connected")
        self.assertEqual(oct(os.stat(core.path("github.json")).st_mode & 0o777), oct(0o600)) if os.name != "nt" else None
        os.environ["RITMO_NOW"] = "2099-01-01T00:00:00+00:00"
        saved = core.read_json("github.json", {})
        saved["expires_at"] = "2098-12-31T23:55:00+00:00"
        core.write_json("github.json", saved, private=True)
        self.assertEqual(core.github_token(fake), "t2")
        self.assertNotIn("client_secret", sent[-1])
        self.assertEqual(sent[-1]["grant_type"], "refresh_token")

    def test_board_connection_by_typed_code(self):
        answers = iter([
            (201, {"codigo": "ABCD-EFGH", "url": "https://painel.example.com/ritmo/conectar", "segredo": "s" * 64,
                   "expira_em_segundos": 600}, {}),
            (200, {"status": "aguardando"}, {}),
            (200, {"status": "conectado", "token": "tok", "usuario": {"email": "ceo@x"}}, {}),
        ])
        started = core.board_connect_start("https://painel.example.com", lambda *a, **k: next(answers))
        self.assertEqual(started["code"], "ABCD-EFGH")
        self.assertNotIn("ABCD", started["url"])
        self.assertEqual(core.board_connect_poll(lambda *a, **k: next(answers))["status"], "pending")
        self.assertEqual(core.board_connect_poll(lambda *a, **k: next(answers))["status"], "connected")
        self.assertEqual(core.read_json("board.json", {})["token"], "tok")


class Settings(Home):
    def test_refusals_explain_the_fix(self):
        for bad, words in [({"board": {"type": "trello"}}, "board.type"),
                           ({"repos": [{"repo": "not a repo"}]}, "owner/name"),
                           ({"repos": [{"repo": REPO, "promote": "sometimes"}]}, "promote"),
                           ({"people": [{"name": "Ana", "github": "bad login!"}]}, "GitHub login"),
                           ({"publish": "maybe"}, "publish"),
                           ({"surprise": 1}, "unknown team key")]:
            with self.assertRaises(core.EquipeError) as caught:
                core.validate_team(bad)
            self.assertIn(words, str(caught.exception))

    def cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = equipe.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def test_cli_builds_the_team_step_by_step(self):
        self.cli("team", "repo", "add", REPO, "--promote", "18:00")
        self.cli("team", "person", "add", "Ana", "Lima", "--github", "ana", "--level", "junior",
                 "--board-person", "ana@acme.com")
        code, shown = self.cli("team", "show")
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["team"]["repos"][0]["promote"], "18:00")
        self.assertEqual(shown["team"]["people"][0]["name"], "Ana Lima")
        self.assertIn("github connect", shown["setup_missing"])
        code, result = self.cli("team", "repo", "remove", "acme/other")
        self.assertEqual(code, 1)


class Gate(Home):
    def test_silent_until_enabled_and_connected(self):
        self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)
        core.save_team(self.team)
        self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "no GitHub connection yet")

    def test_hands_out_a_review_once_then_retries_after_30_minutes(self):
        core.save_team(self.team)
        core.write_json("github.json", {"access_token": "t"}, private=True)
        work = [{"type": "review", "repo": REPO, "number": 7, "sha": SHA, "author": "ana", "base": "develop"}]
        calls = {"n": 0}

        def fake_pending(team, github, state, limit=3):
            calls["n"] += 1
            return [w for w in work if state["reviews"].get(equipe.key(REPO, 7), {}).get("sha") != SHA]

        original = equipe.pending_work
        equipe.pending_work = fake_pending
        try:
            os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"
            first = json.loads(equipe_gate.gate())
            self.assertEqual(first["equipe"][0]["number"], 7)
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "in review: not handed out again right away")
            os.environ["RITMO_NOW"] = "2026-10-19T15:31:00+00:00"
            self.assertIn("equipe", json.loads(equipe_gate.gate()), "retried once after 30 minutes")
            os.environ["RITMO_NOW"] = "2026-10-19T16:05:00+00:00"
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "then left for the owner")
        finally:
            equipe.pending_work = original


if __name__ == "__main__":
    unittest.main()
