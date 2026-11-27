#!/usr/bin/env python3
"""ghbroker: the only road from agent containers to GitHub.

Agents hold no GitHub token. Each agent holds a broker bearer token instead; the broker
checks it, applies a per-agent policy (repo scope, protected branches, merge-gate repos,
hourly budget, commit message policy) and only then adds the host's own GitHub token
and forwards the call.

  /gh/<REST path>                       -> GitHub REST API
  /git/OWNER/REPO.git/<smart-HTTP path> -> git over HTTP (info/refs, git-upload-pack,
                                           git-receive-pack)
  /healthz                              -> "ok" (no auth, no GitHub call); 503 while no policy is in force

Fails closed: unknown token, unknown repo, unparseable body, missing GitHub token -> refusal.
Paths are canonicalised before any decision (percent-escapes decoded, dot segments and
ambiguous encodings refused) and the canonical form, re-encoded, is what gets forwarded.
Limits (documented, not hidden): the commit message policy covers REST JSON bodies only, not
the contents of pushed commits; merges through the REST API on repos that are not merge-gate
repos land on the PR's base branch without the broker looking the base up (keep GitHub
branch protection on as the backstop).
Run `ghbroker.py --help` for flags. Configuration is by environment variable (see README).
"""
import argparse
import base64
import fnmatch
import hashlib
import hmac
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ.get
MAX_BODY = int(ENV("GHBROKER_MAX_BODY", str(256 * 1024 * 1024)))
FORWARD_RESPONSE_HEADERS = ("content-type", "content-length", "etag", "link", "cache-control",
                            "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                            "x-ratelimit-reset", "x-github-request-id")
# REST write paths that change repository administration, never delegated to an agent.
# Matched on canonical path segments after /repos/OWNER/REPO (a segment starts with the name,
# so "hooks" covers "hooks/1/tests" but not "contents/hooks.md").
ADMIN_FIRST = ("hooks", "keys", "collaborators", "invitations", "transfer", "rules", "rulesets",
               "environments", "secrets", "pages", "visibility", "merge-upstream", "autolinks",
               "deployment-protection-rules", "vulnerability-alerts", "automated-security-fixes",
               "private-vulnerability-reporting", "interaction-limits", "teams")
ADMIN_ACTIONS = ("permissions", "secrets", "variables", "runners", "oidc")  # after "actions"
ADMIN_SECRET_SCOPES = ("dependabot", "codespaces")  # "<scope>/secrets..."
# Writes the merge gate's inputs depend on (commit statuses, checks); label writes are separate.
# Refused on merge-gate repos when the name is ANY segment after /repos/OWNER/REPO, so the
# commits/<sha>/statuses alias of POST statuses/<sha> is refused too.
GATE_SEGMENTS = ("statuses", "check-runs", "check-suites")
POLICY_KEYS = {"protected_branches", "merge_gate_repos", "merge_label", "hold_label", "hourly_budget", "agents",
               "extra_message_patterns", "_comment"}
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Issue, pull request, comment, review and reaction numbers on write routes. GitHub reads only the leading
# digits of a number segment (pulls/7abc/merge is the merge of PR 7), so every rule that names a number
# would miss such a spelling; writes whose number segment is not exactly this are refused (400).
NUMBER_RE = re.compile(r"[1-9][0-9]*", re.ASCII)
# Segments after issues/ or pulls/ that are not a number: the repo-wide comment collections
# (issues/comments/<id>, pulls/comments/<id>).
NAMED_AFTER_NUMBERED = ("comments",)
# Sub-collections whose next segment, when present, is an id (pulls/N/reviews/<id>, .../reactions/<id>).
ID_COLLECTIONS = ("comments", "reactions", "reviews", "blocked_by", "replies")
# Run routes that approve a deployment or a fork's run (actions/runs/<id>/<name>); refused on every repo, since the
# broker's account may be a required reviewer of an environment anywhere.
RUN_APPROVALS = ("pending_deployments", "approve", "deployment_protection_rule")
# A route name on a write (pulls/7/merge, issues/7/labels, actions/runs/9/approve, labels, statuses ...): lower-case
# letters, "_" and "-" only, so a rule that names one cannot be dodged with a suffix such as merge.json.
ROUTE_NAME_RE = re.compile(r"[a-z][a-z_-]*", re.ASCII)
BRANCH_HEAD_RE = re.compile(r"[^\s:~^?*\[\\]+")  # a plain branch name: no space, colon or ref-forbidden characters
READ_ONLY_TOP = ("/rate_limit",)  # non-repo REST paths an agent may GET

# ---- commit message policy -------------------------------------------------------------
# No built-in patterns: the policy is the list of regexes in the policy file's extra_message_patterns
# (compiled case-insensitive and multi-line, the same flags as the merge gate). Empty by default.


def _strings(obj):
    """Every string value (and key) inside a decoded JSON document."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def compile_message_patterns(patterns):
    """Compile extra_message_patterns; raises ValueError on a non-list, a non-string or a bad regex."""
    if not isinstance(patterns, list):
        raise ValueError("extra_message_patterns must be a list of regex strings")
    out = []
    for i, p in enumerate(patterns):
        if not isinstance(p, str) or not p:
            raise ValueError("extra_message_patterns[%d] is not a non-empty string" % i)
        try:
            out.append(re.compile(p, re.I | re.M))
        except re.error as e:
            raise ValueError("extra_message_patterns[%d] is not a valid regex: %s" % (i, e))
    return out


def screen_message_policy(body: bytes, patterns=()):
    """Return a reason string if the body matches one of the compiled policy patterns, else None."""
    if not body or not patterns:
        return None
    text = body.decode("utf-8", "replace")
    try:
        chunks = list(_strings(json.loads(text)))
    except ValueError:
        chunks = [text]  # not JSON: screen the raw text
    for chunk in chunks + [text]:  # raw text too: markers hidden in keys or escapes
        for i, rx in enumerate(patterns):
            if rx.search(chunk):
                return "commit message policy: refused (pattern %d)" % i
    return None


# ---- tokens and policy --------------------------------------------------------------
def load_tokens(path):
    """File of '<sha256 hex>  <agent>' lines. Raw tokens never live in config."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            digest, agent = line.split()[:2]
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                out[digest] = agent
    return out


def lookup_agent(tokens, raw):
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    for known, agent in tokens.items():
        if hmac.compare_digest(known, digest):
            return agent
    return None


class Policy:
    def __init__(self, data):
        """Raises ValueError on a policy that would silently switch the merge-gate rules off: a missing
        or misspelled merge_gate_repos key, an unknown top-level key, or a merge_gate_repos entry that
        is not OWNER/REPO or names a repo no agent has. An empty list is allowed (no gate repos) but
        must be written out."""
        if not isinstance(data, dict):
            raise ValueError("policy must be a JSON object")
        unknown = sorted(set(data) - POLICY_KEYS)
        if unknown:
            raise ValueError("unknown policy key(s): %s" % ", ".join(unknown))
        if "merge_gate_repos" not in data or not isinstance(data["merge_gate_repos"], list):
            raise ValueError("merge_gate_repos must be present (a list; [] when no repo is merge-gated)")
        self.protected = list(data.get("protected_branches", [])) + ["main", "master"]
        agent_repos = {r.lower() for repos in data.get("agents", {}).values() for r in repos}
        for r in data["merge_gate_repos"]:
            if not isinstance(r, str) or not REPO_RE.fullmatch(r):
                raise ValueError("merge_gate_repos entry %r is not OWNER/REPO" % (r,))
            if r.lower() not in agent_repos:
                raise ValueError("merge_gate_repos entry %r matches no repo in agents (misspelled?)" % r)
        self.merge_gate_repos = {r.lower() for r in data["merge_gate_repos"]}
        self.merge_label = str(data.get("merge_label", "automerge")).strip().casefold()
        self.hold_label = str(data.get("hold_label", "hold")).strip().casefold()
        self.budget = data.get("hourly_budget", {})
        self.agents = {a: {r.lower(): m for r, m in repos.items()} for a, repos in data.get("agents", {}).items()}
        self.message_patterns = compile_message_patterns(data.get("extra_message_patterns", []))

    def is_protected(self, branch):
        branch = re.sub(r"^(refs/)?(heads/)?", "", branch or "")
        return any(fnmatch.fnmatchcase(branch, p) for p in self.protected)

    def access(self, agent, repo):
        return self.agents.get(agent, {}).get(repo.lower())

    def limit(self, agent):
        return int(self.budget.get(agent, self.budget.get("default", 600)))


def need(policy, agent, repo, mode):
    have = policy.access(agent, repo)
    if have is None:
        return 403, "agent %s has no access to %s" % (agent, repo)
    if mode == "write" and have != "write":
        return 403, "agent %s has read-only access to %s" % (agent, repo)
    return None


_PCT_FORBIDDEN = re.compile(r"%(2e|2f|5c|25)", re.I)
_WIRE_SAFE = "-._~!$&'()*+,=:@"


def canon_rest_path(raw):
    """Canonicalise a REST path (the part after /gh, no query string).

    Returns (canonical_path, None) or (None, reason). Decisions are taken on the canonical path
    and the canonical path is what is forwarded, so what the broker checks is what GitHub sees.
    Refused: encoded dots, slashes, backslashes and percent signs (any case, so nothing is
    decoded twice), malformed escapes, non-ASCII characters (they must be percent-encoded),
    invalid UTF-8, control characters and NUL, ';' parameters, '.' and '..' segments, empty
    segments, a literal '?' or '#'. One trailing slash is dropped.
    """
    if not isinstance(raw, str) or not raw.startswith("/"):
        return None, "path must start with /"
    if _PCT_FORBIDDEN.search(raw) or "\\" in raw:
        return None, "encoded dots, slashes, backslashes or percent signs are refused"
    if "?" in raw or "#" in raw:
        return None, "query or fragment markers are not part of the path"
    if not raw.isascii():
        return None, "non-ASCII characters in the path must be percent-encoded"
    segs = raw.split("/")[1:]
    if len(segs) > 1 and segs[-1] == "":
        segs.pop()  # one trailing slash is normalised away
    if segs == [""]:
        return "/", None
    out = []
    for seg in segs:
        passes = 0
        for _ in range(4):  # decode until stable (bounded)
            try:
                dec = urllib.parse.unquote_to_bytes(seg).decode("utf-8")
            except UnicodeDecodeError:
                return None, "path is not valid UTF-8"
            if dec == seg:
                break
            seg, passes = dec, passes + 1
        else:
            return None, "nested percent-escapes are refused"
        if passes > 1:
            return None, "nested percent-escapes are refused"
        if "%" in seg:
            return None, "malformed percent-escape"
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in seg):
            return None, "control characters in the path are refused"
        if ";" in seg:
            return None, "path parameters are refused"
        if "/" in seg or "\\" in seg:
            return None, "encoded slashes are refused"
        if seg in ("", ".", ".."):
            return None, "empty and dot segments are refused"
        out.append(seg)
    return "/" + "/".join(out), None


