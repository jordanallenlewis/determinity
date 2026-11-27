# gateway: one checked road from agents to devices, accounts and third-party APIs

## What it does

`gateway.py` is a small host-side HTTP service (Python stdlib only). Agents call it instead of calling a device,
an account or an API directly. The gateway holds the credentials; an agent holds only its own gateway bearer token.

An agent calls `POST /act/<action>` with `{"params": {...}}`. The gateway then checks, in order:

1. token -> agent (the tokens file holds sha256 hashes, never raw tokens; same format as the GitHub broker),
2. the agent's ACL lists the action (`acl.json`),
3. every param is allowed and matches its regex (`actions.json`),
4. for `kind: "write"`: the request carries a `plan_id` the gateway issued and a person approved (see below),
5. the optional `max_per_hour` cap for that agent and action.

It then builds the upstream request from the action template (params are percent-encoded into the URL; the rest of
a non-GET call goes as a JSON body), adds the credential from a root-only file in the header you configured, sends
it and returns the upstream status and body. Responses over `GATEWAY_MAX_RESPONSE` (1 MiB) are refused, redirects
are never followed, and every refusal is JSON `{"error": "refused", "reason": "..."}` with 400, 401, 403, 413, 429
or 503.

`POST /plan/<action>` validates the same way (ACL, params). For a write action it then raises the approval ask in
Trinity itself and returns `plan_id`, `queue_item_id`, and the `title` and `question` the person will read. For a
read action it sends nothing and returns `needs_approval: false`. `GET /healthz` answers without auth.

**Approval for writes.** The person approves text the gateway wrote, not text the agent wrote.

1. The agent calls `POST /plan/<action>` with `{"params": {...}}`. The gateway checks ACL and param regexes, then
   raises an `approval` ask (options `Approve` / `Deny`) for that agent. The title comes from the action's
   `describe` line (`"Turn light {id} {state}"`, filled with the values, JSON-escaped); the question lists the agent,
   the action and every parameter as `key = "value"`; the params and action also go in the structured
   `context.gateway`. A secret param (below) appears everywhere as `(secret, not shown)`, and the ask carries no
   hash of the params, since a short code could be recovered from its hash by trying every value. Anything else the
   agent puts in the plan body is ignored.
2. The gateway records, in its own plans file (`GATEWAY_PLANS`, mode 0600): the plan id (also the ask's
   `request_id`, 128 random bits), agent, action, sha256 of the canonical action + params, the queue item id Trinity
   returned, and the exact title and question it sent. Plans expire after `GATEWAY_PLAN_TTL` (24 h).
3. A person answers `Approve` in Trinity.
4. The agent calls `POST /act/<action>` with `{"params": {...}, "plan_id": "..."}`. The gateway looks the plan up in
   its own record, requires the same agent, action and a params hash equal to this call's, then re-reads the queue
   item and requires: the item id and `request_id` it recorded; the agent; title and question still equal to what
   it wrote; status `responded` or `acknowledged` with an approving answer (`approve`, `approved`, `yes`, `ok`,
   case-insensitive; change with `GATEWAY_APPROVE_VALUES`). Nothing is matched from free text, so an ask an agent
   raises by itself (any title, any marker in its text) can never approve a call. `approval_id` is no longer accepted.
5. The plan id and the queue item id are written to the ledger (`GATEWAY_LEDGER`, fsynced, survives restarts)
   before the upstream call, so one approval buys one call even if the upstream call fails. Ids are normalised to a
   single canonical string first (trimmed, lowercased, leading zeros of an all-digit id dropped; booleans, floats and
   other types are not ids), so `"12"`, `12` and `"012"` are one id. If Trinity cannot be reached, a receipt is not
   what the gateway expects, the plans file or ledger cannot be read or written, or the credential file is missing,
   the write is refused (fail closed). A plan that was raised but never recorded is harmless: it matches nothing.
   `/plan` is capped per agent (`GATEWAY_PLAN_MAX_PER_HOUR`, default 20) so an agent cannot flood the queue.

