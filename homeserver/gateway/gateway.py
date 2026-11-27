#!/usr/bin/env python3
"""gateway: deterministic egress gateway for agent containers.

Agents hold no device, account or third-party API credentials. Each agent holds one gateway
bearer token. It calls `POST /act/<action>` with JSON params; the gateway checks

  token -> agent -> ACL (agent may call this action) -> param validation (regex per param)
  -> for kind=write: a plan this gateway raised, approved by a person, single use
     -> hourly cap

then builds the upstream request from the action template, injects the credential from a
root-only file and forwards it. Responses are size-capped. Redirects are never followed
(they could carry the credential elsewhere).

  POST /plan/<action>  body: {"params": {...}}. Validates ACL and params; for a write action the
                       GATEWAY raises the operator-queue ask itself and returns {"plan_id": ...}.
  POST /act/<action>   body: {"params": {...}, "plan_id": "..."}  (plan_id: writes only)
                       (a flat object {"lat": "1", ...} is also accepted when it has no "params" key)
  GET  /healthz        "ok", no auth

Approval. The person approving reads text the gateway wrote, never text the agent wrote: the ask's
title and question come from the action's `describe` line and the exact validated params, and the
params are also stored in the ask's structured context. The gateway records, in its own plans file,
the plan id, agent, action, sha256 of the canonical action+params, the queue item id it got back and
the exact title and question. On the write call it re-reads that item and requires: same agent, same
item id and request id, title and question still equal to what it wrote, status responded or
acknowledged with an approving answer, plan not expired, and a plan hash equal to the call's action
and params. The plan id is burned (used.json, fsynced) before the upstream call, so a failed write
still needs a new plan. Ids are normalised to one canonical string before any lookup or ledger check.

Fails closed: Trinity unreachable, ledger unwritable, credential file missing -> refusal.
Kill switch: the file GATEWAY_DISABLE refuses every write; reads keep working.
Logs one line per call: agent, action, decision, status. Never tokens or credentials, and no
param values for actions marked "sensitive". Secret params (a param with "secret": true, or every
param of a "sensitive" action that marks none) never leave the gateway: the ask shows them as
"(secret, not shown)" in the title, question and context, and the ask carries no hash of the params
(a short code would be recovered from it by trying every value). The plan record binds the real values.
Flags: --dry-run, --check, --hash (see --help).
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ.get
MAX_REQUEST = int(ENV("GATEWAY_MAX_REQUEST", str(64 * 1024)))
MAX_RESPONSE = int(ENV("GATEWAY_MAX_RESPONSE", str(1024 * 1024)))
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
TITLE_MAX = 120  # the platform caps an ask title at 120 characters
QUESTION_MAX = 3500
APPROVING = {v.strip().lower() for v in ENV("GATEWAY_APPROVE_VALUES", "approve,approved,yes,ok").split(",")}
TEMPLATE_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class Refuse(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


# ---- config -----------------------------------------------------------------------
def load_tokens(path):
    """File of '<sha256 hex>  <agent>' lines (same format as the GitHub broker)."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                digest, agent = line.split()[:2]
                if re.fullmatch(r"[0-9a-f]{64}", digest):
                    out[digest] = agent
    return out


def lookup_agent(tokens, raw):
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    found = None
    for known, agent in tokens.items():  # no early exit: constant work per call
        if hmac.compare_digest(known, digest):
            found = agent
    return found


