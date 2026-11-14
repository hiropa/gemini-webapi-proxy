# Linking machines: master ↔ slave ("nodes")

Per-account HTTP/SOCKS proxies are unreliable: providers detect datacenter proxies
easily and SOCKS often breaks WebSocket-style traffic. A more reliable way to spread
accounts over several IP addresses is to run **this proxy on each machine** and link
them: one instance is the **master** (the one your clients talk to), the others are
**slaves** (the "nodes"). The slave's accounts then appear in the master's pool and
are used exactly like local accounts — same `/openai/v1/models`, same failover, same
admin UI listing. Google sees each account's traffic coming from the slave's own
address, because it does.

```
client ──► master :4982 ──/node/* (Bearer node token, https)──► slave :4982 ──► Google
                              (its accounts, pinned)               (from the slave's IP)
```

## How it works

- **Slave**: an ordinary instance with a *node token* set — `GOP_NODE_TOKEN=...` in
  `.env`, or generated in the admin UI (**System** tab → "Regenerate token", stored in
  `<data_dir>/node_token.txt`, mode 0600). Without a token the `/node/*` API is off.
  The token only guards `/node/*` (ping, account list, generate, video jobs); it never
  exposes cookies — only public account data (state, quota, models) crosses the link,
  and generated images/videos come back as bytes.
- **Master**: admin UI **Nodes** tab → **Link a node** with the slave's URL and token
  (or `POST /admin/nodes`). Links live in `<data_dir>/nodes.json` (mode 0600).
  The master re-reads each node's accounts every `GOP_NODE_REFRESH_INTERVAL` seconds
  (default 30) and mirrors them as accounts named `<node>/<account>` (slot id
  `<node>::<account>`).
- **Requests**: when the pool picks a remote account, the master forwards the
  generation to the slave's `/node/generate`, which runs it through *its own* pool
  pinned to that account (same pacing/concurrency limits as local). Quota (429),
  logged-out (401) and mid-request node failures make the master move on to the next
  account — local or on another node — exactly like local failover. Video jobs run on
  the slave and the mp4 is downloaded to the master via `/node/jobs/{id}/content`.
- **Sign-in** still happens on the slave itself (its own admin UI / browser). On the
  master's Accounts tab, remote accounts show a `via <node>` chip and a button that
  opens the slave's admin UI; they can't be edited from the master.

## Setup

1. On the **slave**: set `GOP_NODE_TOKEN` (or use the generated one), expose the port
   over **https** (nginx/Caddy with TLS) or an SSH tunnel
   (`ssh -N -L 4982:127.0.0.1:4982 slave`), then sign in its Google accounts as usual.
2. On the **master**: admin UI → **Nodes** → name, slave URL (`https://slave…:4982`),
   token → **Link node**. The slave's accounts appear on the **Accounts** tab within a
   refresh interval.
3. Link as many slaves as you like; order them with the node priority (lower first).
   Disable/unlink a node any time — its accounts simply leave the master's pool.

## Security notes

- The link is application-level JSON over HTTPS with a dedicated bearer token — no
  raw TCP/SOCKS forwarding. Use TLS (or an SSH tunnel) so the token and the traffic
  are not readable on the network.
- The node token is separate from `GOP_API_KEY`: a leaked client key cannot drive
  `/node/*`, and the node token cannot call `/openai/v1/*`.
- Regenerating the slave's token invalidates all links to it immediately.
- Events `node_down` / `node_up` are emitted (and can be sent to Telegram) when a
  linked node becomes unreachable / reachable again.

## API (for scripts)

Slave, all under `Authorization: Bearer <node token>`:

| Endpoint | Meaning |
|---|---|
| `GET /node/ping` | `{ok, version}` |
| `GET /node/accounts` | local accounts: state, quota, models, cooldowns (public fields only) |
| `POST /node/generate` | `{account, model, prompt, images[], mode: chat|image|video, seconds?, size?}` → `{text, images:[{b64}], job?, status?}`; errors: 429 `quota`/`unavailable`, 401 `auth`, 403 `refused`, 404 `no_account` |
| `GET /node/jobs/{id}` | video job status (`has_file` when done) |
| `GET /node/jobs/{id}/content` | the mp4 |

Master, admin session / API key:

| Endpoint | Meaning |
|---|---|
| `GET/POST /admin/nodes`, `PATCH/DELETE /admin/nodes/{id}`, `POST /admin/nodes/{id}/refresh` | manage linked slaves |
| `GET/POST /admin/node-token` | view (masked) / regenerate this instance's slave token |