Why the gateway raises the ask: Trinity offers `POST /api/agents/{name}/operator-queue` (`routers/operator_queue.py:662-702`,
`raise_my_ask`), which stores a title, question, options and structured `context` as given and returns a receipt
with the row `id` and `request_id`. The caller must be the agent itself or an admin's user-scoped key naming the
agent in `X-Trinity-Act-As-Agent` (`dependencies.py:828`, `:841`, `:2551`). The gateway uses the second form, so
agents keep no new capability and the ask is recorded as that agent's. This lets the gateway own the text and
bind it to the params in its own state, which is the strongest design. The cost is a more powerful token on the
host (see step 4 below); the fallback of trusting a structured `context` written by the agent was rejected because
the agent still authors what the person reads.

Limits of what the person sees: every parameter value is shown to the approver and stored in Trinity, except params
marked `"secret": true` (a door code, a PIN). Their values never leave the gateway: the approver sees
`(secret, not shown)`, and the plan still binds the real value, so a different code needs a new approval. A
`sensitive` action that marks no param secret has every param hidden, which leaves the approver nothing to check, so
mark the secret ones explicitly. Credentials live in the gateway, never in params.
The approver should read the `What it does` line and the `key = "value"` lines. A request whose rendering exceeds
about 3500 characters is refused, so params cannot be hidden below the fold.

Trinity endpoints used: `POST /api/agents/{name}/operator-queue` (raise, above) and `GET /api/operator-queue/{item_id}`
(`routers/operator_queue.py:388-389`, router prefix at line 47). The fields read are `id`, `request_id`, `agent_name`,
`status`, `response`, `title`, `question`.

**Kill switch.** Creating `/etc/homeserver/gateway.disable` refuses every write at once (503) and leaves reads
working. It is checked on each call; no restart needed. `--dry-run` decides and logs but never contacts the
upstream and does not burn approvals or raise asks.

Logs (stdout, journald): one line per call with agent, action, decision and HTTP status. Param values are logged
for non-sensitive actions only, and secret params never; actions with `"sensitive": true` log no values and no URL. Tokens and credentials
are never logged.

## Why

Approval has to live in the path the call takes, not in the agent's instructions. An agent told "ask first" can
still call the API directly if it holds the credential or can reach the device. Here the credential sits where the
agent cannot read it, the network fence lets the agent reach only this service, and the service refuses a write
unless a human approved those exact params. A prompt injection can make an agent ask; it cannot make it skip asking.

Limits: the gateway trusts Trinity's record of who answered, and the admin key it holds for raising asks. That key
is a user-scoped admin key: whoever holds it (the gateway, or anyone who can read its credential file) can do what
that Trinity user can, including answering an approval ask the gateway raised, so keep the file root-only; a dedicated
agent key that cannot answer asks is the planned fix. It does not make an upstream API safe by itself: keep
each action narrow (regexes, `max_per_hour`) and put no admin-capable credential behind a broad action.

## How to enable

1. Install and create the directories (non-secret files world-readable, credentials root-only):
   ```
   sudo install -m 0755 gateway.py /usr/local/sbin/hs-gateway
   sudo install -d -m 0755 /etc/homeserver/gateway
   sudo install -d -m 0700 /etc/homeserver/gateway/creds
   sudo install -m 0644 actions.example.json /etc/homeserver/gateway/actions.json
   sudo install -m 0644 acl.example.json /etc/homeserver/gateway/acl.json
   sudo install -m 0644 tokens.example /etc/homeserver/gateway/tokens
   ```
2. Edit `actions.json` (your real upstream URLs, one regex per param, `credential.ref` file names) and `acl.json`.
3. Put each credential in its own root-only file, one line. Never paste it into a config file:
   ```
   sudo sh -c 'umask 077; cat > /etc/homeserver/gateway/creds/weather-key'
   ```
   Add a matching `LoadCredential=<ref>:/etc/homeserver/gateway/creds/<ref>` line to `gateway.service` per file.
