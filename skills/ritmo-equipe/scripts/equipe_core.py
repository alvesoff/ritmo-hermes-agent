"""Ritmo's Team module: who works on what, the GitHub connection, the demand board,
and every rule that decides whether a pull request may be merged.

The LLM reads a pull request against its demand and says what it thinks. The
code here decides what actually happens: a merge needs the exact commit that
was reviewed, every check finished green, no change to CI, an allowed target
branch and, for production, the owner's active hours. A promotion to production
carries only commits Ritmo itself reviewed and merged. Nothing the team writes
in a PR can change those rules.

Two boards are supported. `github`: the repository's own Issues. `ritmo`: any
system that implements the Ritmo board API (see the OpenSeed painel, ritmo.js).
Standard library only.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import datetime as dt
import fnmatch
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

SCHEMA_VERSION = 1
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
APP_FILE = os.environ.get("RITMO_GITHUB_APP_FILE", "/opt/ritmo/github_app.json")
GREEN = {"success", "neutral", "skipped"}
# CI lives in workflows and the composite actions they call: a PR touching either can change its own checks.
CI_PATHS = (".github/workflows/", ".github/actions/")
# A commit with no check at all after this long is taken as "CI does not run on PRs here".
NO_CHECKS_AFTER = dt.timedelta(minutes=15)
PATCH_LIMIT = 6000
BUNDLE_LIMIT = 60000
_SHA = re.compile(r"^[0-9a-f]{40}$")

DEFAULT_TEAM: dict = {
    "version": SCHEMA_VERSION,
    "enabled": False,
    "board": {"type": "github", "url": ""},
    "repos": [],
    "people": [],
    "rules": [],
    "publish": "auto",           # follow-up demands: "auto" or "approve"
    "merge_method": "merge",     # merge or squash for team PRs; promotions are always merge commits
    "long_lived": ["main", "master", "develop", "release/*"],
    "review_model": "anthropic/claude-sonnet-5",
    "paused_until": "",
}
REPO_DEFAULTS = {"dev": "develop", "prod": "main", "promote": "now", "auto_merge": True, "allow_no_ci": False,
                 "scope": ""}   # what the owner told about the project, read before writing its demands
SCOPE_MAX = 4000
LEVELS = ("junior", "mid", "senior")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class EquipeError(ValueError):
    """A refusal to show the model verbatim: what was wrong and what to do."""


# --- files -------------------------------------------------------------------

def home() -> str:
    return os.environ.get("HERMES_HOME") or "/var/lib/hermes"


def path(name: str) -> str:
    return os.path.join(home(), "ritmo", name)


@contextlib.contextmanager
def locked() -> Iterator[None]:
    os.makedirs(os.path.dirname(path("x")), exist_ok=True)
    handle = open(path(".equipe.lock"), "a+")
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def read_json(name: str, default: dict) -> dict:
    try:
        with open(path(name), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except ValueError as error:
        raise EquipeError(f"{path(name)} is not valid JSON ({error})") from error
    if not isinstance(data, dict):
        raise EquipeError(f"{path(name)} should hold a JSON object")
    return data


def write_json(name: str, data: dict, *, private: bool = False) -> None:
    """Atomic write; credential files are 0600 from the first byte."""
    directory = os.path.dirname(path(name))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".new")
    try:
        if private:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path(name))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def now_utc() -> dt.datetime:
    pinned = os.environ.get("RITMO_NOW")
    if pinned:
        return dt.datetime.fromisoformat(pinned).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def parse_moment(text: str, field: str) -> dt.datetime:
    """An ISO moment with its UTC offset ("Z" accepted on every Python)."""
    try:
        moment = dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise EquipeError(f"{field} must be an ISO date and time with its offset, e.g. 2026-10-20T08:00:00-03:00") from error
    if moment.tzinfo is None:
        raise EquipeError(f"{field} needs its UTC offset, e.g. 2026-10-20T08:00:00-03:00")
    return moment


def ritmo_config() -> dict:
    """The routine settings (timezone, quiet hours) the production rules follow."""
    try:
        with open(path("config.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- team config ---------------------------------------------------------------

def validate_team(team: dict) -> dict:
    if not isinstance(team, dict):
        raise EquipeError("the team settings must be a JSON object")
    merged = copy.deepcopy(DEFAULT_TEAM)
    for key, value in team.items():
        if key not in DEFAULT_TEAM:
            raise EquipeError(f"unknown team key {key!r}; valid keys: {sorted(DEFAULT_TEAM)}")
        merged[key] = copy.deepcopy(value)
    merged["version"] = SCHEMA_VERSION
    if not isinstance(merged["enabled"], bool):
        raise EquipeError("enabled must be true or false")
    board = merged["board"]
    if not isinstance(board, dict) or board.get("type") not in ("github", "ritmo"):
        raise EquipeError("board.type is 'github' (the repo's Issues) or 'ritmo' (a system with the Ritmo board API)")
    if board["type"] == "ritmo" and not str(board.get("url", "")).startswith("https://"):
        raise EquipeError("a ritmo board needs its https url, e.g. https://painel.example.com")
    repos, seen = [], set()
    for entry in merged["repos"] if isinstance(merged["repos"], list) else []:
        if not isinstance(entry, dict) or not _REPO.match(str(entry.get("repo", ""))):
            raise EquipeError("each repo is {\"repo\": \"owner/name\", \"dev\": \"develop\", \"prod\": \"main\"}")
        repo = {**REPO_DEFAULTS, **entry}
        repo["repo"] = repo["repo"].strip()
        if repo["repo"].lower() in seen:
            raise EquipeError(f"{repo['repo']} is listed twice")
        seen.add(repo["repo"].lower())
        if repo["promote"] not in ("now", "manual") and not _TIME.match(str(repo["promote"])):
            raise EquipeError("promote is 'now', 'manual' or a time like '18:00'")
        for flag in ("auto_merge", "allow_no_ci"):
            if not isinstance(repo[flag], bool):
                raise EquipeError(f"{flag} must be true or false")
        if not isinstance(repo["scope"], str) or len(repo["scope"]) > SCOPE_MAX:
            raise EquipeError(f"scope is the project's description in up to {SCOPE_MAX} characters")
        if not repo["dev"] or not repo["prod"]:
            raise EquipeError("dev and prod are branch names; with a single branch, give the same name to both")
        repos.append(repo)
    merged["repos"] = repos
    people, logins = [], set()
    for person in merged["people"] if isinstance(merged["people"], list) else []:
        if not isinstance(person, dict) or not str(person.get("name", "")).strip():
            raise EquipeError("each person is {\"name\", \"github\", \"level\", \"board_person\"}")
        login = str(person.get("github", "")).strip()
        if not _LOGIN.match(login):
            raise EquipeError(f"{person['name']}: github must be their GitHub login")
        if login.lower() in logins:
            raise EquipeError(f"two people share the GitHub login {login}")
        logins.add(login.lower())
        level = person.get("level", "mid")
        if level not in LEVELS:
            raise EquipeError(f"level is one of {', '.join(LEVELS)}")
        people.append({"name": person["name"].strip(), "github": login, "level": level,
                       "board_person": str(person.get("board_person", "")).strip()})
    merged["people"] = people
    if not isinstance(merged["rules"], list) or not all(isinstance(r, str) for r in merged["rules"]):
        raise EquipeError("rules is a list of sentences")
    if merged["publish"] not in ("auto", "approve"):
        raise EquipeError("publish is 'auto' or 'approve'")
    if merged["merge_method"] not in ("merge", "squash"):
        raise EquipeError("merge_method is merge or squash")
    if not isinstance(merged["long_lived"], list):
        raise EquipeError("long_lived is a list of branch names or patterns")
    if merged["paused_until"]:
        merged["paused_until"] = parse_moment(merged["paused_until"], "paused_until").isoformat()
    if merged["enabled"] and not ritmo_config().get("timezone"):
        raise EquipeError("set the owner's city and timezone in Ritmo first: production merges follow their hours")
    return merged


def load_team() -> dict:
    raw = read_json("equipe.json", {})
    try:
        return validate_team(raw)
    except EquipeError as error:
        if "timezone" in str(error):
            # A routine reset (new agent, empty config) must not break the team commands that repair it.
            raw["enabled"] = False
            return validate_team(raw)
        raise


def save_team(team: dict) -> dict:
    valid = validate_team(team)
    write_json("equipe.json", valid)
    return valid


def load_state() -> dict:
    state = read_json("equipe_state.json", {"version": SCHEMA_VERSION})
    for key in ("reviews", "followups", "asked", "promotions", "reviewed_commits", "problems"):
        state.setdefault(key, {})
    return state


def person_for(team: dict, login: str) -> dict | None:
    return next((p for p in team["people"] if p["github"].lower() == str(login).lower()), None)


def repo_for(team: dict, name: str) -> dict:
    """By owner/name, or by name alone when only one team repository has it."""
    name = name.strip().lower()
    repo = next((r for r in team["repos"] if r["repo"].lower() == name), None)
    if repo is None and "/" not in name:
        matches = [r for r in team["repos"] if r["repo"].lower().split("/", 1)[1] == name]
        repo = matches[0] if len(matches) == 1 else None
    if repo is None:
        raise EquipeError(f"{name} is not one of the team's repositories; add it with `team repo add`")
    return repo


def check_sha(sha: str) -> str:
    if not _SHA.match(str(sha)):
        raise EquipeError("use the full 40-character sha from `review` (the top-level `sha`), not a short one")
    return sha


# --- HTTP ----------------------------------------------------------------------

Requester = Callable[..., tuple[int, object, dict]]


def http(method: str, url: str, *, token: str = "", body: object = None, form: dict | None = None,
         accept: str = "application/vnd.github+json", timeout: int = 30) -> tuple[int, object, dict]:
    """(status, parsed JSON or text, headers). Never raises on HTTP status; network errors raise EquipeError."""
    headers = {"Accept": accept, "User-Agent": "ritmo-agent", "X-GitHub-Api-Version": "2022-11-28"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw, got = response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        status, raw, got = error.code, error.read(), dict(error.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EquipeError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {error}") from error
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text else None, got
    except ValueError:
        return status, text, got


# --- GitHub connection (GitHub App, device flow) ---------------------------------

def github_app() -> dict:
    try:
        with open(APP_FILE, encoding="utf-8") as handle:
            app = json.load(handle)
    except (OSError, ValueError) as error:
        raise EquipeError("this Ritmo image has no GitHub App configured") from error
    if not app.get("client_id") or not app.get("slug"):
        raise EquipeError("this Ritmo image has no GitHub App configured")
    return app


def github_connect_start(request: Requester = http) -> dict:
    app = github_app()
    status, data, _ = request("POST", f"{GITHUB_WEB}/login/device/code", form={"client_id": app["client_id"]},
                              accept="application/json")
    if status != 200 or not isinstance(data, dict) or "device_code" not in data:
        raise EquipeError(f"GitHub refused to start the connection ({status}): {data}")
    pending = {"device_code": data["device_code"], "interval": int(data.get("interval", 5)),
               "expires_at": (now_utc() + dt.timedelta(seconds=int(data.get("expires_in", 900)))).isoformat()}
    saved = read_json("github.json", {})
    saved["pending"] = pending
    write_json("github.json", saved, private=True)
    return {"code": data["user_code"], "url": data.get("verification_uri", f"{GITHUB_WEB}/login/device"),
            "install_url": f"{GITHUB_WEB}/apps/{app['slug']}/installations/new",
            "expires_in_minutes": int(data.get("expires_in", 900)) // 60}


def _store_tokens(data: dict) -> dict:
    now = now_utc()
    saved = read_json("github.json", {})
    saved.pop("pending", None)
    saved.update({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": (now + dt.timedelta(seconds=int(data["expires_in"]))).isoformat() if data.get("expires_in") else "",
        "refresh_expires_at": (now + dt.timedelta(seconds=int(data["refresh_token_expires_in"]))).isoformat()
        if data.get("refresh_token_expires_in") else "",
    })
    write_json("github.json", saved, private=True)
    return saved


def github_connect_poll(request: Requester = http) -> dict:
    """One poll of the device flow: connected, pending, or a clear reason it ended."""
    saved = read_json("github.json", {})
    pending = saved.get("pending")
    if not pending:
        return {"status": "connected" if saved.get("access_token") else "not_started"}
    if now_utc() > dt.datetime.fromisoformat(pending["expires_at"]):
        saved.pop("pending", None)
        write_json("github.json", saved, private=True)
        return {"status": "expired"}
    status, data, _ = request("POST", f"{GITHUB_WEB}/login/oauth/access_token", accept="application/json", form={
        "client_id": github_app()["client_id"], "device_code": pending["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if isinstance(data, dict) and data.get("access_token"):
        _store_tokens(data)
        return {"status": "connected"}
    error = data.get("error") if isinstance(data, dict) else str(status)
    if error in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    saved.pop("pending", None)
    write_json("github.json", saved, private=True)
    return {"status": "failed", "reason": error}


def github_token(request: Requester = http) -> str:
    """A valid user token, refreshed when it is about to expire (device-flow tokens refresh without a secret)."""
    saved = read_json("github.json", {})
    token = saved.get("access_token")
    if not token:
        raise EquipeError("GitHub is not connected; run `github connect`")
    expires = saved.get("expires_at")
    if expires and now_utc() > dt.datetime.fromisoformat(expires) - dt.timedelta(minutes=10):
        if not saved.get("refresh_token"):
            raise EquipeError("the GitHub connection expired; run `github connect` again")
        status, data, _ = request("POST", f"{GITHUB_WEB}/login/oauth/access_token", accept="application/json", form={
            "client_id": github_app()["client_id"], "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"]})
        if not (isinstance(data, dict) and data.get("access_token")):
            raise EquipeError("the GitHub connection expired and could not be renewed; run `github connect` again")
        saved = _store_tokens(data)
    return saved["access_token"]


class GitHub:
    """The few GitHub calls the module needs, with the owner's user token."""

    def __init__(self, token: str, request: Requester = http):
        self.token, self.request = token, request

    def call(self, method: str, route: str, body: object = None, ok: tuple = (200, 201, 204)) -> object:
        status, data, _ = self.request(method, GITHUB_API + route, token=self.token, body=body)
        if status not in ok:
            message = data.get("message") if isinstance(data, dict) else data
            raise EquipeError(f"GitHub {method} {route} answered {status}: {message}")
        return data

    def text_file(self, repo: str, path: str, limit: int = 6000) -> str:
        """A text file of the default branch, or "" when it does not exist."""
        status, data, _ = self.request("GET", f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}",
                                       token=self.token)
        if status == 404:
            return ""
        if status != 200:
            raise EquipeError(f"GitHub GET {path} answered {status}")
        if not isinstance(data, dict) or data.get("type") != "file":
            return ""
        text = base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        return text if len(text) <= limit else text[:limit] + "\n[...]"

    def pages(self, route: str, limit: int = 300) -> list:
        items, page = [], 1
        joiner = "&" if "?" in route else "?"
        while len(items) < limit:
            batch = self.call("GET", f"{route}{joiner}per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    def checks(self, repo: str, sha: str) -> dict:
        """Every check run and commit status on a commit, as one verdict: green, pending, red or none."""
        runs, page = [], 1
        while True:
            data = self.call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}")
            runs += data.get("check_runs", [])
            if len(runs) >= int(data.get("total_count", 0)) or not data.get("check_runs"):
                break
            page += 1
        statuses = self.call("GET", f"/repos/{repo}/commits/{sha}/status").get("statuses", [])
        items = [{"name": r["name"], "state": r["status"], "conclusion": r.get("conclusion")} for r in runs]
        items += [{"name": s["context"], "state": "completed" if s["state"] != "pending" else "pending",
                   "conclusion": {"success": "success", "pending": None}.get(s["state"], "failure")} for s in statuses]
        if not items:
            verdict = "none"
        elif any(i["state"] != "completed" for i in items):
            verdict = "pending"
        elif all(i["conclusion"] in GREEN for i in items):
            verdict = "green"
        else:
            verdict = "red"
        return {"verdict": verdict, "checks": items}

    def commit_age(self, repo: str, sha: str) -> dt.timedelta:
        commit = self.call("GET", f"/repos/{repo}/commits/{sha}")
        when = commit.get("commit", {}).get("committer", {}).get("date") or commit.get("commit", {}).get("author", {}).get("date")
        return now_utc() - parse_moment(when, "commit date") if when else dt.timedelta(0)

    def has_workflows(self, repo: str) -> bool:
        return bool(self.call("GET", f"/repos/{repo}/actions/workflows?per_page=1").get("total_count"))

    def pr_files(self, repo: str, pr: dict) -> list[dict]:
        """The files of exactly the PR's head commit, so the reviewed diff and the reviewed sha are one thing."""
        compare = self.call("GET", f"/repos/{repo}/compare/{pr['base']['sha']}...{pr['head']['sha']}")
        return compare.get("files", [])

    def pr_commits(self, repo: str, number: int) -> list[dict]:
        return self.pages(f"/repos/{repo}/pulls/{number}/commits", limit=250)


