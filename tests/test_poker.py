"""Оценка покерных рук (examples/poker/holdem.fsm) против эталона на Python.

Эталон намеренно устроен иначе: перебирает все C(7,5) = 21 пятёрку и
оценивает каждую по определению комбинаций. Компилятору FSM он не нужен —
он нужен, чтобы поймать ошибку в holdem.fsm или в генерации кода массивов,
функций и циклов (дифференциальное тестирование, как у банкомата).
"""

from __future__ import annotations

import itertools
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from helpers import NODE, ROOT, compile_source, dict_loader, run_node

POKER = ROOT / "examples" / "poker"

# Обвязка: 7 карт приходят событиями, eval печатает оценку.
HARNESS = """
machine HandTest
import holdem
context { h: int[7], n: int = 0 }
events { c(x: int), go }
states S
initial S
terminal S
action add(x: int) { h[n] = x
                     n = n + 1 }
action eval { say holdem.score(h, 7)
              n = 0 }
S -- c(x) --> / add(x)
S -- go ----> / eval
"""


def best5(cards: tuple[int, ...]) -> tuple:
    ranks = sorted((c % 13 + 2 for c in cards), reverse=True)
    groups = sorted(Counter(ranks).items(), key=lambda kv: (-kv[1], -kv[0]))
    flush = len({c // 13 for c in cards}) == 1
    distinct = sorted(set(ranks), reverse=True)
    top = 0
    if len(distinct) == 5 and distinct[0] - distinct[4] == 4:
        top = distinct[0]
    if distinct == [14, 5, 4, 3, 2]:
        top = 5  # колесо
    if top and flush:
        return (8, top)
    if groups[0][1] == 4:
        return (7, groups[0][0], groups[1][0])
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, groups[0][0], groups[1][0])
    if flush:
        return (5, *ranks)
    if top:
        return (4, top)
    if groups[0][1] == 3:
        return (3, groups[0][0], *[r for r, _ in groups[1:]])
    if groups[0][1] == 2 and groups[1][1] == 2:
        return (2, groups[0][0], groups[1][0], groups[2][0])
    if groups[0][1] == 2:
        return (1, groups[0][0], *[r for r, _ in groups[1:]])
    return (0, *ranks)


def pack(t: tuple) -> int:
    v = 0
    for x in list(t) + [0] * (6 - len(t)):
        v = v * 15 + x
    return v


def reference(hand: list[int]) -> int:
    return max(pack(best5(c)) for c in itertools.combinations(hand, 5))


@unittest.skipUnless(NODE, "node не найден")
class HandEvaluator(unittest.TestCase):
    def test_matches_reference(self):
        files = {p.name: p.read_text(encoding="utf-8") for p in POKER.glob("*.fsm")}
        r = compile_source(HARNESS, loader=dict_loader(files))
        self.assertTrue(r.ok, [d.format() for d in r.diagnostics])
        rng = random.Random(2026)
        hands = [
            [12, 0, 1, 2, 3, 30, 44],    # колесо A-2-3-4-5
            [0, 1, 2, 3, 4, 20, 33],     # стрит-флеш
            [5, 18, 31, 44, 0, 1, 2],    # каре
            [0, 13, 26, 1, 14, 40, 41],  # два сета → фулл-хаус
            [0, 2, 4, 6, 8, 13, 26],     # флеш и сет
        ] + [rng.sample(range(52), 7) for _ in range(1500)]
        lines = []
        for h in hands:
            lines += [f"c {x}" for x in h] + ["go"]
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "hands.events"
            scenario.write_text("\n".join(lines), encoding="utf-8")
            out = run_node(r.wasm, scenario).splitlines()
        categories = Counter()
        for h, got in zip(hands, out):
            want = reference(h)
            categories[want // 15 ** 5] += 1
            self.assertEqual(int(got), want, f"рука {h}")
        # Выборка покрывает все девять категорий.
        self.assertEqual(set(categories), set(range(9)))


if __name__ == "__main__":
    unittest.main()