def wire_path(canonical):
    """Re-encode a canonical path segment by segment for forwarding to GitHub."""
    return "/" + "/".join(urllib.parse.quote(s, safe=_WIRE_SAFE) for s in canonical.split("/") if s)


def _touches_gate_label(policy, obj):
    names = {policy.merge_label, policy.hold_label}
    return any(s.strip().casefold() in names for s in _strings(obj))


def number_segments_reason(low):
    """Reason to refuse a write whose issue/PR/comment/review number is not a plain number, else None.

    low: the lower-cased segments after /repos/OWNER/REPO. issues/<n>..., pulls/<n>... and
    {issues,pulls}/comments/<id>... must use NUMBER_RE; so must the id after comments, reactions, reviews,
    blocked_by and replies further down (pulls/<n>/reviews/<id>/events, .../reactions/<id>)."""
    if not low or low[0] not in ("issues", "pulls") or len(low) < 2:
        return None
    bad = None
    if low[1] in NAMED_AFTER_NUMBERED:
        if len(low) > 2 and not NUMBER_RE.fullmatch(low[2]):
            bad = low[2]
    elif not NUMBER_RE.fullmatch(low[1]):
        bad = low[1]
    for i in range(2, len(low) - 1):
        if bad is None and low[i] in ID_COLLECTIONS and not NUMBER_RE.fullmatch(low[i + 1]):
            bad = low[i + 1]
    if bad is not None:
        return "%r is not a plain number (GitHub reads only its leading digits); write it as digits only" % bad
    return None


