"""The Team module against a fake GitHub and a fake board: merge rules, promotions, demand links,
connections, settings and the gate."""

from __future__ import annotations

import contextlib
import datetime as dt
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
BASE_SHA = "c" * 40
SAO_PAULO = {"timezone": "America/Sao_Paulo", "quiet": {"start": "22:00", "end": "07:00"}}


class FakeGitHub:
    """Routes (method, url) to canned answers; records every call."""

    def __init__(self):
        self.calls = []
        self.checks = {SHA: ("completed", "success")}
        self.statuses = {}
        self.files = [{"filename": "calc.py", "status": "modified", "additions": 3, "deletions": 0, "patch": "+x"}]
        self.workflows = 1
        self.commit_date = "2000-01-01T00:00:00Z"
        self.compare_commits = []
        self.pr = {"number": 7, "state": "open", "merged": False, "draft": False, "mergeable": True,
                   "html_url": f"https://github.com/{REPO}/pull/7", "title": "Subtração", "body": "Closes #3",
                   "user": {"login": "ana"},
                   "head": {"sha": SHA, "ref": "feat/sub", "repo": {"full_name": REPO}},
                   "base": {"ref": "develop", "sha": BASE_SHA, "repo": {"full_name": REPO}}}
        self.issue = {"number": 3, "html_url": f"https://github.com/{REPO}/issues/3", "title": "Subtração",
                      "body": "- [ ] subtract\n- [ ] teste", "state": "open", "assignees": [{"login": "ana"}]}
        self.merge_answers = [(200, {"merged": True, "sha": "m" * 40}, {})]
        self.deleted = []

    def __call__(self, method, url, token="", body=None, form=None, accept="", timeout=30):
        self.calls.append((method, url, body or form))
        path = re.sub(r"^https://api\.github\.com", "", url)
        if method == "GET" and re.match(rf"/repos/{REPO}/pulls/7$", path):
            return 200, self.pr, {}
        if method == "GET" and re.match(rf"/repos/{REPO}/pulls/7/commits", path):
            return 200, [{"sha": "p" * 40, "commit": {"message": "Função subtract"}}], {}
        if method == "GET" and path.startswith(f"/repos/{REPO}/compare/{BASE_SHA}...{SHA}"):
            return 200, {"files": self.files}, {}
        if method == "GET" and path.startswith(f"/repos/{REPO}/compare/main...develop"):
            return 200, {"ahead_by": len(self.compare_commits), "commits": [{"sha": s} for s in self.compare_commits]}, {}
        if method == "GET" and "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            state = self.checks.get(sha)
            runs = [{"name": "ci", "status": state[0], "conclusion": state[1]}] if state else []
            return 200, {"total_count": len(runs), "check_runs": runs}, {}
        if method == "GET" and path.endswith("/status"):
            sha = path.split("/commits/")[1].split("/")[0]
            return 200, {"statuses": self.statuses.get(sha, [])}, {}
        if method == "GET" and re.match(rf"/repos/{REPO}/commits/[0-9a-f]+$", path):
            return 200, {"commit": {"committer": {"date": self.commit_date}}}, {}
        if method == "GET" and "/actions/workflows" in path:
            return 200, {"total_count": self.workflows}, {}
        if method == "PUT" and path.endswith("/merge"):
            return self.merge_answers.pop(0)
        if method == "GET" and "/pulls?state=open" in path:
            return 200, [], {}
        if method == "DELETE" and "/git/refs/heads/" in path:
            self.deleted.append(path.split("/heads/")[1])
            return 204, None, {}
        if method == "GET" and re.match(rf"/repos/{REPO}/issues/\d+$", path):
            return 200, self.issue, {}
        raise AssertionError(f"unexpected call {method} {url}")


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="equipe-test-")
        self.saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "RITMO_NOW", "RITMO_GITHUB_APP_FILE")}
        os.environ["HERMES_HOME"] = self.home
        os.environ.pop("RITMO_NOW", None)
        os.makedirs(os.path.join(self.home, "ritmo"))
        with open(core.path("config.json"), "w") as handle:
            json.dump(SAO_PAULO, handle)
        app = os.path.join(self.home, "github_app.json")
        with open(app, "w") as handle:
            json.dump({"slug": "ritmo-openseed", "client_id": "Iv1.test"}, handle)
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
                                   ritmo_config=SAO_PAULO if ritmo_config is None else ritmo_config, owner_ok=owner_ok)

    def cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = equipe.main(list(argv))
        return code, json.loads(buffer.getvalue())