# --- boards ------------------------------------------------------------------------

_ISSUE_REF = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\b\s*:?\s*#(\d+)", re.I)
_ISSUE_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", re.I)
_DEMAND_WORD = re.compile(r"\bdemand(?:a|e|as)?[\s#:_-]*(\d+)\b", re.I)


def demand_refs(team: dict, pr: dict) -> list[str]:
    """Demand ids a PR names, strongest first: an explicit reference, then a demand word or branch name."""
    text = "\n".join(str(pr.get(k) or "") for k in ("title", "body"))
    branch = str(pr.get("head", {}).get("ref", ""))
    refs: list[str] = []
    if team["board"]["type"] == "github":
        refs += [m.group(1) for m in _ISSUE_REF.finditer(text)]
        refs += [m.group(2) for m in _ISSUE_URL.finditer(text) if m.group(1).lower() == pr["base"]["repo"]["full_name"].lower()]
    else:
        base = re.escape(team["board"]["url"].rstrip("/"))
        refs += re.findall(base + r"/tarefas\?t=(\d+)", text)
        refs += [m.group(1) for m in _DEMAND_WORD.finditer(text)]
        refs += [m.group(1) for m in _DEMAND_WORD.finditer(branch.replace("/", " "))]
    unique: list[str] = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


class GithubIssuesBoard:
    def __init__(self, github: GitHub, repo: str):
        self.github, self.repo = github, repo

    def get(self, ref: str) -> dict:
        issue = self.github.call("GET", f"/repos/{self.repo}/issues/{int(ref)}")
        # /issues/N also answers for pull requests: a PR naming itself would make its own text the "demand".
        if issue.get("pull_request"):
            raise EquipeError(f"#{ref} is a pull request, not a demand")
        if issue.get("state") != "open":
            raise EquipeError(f"#{ref} is closed")
        return {"id": str(issue["number"]), "url": issue["html_url"], "title": issue["title"],
                "description": issue.get("body") or "", "status": issue["state"],
                "person": (issue.get("assignees") or [{}])[0].get("login"), "steps": []}

    def create(self, demand: dict) -> dict:
        body = demand["description"]
        if demand.get("steps"):
            body += "\n\n" + "\n".join(f"- [ ] {s['title']}" + (f" ({s['location']})" if s.get("location") else "")
                                      for s in demand["steps"])
        issue = self.github.call("POST", f"/repos/{self.repo}/issues", {
            "title": demand["title"], "body": body,
            **({"assignees": [demand["person"]]} if demand.get("person") else {})})
        return {"id": str(issue["number"]), "url": issue["html_url"]}

    def comment(self, ref: str, text: str) -> None:
        self.github.call("POST", f"/repos/{self.repo}/issues/{int(ref)}/comments", {"body": text})