def load_actions(path):
    """Read and validate the action catalogue; raises ValueError on any malformed entry."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    actions = {}
    for name, a in raw.items():
        if name.startswith("_"):
            continue  # comment keys
        if not NAME_RE.fullmatch(name):
            raise ValueError("bad action name %r" % name)
        if a.get("kind") not in ("read", "write"):
            raise ValueError("%s: kind must be read or write" % name)
        if a.get("method", "").upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("%s: unsupported method" % name)
        if a["kind"] == "read" and a["method"].upper() != "GET":
            raise ValueError("%s: a read action must use GET" % name)
        if not re.match(r"https?://", a.get("url", "")):
            raise ValueError("%s: url must be http(s)" % name)
        cred = a.get("credential")
        if cred is not None and not NAME_RE.fullmatch(str(cred.get("ref", ""))):
            raise ValueError("%s: credential.ref must be a plain file name" % name)
        params = a.get("params", {})
        for p, spec in params.items():
            re.compile(spec["pattern"])
            if not isinstance(spec.get("secret", False), bool):
                raise ValueError("%s: param %s: secret must be true or false" % (name, p))
        for p in TEMPLATE_RE.findall(a["url"]):
            if p not in params:
                raise ValueError("%s: url uses {%s} which is not an allowed param" % (name, p))
        describe = a.get("describe", "")
        if not isinstance(describe, str):
            raise ValueError("%s: describe must be a string" % name)
        for p in TEMPLATE_RE.findall(describe):
            if p not in params:
                raise ValueError("%s: describe uses {%s} which is not an allowed param" % (name, p))
        a["method"] = a["method"].upper()
        actions[name] = a
    return actions


class RateWindow:
    """Sliding one-hour window per (agent, action)."""

    def __init__(self):
        self.hits, self.lock = {}, threading.Lock()

    def take(self, key, limit):
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] < now - 3600:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


# ---- request logic ----------------------------------------------------------------
def plan_hash(action, params):
    """sha256 of the canonical JSON of the action name and its validated params (values as strings)."""
    canon = json.dumps({"action": action, "params": {k: str(v) for k, v in params.items()}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def canon_id(value):
    """One canonical string per id, or None. '12', 12, ' 12 ' and '012' all become '12', so one
    approval cannot be spent twice through different spellings. Booleans, floats and anything
    else that is not a string or an int are not ids."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip().lower()
    if text.isascii() and text.isdigit():
        text = text.lstrip("0") or "0"
    return text if ID_RE.fullmatch(text) else None


def _plain(value):
    """Text that cannot hide anything: JSON string escaping, quotes dropped."""
    return json.dumps(str(value))[1:-1]


SECRET_SHOWN = "(secret, not shown)"


def secret_params(action):
    """Names of the params whose values must not reach Trinity, its database or any channel an ask goes to:
    those marked "secret": true; for a "sensitive" action that marks none, every param."""
    spec = action.get("params", {})
    marked = {p for p, s in spec.items() if s.get("secret")}
    return marked or (set(spec) if action.get("sensitive") else set())


def shown_params(action, params):
    """The params as a person may see them: secret values replaced."""
    hidden = secret_params(action)
    return {k: (SECRET_SHOWN if k in hidden else v) for k, v in params.items()}


def render_plan(agent, name, action, params):
    """The title and question a person reads, written from the catalogue and the validated params only.
    Secret params are shown as SECRET_SHOWN, never their values."""
    params = shown_params(action, params)
    what = TEMPLATE_RE.sub(lambda m: _plain(params.get(m.group(1), "")), action.get("describe") or "Run " + name)
    title = ("Gateway: %s" % what)
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 3] + "..."
    lines = ["Agent %s asks the gateway to run the write action %s." % (agent, name),
             "What it does: %s" % what,
             "Exact parameters (only these will be sent; text is JSON-escaped):"]
    lines += ["  %s = %s" % (k, json.dumps(str(params[k]))) for k in sorted(params)] or ["  (none)"]
    lines.append("Approving allows this one call, once. Choose Deny if anything above is not what you want.")
    return title, "\n".join(lines)