def route_names_reason(low):
    """Reason to refuse a write whose route name segment is not spelled exactly, else None.

    low: the lower-cased segments after /repos/OWNER/REPO. Checked: the first segment; the one after
    issues/<n> or pulls/<n> (or after {issues,pulls}/comments/<id>); the one after actions/; the one after
    actions/runs/<id>; the one after commits/<sha>. Free-form values (file paths, branch, tag and label names)
    sit at other positions and are not checked."""
    if not low:
        return None
    at = [0]
    if low[0] in ("issues", "pulls") and len(low) > 1:
        at.append(3 if low[1] in NAMED_AFTER_NUMBERED else 2)
    elif low[0] == "actions":
        at.append(1)
        if len(low) > 1 and low[1] == "runs":
            at.append(3)
    elif low[0] == "commits":
        at.append(2)
    for i in at:
        if i < len(low) and not ROUTE_NAME_RE.fullmatch(low[i]):
            return "%r is not an exact route name; write route names without suffixes" % low[i]
    return None


def decide_rest(policy, agent, method, path, body):
    """Return None to allow, or (status, reason) to refuse. path is the REST path after /gh."""
    canon, why = canon_rest_path(path.split("?")[0])
    if canon is None:
        return 400, why
    parts = [p for p in canon.split("/") if p]
    write = method not in ("GET", "HEAD")
    if parts[:1] == ["graphql"]:
        return 403, "graphql is not proxied (cannot be scoped to a repo)"
    if parts[:1] != ["repos"] or len(parts) < 3:
        if not write and canon in READ_ONLY_TOP:
            return None
        return 403, "only /repos/OWNER/REPO/... and /rate_limit are proxied"
    repo = parts[1] + "/" + parts[2]
    refuse = need(policy, agent, repo, "write" if write else "read")
    if refuse or not write:
        return refuse
    sub = parts[3:]
    low = [p.lower() for p in sub]
    gate = repo.lower() in policy.merge_gate_repos
    if not low:
        return 403, "repository settings and deletion are not allowed"
    first, second = low[0], (low[1] if len(low) > 1 else "")
    if (first.startswith(ADMIN_FIRST)
            or (first == "actions" and second.startswith(ADMIN_ACTIONS))
            or (first in ADMIN_SECRET_SCOPES and second.startswith("secrets"))
            or (first == "branches" and "protection" in low[1:])):
        return 403, "repository administration is not allowed"
    bad_number = number_segments_reason(low)
    if bad_number:
        return 400, bad_number
    bad_name = route_names_reason(low)
    if bad_name:
        return 400, bad_name
    if first == "actions" and second == "runs" and len(low) >= 4 and low[3] in RUN_APPROVALS:
        # The broker's account may be an environment's required reviewer; an agent must not approve its own deploy.
        return 403, "deployment and run approvals are refused"
    if first == "pulls" and len(low) >= 3 and low[2] == "update-branch":
        # GitHub merges the PR's base into its head branch; with a protected head that is a write to a
        # protected branch the broker cannot see. Agents merge the base locally and push instead.
        return 403, "update-branch is refused; merge the base locally and push"
    if gate:
        if any(p in GATE_SEGMENTS for p in low):
            return 403, "%s is owned by the merge gate; status and check writes are refused" % repo
        if first == "labels" and len(low) > 1:
            return 403, "%s is owned by the merge gate; label definitions cannot be changed" % repo
        if first == "actions" and (method == "DELETE" or (second == "workflows" and low[-1] in ("disable", "enable"))):
            return 403, "%s is owned by the merge gate; deleting runs or switching workflows is refused" % repo
    reason = screen_message_policy(body, policy.message_patterns)
    if reason:
        return 403, reason
    try:
        raw_doc = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        return 400, "write body is not valid JSON"
    doc = raw_doc if isinstance(raw_doc, dict) else {}
    if gate:
        owned = "%s is owned by the merge gate; " % repo
        if first == "pulls" and len(low) >= 3 and low[2] == "merge" and method == "PUT":
            return 403, owned + "direct merges are refused"
        if first == "pulls" and len(low) == 2 and method == "PATCH" and any(str(k).lower() == "base" for k in doc):
            return 403, owned + "a pull request's base cannot be changed"
        numbered = len(low) >= 2 and first in ("issues", "pulls") and NUMBER_RE.fullmatch(low[1]) is not None
        if numbered and len(low) == 2 and any(str(k).lower() == "labels" for k in doc):
            return 403, owned + "labels cannot be set through the issue or pull request body"
        if first == "issues" and numbered and len(low) >= 3 and low[2] == "labels":
            if method == "PUT" or (method == "DELETE" and len(low) == 3):
                return 403, owned + "labels cannot be replaced or cleared"
            if method == "DELETE" and sub[3].strip().casefold() in (policy.merge_label, policy.hold_label):
                return 403, owned + "merge and hold labels are not writable"
            if method == "POST" and _touches_gate_label(policy, raw_doc):
                return 403, owned + "merge and hold labels are not writable"
        if first == "issues" and len(low) == 1 and method == "POST" and _touches_gate_label(policy, doc.get("labels")):
            return 403, owned + "merge and hold labels are not writable"
    targets = []
    if low == ["pulls"] and method == "POST":
        head = doc.get("head", "")
        # "owner:branch" names a branch in another fork; the protected check applies to the branch name,
        # which must be a plain one (no space, no second colon) so the check reads what GitHub reads.
        branch = head.split(":", 1)[-1] if isinstance(head, str) else head
        if isinstance(branch, str) and not BRANCH_HEAD_RE.fullmatch(branch):
            return 400, "head must be BRANCH or OWNER:BRANCH with a plain branch name"
        targets.append(branch)
    if low == ["git", "refs"] and method == "POST":
        targets.append(doc.get("ref", ""))
    elif first == "git" and second == "refs" and len(low) > 2:
        targets.append("/".join(sub[2:]))
    elif low == ["merges"]:
        targets.append(doc.get("base", ""))
    elif first == "branches" and len(low) > 1:
        names = sub[1:-1] if len(low) > 2 and low[-1] == "rename" else sub[1:]
        targets.append("/".join(names))
    elif first == "contents" and len(low) > 1 and method in ("PUT", "DELETE"):
        if not doc.get("branch"):
            return 403, "contents writes must name a non-protected branch"
        targets.append(doc["branch"])
    for t in targets:
        if not isinstance(t, str) or policy.is_protected(t):
            return 403, "write to protected branch refused (%s)" % t
    return None


