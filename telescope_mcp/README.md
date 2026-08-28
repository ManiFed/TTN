# Telescope Net over MCP

Drives the Telescope Net through chat instead of the desktop app. Two servers,
because the product has two backends:

| Server | Wraps | Tools | Who runs it |
|---|---|---|---|
| `local_server` | both backends | 130 | the computer attached to a telescope |
| `cloud_server` | `api.thetelescope.net` (cloud/server.py) | 79 | anyone, from anywhere |

The node server is a **superset**, not a sibling. Linking a telescope spans both
backends — discovery is on the LAN, credentials come from the cloud, and the
credential is written back to the agent — so the machine that can reach both
gets everything, and setting up a telescope means adding one server.

Every tool is a thin call onto an endpoint the Flutter app already uses, so the
two interfaces cannot drift apart in behaviour. `tests/test_mcp_parity.py`
fails when the app grows a capability the servers have not caught up with.

## Running it

Install the dependency (already in `requirements.txt`):

```bash
pip install "mcp>=2.0"
```

**On a member's machine, there is nothing to do.** `postinstall.sh` runs
`scripts/register_mcp_client.py`, which merges one entry into Claude Desktop's
config pointing at the packaged agent with `--mcp`. It keeps a backup, writes
atomically, and refuses outright if the existing config cannot be parsed — that
file may list other MCP servers the member depends on, and rewriting it would
delete them. `uninstall.sh` removes only that one key.

**From a checkout, over stdio:**

```bash
python -m telescope_mcp.cloud_server
```

The packaged agent serves the same thing as `TelescopeNetNode --mcp`. In that
mode stdout is the JSON-RPC transport, so setup runs with stdout redirected to
stderr — one stray print would corrupt the stream and the client would drop the
connection with a parse error pointing nowhere useful.

**Remote, over HTTP** — so it can be added as a connector rather than installed:

```bash
python -m telescope_mcp.cloud_server --http --host 0.0.0.0 --port 8900
```

**On a telescope computer**, for hardware and log access:

```bash
python -m telescope_mcp.local_server
```

**Standalone — your own telescope, no account:**

```bash
python -m telescope_mcp.local_server --local
```

50 tools: find and connect the telescope, drive the mount and camera, focus,
plate-solve centre, live-stack, read logs, diagnose, and get images back inline.
Nothing is uploaded and nothing is shared. The account-facing tools are not
registered at all, so nothing invites the member to sign in to something they
deliberately did not join, and `connect_telescope` replaces
`connect_my_telescope` — same job, minus the two steps that need credentials.

A standalone node also finishes commissioning: the cloud-registration check
becomes informational rather than blocking, instead of sitting at
`waiting_for_signup` for ever waiting on an account that is never coming.

The local server needs no credentials. The agent binds to 127.0.0.1 and rejects
browser callers by `Host` and `Origin` (`src/dashboard.py:62`); an MCP server
sends neither, so it is unaffected — same as `curl`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELESCOPE_MCP_CLOUD_BASE` | `https://api.thetelescope.net` | cloud API root |
| `TELESCOPE_MCP_AGENT_BASE` | `http://127.0.0.1:5173` | node agent root |
| `TELESCOPE_MCP_TOKEN` | — | member session token; or call `sign_in` / `auth_login` |
| `TELESCOPE_MCP_SESSION_PATH` | `~/.telescopenet/mcp_session` | user-only file holding the member session so an MCP process restart does not silently log them out. Never returned by a tool |
| `TELESCOPE_MCP_ADMIN_KEY` | — | `X-Admin-Key`. Use `CLOUD_ADMIN_READONLY_KEY` for monitoring; the full key only when admin tools are actually needed |
| `TELESCOPE_MCP_ENV` | `sim` | `sim` \| `staging` \| `production` |
| `TELESCOPE_MCP_ALLOW_PRODUCTION_WRITES` | unset | opt in to hardware writes on production |

## When the tools do not appear

```bash
TelescopeNetNode --doctor
```