def validate_params(action, given):
    spec = action.get("params", {})
    if not isinstance(given, dict):
        raise Refuse(400, "params must be a JSON object")
    out = {}
    for key, val in given.items():
        if key not in spec:
            raise Refuse(400, "param %r is not allowed for this action" % key)
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise Refuse(400, "param %r must be a string or number" % key)
        if not re.fullmatch(spec[key]["pattern"], str(val)):
            raise Refuse(400, "param %r does not match its pattern" % key)
        out[key] = str(val)
    for key, s in spec.items():
        if key not in out and not s.get("optional"):
            raise Refuse(400, "param %r is required" % key)
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # a 3xx comes back to the agent as-is; the credential never follows it


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.setdefault("plans", os.path.join(os.path.dirname(cfg["ledger"]), "plans.json"))
        cfg.setdefault("plan_ttl", 86400)
        cfg.setdefault("plan_max_per_hour", 20)
        self.tokens = load_tokens(cfg["tokens"])
        self.actions = load_actions(cfg["actions"])
        with open(cfg["acl"], encoding="utf-8") as fh:
            self.acl = json.load(fh)
        self.rate = RateWindow()
        self.ledger_lock = threading.Lock()
        self.plan_lock = threading.Lock()
        self.opener = urllib.request.build_opener(NoRedirect)

    # -- Trinity and upstream; tests replace these three --
    def _trinity_token(self):
        with open(self.cfg["trinity_token_file"], encoding="utf-8") as fh:
            return fh.read().strip()

    def trinity_get(self, item_id):
        """GET /api/operator-queue/{item_id}: routers/operator_queue.py:388-389 in the Trinity source."""
        req = urllib.request.Request("%s/api/operator-queue/%s" % (self.cfg["trinity_url"], urllib.parse.quote(str(item_id), safe="")),
                                     headers={"Authorization": "Bearer " + self._trinity_token()})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read(MAX_RESPONSE))

    def trinity_raise(self, agent, ask):
        """POST /api/agents/{agent}/operator-queue: routers/operator_queue.py:662-702 (raise_my_ask).
        The token is an admin's user-scoped key; the X-Trinity-Act-As-Agent header makes the ask the
        calling agent's own (dependencies.py:828, :841). Returns the receipt (status, id, request_id)."""
        req = urllib.request.Request(
            "%s/api/agents/%s/operator-queue" % (self.cfg["trinity_url"], urllib.parse.quote(agent, safe="")),
            data=json.dumps(ask).encode(), method="POST",
            headers={"Authorization": "Bearer " + self._trinity_token(), "Content-Type": "application/json",
                     "X-Trinity-Act-As-Agent": agent})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read(MAX_RESPONSE))

    def send(self, method, url, headers, body):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            resp = self.opener.open(req, timeout=self.cfg["timeout"])
        except urllib.error.HTTPError as e:  # non-2xx is a normal answer, pass it on
            resp = e
        data = resp.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise Refuse(502, "upstream response larger than %d bytes" % MAX_RESPONSE)
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers.get("Content-Type", "application/octet-stream"), data

    # -- state files (plans the gateway raised; ids already spent) --
    def _read_json(self, path, what):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise Refuse(503, "%s unavailable; writes fail closed" % what)
        if not isinstance(data, dict):
            raise Refuse(503, "%s unavailable; writes fail closed" % what)
        return data

    def _write_json(self, path, obj, what):
        tmp = path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except (OSError, ValueError):
            raise Refuse(503, "%s unavailable; writes fail closed" % what)

    def new_plan_id(self):
        return "gwp-" + secrets.token_hex(16)

    # -- plan: the gateway raises the ask, in its own words --
    def make_plan(self, agent, name, action, params):
        if not self.rate.take((agent, "plan"), self.cfg["plan_max_per_hour"]):
            raise Refuse(429, "too many plans this hour")
        title, question = render_plan(agent, name, action, params)
        if len(question) > QUESTION_MAX:
            raise Refuse(413, "the rendered request is too long for a person to read in full; shorten the params")
        digest = plan_hash(name, params)
        plan_id = self.new_plan_id()
        if canon_id(plan_id) is None:
            raise Refuse(503, "cannot make a plan id")
        ask = {"request_id": plan_id, "type": "approval", "title": title, "question": question,
               "options": ["Approve", "Deny"],
               "context": {"gateway": {"plan_id": plan_id, "agent": agent, "action": name,
                                       "params": shown_params(action, params)}}}
        try:
            receipt = self.trinity_raise(agent, ask)
        except Exception as exc:
            raise Refuse(503, "cannot raise the approval request (%s); writes fail closed" % type(exc).__name__)
        item_id = receipt.get("id") if isinstance(receipt, dict) else None
        if not isinstance(receipt, dict) or receipt.get("status") != "created" or receipt.get("request_id") != plan_id \
                or isinstance(item_id, bool) or not isinstance(item_id, (str, int)) or canon_id(item_id) is None:
            raise Refuse(503, "unexpected answer when raising the approval request; writes fail closed")
        now = int(time.time())
        with self.plan_lock:
            plans = self._read_json(self.cfg["plans"], "plans file")
            plans = {k: v for k, v in plans.items() if isinstance(v, dict) and now - int(v.get("at", 0)) < self.cfg["plan_ttl"]}
            plans[canon_id(plan_id)] = {"agent": agent, "action": name, "hash": digest, "item_id": str(item_id),
                                        "title": title, "question": question, "at": now}
            self._write_json(self.cfg["plans"], plans, "plans file")
        return plan_id, str(item_id), title, question

    def load_plan(self, agent, name, params, plan_id):
        """The plan record this gateway wrote, if it is this agent's, for this action and these params."""
        cid = canon_id(plan_id)
        if cid is None:
            raise Refuse(403, "write actions need a plan_id from POST /plan/<action>")
        with self.plan_lock:
            plans = self._read_json(self.cfg["plans"], "plans file")
        rec = plans.get(cid)
        if not isinstance(rec, dict) or rec.get("agent") != agent:
            raise Refuse(403, "unknown plan")
        if rec.get("action") != name or rec.get("hash") != plan_hash(name, params):
            raise Refuse(403, "the plan was made for a different action or different params")
        if int(time.time()) - int(rec.get("at", 0)) >= self.cfg["plan_ttl"]:
            raise Refuse(403, "plan expired; make a new one")
        return cid, rec

    # -- approval --
    def check_approval(self, agent, name, params, plan_id):
        cid, rec = self.load_plan(agent, name, params, plan_id)
        if self.is_used(cid, rec.get("item_id")):
            raise Refuse(403, "approval already used")
        try:
            item = self.trinity_get(rec["item_id"])
        except Exception as exc:  # unreachable, 4xx, bad JSON: all the same answer
            raise Refuse(503, "cannot verify approval (%s); writes fail closed" % type(exc).__name__)
        if not isinstance(item, dict) or item.get("agent_name") != agent:
            raise Refuse(403, "approval does not belong to this agent")
        if canon_id(item.get("id")) != canon_id(rec["item_id"]) or canon_id(item.get("request_id")) != cid:
            raise Refuse(403, "approval is not the request this gateway raised for this plan")
        if str(item.get("title") or "").strip() != rec["title"].strip() or str(item.get("question") or "").strip() != rec["question"].strip():
            raise Refuse(403, "approval text differs from what this gateway wrote")
        if str(item.get("status", "")).lower() not in ("responded", "acknowledged") or \
                str(item.get("response", "")).strip().lower() not in APPROVING:
            raise Refuse(403, "approval is not approved")
        return cid, rec

    def burn(self, cid, item_id, agent, name):
        """Record the plan and its queue item as used; refuse if either was used or the ledger cannot be written."""
        with self.ledger_lock:
            used = self.read_ledger()
            keys = [cid, "item:" + (canon_id(item_id) or "")]
            if any(k in used for k in keys):
                raise Refuse(403, "approval already used")
            for k in keys:
                used[k] = {"agent": agent, "action": name, "at": int(time.time())}
            self._write_json(self.cfg["ledger"], used, "used-approval ledger")

    def read_ledger(self):
        return self._read_json(self.cfg["ledger"], "used-approval ledger")

    def is_used(self, cid, item_id=None):
        used = self.read_ledger()
        return cid in used or ("item:" + (canon_id(item_id) or "")) in used

    def used(self, plan_id):
        return self.is_used(canon_id(plan_id) or "")

    # -- the one entry point --
    def process(self, route, token, name, raw_body):
        """Return (status, content_type, bytes, log_info). log_info: dict(agent, action, decision, detail)."""
        info = {"agent": "-", "action": name, "decision": "refuse", "detail": ""}
        try:
            agent = lookup_agent(self.tokens, token)
            if not agent:
                raise Refuse(401, "unknown token")
            info["agent"] = agent
            action = self.actions.get(name)
            if action is None or name not in self.acl.get(agent, []):
                raise Refuse(403, "agent %s may not call action %r" % (agent, name))  # same text for unknown and denied
            is_write = action["kind"] == "write"
            if len(raw_body) > MAX_REQUEST:
                raise Refuse(413, "request too large")
            try:
                body = json.loads(raw_body or b"{}")
            except ValueError:
                raise Refuse(400, "body must be JSON")
            if not isinstance(body, dict):
                raise Refuse(400, "body must be a JSON object")
            plan_id = body.get("plan_id")
            params = validate_params(action, body["params"] if "params" in body else
                                     {k: v for k, v in body.items() if k not in ("plan_id", "approval_id")})
            shown = "[sensitive]" if action.get("sensitive") else json.dumps(shown_params(action, params))[:300]
            info["detail"] = "params=%s" % shown
            if route == "plan":
                info["decision"] = "plan"
                out = {"action": name, "kind": action["kind"], "needs_approval": is_write, "plan_sha256": plan_hash(name, params)}
                if is_write:
                    if os.path.exists(self.cfg["disable"]):
                        raise Refuse(503, "writes are switched off (kill switch)")
                    if self.cfg["dry_run"]:
                        out["dry_run"] = True  # nothing is raised in dry-run
                    else:
                        pid, item_id, title, question = self.make_plan(agent, name, action, params)
                        out.update({"plan_id": pid, "queue_item_id": item_id, "title": title, "question": question,
                                    "hint": "a person must approve the queue item; then call POST /act/%s with this plan_id" % name})
                return 200, "application/json", json.dumps(out).encode(), info
            if is_write and os.path.exists(self.cfg["disable"]):
                raise Refuse(503, "writes are switched off (kill switch)")
            limit = action.get("max_per_hour")
            if is_write:
                if plan_id is None and body.get("approval_id") is not None:
                    raise Refuse(403, "approval_id is no longer accepted: call POST /plan/<action> and use its plan_id")
                cid, rec = self.check_approval(agent, name, params, plan_id)
            if limit and not self.rate.take((agent, name), int(limit)):
                raise Refuse(429, "hourly cap for this action reached")
            url, headers, payload = self.build(action, params)
            if self.cfg["dry_run"]:
                info["decision"] = "dry-run"
                out = {"dry_run": True, "would_call": "%s %s" % (action["method"], "[sensitive]" if action.get("sensitive") or secret_params(action) else url),
                       "headers": sorted(headers), "body_bytes": len(payload or b"")}
                return 200, "application/json", json.dumps(out).encode(), info
            if is_write:
                self.burn(cid, rec["item_id"], agent, name)  # before sending: single use even if upstream fails
            info["decision"] = "allow"
            status, ctype, data = self.send(action["method"], url, headers, payload)
            return status, ctype, data, info
        except Refuse as r:
            return r.status, "application/json", json.dumps({"error": "refused", "reason": r.reason}).encode(), info
        except Exception as exc:  # fail closed, never leak internals
            info["decision"] = "error"
            return 502, "application/json", json.dumps({"error": "gateway error", "type": type(exc).__name__}).encode(), info

    def build(self, action, params):
        quoted = {k: urllib.parse.quote(v, safe="") for k, v in params.items()}
        url = TEMPLATE_RE.sub(lambda m: quoted.get(m.group(1), ""), action["url"])
        headers = {"Accept": "application/json", "User-Agent": "homeserver-gateway"}
        cred = action.get("credential")
        if cred:
            try:
                path = os.path.join(self.cfg["creds"], cred["ref"])
                with open(path, encoding="utf-8") as fh:
                    value = fh.read().strip()
            except OSError:
                raise Refuse(503, "credential %r not available" % cred["ref"])
            if not value:
                raise Refuse(503, "credential %r is empty" % cred["ref"])
            headers[cred.get("header", "Authorization")] = cred.get("prefix", "Bearer ") + value
        payload = None
        if action["method"] != "GET":  # params not used in the URL travel as the JSON body
            used = set(TEMPLATE_RE.findall(action["url"]))
            payload = json.dumps({k: v for k, v in params.items() if k not in used}).encode()
            headers["Content-Type"] = "application/json"
        return url, headers, payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gateway"

    def log_message(self, *a):
        pass

    def reply(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "application/json", b'{"ok": true}')
        self.reply(404, "application/json", b'{"error": "unknown route"}')

    def do_POST(self):
        m = re.fullmatch(r"/(act|plan)/([A-Za-z0-9._-]{1,64})", self.path.split("?")[0])
        if not m:
            return self.reply(404, "application/json", b'{"error": "unknown route"}')
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, MAX_REQUEST + 1))
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        status, ctype, data, info = self.server.gw.process(m.group(1), token, m.group(2), raw)
        self.reply(status, ctype, data)
        print("%s agent=%s action=%s decision=%s status=%s %s" % (
            time.strftime("%FT%T"), info["agent"], info["action"][:64], info["decision"], status,
            info["detail"]), flush=True)

    do_PUT = do_PATCH = do_DELETE = lambda self: self.reply(405, "application/json", b'{"error": "use POST /act/<action>"}')