def parse_push_refs(body):
    """Ref names from the command section of a git-receive-pack request (pkt-lines)."""
    refs, pos = [], 0
    while True:
        try:
            n = int(body[pos:pos + 4], 16)
        except ValueError:
            raise ValueError("malformed pkt-line")
        if n == 0:
            return refs
        if n < 4 or pos + n > len(body):
            raise ValueError("truncated pkt-line")
        line = body[pos + 4:pos + n].split(b"\0")[0].strip()
        pos += n
        if line.startswith(b"push-cert"):
            raise ValueError("signed pushes are not supported")
        if line.startswith(b"shallow "):
            continue
        fields = line.split(b" ")
        if len(fields) != 3:
            raise ValueError("bad command line")
        refs.append(fields[2].decode("utf-8", "replace"))


def decide_git(policy, agent, method, repo, sub, query, body):
    service = urllib.parse.parse_qs(query).get("service", [""])[0]
    if sub == "info/refs":
        if method != "GET" or service not in ("git-upload-pack", "git-receive-pack"):
            return 400, "unsupported info/refs request"
        return need(policy, agent, repo, "write" if service == "git-receive-pack" else "read")
    if method != "POST":
        return 405, "POST required"
    if sub == "git-upload-pack":
        return need(policy, agent, repo, "read")
    refuse = need(policy, agent, repo, "write")
    if refuse:
        return refuse
    try:
        refs = parse_push_refs(body)
    except ValueError as exc:
        return 400, "push refused: %s" % exc
    for ref in refs:
        if ref.startswith("refs/heads/") and policy.is_protected(ref):
            return 403, "push to protected branch refused (%s)" % ref
    # Out of scope: no pre-receive commit message check on pushed commits; do that in a
    # GitHub-side check or hook. Only REST writes are screened here.
    return None