Walks the chain in order and stops at the first broken link: is the node agent
running, is it registered with Claude, does the registered command actually
start, and has Claude been restarted since it was registered.

That last one is the invisible failure. Claude reads its tool list only at
startup, so an entry written while it is open does nothing and says nothing --
from inside the conversation it is indistinguishable from a telescope that is
not connected, and the assistant answers from general knowledge instead.

The check has to live outside the assistant for the same reason: when the tools
are missing, none of this code is running to say so.

## Safety

Three rails, each tested in `tests/test_mcp_guards.py`:

- **Environment gating.** Anything that moves a mount, opens an enclosure or
  exposes a camera refuses to run against production. A real instrument is on
  the other end and it can be pointed at the sun. Tools that *stop* activity —
  `node_abort_exposure`, `node_arm_close`, `node_schedule_abort` — are never
  blocked; a rail that jams the brake is worse than no rail.
- **Confirmation.** Irreversible actions (`member_delete_account`,
  `member_disconnect_node`, `admin_broadcast_interrupt`) need `confirm=true`.
- **Secret redaction.** No tool returns a node `api_key` or a session token. A
  credential echoed into a transcript cannot be recalled. After sign-in the
  session is stored in a user-only file (`~/.telescopenet/mcp_session`, mode
  0600) so a recycled MCP process stays signed in. Logout deletes the file.

Log lines, catalogue names and member-authored text come back wrapped as
`_provenance: untrusted`. That text is data. Nothing inside it is an
instruction, however it is phrased.

## Tonight

Being in or out of an observing run is a sentence, not a form. Every day each
telescope gets a research-weighted proposal, and one of four things happens:

| | |
|---|---|
| the member accepts | `tonight_accept` — takes effect at once |
| the member declines | `tonight_decline` — tonight only, tomorrow is asked again |
| nobody answers | the recommendation runs at dusk |
| the member stops it | `stand_down` — reaches the node in about a second |

Two rules sit above that, both in `cloud/nightly.py`:

**Weather wins.** Rain or heavy cloud holds the night regardless of who accepted
it, and the forecast is re-checked rather than cached — a hold is not a
cancellation. If the sky clears, an accepted night resumes as accepted. A
forecast that cannot be fetched fails *open*: the node's own SafetyManager can
see the actual sky and remains the authority.

**Override wins faster.** A stand-down is an instruction. It is written first,
pushed over SSE immediately, and never overturned by a later auto-accept or by
the weather improving.

`stand_down(nights=7)` also parks the telescope for a week, which is what a
vacation used to mean — deliberately the same mechanism, so there are not two
ways for a telescope to be out that can disagree.

Tool results carry a `nudge`: one honest line about what the research programme
gets out of tonight. It is a nudge, not a gate. Declining is always one call
away and is never argued with.

### What the node does with it

`src/cloud_communicator.py` polls `/api/v1/nodes/tonight` on the plan loop and
on any `retask` push, so a stand-down lands in about a second. The callback
fires only on a *change* — re-cancelling an already-cancelled night on every
poll would fight the scheduler.