class MergeRules(Home):
    def setUp(self):
        super().setUp()
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"   # 12:00 in São Paulo: active hours

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

    def test_no_checks_on_a_fresh_commit_waits_then_needs_the_owner(self):
        del self.gh.checks[SHA]
        self.gh.commit_date = "2026-10-19T14:55:00Z"
        self.assertIn("no checks have started yet for this commit", self.blockers())
        self.gh.commit_date = "2026-10-19T14:00:00Z"
        self.assertTrue(any("no CI runs on this PR" in b for b in self.blockers()), "workflows that never run on PRs")
        self.assertEqual(self.blockers(owner_ok=True), [])
        self.repo["allow_no_ci"] = True
        self.assertEqual(self.blockers(), [])

    def test_changing_ci_needs_the_owner(self):
        for path in (".github/workflows/ci.yml", ".github/actions/setup/action.yml"):
            self.gh.files = [{"filename": path, "status": "modified", "additions": 1, "deletions": 1, "patch": ""}]
            self.assertTrue(any("changes CI" in b for b in self.blockers()), path)
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

    def test_production_waits_for_active_hours_and_fails_closed_without_a_timezone(self):
        os.environ["RITMO_NOW"] = "2026-10-19T02:00:00+00:00"   # 23:00 in São Paulo
        self.assertTrue(any("active hours" in b for b in self.blockers(production=True)))
        self.assertEqual(self.blockers(production=False), [], "develop merges at any hour")
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"
        self.assertTrue(any("timezone" in b for b in self.blockers(production=True, ritmo_config={})))

    def test_manual_production_needs_the_owner_ok(self):
        self.repo["promote"] = "manual"
        self.assertTrue(any("owner's ok" in b for b in self.blockers(production=True)))
        self.assertEqual(self.blockers(production=True, owner_ok=True), [])

    def test_merge_pins_the_sha_and_falls_back_only_when_the_method_is_forbidden(self):
        self.gh.merge_answers = [(405, {"message": "Merge commits are not allowed on this repository"}, {}),
                                 (200, {"merged": True, "sha": "s"}, {})]
        result = core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA)
        self.assertEqual(result, {"merged": True, "method": "squash", "sha": "s"})
        self.assertTrue(all(body["sha"] == SHA for method, _, body in self.gh.calls if method == "PUT"))
        self.gh.merge_answers = [(405, {"message": "Required status check is expected"}, {})]
        self.assertFalse(core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA)["merged"])

    def test_promotions_are_always_merge_commits(self):
        self.gh.merge_answers = [(405, {"message": "Merge commits are not allowed"}, {})]
        result = core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA, promotion=True)
        self.assertFalse(result["merged"])
        self.assertEqual([b["merge_method"] for m, _, b in self.gh.calls if m == "PUT"], ["merge"])

    def test_a_moved_head_at_merge_time_is_not_merged(self):
        self.gh.merge_answers = [(409, {"message": "Head branch was modified"}, {})]
        self.assertFalse(core.merge(core.GitHub("t", self.gh), self.team, self.repo, 7, SHA)["merged"])

    def test_long_lived_branches_are_never_deleted(self):
        github = core.GitHub("t", self.gh)
        self.gh.pr["head"]["ref"] = "release/2.0"
        self.assertFalse(core.delete_branch_if_safe(github, self.team, self.repo, self.gh.pr))
        self.gh.pr["head"]["ref"] = "feat/sub"
        self.assertTrue(core.delete_branch_if_safe(github, self.team, self.repo, self.gh.pr))
        self.assertEqual(self.gh.deleted, ["feat/sub"])

    def test_a_successful_merge_makes_its_commits_promotable(self):
        state = core.load_state()
        result = equipe.try_merge(self.team, self.repo, core.GitHub("t", self.gh), state, 7, SHA)
        self.assertTrue(result["merged"])
        self.assertEqual(set(state["reviewed_commits"][REPO]), {"p" * 40, "m" * 40})


