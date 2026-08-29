#!/usr/bin/env python3
"""Build-time check: fail the image build if lc0 + Maia can't produce a legal
move, rather than discovering it on the first voice command in production."""
import os
import sys

import chess
import chess.engine

LC0 = os.environ.get("LC0_PATH", "lc0")
WEIGHTS_DIR = os.environ.get("MAIA_WEIGHTS_DIR", "./maia_weights")
WEIGHTS = os.path.join(WEIGHTS_DIR, "maia-1500.pb.gz")

board = chess.Board()
engine = chess.engine.SimpleEngine.popen_uci([LC0, f"--weights={WEIGHTS}"])
try:
    result = engine.play(board, chess.engine.Limit(nodes=1))
    move = result.move
    if move is None or move not in board.legal_moves:
        print(f"FAIL: lc0 returned illegal/no move: {move}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: lc0 (maia-1500, nodes=1) returned legal move {board.san(move)}")
finally:
    engine.quit()