4. Give the gateway access to Trinity: write an admin's user-scoped Trinity API key (it must be allowed to act on
   behalf of the agents, and to read operator-queue items) to `/etc/homeserver/gateway/trinity-token` (mode 0600,
   root). Use a dedicated admin user for it so its audit rows are easy to find. Set `TRINITY_URL` in the unit if Trinity is not on
   `http://127.0.0.1:8000`.
5. Create one token per agent and store only its hash in `/etc/homeserver/gateway/tokens`:
   `T=$(openssl rand -hex 32); echo "$T" | tr -d '\n' | sha256sum | cut -d' ' -f1`. Hand `$T` to that agent.
6. Check the config, then start:
   ```
   GATEWAY_TOKENS=/etc/homeserver/gateway/tokens python3 gateway.py --check
   sudo install -m 0644 gateway.service /etc/systemd/system/hs-gateway.service
   sudo systemctl daemon-reload && sudo systemctl enable --now hs-gateway
   curl -s http://172.28.0.1:8780/healthz
   ```
   For a first trial add `--dry-run` to `ExecStart` and watch `journalctl -u hs-gateway`.
7. Fence the agents. This is what makes the gateway mandatory instead of optional: in `../firewall`, allow the agent
   bridge to reach only this port (8780) and the GitHub broker's port on the host, and drop agent traffic to private
   ranges. Do not keep the credentials anywhere an agent container can read.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_BIND` / `GATEWAY_PORT` | `127.0.0.1` / `8780` | listen address (the unit uses the bridge address) |
| `GATEWAY_TOKENS` | `/etc/homeserver/gateway/tokens` | `sha256  agent` lines |
| `GATEWAY_ACTIONS` | `/etc/homeserver/gateway/actions.json` | action catalogue |
| `GATEWAY_ACL` | `/etc/homeserver/gateway/acl.json` | agent -> action names |
| `GATEWAY_CREDS` | `$CREDENTIALS_DIRECTORY`, else `/etc/homeserver/gateway/creds` | where `credential.ref` files are read |
| `GATEWAY_LEDGER` | `/var/lib/homeserver/gateway/used.json` | single-use ledger (plan ids and queue item ids), fsynced, persists across restarts |
| `GATEWAY_PLANS` | `/var/lib/homeserver/gateway/plans.json` | plans the gateway raised (mode 0600) |
| `GATEWAY_PLAN_TTL` / `GATEWAY_PLAN_MAX_PER_HOUR` | `86400` / `20` | plan lifetime in seconds; plans per agent per hour |
| `GATEWAY_DISABLE` | `/etc/homeserver/gateway.disable` | kill switch file: refuses writes |
| `GATEWAY_MAX_REQUEST` / `GATEWAY_MAX_RESPONSE` | 65536 / 1048576 | size caps in bytes |
| `GATEWAY_TIMEOUT` | `20` | upstream timeout, seconds |
| `GATEWAY_APPROVE_VALUES` | `approve,approved,yes,ok` | answers that count as approval |
| `TRINITY_URL` | `http://127.0.0.1:8000` | Trinity backend |
| `TRINITY_TOKEN_FILE` | `/etc/homeserver/gateway/trinity-token` | bearer token for Trinity |

Action fields: `kind` (`read` must use GET; or `write`), `method`, `url` (with `{param}` placeholders), `credential`
(`ref`; `header` default `Authorization`; `prefix` default `Bearer `; omit the object for none), `params` (name ->
`pattern`, optional `"optional": true`, optional `"secret": true`; a pattern must match the whole value), `describe` (write actions: the sentence
the approver reads, `{param}` placeholders allowed; default `Run <action>`), `max_per_hour`, `sensitive`.

## Undo

- Stop writes only: `sudo touch /etc/homeserver/gateway.disable` (remove the file to resume).
- Stop everything: `sudo systemctl disable --now hs-gateway`.
- Remove: `sudo rm -rf /usr/local/sbin/hs-gateway /etc/systemd/system/hs-gateway.service /etc/homeserver/gateway /var/lib/homeserver/gateway`,
  then `sudo systemctl daemon-reload`.
- Rotate a leaked credential: replace its file under `creds/` and restart; agents never held it.
- Revoke an agent: delete its line from `tokens` and restart.