class RitmoBoard:
    """Any system with the Ritmo board API (OpenSeed's painel implements it in ritmo.js)."""

    def __init__(self, url: str, token: str, request: Requester = http):
        self.url, self.token, self.request = url.rstrip("/"), token, request

    def call(self, method: str, route: str, body: object = None, ok: tuple = (200, 201)) -> object:
        status, data, _ = self.request(method, self.url + route, token=self.token, body=body, accept="application/json")
        if status == 401:
            raise EquipeError("the board connection was revoked or expired; run `board connect` again")
        if status not in ok:
            message = data.get("error") if isinstance(data, dict) else data
            raise EquipeError(f"board {method} {route} answered {status}: {message}")
        return data

    def get(self, ref: str) -> dict:
        d = self.call("GET", f"/api/ritmo/v1/demandas/{int(ref)}")
        if d.get("status") == "done":
            raise EquipeError(f"demand {ref} is already done")
        return {"id": str(d["id"]), "url": d["url"], "title": d["title"], "description": d.get("description", ""),
                "status": d["status"], "person": d.get("person", {}).get("email"), "githubUrl": d.get("githubUrl"),
                "steps": d.get("steps", []), "dueDate": d.get("dueDate"), "priority": d.get("priority")}

    def open_demands(self, github: str = "", person: str = "") -> list[dict]:
        query = urllib.parse.urlencode({k: v for k, v in (("github", github), ("pessoa", person)) if v})
        return self.call("GET", "/api/ritmo/v1/demandas" + (f"?{query}" if query else "")).get("demandas", [])

    def create(self, demand: dict) -> dict:
        created = self.call("POST", "/api/ritmo/v1/demandas", {
            "person": demand.get("person"), "title": demand["title"], "description": demand["description"],
            "githubUrl": demand.get("githubUrl"), "priority": demand.get("priority", "normal"),
            "dueDate": demand.get("dueDate"), "steps": demand.get("steps", []),
            **({"projectId": demand["projectId"]} if demand.get("projectId") else {})})
        return {"id": str(created["id"]), "url": created["url"], **({"incomplete": created["incompleta"]}
                                                                 if created.get("incompleta") else {})}

    def comment(self, ref: str, text: str) -> None:
        self.call("POST", f"/api/ritmo/v1/demandas/{int(ref)}/comentarios", {"text": text})

    def projects(self, person: str) -> list[dict]:
        query = urllib.parse.urlencode({"pessoa": person})
        return self.call("GET", f"/api/ritmo/v1/projetos?{query}").get("projetos", [])

    def create_project(self, person: str, name: str) -> dict:
        return self.call("POST", "/api/ritmo/v1/projetos", {"person": person, "name": name})