def config(dry_run=False):
    # under systemd LoadCredential the files appear in $CREDENTIALS_DIRECTORY
    creds = ENV("GATEWAY_CREDS") or ENV("CREDENTIALS_DIRECTORY") or "/etc/homeserver/gateway/creds"
    return {
        "bind": ENV("GATEWAY_BIND", "127.0.0.1"), "port": int(ENV("GATEWAY_PORT", "8780")),
        "tokens": ENV("GATEWAY_TOKENS", "/etc/homeserver/gateway/tokens"),
        "actions": ENV("GATEWAY_ACTIONS", "/etc/homeserver/gateway/actions.json"),
        "acl": ENV("GATEWAY_ACL", "/etc/homeserver/gateway/acl.json"),
        "creds": creds, "disable": ENV("GATEWAY_DISABLE", "/etc/homeserver/gateway.disable"),
        "ledger": ENV("GATEWAY_LEDGER", "/var/lib/homeserver/gateway/used.json"),
        "plans": ENV("GATEWAY_PLANS", "/var/lib/homeserver/gateway/plans.json"),
        "plan_ttl": int(ENV("GATEWAY_PLAN_TTL", "86400")), "plan_max_per_hour": int(ENV("GATEWAY_PLAN_MAX_PER_HOUR", "20")),
        "trinity_url": ENV("TRINITY_URL", "http://127.0.0.1:8000").rstrip("/"),
        "trinity_token_file": ENV("TRINITY_TOKEN_FILE", "/etc/homeserver/gateway/trinity-token"),
        "timeout": int(ENV("GATEWAY_TIMEOUT", "20")), "dry_run": dry_run,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic egress gateway for agent containers.")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, but never call the upstream (approvals are not burned)")
    ap.add_argument("--check", action="store_true", help="load tokens, actions and ACL, print a summary, exit")
    ap.add_argument("--hash", nargs=2, metavar=("ACTION", "PARAMS_JSON"), help="print the plan sha256 for an action and params, exit")
    args = ap.parse_args(argv)
    if args.hash:
        print(plan_hash(args.hash[0], json.loads(args.hash[1])))
        return 0
    gw = Gateway(config(args.dry_run))
    if args.check:
        print("tokens: %d, actions: %d, agents in acl: %d" % (len(gw.tokens), len(gw.actions), len(gw.acl)))
        return 0
    for agent, names in gw.acl.items():
        for n in names:
            if n not in gw.actions:
                print("warning: acl for %s names unknown action %s" % (agent, n), flush=True)
    srv = ThreadingHTTPServer((gw.cfg["bind"], gw.cfg["port"]), Handler)
    srv.gw = gw
    print("gateway listening on %s:%d%s" % (gw.cfg["bind"], gw.cfg["port"], " (dry-run)" if args.dry_run else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
