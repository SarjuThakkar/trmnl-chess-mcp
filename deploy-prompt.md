# Prompt for Claude Code — deploy the voice chess server

I have a working `chess_mcp_server.py` in this directory. It's an MCP server
that lets my Pebble Index ring play chess by voice against Maia, with the board
rendered on a TRMNL e-ink display. Get it deployed to Railway.

Read the file first. Don't rewrite the game logic — the move parser and level
handling are tested and I like them. You're doing packaging, deployment, and
walking me through setup.

## Architecture, so you know what you're wiring

Ring → Pebble's cloud agent (their infra, their LLM) → **this server** over
Streamable HTTP with a static bearer token → lc0 running Maia weights → board
image URL pushed to TRMNL. Pebble never sees the board; my server does all
validation. TRMNL is a passive renderer.

## Part 1: containerize

The tricky dependency is **lc0** — Maia is neural network weights, not a
standalone engine, so the image needs the lc0 binary plus the `.pb.gz` nets.

- Write a Dockerfile. Python 3.12 base. Install lc0 **0.26.3 or later**;
  older builds can't read Maia's network format. Prefer a distro package if
  one exists for the base image, otherwise build from source and tell me how
  long that adds to the build.
- Download `maia-1100.pb.gz` through `maia-1900.pb.gz` from
  `github.com/CSSLab/maia-chess` (the `maia_weights` directory) into
  `/app/maia_weights` at build time, not runtime.
  **Watch out:** GitHub's web UI sometimes auto-decompresses `.pb.gz` files,
  which breaks the filenames the server expects. Use `curl` against the raw
  URLs and verify each file is actually gzip (`file` should say gzip
  compressed) before the build succeeds. Fail loudly if not.
- CPU-only lc0 is fine. This does one evaluation per move at nodes=1, so
  there's nothing to accelerate.
- `requirements.txt`: fastmcp, chess, httpx, uvicorn.
- Add a smoke test that runs in the container: load a starting position, ask
  lc0 for a move with the 1500 net at nodes=1, assert it returns something
  legal. I want the build to fail if the engine is broken, not the first
  voice command.

## Part 2: state persistence

`game.json` currently sits on local disk, which Railway wipes on redeploy.
Attach a Railway volume and point `STATE_FILE` into it. If you think a volume
is overkill for one small JSON file, propose the alternative and let me pick.

## Part 3: deploy

Railway. Confirm the deployed service gets public HTTPS — Pebble won't connect
otherwise. Show me the final `https://.../mcp` URL.

## Part 4: walk me through what you need from me

**Do not guess at or invent any of these. Stop and ask.** Ask for one at a
time, in this order, and tell me exactly where to click:

1. `MCP_BEARER_TOKEN` — you generate this with `openssl rand -hex 32`. Show me
   the value so I can paste it into the Pebble app later. Never commit it.
2. `TRMNL_PLUGIN_UUID` — I need to create a Private Plugin on TRMNL first, set
   its strategy to **Webhook**, and paste in the Liquid markup. Walk me through
   that, then tell me where on the page to find the UUID.
3. Railway account and project — tell me whether to create the project in the
   dashboard or via CLI, and which is less painful.
4. `ENGINE_LEVEL` — ask me what rating I want to start at, 1100–1900. Mention
   that 1900 is the maia9 bot on Lichess.

Set `ENGINE_KIND=maia`, `MAIA_WEIGHTS_DIR=/app/maia_weights`, and
`LC0_PATH` to wherever lc0 lands in the image. Those are yours to determine,
not mine.

## Part 5: verify before declaring victory

In this order, and show me the output of each:

1. `curl` the `/healthz` endpoint — should return 200 with no auth header
2. `curl` the `/mcp` endpoint with no auth — must be 401
3. `curl` with the bearer token — must not be 401
4. MCP Inspector against the deployed URL, listing all four tools:
   `make_move`, `board_state`, `new_game`, `set_level`
5. Call `new_game` through Inspector, then confirm with me that the starting
   position actually appeared on my TRMNL before you move on

Step 5 is the real test. Everything before it can pass while the display stays
blank.

## Then hand me the last mile

Tell me exactly what to enter in the Pebble app: the MCP server URL, the
`Authorization: Bearer <token>` header, and that it should go in a sandbox
group mapped to **double-click** so single-click stays on the normal offline
note agent.

## How I want you to work

Ask before assuming. If a step needs something only I can do — clicking through
a dashboard, pasting a token — stop and wait rather than working around it.
Show me real command output rather than telling me it worked. If lc0 or the
weights turn out to be a problem in Railway's build environment, say so early
rather than fighting it for twenty minutes.

One known constraint to respect: TRMNL rate limits webhook pushes to once per
five minutes and returns 429 above that. The server already handles this by
dropping pushes inside the window. Don't "fix" it with a retry loop.
