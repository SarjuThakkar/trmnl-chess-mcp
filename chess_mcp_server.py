"""
Voice chess: Pebble Index -> MCP -> chess engine -> TRMNL e-ink display.

The ring speaks a move, this server validates it against the legal move list,
the engine replies, and the new position is pushed to TRMNL. Both lc0/Maia
(human-like play, rating 1100-1900) and Stockfish (skill 0-20) ship in the
container; ENGINE_KIND picks which one a fresh game starts on, and the
`set_engine` tool switches mid-game by voice ("set engine to stockfish").

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
import re
import time
from urllib.parse import urlencode
from pathlib import Path

import chess
import chess.engine
import httpx
from fastmcp import FastMCP
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
    }


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# voice-tolerant move parsing
# --------------------------------------------------------------------------

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
    t = t.replace("-", " ").replace(".", " ")
    t = re.sub(r"\b(to|on|at|the|please|move|go|and)\b", " ", t)
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

    squares = re.findall(r"\b([a-h])\s*([1-8])\b", t)
    target = f"{squares[-1][0]}{squares[-1][1]}" if squares else None
    origin = f"{squares[0][0]}{squares[0][1]}" if len(squares) > 1 else None

    piece_type = None
    for sym in "NBRQK":
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

def board_image_url(board: chess.Board, last_uci: str | None) -> str:
    """Build a board image URL. The renderer is a hosted service, so TRMNL
    just fetches an <img> — no CSS board, no dithering headaches."""
    params = {
        "fen": board.board_fen(),
        "orientation": "white",
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


async def push(state: dict) -> None:
    """POST merge variables to TRMNL. Rate limited to once per 5 minutes."""
    global _last_push
    board = chess.Board(state["fen"])
    engine_kind = state.get("engine", ENGINE_KIND)
    skill = state.get("skill", DEFAULT_LEVEL)
    payload = {
        "merge_variables": {
            "image_url": board_image_url(board, state.get("last_uci")),
            "status": state["status"],
            "last_move": state["history"][-1] if state["history"] else "—",
            "move_number": board.fullmove_number,
            "turn": "White" if board.turn else "Black",
            "history": state["history"][-6:],
            "engine": engine_label(engine_kind),
            "level": level_name(skill, engine_kind),
        }
    }
    if time.time() - _last_push < 300:
        return  # TRMNL 429s below a five minute interval
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://usetrmnl.com/api/custom_plugins/{PLUGIN_UUID}",
            json=payload,
        )
        r.raise_for_status()
    _last_push = time.time()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

mcp = FastMCP("chess")


@mcp.tool()
async def make_move(move: str) -> str:
    """Play a chess move on the board shown on the TRMNL display.

    Pass the user's words through almost verbatim — this server does the
    interpreting. Do not translate to algebraic notation yourself and do not
    guess when the user is unclear; pass the raw phrasing and relay any error.

    Args:
        move: What the user said, e.g. "knight to f3", "e4", "take the bishop",
              "castle kingside". Loose phrasing is expected and fine.

    Returns a short sentence describing your move and the engine's reply.
    """
    logger.info("make_move: received %r", move)
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
    return (
        f"{'White' if board.turn else 'Black'} to move, "
        f"move {board.fullmove_number}. Recent: {recent}. {state['status']}"
    )


@mcp.tool()
async def new_game(level: str = "") -> str:
    """Start a fresh game, optionally setting the engine difficulty.

    Call this when the user says anything like "new game", "start over",
    "reset the board", or "let's play again".

    Args:
        level: Optional difficulty in the user's own words, e.g. "easy",
               "club", "hard", "max", or a number 0-20. Leave empty to keep
               the current setting. Pass the user's phrasing verbatim.
    """
    logger.info("new_game: level=%r", level)
    async with _lock:
        prior = load()
        engine_kind = prior.get("engine", ENGINE_KIND)
        skill = prior.get("skill", DEFAULT_LEVEL)
        if level.strip():
            try:
                skill = parse_level(level, engine_kind)
            except ValueError as e:
                return str(e)

        state = {
            "fen": chess.STARTING_FEN,
            "history": [],
            "status": "Your move.",
            "engine": engine_kind,
            "skill": skill,
        }
        save(state)
        global _last_push
        _last_push = 0  # a new game always earns an immediate refresh
        await push(state)
    return (
        f"New game ({engine_label(engine_kind)}) at "
        f"{level_name(skill, engine_kind)}. You're white — your move."
    )


@mcp.tool()
async def set_level(level: str) -> str:
    """Change the engine's difficulty without disturbing the game in progress.

    Call this for "make it harder", "set difficulty to easy", "play stronger",
    and similar. Takes effect on the engine's next move.

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
