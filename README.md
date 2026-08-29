# TRMNL Chess MCP server

Play chess by voice against a [Pebble Index](https://repebble.com/index) ring,
with the board rendered on a [TRMNL](https://usetrmnl.com/) e-ink display.
Pebble's cloud agent is the MCP client; this server validates every move,
runs the chess engine, and pushes the resulting board image to TRMNL. Pebble
never sees the board — this server does all the validation and rendering
coordination.

```
Ring -> Pebble's cloud agent -> this server (Streamable HTTP + bearer token)
                                       |
                                       v
                          lc0 (Maia weights) or Stockfish
                                       |
                                       v
                          board image URL -> TRMNL webhook
```

Four tools: `make_move`, `board_state`, `new_game`, `set_level`, plus
`set_engine` to switch between Maia and Stockfish mid-game.

## Engines

Both ship in the container, switchable by voice without resetting the game:

- **Maia** (default) — [lc0](https://github.com/LeelaChessZero/lc0) running
  Leela weights trained on human games at a specific rating (1100-1900).
  Runs at `nodes=1` (no tree search), so it blunders like a human of that
  rating rather than playing like a weakened engine. This is what makes it
  fun to actually beat.
- **Stockfish** — the classic engine, skill levels 0-20 via its own `Skill
  Level` UCI option.

Say "set engine to maia" or "set engine to stockfish" any time; the
difficulty resets to that engine's own default (a Maia rating and a
Stockfish skill number aren't the same scale). Say "set level to 1600" or
"set level to 8" to tune within whichever engine is active, or "make it
harder" / "make it easier" for a relative nudge.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `MCP_BEARER_TOKEN` | yes | Static token Pebble sends as `Authorization: Bearer <token>`. Generate with `openssl rand -hex 32`. |
| `TRMNL_PLUGIN_UUID` | yes | UUID of your TRMNL private plugin (Webhook strategy). Treat this like a secret — anyone with it can push arbitrary content to your display. |
| `ENGINE_KIND` | no | `maia` (default) or `stockfish`. Only sets the *starting* engine for a fresh game — `set_engine` switches it live. |
| `ENGINE_LEVEL` | no | Starting difficulty for `ENGINE_KIND`. Defaults to `1500` for Maia, `5` for Stockfish. |
| `STATE_FILE` | no | Where `game.json` lives. Point this at a mounted volume in production or state is lost on every redeploy. |
| `LC0_PATH`, `MAIA_WEIGHTS_DIR`, `STOCKFISH_PATH` | no | Set by the Dockerfile; only override for a non-container local run. |
| `PORT` | no | Set automatically by Railway/most hosts. Defaults to `8000` locally. |

## Running locally

The real dependency here is the engines, not Python — `chess_mcp_server.py`
needs a working `lc0` binary + Maia weights, or a `stockfish` binary, on
`PATH` or at the configured `*_PATH`. Building lc0 from source by hand is a
whole project on its own (see the Dockerfile), so unless you already have
these installed, running via Docker is the realistic path even for local
testing:

```bash
docker build -t chess-mcp .
docker run --rm -p 8000:8000 \
  -e MCP_BEARER_TOKEN=$(openssl rand -hex 32) \
  -e TRMNL_PLUGIN_UUID=your-uuid \
  -v "$(pwd)/data:/data" -e STATE_FILE=/data/game.json \
  chess-mcp
```

If you do have Stockfish installed locally (`brew install stockfish`) and
just want to test the move parser/game logic without Maia:

```bash
python3 -m venv .venv && source .venv/bin/activate   # needs Python 3.10+
pip install -r requirements.txt
export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
export TRMNL_PLUGIN_UUID=your-uuid
export ENGINE_KIND=stockfish
export STOCKFISH_PATH=/opt/homebrew/bin/stockfish
python chess_mcp_server.py
```

Endpoint at `/mcp`, health check at `/healthz` (no auth required).

## Deploying to Railway

```bash
railway login                                    # browser OAuth
railway init --name chess-mcp                    # first time only
railway add --service chess-mcp                  # creates the empty service

railway variable set "MCP_BEARER_TOKEN=$(openssl rand -hex 32)" --service chess-mcp --skip-deploys
railway variable set "TRMNL_PLUGIN_UUID=your-uuid" --service chess-mcp --skip-deploys
railway variable set "ENGINE_LEVEL=1500" --service chess-mcp --skip-deploys
railway variable set "STATE_FILE=/data/game.json" --service chess-mcp

railway up -c -y --service chess-mcp             # builds the Dockerfile
railway domain --service chess-mcp               # public HTTPS URL, real cert
```

**Volume**: `game.json` must survive redeploys, so mount a volume at `/data`
matching `STATE_FILE` above. **The `railway volume add` CLI command is
currently broken** (panics with a Rust `unwrap()` on None, reproduced on
CLI v5.44.1 and v5.45.7, both `--json` and interactive, with and without
`--environment`) — add it from the dashboard instead: open the service,
go to the **Volumes** tab, **Add Volume**, mount path `/data`. Railway
redeploys automatically once it's attached.

**Redeploying later** is just:

```bash
railway up -c -y --service chess-mcp
```

Python-only changes rebuild fast — Docker's layer cache skips recompiling
lc0 (the slow part) as long as the Dockerfile itself didn't change.
`railway logs --service chess-mcp` tails live logs.

## Configuring the Pebble app

- **Name**: alphanumeric + hyphens only, no spaces. A space in this field is
  a confirmed Pebble bug — the agent silently never calls the tool
  (`ListToolsRequest` succeeds, `CallToolRequest` never happens). `ChessMCP`
  or `chess-mcp` both work.
- **URL**: `https://<your-railway-domain>/mcp`
- **Transport**: **Streamable** (the dropdown is "SSE/Streamable" — pick
  Streamable, not SSE)
- **Authorization**: `Bearer <your MCP_BEARER_TOKEN>` — full string,
  including the `Bearer ` prefix

Custom MCP tools only run in Pebble's **double-click** recording mode;
single-click stays on Pebble's built-in offline agent. Assign this server to
whichever sandbox group your double-click uses.

## Setting up the TRMNL private plugin

1. TRMNL dashboard -> **Plugins** -> search **"Private Plugin"** -> **Add New**
2. Name it, set **Strategy** to **Webhook**, save
3. On the plugin's settings page, click **Edit Markup**, paste in
   `trmnl_markup.liquid` from this repo
4. Find the **Webhook URL** field (`https://usetrmnl.com/api/custom_plugins/<uuid>`)
   — the `<uuid>` is your `TRMNL_PLUGIN_UUID`

The markup shows the board, whose move it is, move history, and an
`{{ engine }} · {{ level }}` badge (e.g. "Maia · 1500") so you can see at a
glance what you're playing against without asking.

TRMNL rate-limits webhook pushes to once per 5 minutes and 429s above that.
The server already drops pushes inside that window rather than retrying —
don't "fix" that with a retry loop.

## Example phrases to try

Double-click the ring, then:

- **"New game."** Starts fresh at the current engine/level.
- **"Pawn to e4."** / **"knight to f3"** / **"e4"** — loose phrasing is fine,
  the server resolves it against the actual legal move list.
- **"Castle kingside."**
- **"Take the bishop."**
- **"Set engine to stockfish."** then **"set level to 8."**
- **"Set engine to maia."** then **"set level to 1700."**
- **"Make it harder."** / **"make it easier."** — relative nudge, no number needed.
- **"What's the position?"** — reads the board state back without a move.

## What's verified vs. assumed

- No Debian/Ubuntu package for lc0 exists (checked packages.debian.org
  directly) — it's compiled from source in the Dockerfile, pinned to release
  `v0.32.1`. The build-time smoke test (`smoke_test.py`) actually runs lc0
  against the Maia 1500 net and asserts a legal move comes back, so a broken
  engine fails the image build, not the first voice command.
- Maia weight files are fetched from
  `raw.githubusercontent.com/CSSLab/maia-chess/master/maia_weights/` and
  verified with `file` to actually be gzip before the build proceeds —
  GitHub's web UI is known to sometimes serve `.pb.gz` already decompressed,
  which breaks the filename lc0 expects.
- Stockfish comes from Debian's own `stockfish` package
  (`/usr/games/stockfish`), no build step needed.
- The original `chess_mcp_server.py` referenced a `PIECES` dict for parsing
  spoken piece names ("knight", "night" -> knight) that was never defined —
  fixed, since it would have thrown `NameError` on most non-exact-notation
  moves.
- The server's `/healthz` route was exempted from the bearer-auth
  middleware but never actually registered as a route, so it 404'd instead
  of returning 200 — fixed by registering it explicitly.

## Troubleshooting

**Pebble says "invalid tool call, action failed."** Check `railway logs
--service chess-mcp` — every tool call's arguments are visible there, and
engine/state errors are caught and returned as text rather than crashing.
The most common cause (seen on a sibling project): a space or special
character in the MCP server's **Name** field in the Pebble app.

**`railway volume add` crashes with a Rust panic.** Known broken CLI
command as of v5.44.1/v5.45.7 — add the volume from the Railway dashboard
instead (Volumes tab -> Add Volume -> mount path `/data`).

**The board didn't update on TRMNL after a move.** Check whether you're
inside the 5-minute rate-limit window (`_last_push` in
`chess_mcp_server.py`) — the server intentionally drops the push rather
than queuing or retrying. The next successful call outside the window will
carry the current position.

**A level/engine word wasn't understood.** Both `set_level` and `set_engine`
return a descriptive error string (visible wherever Pebble surfaces tool
results) instead of crashing, and `set_level`'s error lists every word it
does understand for the currently active engine.

## Verifying the bearer check

```bash
curl -i https://<your-railway-domain>/healthz    # should be 200, no auth needed
curl -i https://<your-railway-domain>/mcp         # should be 401

curl -i https://<your-railway-domain>/mcp \
  -H "Authorization: Bearer <your MCP_BEARER_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```