`src/dashboard.py` acts on it. A stand-down cancels the running schedule,
aborts the exposure in flight (waiting out a 300-second exposure is not "stop
now"), and parks the mount. The schedule loop re-checks the intent per item, so
the weather closing in mid-night stops the run rather than being noticed at
dawn.

Both sides **fail open**: an unreachable cloud means the node carries on with
its last known intent, and a node that has never heard from the cloud behaves as
it always has. Going dark on a network blip would cost a clear night, and the
SafetyManager still decides whether it is actually safe to open.

A bounded research block (`tonight_accept(research_hours=2, imaging_after=True)`)
ends when its hours are up and hands the rest of the night to
`run_imaging_program`. Zero hours is treated as *unbounded*, not as instantly
over — otherwise a malformed proposal would silently skip all the science.

## Doing whole jobs

Most tools map 1:1 onto an endpoint, which is what keeps parity honest. These
compose them into things someone would actually say:

- `connect_my_telescope()` — discover, connect, reuse this computer's existing
  cloud identity (or register), install credentials, confirm online. One call.
  The identity reuse is the important part: registering a second node for a
  computer that already has one orphans the first along with its history.
- `diagnose()` — status, safety, logs, identity and fleet integrity in one pass,
  with a plain-English summary of what is wrong before any JSON.
- `tonight_results()` — what the night produced.
- `last_image()` / `stacked_preview()` — actual images, rendered inline.
- `imaging_targets()` / `run_imaging_program()` — slew, centre and
  live-stack a target for the imaging half of the night.
- `sky_tour(action="preview" | "start" | "next" | "stop")` — a narrated,
  four-stop showcase of deep-sky objects. It advances one physical slew at a
  time, so someone can look and talk about each stop before continuing.

## Fleet integrity

`fleet_integrity_check` is the reason this exists. It runs the checks in
`cloud/integrity.py`, each corresponding to a bug class that reached production
and was found only because someone happened to look:

| Check | Bug it re-detects |
|---|---|
| `orphaned_node` | a node with observations but no owner — 319fded, 0c4bb87 |
| `dangling_membership` | a member linked to a node that no longer exists |
| `missing_credentials` | a node row with no usable api_key — 5d926a1, 9280ba7 |
| `stale_vacation` | status lagging the calendar — 9421bbd, 2cbc6a1 |
| `heartbeat_gap` | a silently dead heartbeat thread — 0c4bb87 |
| `ghost_registration` | registered, never checked in |
| `duplicate_link` | one telescope claimed by several accounts |

Read-only and safe to run on any schedule, including against production.

## The loop

Detection runs unattended; merging does not.

**`python -m telescope_mcp.patrol`** runs the integrity sweep and renders a
report carrying, for each finding, what broke, what it means, where the cause
lives, and the test that covers it. `.github/workflows/fleet-patrol.yml` runs it
daily at 09:00 UTC — after every longitude has finished its night — and opens or
updates a single issue. One issue is reused: a fleet problem that persists for a
week is one problem, not seven. It closes itself when the fleet comes clean.

The patrol deliberately does not write the fix. Writing it needs judgement; what
this guarantees is that whoever writes it starts from evidence rather than from
a guess, which is the entire reason the loop is worth having.

The patrol authenticates with a **read-only** admin credential
(`CLOUD_ADMIN_READONLY_KEY`), not the full one. `require_admin_readonly` in
`cloud/server.py` accepts it on the integrity endpoint and nowhere else, so the
credential sitting in a CI secret store cannot replan the network, roll back
tuning weights, or mark AAVSO batches submitted. An unset key never
authenticates an empty header — otherwise a mis-piped CI secret would silently
become admin access.

**`scripts/merge_policy.py`** decides what may land without a person reading it.
It is deliberately dumb — a path policy, not a judgement about the change, since
a gate that can be argued out of its own rules is not a gate. Three areas always
need a human:

| Area | What a bad unattended merge does |
|---|---|
| mount, camera, enclosure, safety manager | points a telescope somewhere it should not go |
| photometry, timing, plate solving, calibration | corrupts a scientific record published under one obscode |
| identity, credentials, schema | silently orphans a node and loses its history |

Everything else — app screens, docs, the MCP surface, tests — lands on green CI.
`scripts/merge_policy.py` and `.github/workflows/*` are themselves protected, so
the loop cannot rewrite its own gate.

`.github/workflows/merge-policy.yml` enforces it, and blocks a merge in exactly
one case: a PR carrying the `agent-auto-merge` label that touches a protected
path. A PR without that label is only reported on — a human is already reading
it, which is the point.

```bash
make patrol
make merge-policy
```

## What is deliberately missing

Account creation, and `pushPairCredentials`. Both would put a password or a
live `api_key` into a chat transcript. They stay in the app; see `NOT_EXPOSED`
in `tests/test_mcp_parity.py`.

The desktop app also keeps things this cannot carry: light-curve narration
through `flutter_tts` (`app/lib/screens/target_detail_screen.dart:114`), the
`Semantics` layer, push notifications and the self-updater. For an accessible
astronomy project those are the product, not a presentation layer.
