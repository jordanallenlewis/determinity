# caddy: sign-in gate for the Trinity web UI

## What it does

Puts Trinity's web UI behind Caddy, reachable only from the home network, with a second factor in front of it.

- `Caddyfile.example`: two sites. `auth.example.com` is the sign-in page (TinyAuth, password + TOTP).
  `trinity.example.com` proxies to Trinity's bundled nginx and asks TinyAuth first through `forward_auth`.
  A `remote_ip` matcher allows only `192.0.2.0/24` (LAN placeholder) and `100.64.0.0/10`; any other client gets 403.
- Paths that carry their own token skip the sign-in page: `/mcp` (MCP server, Trinity API key as bearer token)
  and `/health`. Optional, commented: `/a2a/*` and `/.well-known/aauth-*`. The path list comes from
  `src/frontend/nginx-server.conf` (locations `/api/`, `/.well-known/aauth-`, `/a2a/`, `/mcp`, `/ws`, `/health`).
- `tinyauth.compose.example.yml`: the TinyAuth container, pinned image tag, read-only, no capabilities. The only
  secret is the users file, bind-mounted read-only. `tinyauth.env.example` holds placeholders, no secrets.
- `hs-trinity-gate`: `init | on | off | status`. Install as `/usr/local/sbin/hs-trinity-gate`.

## Why

Trinity's web UI can create agents, run commands and read credentials. A password alone, behind a LAN firewall, is
one stolen cookie or one reused password from code execution. A TOTP code makes that a second thing to steal.
Agents and automation do not pass through Caddy (they use the Docker network), so they are unaffected.
`/api/` is deliberately not bypassed: the backend has a few unauthenticated `/api/auth/*` routes that should not
be exposed to the whole LAN (see the closing comment in `Caddyfile.example` for the trade-off).

## How to enable

1. Trinity: in its `.env` set `FRONTEND_BIND_ADDRESS=127.0.0.1` and `FRONTEND_PORT=8081`, then
   `docker compose -f docker-compose.prod.yml up -d`, so only Caddy can reach the UI. Check:
   `curl -s http://127.0.0.1:8081/health` prints `healthy`.
2. Install the pieces. Needs `caddy`, `docker`, `htpasswd` (apt: `apache2-utils`), `openssl`, optional `qrencode`.

   ```
   sudo install -m 0755 hs-trinity-gate /usr/local/sbin/hs-trinity-gate
   sudo install -d -m 0711 /etc/homeserver/trinity-gate /var/lib/homeserver/trinity-gate
   sudo chown 8783:8783 /var/lib/homeserver/trinity-gate
   sudo install -m 0644 tinyauth.env.example /etc/homeserver/trinity-gate/tinyauth.env
   ```

   Edit `TINYAUTH_APPURL` in that file. Set the real LAN range in `Caddyfile.example` and append it to
   `/etc/caddy/Caddyfile`, with your DNS names.
3. Create the user, on an interactive terminal as root: `sudo hs-trinity-gate init`. Enter a password (not echoed).
   The authenticator QR/URI is shown once; scan it. Only the password hash and the TOTP seed are stored, in
   `/etc/homeserver/trinity-gate/` (root-only; the users file is readable by the container's group only).
4. Start the sign-in service: `docker compose -f tinyauth.compose.example.yml up -d`.
   Check: `sudo hs-trinity-gate status` shows `sign-in service: up`.
5. Create the switch file and turn the gate on:
   `echo '# off' | sudo tee /etc/caddy/trinity-gate.caddy`, then `sudo caddy validate --config /etc/caddy/Caddyfile`,
   `sudo systemctl reload caddy`, and `sudo hs-trinity-gate on`.
6. Test: from the LAN, `https://trinity.example.com` redirects to the sign-in page; after password and code, the UI
   loads. `curl -sk https://trinity.example.com/health` returns `healthy` without sign-in. From outside the
   allowed ranges the answer is 403.

## Configuration

Environment variables read by `hs-trinity-gate` (defaults shown):

| Variable | Default | Meaning |
|---|---|---|
| `HS_GATE_DIR` | `/etc/homeserver/trinity-gate` | root-only secrets directory |
| `HS_GATE_SNIPPET` | `/etc/caddy/trinity-gate.caddy` | file the Caddyfile imports; `on`/`off` rewrite it |
| `HS_GATE_USER` | `admin` | sign-in user name |
| `HS_GATE_ADDR` | `127.0.0.1:8783` | where TinyAuth listens |
| `HS_GATE_UID` | `8783` | uid/gid of the container (matches the compose file) |
| `HS_GATE_ISSUER` | `Trinity` | label shown in the authenticator app |
| `HS_GATE_CADDYFILE` | `/etc/caddy/Caddyfile` | used by `status` |
| `HS_GATE_VALIDATE_CMD` / `HS_GATE_RELOAD_CMD` | `caddy validate ...` / `systemctl reload caddy` | Caddy checks |

`-n` / `--dry-run` shows what `init`, `on` or `off` would do without changing anything. If Caddy rejects a change,
the previous snippet is restored.

## Break-glass

Lost the authenticator or the sign-in service is down: `sudo hs-trinity-gate off`. Caddy stops asking; Trinity's own
login still applies and the LAN/CGNAT restriction still holds. Fix the cause, then `sudo hs-trinity-gate on`.
To reset the second factor: `sudo rm /etc/homeserver/trinity-gate/users`, then `sudo hs-trinity-gate init`
and `docker restart trinity-gate`.

## Undo

1. `sudo hs-trinity-gate off`
2. `docker compose -f tinyauth.compose.example.yml down`
3. Remove the `import /etc/caddy/trinity-gate.caddy` line and the `auth.example.com` site from the Caddyfile and
   reload Caddy.
4. Optionally `sudo rm -r /etc/homeserver/trinity-gate /var/lib/homeserver/trinity-gate /etc/caddy/trinity-gate.caddy`.

## Notes

- Not run against a live Caddy or TinyAuth by its author (neither was available). Run `caddy validate` first.
  The `forward_auth` URI `/api/auth/caddy` and the `TINYAUTH_*` names follow TinyAuth v5; check them if you change
  the image tag.
- `TINYAUTH_AUTH_SUBDOMAINSENABLED=true` scopes the session cookie to the parent domain, so it is also sent to
  other hosts under it.
