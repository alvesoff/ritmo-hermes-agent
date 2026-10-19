#!/usr/bin/env python3
"""The Team module's one door: settings, connections, reviews and merges.

Every command prints JSON; a refusal prints {"error": ...} and exits 1.

  equipe.py team show
  equipe.py team set '<json>'                      (board, publish, merge_method, rules, review_model, enabled)
  equipe.py team person add <name> --github LOGIN [--level junior|mid|senior] [--board-person EMAIL]
  equipe.py team person remove <login>
  equipe.py team repo add owner/name [--dev develop] [--prod main] [--promote now|HH:MM|manual] [--allow-no-ci]
  equipe.py team repo remove owner/name
  equipe.py github connect | github status
  equipe.py board connect <https-url> | board status | board people
  equipe.py pending
  equipe.py review <owner/name> <number>
  equipe.py verdict <owner/name> <number> --sha SHA (--ok | --not-ok) --summary TEXT
  equipe.py merge <owner/name> <number> --sha SHA [--owner-ok]
  equipe.py followup <owner/name> <number> --title T --description D [--steps '<json>']
  equipe.py hold <owner/name> <number> --sha SHA --summary TEXT
  equipe.py link <owner/name> <number> <demand-id>
  equipe.py promote <owner/name> [--owner-ok]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import equipe_core as core  # noqa: E402
from equipe_core import EquipeError  # noqa: E402

RITMO_SCRIPTS = os.environ.get("RITMO_SCRIPTS", "/opt/hermes/skills/ritmo/scripts")


def out(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def ritmo_config() -> dict:
    """The routine settings (timezone, quiet hours) that production merges respect."""
    try:
        with open(core.path("config.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def gh() -> core.GitHub:
    return core.GitHub(core.github_token())


def key(repo: str, number: int) -> str:
    return f"{repo.lower()}#{number}"


def connections() -> dict:
    github = core.read_json("github.json", {})
    board = core.read_json("board.json", {})
    return {"github": "connected" if github.get("access_token") else ("pending" if github.get("pending") else "no"),
            "board": "connected" if board.get("token") else ("pending" if board.get("pending") else "no"),
            "board_user": board.get("user", {}).get("email")}


# --- settings ------------------------------------------------------------------

def cmd_team(args):
    with core.locked():
        team = core.load_team()
        if args.action == "show":
            missing = []
            conn = connections()
            if conn["github"] != "connected":
                missing.append("github connect")
            if team["board"]["type"] == "ritmo" and conn["board"] != "connected":
                missing.append("board connect")
            if not team["repos"]:
                missing.append("repo add")
            if not team["people"]:
                missing.append("person add")
            out({"team": team, "connections": conn, "setup_missing": missing})
            return
        if args.action == "set":
            try:
                update = json.loads(args.rest[0] if args.rest else "")
            except ValueError as error:
                raise EquipeError(f"team set takes a JSON object: {error}") from error
            if not isinstance(update, dict):
                raise EquipeError("team set takes a JSON object")
            team.update(update)
        elif args.action == "person":
            sub = args.rest[0] if args.rest else ""
            if sub == "add":
                if len(args.rest) < 2 or not args.github:
                    raise EquipeError("team person add <name> --github LOGIN [--level junior] [--board-person EMAIL]")
                team["people"] = [p for p in team["people"] if p["github"].lower() != args.github.lower()]
                team["people"].append({"name": " ".join(args.rest[1:]), "github": args.github, "level": args.level,
                                       "board_person": args.board_person or ""})
            elif sub == "remove" and len(args.rest) == 2:
                before = len(team["people"])
                team["people"] = [p for p in team["people"] if p["github"].lower() != args.rest[1].lower()]
                if len(team["people"]) == before:
                    raise EquipeError(f"no person with GitHub login {args.rest[1]}")
            else:
                raise EquipeError("team person add|remove")
        elif args.action == "repo":
            sub = args.rest[0] if args.rest else ""
            if sub == "add" and len(args.rest) == 2:
                team["repos"] = [r for r in team["repos"] if r["repo"].lower() != args.rest[1].lower()]
                team["repos"].append({"repo": args.rest[1], "dev": args.dev, "prod": args.prod, "promote": args.promote,
                                      "allow_no_ci": args.allow_no_ci})
            elif sub == "remove" and len(args.rest) == 2:
                before = len(team["repos"])
                team["repos"] = [r for r in team["repos"] if r["repo"].lower() != args.rest[1].lower()]
                if len(team["repos"]) == before:
                    raise EquipeError(f"{args.rest[1]} is not in the team's repositories")
            else:
                raise EquipeError("team repo add|remove owner/name")
        saved = core.save_team(team)
    out({"team": saved})


def cmd_github(args):
    if args.action == "connect":
        started = core.github_connect_start()
        out({**started, "say": "two steps: install the app on the repositories, then type the code at the url"})
        return
    result = core.github_connect_poll()
    if result["status"] == "connected":
        github = gh()
        me = github.call("GET", "/user")
        installs = github.call("GET", "/user/installations").get("installations", [])
        repos = []
        for install in installs:
            data = github.call("GET", f"/user/installations/{install['id']}/repositories?per_page=100")
            repos += [r["full_name"] for r in data.get("repositories", [])]
        result.update(login=me["login"], repositories=repos)
    out(result)


def cmd_board(args):
    if args.action == "connect":
        if not args.url:
            raise EquipeError("board connect <https-url>")
        started = core.board_connect_start(args.url)
        with core.locked():
            team = core.load_team()
            team["board"] = {"type": "ritmo", "url": args.url.rstrip("/")}
            core.save_team(team)
        out(started)
        return
    if args.action == "status":
        out(core.board_connect_poll())
        return
    team = core.load_team()
    if team["board"]["type"] != "ritmo":
        raise EquipeError("the board is GitHub Issues; people come from `team person add`")
    saved = core.read_json("board.json", {})
    board = core.RitmoBoard(saved.get("url", ""), saved.get("token", ""))
    out(board.call("GET", "/api/ritmo/v1/pessoas"))


# --- reviews ---------------------------------------------------------------------

def find_demand(team: dict, github: core.GitHub, repo: str, pr: dict, state: dict):
    """The demand a PR delivers: an explicit reference, an owner-given link, or one unambiguous match."""
    board = core.board_for(team, github, repo)
    linked = state["asked"].get(key(repo, pr["number"]), {})
    refs = ([linked["demand"]] if linked.get("demand") else []) + core.demand_refs(team, pr)
    for ref in refs:
        try:
            return board.get(ref), "reference"
        except EquipeError:
            continue
    if isinstance(board, core.RitmoBoard):
        person = core.person_for(team, pr["user"]["login"])
        repo_url = f"https://github.com/{repo}".lower()
        candidates = [d for d in board.open_demands(github=repo_url)
                      if d.get("githubUrl") and (d["githubUrl"].rstrip("/") == pr["html_url"].rstrip("/")
                                                 or (d["githubUrl"].rstrip("/").lower() == repo_url
                                                     and person and d.get("person") == person["board_person"]
                                                     and d.get("status") in ("doing", "review")))]
        if len(candidates) == 1:
            return board.get(str(candidates[0]["id"])), "github link"
        return None, [{"id": d["id"], "title": d["title"]} for d in candidates[:5]]
    return None, []


def pending_work(team: dict, github: core.GitHub, state: dict, limit: int = 3) -> list[dict]:
    """PRs whose current commit has not been handled, with finished checks; and promotions that are due."""
    work = []
    config = ritmo_config()
    for repo in team["repos"]:
        bases = [repo["dev"]] + ([repo["prod"]] if repo["prod"] != repo["dev"] else [])
        for base in bases:
            for pr in github.pages(f"/repos/{repo['repo']}/pulls?state=open&base={base}", limit=50):
                if pr.get("draft") or pr["head"]["ref"] == repo["dev"]:
                    continue
                record = state["reviews"].get(key(repo["repo"], pr["number"]), {})
                if record.get("sha") == pr["head"]["sha"] and record.get("verdict") != "ok_waiting":
                    continue
                if github.checks(repo["repo"], pr["head"]["sha"])["verdict"] == "pending":
                    continue
                kind = "retry_merge" if record.get("verdict") == "ok_waiting" and record.get("sha") == pr["head"]["sha"] \
                    else "review"
                work.append({"type": kind, "repo": repo["repo"], "number": pr["number"], "sha": pr["head"]["sha"],
                             "author": pr["user"]["login"], "base": base})
                if len(work) >= limit:
                    return work
        if core.promotion_due(repo, config, state):
            compare = github.call("GET", f"/repos/{repo['repo']}/compare/{repo['prod']}...{repo['dev']}")
            if compare.get("ahead_by"):
                dev_sha = github.call("GET", f"/repos/{repo['repo']}/branches/{repo['dev']}")["commit"]["sha"]
                if github.checks(repo["repo"], dev_sha)["verdict"] in ("green", "none"):
                    work.append({"type": "promote", "repo": repo["repo"], "ahead_by": compare["ahead_by"]})
        if len(work) >= limit:
            break
    return work[:limit]


def cmd_pending(args):
    team = core.load_team()
    out({"work": pending_work(team, gh(), core.load_state(), limit=10)})


def cmd_review(args):
    team = core.load_team()
    repo = core.repo_for(team, args.repo)
    github = gh()
    state = core.load_state()
    pr = github.call("GET", f"/repos/{repo['repo']}/pulls/{args.number}")
    demand, how = find_demand(team, github, repo["repo"], pr, state)
    files, size = [], 0
    for f in github.pages(f"/repos/{repo['repo']}/pulls/{args.number}/files"):
        patch = f.get("patch") or ""
        if len(patch) > core.PATCH_LIMIT:
            patch = patch[:core.PATCH_LIMIT] + "\n[... cut: open the file in GitHub to read the rest]"
        if size + len(patch) > core.BUNDLE_LIMIT:
            patch = "[not shown: the change is too large for one review; judge from the file list]"
        size += len(patch)
        files.append({"file": f["filename"], "status": f["status"], "+": f["additions"], "-": f["deletions"], "patch": patch})
    person = core.person_for(team, pr["user"]["login"])
    out({
        "repo": repo["repo"], "number": pr["number"], "sha": pr["head"]["sha"], "url": pr["html_url"],
        "title": pr["title"], "base": pr["base"]["ref"], "branch": pr["head"]["ref"],
        "author": {"login": pr["user"]["login"], **(person or {"name": None, "level": "mid"})},
        "pr_text_UNTRUSTED": (pr.get("body") or "")[:4000],
        "demand": demand if demand else {"found": False, "candidates": how},
        "demand_found_by": how if demand else None,
        "commits": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:300]}
                    for c in github.pages(f"/repos/{repo['repo']}/pulls/{args.number}/commits", limit=100)],
        "files": files,
        "checks": github.checks(repo["repo"], pr["head"]["sha"]),
        "rules": team["rules"],
        "previous_followup": state["followups"].get(key(repo["repo"], pr["number"])),
        "production": pr["base"]["ref"] == repo["prod"],
    })


def try_merge(team, repo, github, number, sha, owner_ok=False) -> dict:
    pr = github.call("GET", f"/repos/{repo['repo']}/pulls/{number}")
    production = pr["base"]["ref"] == repo["prod"]
    blockers = core.merge_blockers(github, team, repo, pr, sha, production=production,
                                   ritmo_config=ritmo_config(), owner_ok=owner_ok)
    if blockers:
        return {"merged": False, "blockers": blockers}
    result = core.merge(github, team, repo, number, sha)
    if result.get("merged"):
        result["branch_deleted"] = core.delete_branch_if_safe(github, team, repo, pr)
        result["into"] = pr["base"]["ref"]
    return result


def cmd_verdict(args):
    team = core.load_team()
    repo = core.repo_for(team, args.repo)
    github = gh()
    result: dict = {"recorded": "ok" if args.ok else "not_ok"}
    verdict = "not_ok"
    if args.ok:
        if repo["auto_merge"]:
            merged = try_merge(team, repo, github, args.number, args.sha)
            result["merge"] = merged
            if merged.get("merged"):
                verdict = "ok_merged"
            elif any("new commits" in b for b in merged.get("blockers", [])):
                verdict = "stale"
            else:
                verdict = "ok_waiting"
        else:
            verdict = "ok_manual"
    with core.locked():
        state = core.load_state()
        state["reviews"][key(repo["repo"], args.number)] = {"sha": args.sha, "verdict": verdict, "summary": args.summary[:500],
                                                            "at": core.now_utc().isoformat(timespec="seconds")}
        core.write_json("equipe_state.json", state)
    out(result)


def cmd_merge(args):
    team = core.load_team()
    repo = core.repo_for(team, args.repo)
    github = gh()
    result = try_merge(team, repo, github, args.number, args.sha, owner_ok=args.owner_ok)
    with core.locked():
        state = core.load_state()
        record = state["reviews"].setdefault(key(repo["repo"], args.number), {"sha": args.sha})
        if result.get("merged"):
            record["verdict"] = "ok_merged"
        core.write_json("equipe_state.json", state)
    out(result)


def cmd_followup(args):
    team = core.load_team()
    repo = core.repo_for(team, args.repo)
    github = gh()
    pr = github.call("GET", f"/repos/{repo['repo']}/pulls/{args.number}")
    try:
        steps = json.loads(args.steps) if args.steps else []
    except ValueError as error:
        raise EquipeError(f"--steps takes a JSON list of {{\"title\", \"location\"}}: {error}") from error
    person = core.person_for(team, pr["user"]["login"])
    board = core.board_for(team, github, repo["repo"])
    state = core.load_state()
    existing = state["followups"].get(key(repo["repo"], args.number))
    if existing:
        # One follow-up per PR: a new round of review updates it instead of opening another.
        board.comment(existing["id"], f"{args.title}\n\n{args.description}")
        out({"updated": existing, "published": True})
        return
    if team["publish"] == "approve" and not args.approved:
        out({"draft": {"title": args.title, "description": args.description, "steps": steps},
             "published": False, "say": "the owner chose to approve follow-ups first: show this draft and wait"})
        return
    person_ref = (person or {}).get("board_person") if team["board"]["type"] == "ritmo" else pr["user"]["login"]
    created = board.create({"title": args.title, "description": args.description, "steps": steps,
                            "person": person_ref or None, "githubUrl": pr["html_url"]})
    with core.locked():
        state = core.load_state()
        state["followups"][key(repo["repo"], args.number)] = created
        core.write_json("equipe_state.json", state)
    out({"created": created, "published": True})


def cmd_hold(args):
    """Park a PR at this commit until the owner answers (no demand found, a question asked)."""
    with core.locked():
        state = core.load_state()
        state["reviews"][key(args.repo, args.number)] = {"sha": args.sha, "verdict": "held", "summary": args.summary[:500],
                                                         "at": core.now_utc().isoformat(timespec="seconds")}
        core.write_json("equipe_state.json", state)
    out({"held": {"pr": args.number, "sha": args.sha[:7]}})


def cmd_link(args):
    team = core.load_team()
    core.repo_for(team, args.repo)
    with core.locked():
        state = core.load_state()
        state["asked"][key(args.repo, args.number)] = {"demand": str(args.demand)}
        record = state["reviews"].get(key(args.repo, args.number))
        if record:
            record["sha"] = ""   # review again, now with the demand
        core.write_json("equipe_state.json", state)
    out({"linked": {"pr": args.number, "demand": str(args.demand)}})


def cmd_promote(args):
    team = core.load_team()
    repo = core.repo_for(team, args.repo)
    github = gh()
    existing = github.pages(f"/repos/{repo['repo']}/pulls?state=open&base={repo['prod']}&head="
                            f"{repo['repo'].split('/')[0]}:{repo['dev']}", limit=5)
    if existing:
        pr = existing[0]
    else:
        compare = github.call("GET", f"/repos/{repo['repo']}/compare/{repo['prod']}...{repo['dev']}")
        if not compare.get("ahead_by"):
            out({"promoted": False, "reason": f"{repo['dev']} has nothing new for {repo['prod']}"})
            return
        pr = github.call("POST", f"/repos/{repo['repo']}/pulls", {
            "title": f"Promote {repo['dev']} to {repo['prod']}", "head": repo["dev"], "base": repo["prod"],
            "body": f"Opened by Ritmo: {compare['ahead_by']} commit(s) already reviewed and merged into {repo['dev']}."})
    pr = github.call("GET", f"/repos/{repo['repo']}/pulls/{pr['number']}")
    result = try_merge(team, repo, github, pr["number"], pr["head"]["sha"], owner_ok=args.owner_ok)
    if result.get("merged"):
        with core.locked():
            state = core.load_state()
            import datetime as dt
            from zoneinfo import ZoneInfo
            zone = ritmo_config().get("timezone") or "UTC"
            state["promotions"][repo["repo"].lower()] = core.now_utc().astimezone(ZoneInfo(zone)).date().isoformat()
            core.write_json("equipe_state.json", state)
    out({"promotion_pr": pr["html_url"], **result})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="equipe.py")
    sub = root.add_subparsers(dest="command", required=True)

    team = sub.add_parser("team")
    team.add_argument("action", choices=["show", "set", "person", "repo"])
    team.add_argument("rest", nargs="*")
    team.add_argument("--github")
    team.add_argument("--level", default="mid", choices=list(core.LEVELS))
    team.add_argument("--board-person")
    team.add_argument("--dev", default="develop")
    team.add_argument("--prod", default="main")
    team.add_argument("--promote", default="now")
    team.add_argument("--allow-no-ci", action="store_true")

    github = sub.add_parser("github")
    github.add_argument("action", choices=["connect", "status"])

    board = sub.add_parser("board")
    board.add_argument("action", choices=["connect", "status", "people"])
    board.add_argument("url", nargs="?")

    sub.add_parser("pending")

    review = sub.add_parser("review")
    review.add_argument("repo")
    review.add_argument("number", type=int)

    verdict = sub.add_parser("verdict")
    verdict.add_argument("repo")
    verdict.add_argument("number", type=int)
    verdict.add_argument("--sha", required=True)
    group = verdict.add_mutually_exclusive_group(required=True)
    group.add_argument("--ok", action="store_true")
    group.add_argument("--not-ok", action="store_true")
    verdict.add_argument("--summary", default="")

    merge = sub.add_parser("merge")
    merge.add_argument("repo")
    merge.add_argument("number", type=int)
    merge.add_argument("--sha", required=True)
    merge.add_argument("--owner-ok", action="store_true")

    followup = sub.add_parser("followup")
    followup.add_argument("repo")
    followup.add_argument("number", type=int)
    followup.add_argument("--title", required=True)
    followup.add_argument("--description", required=True)
    followup.add_argument("--steps")
    followup.add_argument("--approved", action="store_true", help="the owner approved this draft")

    hold = sub.add_parser("hold")
    hold.add_argument("repo")
    hold.add_argument("number", type=int)
    hold.add_argument("--sha", required=True)
    hold.add_argument("--summary", default="")

    link = sub.add_parser("link")
    link.add_argument("repo")
    link.add_argument("number", type=int)
    link.add_argument("demand")

    promote = sub.add_parser("promote")
    promote.add_argument("repo")
    promote.add_argument("--owner-ok", action="store_true")
    return root


COMMANDS = {"team": cmd_team, "github": cmd_github, "board": cmd_board, "pending": cmd_pending, "review": cmd_review,
            "verdict": cmd_verdict, "merge": cmd_merge, "followup": cmd_followup, "hold": cmd_hold, "link": cmd_link,
            "promote": cmd_promote}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        COMMANDS[args.command](args)
    except EquipeError as error:
        out({"error": str(error)})
        return 1
    except Exception as error:  # noqa: BLE001 -- the agent reads JSON, never a traceback
        out({"error": f"unexpected {error.__class__.__name__}: {error}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