# ---- budget -------------------------------------------------------------------------
class Budget:
    def __init__(self):
        self.hits, self.lock = {}, threading.Lock()

    def take(self, agent, limit, now=None):
        """Return 0 if the call fits the hourly budget, else seconds to wait."""
        now = time.time() if now is None else now
        with self.lock:
            q = self.hits.setdefault(agent, deque())
            while q and q[0] <= now - 3600:
                q.popleft()
            if len(q) >= limit:
                return max(1, int(q[0] + 3600 - now) + 1)
            q.append(now)
            return 0


# ---- HTTP handler -------------------------------------------------------------------
GIT_RE = re.compile(r"^/git/([\w.-]+)/([\w.-]+?)\.git/(info/refs|git-upload-pack|git-receive-pack)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ghbroker"

    def log_message(self, *a):  # replaced by our own one-line log (no bodies, no tokens, no query)
        pass

    def reply(self, status, obj, extra=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.status = status

    def bearer(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if auth.lower().startswith("basic "):  # git: token as the password (or user name)
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                return pw or user
            except ValueError:
                return ""
        return ""

    def handle_any(self):
        self.status, agent = 500, "-"
        srv = self.server
        try:
            path = urllib.parse.urlsplit(self.path)
            if path.path == "/healthz":
                if srv.policy_now() is None:
                    return self.reply(503, {"ok": False, "error": "no policy in force"})
                return self.reply(200, {"ok": True})
            if os.path.exists(srv.cfg["disable"]):
                return self.reply(503, {"error": "ghbroker is switched off"})
            agent = lookup_agent(srv.tokens, self.bearer()) or "-"
            if agent == "-":
                return self.reply(401, {"error": "unknown token"}, {"WWW-Authenticate": "Bearer"})
            policy = srv.policy_now()
            if policy is None:
                return self.reply(503, {"error": "ghbroker policy does not load; every call is refused until it is fixed"})
            wait = srv.budget.take(agent, policy.limit(agent))
            if wait:
                return self.reply(429, {"error": "hourly budget used up"}, {"Retry-After": str(wait)})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.reply(413, {"error": "body too large"})
            body = self.rfile.read(length) if length else b""
            m = GIT_RE.match(path.path)
            if m and ".." in (m.group(1), m.group(2)):
                m = None
            if m:
                repo = m.group(1) + "/" + m.group(2)
                refuse = decide_git(policy, agent, self.command, repo, m.group(3), path.query, body)
                upstream = srv.cfg["git_base"] + "/%s.git/%s" % (repo, m.group(3))
                kind = "git"
            elif path.path.startswith("/gh/") or path.path == "/gh":
                rest = path.path[3:] or "/"
                refuse = decide_rest(policy, agent, self.command, rest, body)
                canon = canon_rest_path(rest)[0]
                if canon is None and not refuse:
                    refuse = (400, "path refused")
                upstream = srv.cfg["api_base"] + (wire_path(canon) if canon else "")
                kind = "rest"
            else:
                return self.reply(404, {"error": "unknown route"})
            if path.query and self.command not in ("GET", "HEAD") and not refuse:
                # Decisions read the path and body only; a query on a write could carry parameters
                # GitHub reads (labels, branch, ...), so it is refused rather than forwarded unread.
                refuse = (400, "query strings are refused on write requests")
            if refuse:
                return self.reply(refuse[0], {"error": "refused", "reason": refuse[1]})
            if path.query:
                upstream += "?" + path.query
            if srv.cfg["dry_run"]:
                return self.reply(200, {"dry_run": True, "agent": agent, "would_call": self.command + " " + upstream})
            self.forward(upstream, body, kind)
        except Exception as exc:  # fail closed, never leak internals
            if self.status == 500:
                self.reply(502, {"error": "broker error", "type": type(exc).__name__})
        finally:
            # one line per call: agent, method, path (no query), status
            print("%s agent=%s %s %s -> %s" % (time.strftime("%FT%T"), agent, self.command, self.path.split("?")[0][:200], self.status), flush=True)

    def forward(self, upstream, body, kind):
        srv = self.server
        token = srv.github_token()
        if not token:
            return self.reply(503, {"error": "broker has no GitHub token configured"})
        u = urllib.parse.urlsplit(upstream)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.netloc, timeout=120)
        hdrs = {"User-Agent": "ghbroker", "Accept": self.headers.get("Accept", "*/*")}
        for h in ("Content-Type", "Content-Encoding", "Git-Protocol", "X-GitHub-Api-Version"):
            if self.headers.get(h):
                hdrs[h] = self.headers[h]
        if kind == "git":
            hdrs["Authorization"] = "Basic " + base64.b64encode(b"x-access-token:" + token.encode()).decode()
        else:
            hdrs["Authorization"] = "Bearer " + token
        conn.request(self.command, u.path + ("?" + u.query if u.query else ""), body=body or None, headers=hdrs)
        resp = conn.getresponse()
        self.status = resp.status
        self.send_response(resp.status)
        host = self.headers.get("Host", "")
        for k, v in resp.getheaders():
            if k.lower() in FORWARD_RESPONSE_HEADERS:
                if k.lower() == "link" and host:  # keep pagination inside the broker
                    v = v.replace(srv.cfg["api_base"], "http://%s/gh" % host)
                self.send_header(k, v)
        chunked = resp.getheader("Content-Length") is None
        if chunked:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                self.wfile.write(buf)
        conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any


class Broker(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg):
        self.cfg, self.budget, self._mtime, self._policy, self._error = cfg, Budget(), None, None, None
        self.tokens = load_tokens(cfg["tokens"])
        self.policy_now()  # a broken or unsafe policy stops the broker at start, before it listens
        super().__init__(addr, Handler)

    def policy_now(self):
        """The policy in force, or None. The file is re-read when it changes. A reload that fails (file
        gone or unreadable, bad JSON, or a policy Policy() refuses) leaves NO policy in force: the old one
        may be looser than the edit (an agent's access just removed), so every call is refused (503) until
        a valid file loads. The full error is logged once per distinct error. At start a failure raises."""
        try:
            m = os.stat(self.cfg["policy"]).st_mtime_ns
            if m != self._mtime or self._policy is None:
                with open(self.cfg["policy"], encoding="utf-8") as fh:
                    pol = Policy(json.load(fh))
                self._policy, self._mtime, self._error = pol, m, None
        except Exception as exc:  # noqa: BLE001 - any failure to load means no policy in force
            if self._mtime is None:
                raise
            error = "%s: %s" % (type(exc).__name__, exc)
            if error != self._error:
                print("policy reload failed (%s); refusing every call until a valid policy loads" % error, flush=True)
            self._policy, self._error = None, error
        return self._policy

    def github_token(self):
        try:
            with open(self.cfg["token_file"], encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""


def gate_config_mismatch(policy, gate_cfg):
    """Differences between the broker policy and the merge gate's config that would leave a gate repo unguarded."""
    out = []
    repos = {(e if isinstance(e, str) else e.get("repo", "")).lower() for e in gate_cfg.get("repos", [])}
    for r in sorted(repos - policy.merge_gate_repos):
        out.append("gate merges %s but the broker policy does not list it in merge_gate_repos" % r)
    for key, mine in (("merge_label", policy.merge_label), ("hold_label", policy.hold_label)):
        theirs = str(gate_cfg.get(key, "automerge" if key == "merge_label" else "hold")).strip().casefold()
        if theirs != mine:
            out.append("%s differs: broker %r, gate %r" % (key, mine, theirs))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Token-scoped GitHub proxy for agent containers.")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, but never call GitHub")
    ap.add_argument("--check", action="store_true", help="load tokens and policy, print a summary, exit")
    ap.add_argument("--gate-config", metavar="PATH", help="with --check: the merge gate's config.json; its repos "
                    "and labels must match this policy's merge_gate_repos and labels")
    args = ap.parse_args(argv)
    cfg = {
        "bind": ENV("GHBROKER_BIND", "127.0.0.1"),
        "port": int(ENV("GHBROKER_PORT", "8790")),
        "tokens": ENV("GHBROKER_TOKENS", "/etc/homeserver/ghbroker/tokens"),
        "policy": ENV("GHBROKER_POLICY", "/etc/homeserver/ghbroker/policy.json"),
        "token_file": ENV("GHBROKER_TOKEN_FILE", "/etc/homeserver/ghbroker/github-token"),
        "disable": ENV("GHBROKER_DISABLE", "/etc/homeserver/ghbroker.disable"),
        "api_base": ENV("GHBROKER_API_BASE", "https://api.github.com").rstrip("/"),
        "git_base": ENV("GHBROKER_GIT_BASE", "https://github.com").rstrip("/"),
        "dry_run": args.dry_run,
    }
    srv = Broker((cfg["bind"], cfg["port"]), cfg) if not args.check else None
    if args.check:
        tokens = load_tokens(cfg["tokens"])
        pol = Policy(json.load(open(cfg["policy"], encoding="utf-8")))
        print("tokens: %d, agents in policy: %d, merge-gate repos: %d" % (len(tokens), len(pol.agents), len(pol.merge_gate_repos)))
        if args.gate_config:
            problems = gate_config_mismatch(pol, json.load(open(args.gate_config, encoding="utf-8")))
            for p in problems:
                print("MISMATCH: " + p)
            return 1 if problems else 0
        return 0
    srv.policy_now()  # fail at start, not on the first call
    print("ghbroker listening on %s:%d%s" % (cfg["bind"], cfg["port"], " (dry-run)" if args.dry_run else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