def board_connect_start(url: str, request: Requester = http) -> dict:
    url = url.rstrip("/")
    if not url.startswith("https://"):
        raise EquipeError("the board address must start with https://")
    status, data, _ = request("POST", f"{url}/api/ritmo/conectar", body={"nome": "Ritmo"}, accept="application/json")
    if status != 201 or not isinstance(data, dict) or "segredo" not in data:
        raise EquipeError(f"{url} does not answer like a Ritmo board ({status})")
    seconds = int(data.get("expira_em_segundos", 600))
    saved = read_json("board.json", {})
    # Ten more minutes than the code itself: the board extends it when the owner approves.
    saved["pending"] = {"url": url, "secret": data["segredo"],
                        "expires_at": (now_utc() + dt.timedelta(seconds=2 * seconds)).isoformat()}
    write_json("board.json", saved, private=True)
    return {"code": data["codigo"], "url": data["url"], "expires_in_minutes": seconds // 60}


def board_connect_poll(request: Requester = http) -> dict:
    saved = read_json("board.json", {})
    pending = saved.get("pending")
    if not pending:
        return {"status": "connected" if saved.get("token") else "not_started"}
    if now_utc() > dt.datetime.fromisoformat(pending["expires_at"]):
        saved.pop("pending", None)
        write_json("board.json", saved, private=True)
        return {"status": "expired"}
    status, data, _ = request("POST", f"{pending['url']}/api/ritmo/aguardar", body={"segredo": pending["secret"]},
                              accept="application/json")
    if status == 200 and isinstance(data, dict) and data.get("status") == "conectado":
        saved = {"url": pending["url"], "token": data["token"], "user": data.get("usuario", {})}
        write_json("board.json", saved, private=True)
        return {"status": "connected", "user": saved["user"]}
    if status == 200:
        return {"status": "pending"}
    saved.pop("pending", None)
    write_json("board.json", saved, private=True)
    return {"status": "expired"}


def board_for(team: dict, github: GitHub, repo: str, request: Requester = http):
    if team["board"]["type"] == "github":
        return GithubIssuesBoard(github, repo)
    saved = read_json("board.json", {})
    if not saved.get("token"):
        raise EquipeError("the demand board is not connected; run `board connect <url>`")
    return RitmoBoard(saved["url"], saved["token"], request)


# --- merge rules -----------------------------------------------------------------

def branch_matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def in_quiet(moment: dt.datetime, config: dict) -> bool:
    """The owner's quiet hours, from Ritmo's own config. Unknown hours count as quiet: production never fails open."""
    from zoneinfo import ZoneInfo
    zone = config.get("timezone")
    quiet = config.get("quiet") or {}
    if not zone:
        return True
    if not quiet.get("start") or not quiet.get("end"):
        return False
    local = moment.astimezone(ZoneInfo(zone))
    minute = local.hour * 60 + local.minute
    first = int(quiet["start"][:2]) * 60 + int(quiet["start"][3:])
    last = int(quiet["end"][:2]) * 60 + int(quiet["end"][3:])
    if first == last:
        return False
    return first <= minute < last if first < last else (minute >= first or minute < last)


def touches_ci(files: list[dict]) -> bool:
    return any(f["filename"].startswith(CI_PATHS) or str(f.get("previous_filename", "")).startswith(CI_PATHS)
               for f in files)


def merge_blockers(github: GitHub, team: dict, repo: dict, pr: dict, reviewed_sha: str, *,
                   production: bool, ritmo_config: dict, owner_ok: bool = False) -> list[str]:
    """Every reason this PR may not be merged right now. Empty means go."""
    number = pr["number"]
    if pr["head"]["sha"] != reviewed_sha:
        # Nothing else is worth checking for a commit that is no longer the PR's head.
        return ["new commits arrived after the review; it will be reviewed again"]
    blockers = []
    promotion = pr["head"]["ref"] == repo["dev"] and pr["base"]["ref"] == repo["prod"] and repo["dev"] != repo["prod"]
    author = (pr.get("user") or {}).get("login", "")
    if not promotion and person_for(team, author) is None and not owner_ok:
        blockers.append(f"{author or 'the author'} is not on the team; merging their PR needs the owner")
    if pr.get("state") != "open" or pr.get("merged"):
        blockers.append(f"PR {number} is not open")
    if pr.get("draft"):
        blockers.append(f"PR {number} is a draft")
    if (pr["head"].get("repo") or {}).get("full_name", "").lower() != repo["repo"].lower():
        blockers.append("the PR comes from a fork; merging it needs the owner")
    base = pr["base"]["ref"]
    if base not in (repo["dev"], repo["prod"]):
        blockers.append(f"the PR targets {base}, which is not this team's {repo['dev']} or {repo['prod']}")
    if production and repo["promote"] == "manual" and not owner_ok:
        blockers.append(f"{repo['prod']} is set to merge only with the owner's ok")
    if production and in_quiet(now_utc(), ritmo_config) and not owner_ok:
        blockers.append("production merges wait for the owner's active hours"
                        + ("" if ritmo_config.get("timezone") else " (Ritmo does not know the owner's timezone yet)"))
    if touches_ci(github.pr_files(repo["repo"], pr)) and not owner_ok:
        blockers.append("the PR changes CI (.github/workflows or .github/actions); that needs the owner's ok")
    checks = github.checks(repo["repo"], reviewed_sha)
    if checks["verdict"] == "pending":
        blockers.append("checks are still running")
    elif checks["verdict"] == "red":
        failed = [c["name"] for c in checks["checks"] if c["conclusion"] not in GREEN]
        blockers.append(f"checks failed: {', '.join(failed[:5])}")
    elif checks["verdict"] == "none" and not (repo["allow_no_ci"] or owner_ok):
        if github.has_workflows(repo["repo"]) and github.commit_age(repo["repo"], reviewed_sha) < NO_CHECKS_AFTER:
            blockers.append("no checks have started yet for this commit")
        else:
            blockers.append("no CI runs on this PR; merging without it needs the owner's ok")
    if pr.get("mergeable") is False:
        blockers.append("the PR has conflicts with its base branch")
    elif pr.get("mergeable") is None:
        blockers.append("GitHub is still computing whether the PR can be merged")
    return blockers


def merge(github: GitHub, team: dict, repo: dict, number: int, sha: str, *, promotion: bool = False) -> dict:
    """Merge with the reviewed SHA pinned. Promotions are always merge commits (develop's history must reach
    production as is); team PRs fall back between merge and squash only when the repository forbids the method."""
    methods = ["merge"] if promotion else [team["merge_method"]] + [m for m in ("merge", "squash") if m != team["merge_method"]]
    last = ""
    for method in methods:
        status, data, _ = github.request("PUT", f"{GITHUB_API}/repos/{repo['repo']}/pulls/{number}/merge",
                                         token=github.token, body={"sha": sha, "merge_method": method})
        if status == 200 and isinstance(data, dict) and data.get("merged"):
            return {"merged": True, "method": method, "sha": data.get("sha")}
        last = data.get("message") if isinstance(data, dict) else str(data)
        if status == 409:
            return {"merged": False, "reason": "the PR head changed; it will be reviewed again"}
        if status != 405 or "not allowed" not in str(last).lower():
            break
    return {"merged": False, "reason": last or "GitHub refused the merge"}


def delete_branch_if_safe(github: GitHub, team: dict, repo: dict, pr: dict) -> bool:
    head = pr["head"]["ref"]
    if (pr["head"].get("repo") or {}).get("full_name", "").lower() != repo["repo"].lower():
        return False
    if head in (repo["dev"], repo["prod"]) or branch_matches(head, team["long_lived"]):
        return False
    open_prs = github.pages(f"/repos/{repo['repo']}/pulls?state=open&base={urllib.parse.quote(head)}", limit=1)
    if open_prs:
        return False
    status, _, _ = github.request("DELETE", f"{GITHUB_API}/repos/{repo['repo']}/git/refs/heads/{urllib.parse.quote(head)}",
                                  token=github.token)
    return status == 204


def remember_reviewed(state: dict, repo: str, shas: list[str]) -> None:
    """Commits that went through a Ritmo review into dev: the only ones a promotion may carry."""
    known = state["reviewed_commits"].setdefault(repo.lower(), [])
    for sha in shas:
        if sha and sha not in known:
            known.append(sha)
    del known[:-5000]


def unreviewed_commits(github: GitHub, state: dict, repo: dict, team: dict) -> list[str]:
    """Team commits on dev that would reach production without having passed a Ritmo review.

    The rule is about the team going around the review: commits the owner merged by hand
    (theirs, a bot's) are the owner's call and pass. A commit GitHub cannot tie to any
    account is treated as the team's: unknown is never trusted.
    """
    compare = github.call("GET", f"/repos/{repo['repo']}/compare/{repo['prod']}...{repo['dev']}")
    known = set(state["reviewed_commits"].get(repo["repo"].lower(), []))
    unreviewed = []
    for commit in compare.get("commits", []):
        if commit["sha"] in known:
            continue
        logins = [(commit.get(role) or {}).get("login") for role in ("author", "committer")]
        if not any(logins) or any(login and person_for(team, login) for login in logins):
            unreviewed.append(commit["sha"])
    return unreviewed


def promotion_due(repo: dict, config: dict, state: dict) -> bool:
    """Whether a dev-to-prod promotion may start now, by the repo's policy and the owner's clock."""
    if repo["dev"] == repo["prod"] or repo["promote"] == "manual" or not config.get("timezone"):
        return False
    moment = now_utc()
    if in_quiet(moment, config):
        return False
    if repo["promote"] == "now":
        return True
    from zoneinfo import ZoneInfo
    local = moment.astimezone(ZoneInfo(config["timezone"]))
    target = local.replace(hour=int(repo["promote"][:2]), minute=int(repo["promote"][3:]), second=0, microsecond=0)
    done = state["promotions"].get(repo["repo"].lower())
    return local >= target and done != local.date().isoformat()
