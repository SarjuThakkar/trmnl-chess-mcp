"""
Voice chess: Pebble Index -> MCP -> chess engine -> TRMNL e-ink display.

The ring speaks a move, this server validates it against the legal move list,
the engine replies, and the new position is pushed to TRMNL. Both lc0/Maia
(human-like play, rating 1100-1900) and Stockfish (skill 0-20) ship in the
container; ENGINE_KIND picks which one a fresh game starts on, and the
`set_engine` tool switches mid-game by voice ("set engine to stockfish").
`new_game` takes an optional color ("new game as black"); the board image
orientation always follows whichever color the player is, and if they're
black the engine plays its opening move before the position is ever pushed.

  pip install fastmcp chess httpx

  export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
  export TRMNL_PLUGIN_UUID=...          # from your private plugin page

  # Maia (default): needs lc0 + maia-*.pb.gz weights (see Dockerfile)
  export LC0_PATH=/usr/local/bin/lc0
  export MAIA_WEIGHTS_DIR=/app/maia_weights

  # Or Stockfish: brew install stockfish / apt install stockfish
  export ENGINE_KIND=stockfish
  export STOCKFISH_PATH=/opt/homebrew/bin/stockfish

  python chess_mcp_server.py
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from urllib.parse import urlencode
from pathlib import Path

import chess
import chess.engine
import httpx
from fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chess_mcp")

BEARER = os.environ["MCP_BEARER_TOKEN"]
PLUGIN_UUID = os.environ["TRMNL_PLUGIN_UUID"]
ENGINE_KIND = os.environ.get("ENGINE_KIND", "maia").lower()  # maia | stockfish
STOCKFISH = os.environ.get("STOCKFISH_PATH", "stockfish")
LC0 = os.environ.get("LC0_PATH", "lc0")
MAIA_WEIGHTS = Path(os.environ.get("MAIA_WEIGHTS_DIR", "./maia_weights"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "game.json"))
BOARD_SERVICE = os.environ.get(
    "BOARD_SERVICE", "https://backscattering.de/web-boardimage/board.svg"
)

# Spoken difficulty words -> engine level. Transcription mangles digits
# ("level ten" vs "level 10"), so words are the safer primary path.
#
# Stockfish: 0-20 skill slider. Maia: a rating-specific network, 1100-1900.
SF_LEVELS = {
    "beginner": 0, "easiest": 0, "baby": 0,
    "easy": 2, "casual": 2, "gentle": 2,
    "club": 5, "normal": 5, "medium": 5, "default": 5,
    "intermediate": 8, "harder": 8,
    "hard": 12, "strong": 12, "tough": 12,
    "expert": 16, "brutal": 18,
    "max": 20, "maximum": 20, "hardest": 20, "full": 20,
}
MAIA_LEVELS = {
    "beginner": 1100, "easiest": 1100, "baby": 1100,
    "easy": 1200, "casual": 1300, "gentle": 1200,
    "club": 1500, "normal": 1500, "medium": 1500, "default": 1500,
    "intermediate": 1600,
    "hard": 1700, "strong": 1800, "tough": 1700,
    "expert": 1900, "brutal": 1900,
    "max": 1900, "maximum": 1900, "hardest": 1900, "full": 1900,
}
# Spoken piece names -> python-chess piece type, for parse_move's word
# substitution pass (handles transcription artifacts like "night" for "knight").
PIECES = {
    "knight": chess.KNIGHT, "knights": chess.KNIGHT, "night": chess.KNIGHT,
    "bishop": chess.BISHOP, "bishops": chess.BISHOP,
    "rook": chess.ROOK, "rooks": chess.ROOK,
    "queen": chess.QUEEN, "queens": chess.QUEEN,
    "king": chess.KING, "kings": chess.KING,
    "pawn": chess.PAWN, "pawns": chess.PAWN,
}

# The engine kind is a per-game setting (state["engine"]), not a fixed
# startup constant, so `set_engine` can switch it mid-game. ENGINE_KIND (the
# env var) only supplies the *initial* engine for a fresh game.
DEFAULT_LEVEL = int(
    os.environ.get("ENGINE_LEVEL", "1500" if ENGINE_KIND == "maia" else "5")
)
_STATIC_DEFAULT_LEVEL = {"maia": 1500, "stockfish": 5}


def _levels_for(engine_kind: str) -> dict:
    return MAIA_LEVELS if engine_kind == "maia" else SF_LEVELS


def _bounds_for(engine_kind: str) -> tuple[int, int, int]:
    """(floor, ceil, step) -- step is one nudge of "harder"/"easier"."""
    return (1100, 1900, 100) if engine_kind == "maia" else (0, 20, 4)


def default_level_for(engine_kind: str) -> int:
    """Starting difficulty for a (possibly just-switched-to) engine kind.

    Honors ENGINE_LEVEL for whichever engine the server booted with; a
    mid-game switch to the *other* engine falls back to that engine's own
    sane default rather than reusing a rating/skill number that means
    something different on the other scale.
    """
    if engine_kind == ENGINE_KIND:
        return DEFAULT_LEVEL
    return _STATIC_DEFAULT_LEVEL[engine_kind]


def engine_label(engine_kind: str) -> str:
    return "Maia" if engine_kind == "maia" else "Stockfish"


def parse_level(text: str, engine_kind: str) -> int:
    """Resolve spoken difficulty to an engine level."""
    t = text.lower().strip()
    levels = _levels_for(engine_kind)
    floor, ceil, _ = _bounds_for(engine_kind)
    for word, val in levels.items():
        if re.search(rf"\b{word}\b", t):
            return val

    if engine_kind == "maia":
        # "maia nine" / "maia 9" -> the 1900 net, matching Lichess bot names.
        short = re.search(r"maia\s*(\d)\b", t)
        if short and 1 <= int(short.group(1)) <= 9:
            return 1000 + int(short.group(1)) * 100
        rating = re.search(r"\b(1[1-9]\d{2})\b", t)
        if rating:
            return max(floor, min(ceil, round(int(rating.group(1)) / 100) * 100))
    else:
        digits = re.search(r"\b(\d{1,2})\b", t)
        if digits and floor <= int(digits.group(1)) <= ceil:
            return int(digits.group(1))

    words = ", ".join(sorted(set(levels)))
    raise ValueError(f"Didn't catch that level. Try one of: {words}.")


def level_name(level: int, engine_kind: str) -> str:
    """Human label for the current difficulty, for speech and the display."""
    if engine_kind == "maia":
        return str(level)
    for name, val in [
        ("beginner", 0), ("easy", 2), ("club", 5), ("intermediate", 8),
        ("hard", 12), ("expert", 16), ("max", 20),
    ]:
        if level <= val:
            return f"{name} ({level})"
    return f"max ({level})"


async def engine_move(board: chess.Board, level: int, engine_kind: str) -> chess.Move:
    """Ask the configured engine for a move.

    Maia is a set of Leela weights rather than a standalone binary, and must
    run at nodes=1 — that disables tree search so the move comes purely from
    the network's policy, which is what makes it blunder like a human of that
    rating instead of like a weakened engine.
    """
    if engine_kind == "maia":
        weights = MAIA_WEIGHTS / f"maia-{level}.pb.gz"
        if not weights.exists():
            raise FileNotFoundError(f"Missing weights: {weights}")
        cmd = [LC0, f"--weights={weights}"]
        limit = chess.engine.Limit(nodes=1)
    else:
        cmd = [STOCKFISH]
        limit = chess.engine.Limit(time=0.5)

    transport, engine = await chess.engine.popen_uci(cmd)
    try:
        if engine_kind != "maia":
            await engine.configure({"Skill Level": level})
        result = await engine.play(board, limit)
        return result.move
    finally:
        await engine.quit()


_lock = asyncio.Lock()
_last_push = 0.0
# Tracks a deferred TRMNL push (see push() below). While this is set and not
# yet done, make_move refuses new moves -- otherwise a player could keep
# moving faster than TRMNL's 5-minute webhook limit lets the display catch
# up, and would be playing a game they can no longer see on the board.
_pending_push: asyncio.Task | None = None
_pending_push_eta = 0.0
# Pebble opens one MCP session per double-click (confirmed live: a fresh
# "Created new transport with session ID" log line per turn). Caught in
# production: the agent called make_move twice within a single session,
# unprompted, to "helpfully" play an obvious-looking recapture after its
# own reply captured the player's queen -- an entirely different failure
# from the earlier retry-after-error chaining, since both calls succeeded.
# One real move per session, enforced server-side, is a hard guarantee the
# docstring alone can't provide.
_moved_sessions: set[str] = set()


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "fen": chess.STARTING_FEN,
        "history": [],
        "status": "Your move.",
        "engine": ENGINE_KIND,
        "skill": DEFAULT_LEVEL,
        "player_color": "white",
    }


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# voice-tolerant move parsing
# --------------------------------------------------------------------------

# piece? origin-file? origin-rank? capture? target-square (promo)? check/mate?
# e.g. "rdf8" -> rook, origin file d, target f8. "exd5" -> pawn, origin file
# e, capture, target d5. Case-insensitive by construction (caller lowercases
# and strips whitespace first) -- python-chess's own parse_san won't accept
# any of this, since real SAN uses case itself to distinguish a piece letter
# from a file letter and rejects a stray "P" for pawn moves entirely.
_COMPACT_MOVE_RE = re.compile(
    r"^(?P<piece>[pnbrqk])?"
    r"(?P<origin_file>[a-h])?(?P<origin_rank>[1-8])?"
    r"(?P<capture>x)?"
    r"(?P<target>[a-h][1-8])"
    r"(?:=(?P<promo>[nbrqk]))?"
    r"[+#]?$"
)


def _try_compact_move(board: chess.Board, cleaned: str, raw: str) -> chess.Move | None:
    """Match a glued, case-insensitive token like "rdf8" or "exd5" directly
    against the real legal move list.

    Returns None if `cleaned` doesn't even look like compact notation, so
    the caller can fall back to looser phrase matching. If it *does* match
    syntactically, this is authoritative and raises ValueError itself on 0
    or >1 candidates (with the candidate list properly filtered by the
    piece/disambiguation actually given) rather than returning None, so an
    ambiguous compact move doesn't fall through to the much looser filter
    below and lose the disambiguation info the caller already provided.
    """
    m = _COMPACT_MOVE_RE.match(cleaned)
    if not m:
        return None

    piece_type = (
        chess.PIECE_SYMBOLS.index(m.group("piece")) if m.group("piece") else chess.PAWN
    )
    origin_file = m.group("origin_file")
    origin_rank = m.group("origin_rank")
    target = m.group("target")
    want_capture = bool(m.group("capture"))
    promo = chess.PIECE_SYMBOLS.index(m.group("promo")) if m.group("promo") else None

    candidates = [
        mv
        for mv in board.legal_moves
        if chess.square_name(mv.to_square) == target
        and board.piece_at(mv.from_square).piece_type == piece_type
        and (not origin_file or chess.square_name(mv.from_square)[0] == origin_file)
        and (not origin_rank or chess.square_name(mv.from_square)[1] == origin_rank)
        and (not want_capture or board.is_capture(mv))
        and (promo is None or mv.promotion == promo)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"No legal move matches '{raw}'.")
    opts = ", ".join(board.san(c) for c in candidates)
    raise ValueError(f"'{raw}' is ambiguous — did you mean {opts}?")


def parse_move(board: chess.Board, text: str) -> chess.Move:
    """Resolve loose spoken text to exactly one legal move.

    Raises ValueError if nothing matches or if the phrasing is ambiguous.
    Never guesses: an ambiguous utterance is an error, not a coin flip.
    """
    raw = text.strip()

    # 1. Exact notation, if the transcription happened to be clean.
    for parser in (board.parse_san, chess.Move.from_uci):
        try:
            mv = parser(raw.replace(" ", ""))
            if mv in board.legal_moves:
                return mv
        except Exception:
            pass

    # 2. Normalise speech artifacts: "night to F3", "e 4", "takes on d5"
    t = raw.lower()
    t = t.replace("-", " ").replace(".", " ").replace(",", " ")
    t = re.sub(r"\b(to|on|at|in|the|please|move|go|and|file|rank)\b", " ", t)
    t = re.sub(r"\btakes?\b|\bcaptures?\b|\bx\b", " capture ", t)

    if "castle" in t or "castling" in t:
        side = "O-O-O" if ("queen" in t or "long" in t) else "O-O"
        try:
            mv = board.parse_san(side)
            if mv in board.legal_moves:
                return mv
        except Exception:
            raise ValueError(f"Can't castle that way right now.")

    for word, piece in PIECES.items():
        t = re.sub(rf"\b{word}\b", chess.piece_symbol(piece).upper(), t)

    # 2.5 Compact/disambiguated notation, case-insensitive: "Rdf8", "RDF8",
    # "rdf8" (rook on the d-file to f8), "exd5", etc. Handles what the loose
    # filter below structurally can't -- a bare file or rank used only to
    # disambiguate ("d" meaning "the d-file rook"), not as a full square.
    # python-chess's own parse_san (step 1) is case-sensitive and rejects
    # this outright; this collapses whitespace and matches directly against
    # the legal move list instead of delegating to it.
    mv = _try_compact_move(board, re.sub(r"\s+", "", t).lower(), raw)
    if mv is not None:
        return mv

    squares = re.findall(r"\b([a-h])\s*([1-8])\b", t)
    target = f"{squares[-1][0]}{squares[-1][1]}" if squares else None
    origin = f"{squares[0][0]}{squares[0][1]}" if len(squares) > 1 else None

    piece_type = None
    for sym in "PNBRQK":
        if re.search(rf"\b{sym}\b", t.upper()):
            piece_type = chess.PIECE_SYMBOLS.index(sym.lower())
            break

    # 3. Filter the legal move list. Small set, so this is cheap and safe.
    candidates = []
    for mv in board.legal_moves:
        if target and chess.square_name(mv.to_square) != target:
            continue
        if origin and chess.square_name(mv.from_square) != origin:
            continue
        if piece_type and board.piece_at(mv.from_square).piece_type != piece_type:
            continue
        if "capture" in t and not board.is_capture(mv):
            continue
        candidates.append(mv)

    if not candidates and (target or piece_type):
        raise ValueError(f"No legal move matches '{raw}'.")
    if len(candidates) > 1:
        opts = ", ".join(board.san(m) for m in candidates)
        raise ValueError(f"'{raw}' is ambiguous — did you mean {opts}?")
    if not candidates:
        raise ValueError(f"Couldn't understand '{raw}'.")
    return candidates[0]


# --------------------------------------------------------------------------
# rendering + push
# --------------------------------------------------------------------------

def board_image_url(board: chess.Board, last_uci: str | None, player_color: str) -> str:
    """Build a board image URL. The renderer is a hosted service, so TRMNL
    just fetches an <img> — no CSS board, no dithering headaches.

    Oriented to the player's own color (verified live: orientation=black
    actually flips the rendered board, not just a label) so their pieces
    are always at the bottom, matching how they'd expect to see the board
    regardless of which side they're playing.
    """
    params = {
        "fen": board.board_fen(),
        "orientation": player_color,
        "coordinates": "true",
        # High-contrast theme; the brown/blue themes muddy on 1-bit e-ink.
        "colors": "wikipedia",
        "size": "600",
    }
    if last_uci:
        params["lastMove"] = last_uci
    if board.is_check():
        king_sq = board.king(board.turn)
        if king_sq is not None:
            params["check"] = chess.square_name(king_sq)
    return f"{BOARD_SERVICE}?{urlencode(params)}"


PUSH_INTERVAL = 300  # TRMNL's own webhook rate limit, in seconds

_PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
# Ascending value, matching the usual Lichess/Chess.com captured-piece tray order.
_DISPLAY_ORDER = [chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN]
# A captured piece is shown with ITS OWN color's glyph (a captured black
# knight reads as u266E in white's tray), not the capturing side's color.
_GLYPH = {
    (chess.PAWN, chess.WHITE): "♙", (chess.PAWN, chess.BLACK): "♟",
    (chess.KNIGHT, chess.WHITE): "♘", (chess.KNIGHT, chess.BLACK): "♞",
    (chess.BISHOP, chess.WHITE): "♗", (chess.BISHOP, chess.BLACK): "♝",
    (chess.ROOK, chess.WHITE): "♖", (chess.ROOK, chess.BLACK): "♜",
    (chess.QUEEN, chess.WHITE): "♕", (chess.QUEEN, chess.BLACK): "♛",
}


def _captured_pieces(history: list[str]) -> tuple[str, str, str, str]:
    """Replay the game from the start to find exactly which pieces were
    captured and by whom, and the resulting material balance.

    Diffing current board piece-counts against the standard starting set
    looks plausible but silently breaks around pawn promotion -- a
    promoted pawn would read as "captured" even though it's still on the
    board as a queen. Replaying real move history sidesteps that
    entirely (a captured piece is recorded at the moment it's actually
    captured), and is cheap even for a long game -- a few dozen moves,
    microseconds each.

    Returns (white_tray, black_tray, white_edge, black_edge): the tray
    strings are the opponent's captured pieces rendered as Unicode chess
    glyphs in ascending value order; the edge strings are the material
    advantage ("+N") shown only next to whichever side is ahead, empty
    otherwise.
    """
    captured_by_white: list[int] = []  # piece types of black pieces white has taken
    captured_by_black: list[int] = []  # piece types of white pieces black has taken
    b = chess.Board()
    for san in history:
        mv = b.parse_san(san)
        if b.is_capture(mv):
            if b.is_en_passant(mv):
                square = mv.to_square + (-8 if b.turn == chess.WHITE else 8)
            else:
                square = mv.to_square
            captured_piece = b.piece_at(square)
            target = captured_by_white if captured_piece.color == chess.BLACK else captured_by_black
            target.append(captured_piece.piece_type)
        b.push(mv)

    def tray(pieces: list[int], color: bool) -> str:
        return "".join(_GLYPH[(pt, color)] for pt in _DISPLAY_ORDER for _ in range(pieces.count(pt)))

    white_value = sum(_PIECE_VALUES[pt] for pt in captured_by_white)
    black_value = sum(_PIECE_VALUES[pt] for pt in captured_by_black)
    diff = white_value - black_value

    return (
        tray(captured_by_white, chess.BLACK),
        tray(captured_by_black, chess.WHITE),
        f"+{diff}" if diff > 0 else "",
        f"+{-diff}" if diff < 0 else "",
    )


def _build_payload(state: dict) -> dict:
    board = chess.Board(state["fen"])
    engine_kind = state.get("engine", ENGINE_KIND)
    skill = state.get("skill", DEFAULT_LEVEL)
    player_color = state.get("player_color", "white")
    white_tray, black_tray, white_edge, black_edge = _captured_pieces(state["history"])
    return {
        "merge_variables": {
            "image_url": board_image_url(board, state.get("last_uci"), player_color),
            "status": state["status"],
            "last_move": state["history"][-1] if state["history"] else "—",
            "move_number": board.fullmove_number,
            "turn": "White" if board.turn else "Black",
            "player_color": player_color.capitalize(),
            "history": state["history"][-6:],
            "engine": engine_label(engine_kind),
            "level": level_name(skill, engine_kind),
            "white_captured": white_tray,
            "black_captured": black_tray,
            "white_edge": white_edge,
            "black_edge": black_edge,
        }
    }


async def _post_to_trmnl(payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://usetrmnl.com/api/custom_plugins/{PLUGIN_UUID}",
            json=payload,
        )
        r.raise_for_status()
        return r


async def _deferred_push(delay: float) -> None:
    """Wait out the rate-limit window, then push whatever the *current* state
    is at that point -- not a stale snapshot from when this was scheduled, so
    several moves inside one window still end up as a single push of the
    latest position rather than a burst of queued sends. This is a single
    scheduled retry, not a retry loop: on failure it just gives up and logs,
    it doesn't reschedule itself again.
    """
    global _last_push, _pending_push
    try:
        await asyncio.sleep(delay)
        await _post_to_trmnl(_build_payload(load()))
        _last_push = time.time()
        logger.info("push: deferred push sent")
    except Exception:
        logger.exception("push: deferred push failed, giving up")
    finally:
        _pending_push = None


async def push(state: dict) -> None:
    """POST merge variables to TRMNL, honoring its 5-minute webhook rate
    limit. Inside the window (or if TRMNL 429s anyway -- our own tracker can
    fall out of sync with theirs across a restart), this schedules exactly
    one deferred push for when the window clears, tracked in _pending_push
    so make_move can refuse new moves until the board is confirmed caught up.
    Any other failure (bad UUID, network error, TRMNL outage) is logged and
    swallowed without blocking play -- those won't resolve on a timer, so
    there's nothing to wait out.
    """
    global _last_push, _pending_push, _pending_push_eta
    elapsed = time.time() - _last_push
    if elapsed < PUSH_INTERVAL:
        if _pending_push is None or _pending_push.done():
            delay = PUSH_INTERVAL - elapsed
            _pending_push_eta = time.time() + delay
            _pending_push = asyncio.create_task(_deferred_push(delay))
            logger.info("push: inside rate-limit window, deferred by %.0fs", delay)
        return

    try:
        await _post_to_trmnl(_build_payload(state))
        _last_push = time.time()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429 and (_pending_push is None or _pending_push.done()):
            delay = float(e.response.headers.get("retry-after", PUSH_INTERVAL))
            _pending_push_eta = time.time() + delay
            _pending_push = asyncio.create_task(_deferred_push(delay))
            logger.warning("push: TRMNL 429'd despite our tracker; deferred by %.0fs", delay)
        elif e.response.status_code != 429:
            logger.exception("push: TRMNL push failed")
    except Exception:
        logger.exception("push: TRMNL push failed")


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

mcp = FastMCP("chess")


@mcp.tool()
async def make_move(move: str, ctx: Context) -> str:
    """Play a chess move on the board shown on the TRMNL display.

    This server understands both loose natural language ("knight to f3",
    "take the bishop", "castle kingside") and compact algebraic notation
    ("Nf3", "Bxe6", "Rdf8") -- case doesn't matter for the latter. If the
    user's own words already fully specify a move -- piece, any needed
    disambiguation, and destination, e.g. "rook on the d-file to f8" -- you
    MAY write it as compact notation ("Rdf8") instead of the raw phrasing;
    that is formatting a move the user already fully specified, not
    guessing, and this server's exact-notation matching is more reliable
    for disambiguated moves than its loose phrase matching. What you must
    NOT do is invent information the user didn't give you: if they only
    said "rook to f8" and two rooks could both go there, pass that through
    as-is and let this tool ask which one -- don't pick one yourself.

    IMPORTANT: if the result describes an error (couldn't parse, ambiguous,
    board not yet refreshed, game over) -- speak that back to the user
    VERBATIM and stop there. Do not call this tool again with a different
    guess, and do not call another tool to investigate. An ambiguous-move
    error already lists the real options as a question for the user to
    answer out loud next turn; picking one for them yourself is exactly the
    guess this instruction tells you not to make.

    Args:
        move: What the user said, e.g. "knight to f3", "e4", "take the
              bishop", "castle kingside", or compact notation like "Rdf8"
              if you're confident it captures everything the user said.

    Returns a short sentence describing your move and the engine's reply.
    """
    logger.info("make_move: received %r", move)
    session_id = ctx.session_id
    if session_id in _moved_sessions:
        logger.warning("make_move: refused second call in session %s", session_id)
        return "Only one move per turn -- double-click again to make your next move."
    _moved_sessions.add(session_id)
    if _pending_push is not None and not _pending_push.done():
        wait_s = max(0, round(_pending_push_eta - time.time()))
        logger.info("make_move: rejected, board not yet refreshed (%ds left)", wait_s)
        return (
            "Hold on — the board hasn't updated on the display yet. TRMNL "
            f"only refreshes once every 5 minutes; try again in about "
            f"{wait_s} seconds so you can actually see the position first."
        )
    async with _lock:
        state = load()
        board = chess.Board(state["fen"])
        if board.is_game_over():
            return f"Game is over: {board.result()}. Start a new one?"

        try:
            mv = parse_move(board, move)
        except ValueError as e:
            logger.info("make_move: couldn't parse %r: %s", move, e)
            return str(e)

        san = board.san(mv)
        state["last_uci"] = mv.uci()
        board.push(mv)
        state["history"].append(san)

        reply_san = None
        if not board.is_game_over():
            reply = await engine_move(
                board,
                state.get("skill", DEFAULT_LEVEL),
                state.get("engine", ENGINE_KIND),
            )
            reply_san = board.san(reply)
            state["last_uci"] = reply.uci()
            board.push(reply)
            state["history"].append(reply_san)

        if board.is_checkmate():
            state["status"] = "Checkmate."
        elif board.is_check():
            state["status"] = "Check."
        elif board.is_game_over():
            state["status"] = f"Draw ({board.result()})."
        else:
            state["status"] = "Your move."

        state["fen"] = board.fen()
        save(state)
        await push(state)

        out = f"You played {san}."
        if reply_san:
            out += f" I played {reply_san}."
        logger.info("make_move: %s -> %s", san, out)
        return f"{out} {state['status']}"


@mcp.tool()
async def board_state() -> str:
    """Describe the current position out loud: whose turn, last moves, status."""
    logger.info("board_state: called")
    state = load()
    board = chess.Board(state["fen"])
    recent = ", ".join(state["history"][-4:]) or "no moves yet"
    player_color = state.get("player_color", "white")
    return (
        f"You're playing {player_color}. "
        f"{'White' if board.turn else 'Black'} to move, "
        f"move {board.fullmove_number}. Recent: {recent}. {state['status']}"
    )


@mcp.tool()
async def new_game(level: str = "", color: str = "") -> str:
    """Start a fresh game, optionally setting the engine difficulty and which
    color you play.

    Call this when the user says anything like "new game", "start over",
    "reset the board", "new game as black", "let's play white this time".

    If the result describes an error (bad level), speak it back verbatim
    and stop -- don't retry with a guessed correction.

    Args:
        level: Optional difficulty in the user's own words, e.g. "easy",
               "club", "hard", "max", or a number 0-20. Leave empty to keep
               the current setting. Pass the user's phrasing verbatim.
        color: Optional -- "white" or "black" to choose which side the user
               plays. Leave empty for a random color each game. Pass the
               user's phrasing verbatim, e.g. "black" or "as white".
    """
    logger.info("new_game: level=%r color=%r", level, color)
    async with _lock:
        prior = load()
        engine_kind = prior.get("engine", ENGINE_KIND)
        skill = prior.get("skill", DEFAULT_LEVEL)
        if level.strip():
            try:
                skill = parse_level(level, engine_kind)
            except ValueError as e:
                return str(e)

        c = color.lower().strip()
        if re.search(r"\bblack\b", c):
            player_color = "black"
        elif re.search(r"\bwhite\b", c):
            player_color = "white"
        else:
            player_color = random.choice(["white", "black"])

        board = chess.Board()
        history: list[str] = []
        last_uci = None
        opener_san = None
        if player_color == "black":
            # Player is black -- the engine plays white and moves first, so
            # the position pushed to TRMNL already reflects whose turn it
            # actually is instead of showing an untouched board the player
            # can't act on.
            mv = await engine_move(board, skill, engine_kind)
            opener_san = board.san(mv)
            board.push(mv)
            history.append(opener_san)
            last_uci = mv.uci()

        state = {
            "fen": board.fen(),
            "history": history,
            "last_uci": last_uci,
            "status": "Your move.",
            "engine": engine_kind,
            "skill": skill,
            "player_color": player_color,
        }
        save(state)
        global _last_push
        _last_push = 0  # a new game always earns an immediate refresh
        await push(state)

    msg = (
        f"New game ({engine_label(engine_kind)}) at "
        f"{level_name(skill, engine_kind)}. You're {player_color}."
    )
    if opener_san:
        msg += f" I played {opener_san} as white."
    msg += " Your move."
    return msg


@mcp.tool()
async def set_level(level: str) -> str:
    """Change the engine's difficulty without disturbing the game in progress.

    Call this for "make it harder", "set difficulty to easy", "play stronger",
    and similar. Takes effect on the engine's next move.

    If the result describes an error (didn't catch that level), speak it
    back verbatim and stop -- don't retry with a guessed correction.

    Args:
        level: The user's phrasing, e.g. "easy", "club", "brutal", "max", a
               number 0-20 (Stockfish) or 1100-1900 (Maia). Relative phrasing
               like "harder" or "easier" also works. Pass it through verbatim.
    """
    logger.info("set_level: level=%r", level)
    async with _lock:
        state = load()
        engine_kind = state.get("engine", ENGINE_KIND)
        current = state.get("skill", DEFAULT_LEVEL)
        levels = _levels_for(engine_kind)
        floor, ceil, step = _bounds_for(engine_kind)
        t = level.lower()

        # Relative adjustments, since "make it harder" carries no absolute value.
        if re.search(r"\bharder|stronger|tougher|up\b", t) and not any(
            w in t for w in levels
        ):
            skill = min(ceil, current + step)
        elif re.search(r"\beasier|weaker|gentler|down\b", t) and not any(
            w in t for w in levels
        ):
            skill = max(floor, current - step)
        else:
            try:
                skill = parse_level(level, engine_kind)
            except ValueError as e:
                return str(e)

        state["skill"] = skill
        save(state)
        await push(state)
    return f"Difficulty set to {level_name(skill, engine_kind)}. Game continues."


@mcp.tool()
async def set_engine(engine: str) -> str:
    """Switch which chess engine you're playing against, without resetting the game.

    Call this for "set engine to maia", "play against stockfish", "switch to
    maia", and similar. The difficulty resets to the new engine's own default
    (a Maia rating and a Stockfish skill level aren't the same scale, so a
    number that made sense for one wouldn't for the other).

    If the result describes an error (didn't catch that engine), speak it
    back verbatim and stop -- don't retry with a guessed correction.

    Args:
        engine: The user's phrasing, e.g. "maia", "maya" (common mishearing
                of "maia"), "stockfish", "stock fish". Pass it through verbatim.
    """
    logger.info("set_engine: engine=%r", engine)
    t = engine.lower().strip()
    if re.search(r"\bmaia\b|\bmaya\b", t):
        engine_kind = "maia"
    elif re.search(r"\bstock\s*fish\b", t):
        engine_kind = "stockfish"
    else:
        return 'Didn\'t catch that engine. Try "maia" or "stockfish".'

    async with _lock:
        state = load()
        if engine_kind == state.get("engine", ENGINE_KIND):
            return f"Already playing against {engine_label(engine_kind)}."

        state["engine"] = engine_kind
        state["skill"] = default_level_for(engine_kind)
        save(state)
        await push(state)
    return (
        f"Switched to {engine_label(engine_kind)} at "
        f"{level_name(state['skill'], engine_kind)}. Game continues."
    )


app = mcp.http_app(path="/mcp")


async def healthz(request):
    return JSONResponse({"status": "ok"})


app.router.routes.insert(0, Route("/healthz", healthz))


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.headers.get("authorization") != f"Bearer {BEARER}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app.add_middleware(BearerAuth)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
