# firewall: fence for Trinity agent containers

## What it does

`agents.nft.example` is an nftables ruleset for the machine that runs Trinity's agent containers. It creates one
separate table, `inet homeserver_agents`, with an `input` and a `forward` chain, both matching only IPv4 traffic
whose source is the agent bridge (default `172.28.0.0/16`, the `trinity-agent` network in `docker-compose.prod.yml`).
Each chain's first rule is `meta nfproto != ipv4 return`, so IPv6 passes through this table unfiltered and
unharmed (without it, the input chain's final drop would also drop all of the host's inbound IPv6). Agent IPv6 is
not fenced: keep IPv6 off on the agent bridge (Docker's default) or add ip6 rules.

- Agents may reach the host only on an explicit port list: the gateway (8780) and the broker (8790) by default,
  plus an optional commented extra. Every other host port is dropped.
- Agents may reach the public internet.
- Agents are dropped from private space: `10/8`, `172.16/12`, `192.168/16`, `100.64.0.0/10` (CGNAT and VPN
  addresses), `169.254.0.0/16` (link-local, including the cloud metadata address) and the LAN placeholder
  `192.0.2.0/24`.
- Agent-to-agent traffic on the same bridge, and replies to connections agents opened, keep working.

Chains run at hook priority -10, before Docker's own priority-0 chains. A drop is final; an accept only ends our
chain, so Docker's and your rules still apply afterwards. Details are in the comments at the top of the file.

## Why

An agent runs model-written commands. Without a fence, a prompt injection or a bad tool call can reach the host's
admin ports, other machines on the home network, VPN peers, or the metadata endpoint. The allow-list makes the
host-side services the only door, and each of those does its own authentication.

## How to enable

1. Copy and edit. Set `AGENT_NET`/`HOST_GW` if your bridge differs (`docker network inspect trinity-agent-network`),
   replace `192.0.2.0/24` and `LAN_DNS` with your real LAN, and set the ports.
   `sudo install -d /etc/homeserver/firewall && sudo cp agents.nft.example /etc/homeserver/firewall/agents.nft`
2. Syntax-check without loading: `sudo nft -c -f /etc/homeserver/firewall/agents.nft`
3. Load: `sudo nft -f /etc/homeserver/firewall/agents.nft`
   Check: `sudo nft list table inet homeserver_agents`
4. Test from inside an agent container (substitute a container name, a LAN address and an allowed port):
   - allowed port works: `docker exec AGENT curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://172.28.0.1:8780/`
     (any HTTP status means the connection was accepted)
   - LAN fails: `docker exec AGENT curl -sS -m 5 http://192.0.2.10/` (expect a timeout)
   - other host port fails: `docker exec AGENT curl -sS -m 5 http://172.28.0.1:22/` (expect a timeout)
   - internet works: `docker exec AGENT curl -sS -m 5 -o /dev/null -w '%{http_code}\n' https://example.com/`
   - counters show the drops: `sudo nft list table inet homeserver_agents | grep counter`
   - the host's IPv6 still works: from the host, `ping -6 -c1 ::1` and, if the host has IPv6, an inbound
     `ssh -6` from another machine. To check in isolation first, load the file in a throwaway network namespace:
     `sudo ip netns add nfttest && sudo ip netns exec nfttest sh -c 'ip link set lo up && nft -f /etc/homeserver/firewall/agents.nft && ping -6 -c1 ::1'; sudo ip netns del nfttest`
     (expect `1 received`; without the first rule of each chain the ping is dropped)
5. Persist across reboots. Add one line at the end of `/etc/nftables.conf`:
   `include "/etc/homeserver/firewall/agents.nft"`, then `sudo systemctl enable --now nftables`.
   The table only exists after boot-time load. To reload by hand later, delete it first so rules do not
   duplicate: `sudo nft delete table inet homeserver_agents; sudo nft -f /etc/homeserver/firewall/agents.nft`
   (if `/etc/nftables.conf` starts with `flush ruleset`, a service reload already does this).

## Configuration

These are `define` lines at the top of the file (nftables has no environment variables).

| Define | Default | Meaning |
|---|---|---|
| `AGENT_NET` | `172.28.0.0/16` | Agent bridge subnet |
| `HOST_GW` | `172.28.0.1` | Host address as seen by agents |
| `PORT_GATEWAY` | `8780` | Allowed host port (gateway) |
| `PORT_BROKER` | `8790` | Allowed host port (GitHub broker) |
| `PORT_EXTRA` | commented | Optional third port; also add it to `HOST_PORTS` |
| `LAN_DNS` | `192.0.2.1` | Resolver on the LAN that agents may query on port 53 |
| `PRIVATE_NETS` | list above | Ranges dropped for agents |

## Undo

Remove the table (no other table was edited, so this restores the host's previous filtering):

    sudo nft delete table inet homeserver_agents

To make it permanent, delete the `include` line from `/etc/nftables.conf` and the copied file.

## Notes

- It was not loaded against a live host by its author: run step 2 (`nft -c -f`) first. `python3 test_firewall.py`
  checks the file's structure offline (both chains return non-IPv4 packets before any accept or drop).
- An agent that calls a host service through the host's LAN address (not `HOST_GW`) is dropped by the input chain.
  Point agents at `HOST_GW`. Agents reach platform containers by name on the shared bridge.