class OnlyTheTeamMerges(Home):
    def setUp(self):
        super().setUp()
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"

    def test_a_pr_from_outside_the_team_never_merges_on_its_own(self):
        self.gh.pr["user"]["login"] = "alvesoff"
        self.assertTrue(any("not on the team" in b for b in self.blockers()))
        self.assertEqual(self.blockers(owner_ok=True), [], "the owner may merge it themselves")
        self.gh.pr["user"] = None
        self.assertTrue(any("not on the team" in b for b in self.blockers()), "a deleted account is not the team")

    def test_promotion_prs_are_not_held_by_the_author_rule(self):
        self.gh.pr["user"]["login"] = "alvesoff"
        self.gh.pr["head"]["ref"], self.gh.pr["base"]["ref"] = "develop", "main"
        self.assertFalse(any("not on the team" in b for b in self.blockers(production=True)))

    def test_owner_and_bot_commits_on_develop_do_not_jam_promotions(self):
        state = core.load_state()
        commits = [{"sha": "1" * 40, "author": {"login": "alvesoff"}, "committer": {"login": "web-flow"}},
                   {"sha": "2" * 40, "author": {"login": "dependabot[bot]"}, "committer": None},
                   {"sha": "3" * 40, "author": {"login": "ana"}, "committer": {"login": "ana"}},
                   {"sha": "4" * 40, "author": None, "committer": None}]
        original = self.gh.__call__

        def router(method, url, **kw):
            if "/compare/main...develop" in url:
                return 200, {"ahead_by": 4, "commits": commits}, {}
            return original(method, url, **kw)

        found = core.unreviewed_commits(core.GitHub("t", router), state, self.repo, self.team)
        self.assertEqual(found, ["3" * 40, "4" * 40], "the team's own push and an unknown author are held")


class OnlyTheTeam(Home):
    def test_prs_from_outside_the_team_are_never_reviewed(self):
        def pr(number, login):
            return {"number": number, "draft": False, "user": {"login": login},
                    "head": {"sha": f"{number}" * 40 if number < 10 else SHA, "ref": f"feat/{number}"}}

        open_prs = [pr(5, "ana"), pr(6, "alvesoff"), pr(7, "dependabot[bot]")]
        original = self.gh.__call__

        def router(method, url, **kw):
            if "/pulls?state=open&base=develop" in url:
                return 200, open_prs, {}
            if "/pulls?state=open&base=main" in url:
                return 200, [], {}
            if "/check-runs" in url:
                return 200, {"total_count": 1, "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}, {}
            if url.endswith("/status"):
                return 200, {"statuses": []}, {}
            return original(method, url, **kw)

        core.write_json("github.json", {"access_token": "t", "login": "alvesoff"}, private=True)
        state = core.load_state()
        open_prs.append(pr(8, "gabrielax"))
        items = equipe.review_items(self.team, core.GitHub("t", router), state, limit=10)
        self.assertEqual([(i["type"], i["number"]) for i in items], [("review", 5), ("outside", 8)],
                         "Ana is reviewed; the owner and the bot are skipped quietly; an unknown person is told once")
        again = equipe.review_items(self.team, core.GitHub("t", router), state, limit=10)
        self.assertEqual([i["type"] for i in again], ["review"], "the unknown person is told only once")


class Promotion(Home):
    def test_policies(self):
        state = core.load_state()
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"   # 12:00 in São Paulo
        self.assertTrue(core.promotion_due(self.repo, SAO_PAULO, state))
        self.assertFalse(core.promotion_due(self.repo, {}, state), "never without the owner's timezone")
        self.repo["promote"] = "manual"
        self.assertFalse(core.promotion_due(self.repo, SAO_PAULO, state))
        self.repo["promote"] = "18:00"
        self.assertFalse(core.promotion_due(self.repo, SAO_PAULO, state))
        os.environ["RITMO_NOW"] = "2026-10-19T21:30:00+00:00"   # 18:30
        self.assertTrue(core.promotion_due(self.repo, SAO_PAULO, state))
        state["promotions"][REPO] = "2026-10-19"
        self.assertFalse(core.promotion_due(self.repo, SAO_PAULO, state), "once a day")
        self.repo["promote"] = "now"
        os.environ["RITMO_NOW"] = "2026-10-19T03:00:00+00:00"   # 00:00, quiet
        self.assertFalse(core.promotion_due(self.repo, SAO_PAULO, state))

    def test_commits_pushed_straight_to_develop_are_not_promoted(self):
        state = core.load_state()
        core.remember_reviewed(state, REPO, ["r1" * 20])
        self.gh.compare_commits = ["r1" * 20, "x9" * 20]
        result = equipe.promote(self.team, self.repo, core.GitHub("t", self.gh), state)
        self.assertFalse(result["promoted"])
        self.assertIn("did not pass a Ritmo review", result["blockers"][0])

    def test_single_branch_repos_have_nothing_to_promote(self):
        team = core.validate_team({"repos": [{"repo": REPO, "dev": "main", "prod": "main"}]})
        self.assertFalse(core.promotion_due(team["repos"][0], SAO_PAULO, core.load_state()))


class DemandLinks(Home):
    def pr(self, title="", body="", branch="feat/x"):
        return {"title": title, "body": body, "head": {"ref": branch}, "base": {"repo": {"full_name": REPO}}}

    def test_github_issue_references(self):
        self.assertEqual(core.demand_refs(self.team, self.pr(body="Closes #12, refs #3")), ["12", "3"])
        self.assertEqual(core.demand_refs(self.team, self.pr(body=f"see https://github.com/{REPO}/issues/9")), ["9"])
        self.assertEqual(core.demand_refs(self.team, self.pr(body="see https://github.com/other/repo/issues/9")), [])
        self.assertEqual(core.demand_refs(self.team, self.pr(body="prefix #3 and suffix")), [], "'fix' inside a word")

    def test_board_references(self):
        team = core.validate_team({"board": {"type": "ritmo", "url": "https://painel.example.com"}})
        body = "Entrega da https://painel.example.com/tarefas?t=24"
        self.assertEqual(core.demand_refs(team, self.pr(body=body)), ["24"])
        self.assertEqual(core.demand_refs(team, self.pr(title="Demanda #31: login")), ["31"])
        self.assertEqual(core.demand_refs(team, self.pr(branch="feat/demanda-42-tela")), ["42"])
        self.assertEqual(core.demand_refs(team, self.pr(body="https://evil.example/tarefas?t=5 https://painel.example.com")),
                         [], "a link to another system is not this board's demand")

    def test_a_pr_or_a_closed_issue_is_never_the_demand(self):
        board = core.GithubIssuesBoard(core.GitHub("t", self.gh), REPO)
        self.assertEqual(board.get("3")["title"], "Subtração")
        self.gh.issue["pull_request"] = {"url": "x"}
        with self.assertRaises(core.EquipeError):
            board.get("3")
        del self.gh.issue["pull_request"]
        self.gh.issue["state"] = "closed"
        with self.assertRaises(core.EquipeError):
            board.get("3")


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
        self.assertIn("/apps/ritmo-openseed/installations/new", started["install_url"])
        self.assertEqual(core.github_connect_poll(fake)["status"], "pending")
        self.assertEqual(core.github_connect_poll(fake)["status"], "connected")
        if os.name != "nt":
            self.assertEqual(os.stat(core.path("github.json")).st_mode & 0o777, 0o600)
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
                           ({"merge_method": "rebase"}, "merge or squash"),
                           ({"paused_until": "2026-10-20T08:00:00"}, "offset"),
                           ({"surprise": 1}, "unknown team key")]:
            with self.assertRaises(core.EquipeError) as caught:
                core.validate_team(bad)
            self.assertIn(words, str(caught.exception))

    def test_cannot_turn_on_before_ritmo_knows_the_owner_timezone(self):
        os.remove(core.path("config.json"))
        with self.assertRaises(core.EquipeError) as caught:
            core.validate_team({"enabled": True})
        self.assertIn("timezone", str(caught.exception))

    def test_cli_builds_the_team_and_edits_only_what_was_said(self):
        self.cli("team", "repo", "add", REPO, "--dev", "dev", "--prod", "master", "--allow-no-ci")
        self.cli("team", "repo", "add", REPO, "--promote", "manual")
        self.cli("team", "person", "add", "Ana", "Lima", "--github", "ana", "--level", "junior",
                 "--board-person", "ana@acme.com")
        self.cli("team", "person", "add", "Ana", "Lima", "--github", "ana")
        code, shown = self.cli("team", "show")
        self.assertEqual(code, 0, shown)
        repo = shown["team"]["repos"][0]
        self.assertEqual((repo["dev"], repo["prod"], repo["promote"], repo["allow_no_ci"]), ("dev", "master", "manual", True))
        person = shown["team"]["people"][0]
        self.assertEqual((person["name"], person["level"], person["board_person"]), ("Ana Lima", "junior", "ana@acme.com"))
        self.assertIn("github connect", shown["setup_missing"])
        code, _ = self.cli("team", "repo", "remove", "acme/other")
        self.assertEqual(code, 1)

    def test_single_branch_repo_is_accepted(self):
        code, result = self.cli("team", "repo", "add", REPO, "--dev", "main", "--prod", "main")
        self.assertEqual(code, 0, result)

    def test_verdict_refuses_a_short_or_stale_sha(self):
        core.save_team(self.team)
        core.write_json("github.json", {"access_token": "t"}, private=True)
        original = core.http
        try:
            core.GitHub.__init__.__defaults__ = (self.gh,)
            code, result = self.cli("verdict", REPO, "7", "--sha", "aaaaaaa", "--ok")
            self.assertEqual(code, 1)
            self.assertIn("40-character", result["error"])
            code, result = self.cli("verdict", REPO, "7", "--sha", "b" * 40, "--ok")
            self.assertEqual(code, 1)
            self.assertIn("not the PR's current commit", result["error"])
        finally:
            core.GitHub.__init__.__defaults__ = (original,)

    def test_owner_ok_is_refused_right_after_an_automatic_handout(self):
        core.save_team(self.team)
        core.write_json("github.json", {"access_token": "t"}, private=True)
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"
        state = core.load_state()
        state["last_handout"] = "2026-10-19T14:55:00+00:00"
        core.write_json("equipe_state.json", state)
        code, result = self.cli("merge", REPO, "7", "--sha", SHA, "--owner-ok")
        self.assertEqual(code, 1)
        self.assertIn("automatic review is running", result["error"])


class Gate(Home):
    def setUp(self):
        super().setUp()
        os.environ["RITMO_NOW"] = "2026-10-19T15:00:00+00:00"

    def connect(self):
        core.save_team(self.team)
        core.write_json("github.json", {"access_token": "t"}, private=True)

    @contextlib.contextmanager
    def patched(self, **functions):
        saved = {name: getattr(equipe, name) for name in functions}
        for name, fn in functions.items():
            setattr(equipe, name, fn)
        try:
            yield
        finally:
            for name, fn in saved.items():
                setattr(equipe, name, fn)

    def test_silent_until_enabled_and_connected(self):
        self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)
        core.save_team(self.team)
        self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "no GitHub connection yet")

    def test_a_review_is_handed_out_once_retried_once_then_reported(self):
        self.connect()
        work = {"type": "review", "repo": REPO, "number": 7, "sha": SHA, "author": "ana", "base": "develop"}

        def items(team, github, state, limit, problems=None):
            record = state["reviews"].get(equipe.key(REPO, 7), {})
            return [] if record.get("sha") == SHA and not record.get("retry") else [dict(work)]

        with self.patched(review_items=items, promote=lambda *a, **k: {"promoted": False, "nothing_new": True}):
            self.assertEqual(json.loads(equipe_gate.gate())["equipe"][0]["number"], 7)
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "in review: not handed out again right away")
            os.environ["RITMO_NOW"] = "2026-10-19T15:31:00+00:00"
            self.assertEqual(len(json.loads(equipe_gate.gate())["equipe"]), 1, "retried once after 30 minutes")
            os.environ["RITMO_NOW"] = "2026-10-19T16:05:00+00:00"
            notices = json.loads(equipe_gate.gate())["notices"]
            self.assertEqual(notices[0]["type"], "review_failed", "then the owner is told")
            os.environ["RITMO_NOW"] = "2026-10-19T16:40:00+00:00"
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "once")

    def test_a_retry_that_is_never_handed_out_is_dropped_not_looped(self):
        self.connect()
        state = core.load_state()
        state["reviews"][equipe.key(REPO, 9)] = {"sha": SHA, "verdict": "in_review", "attempts": 1,
                                                 "at": "2026-10-19T14:00:00+00:00"}
        core.write_json("equipe_state.json", state)
        with self.patched(review_items=lambda *a, **k: [], promote=lambda *a, **k: {"promoted": False, "nothing_new": True}):
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)
            self.assertTrue(core.load_state()["reviews"][equipe.key(REPO, 9)]["retry"])
            os.environ["RITMO_NOW"] = "2026-10-19T15:31:00+00:00"
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)
            self.assertNotIn(equipe.key(REPO, 9), core.load_state()["reviews"])

    def test_waiting_merges_happen_in_code_and_the_same_blocker_is_told_once(self):
        self.connect()
        state = core.load_state()
        state["reviews"][equipe.key(REPO, 7)] = {"sha": SHA, "verdict": "ok_waiting", "blockers": []}
        core.write_json("equipe_state.json", state)
        answers = [{"merged": False, "blockers": ["checks failed: ci"]}, {"merged": False, "blockers": ["checks failed: ci"]},
                   {"merged": True, "into": "develop"}]
        with self.patched(review_items=lambda *a, **k: [], try_merge=lambda *a, **k: answers.pop(0),
                          promote=lambda *a, **k: {"promoted": False, "nothing_new": True}):
            first = json.loads(equipe_gate.gate())["notices"]
            self.assertEqual(first[0]["type"], "waiting")
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT, "same blocker: no new message, no LLM")
            merged = json.loads(equipe_gate.gate())["notices"]
            self.assertEqual(merged[0], {"type": "merged", "repo": REPO, "number": 7, "into": "develop"})

    def test_a_broken_repository_is_reported_once_and_does_not_stop_the_others(self):
        self.connect()

        def items(team, github, state, limit, problems=None):
            problems[REPO] = "GitHub GET /repos/acme/app/pulls answered 404: Not Found"
            return []

        with self.patched(review_items=items, promote=lambda *a, **k: {"promoted": False, "nothing_new": True}):
            notices = json.loads(equipe_gate.gate())["notices"]
            self.assertEqual(notices[0]["type"], "problem")
            self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)

    def test_paused(self):
        self.connect()
        team = core.load_team()
        team["paused_until"] = "2026-10-20T08:00:00-03:00"
        core.save_team(team)
        self.assertEqual(equipe_gate.gate(), equipe_gate.SILENT)


if __name__ == "__main__":
    unittest.main()
