"""Ошибки выполнения: то, что компилятор доказать не может, проверяет код.

* индекс массива вне границ — ``$chk`` вызывает ``env.fail``;
* бесконечная переписка автоматов — ``$drain`` останавливается после
  MAX_STEPS событий за один внешний вызов;
* событие из очереди, которое никто не обработал, — ``env.dropped``.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import NODE, ROOT, compile_source

ARRAY = """
machine Arr
context { a: int[3] }
events { set(i: int) }
states S
initial S
terminal S
action put(i: int) { a[i] = i
                     say "a[", i, "] = ", a[i] }
S -- set(i) --> / put(i)
"""

PINGPONG = """
system PingPong
interface Ball { hit }
machine Player(other: Ball) {
  events { hit }
  states S
  initial S
  terminal S
  S -- hit --> / send other.hit()
}
a: Player(b)
b: Player(a)
input a.hit
"""

DROPPED = """
system Drop
interface Sink { put }
machine Src(out: Sink) {
  events { go }
  states S
  initial S
  terminal S
  S -- go --> / send out.put()
}
machine Busy {
  events { put, done }
  states Work, Free
  initial Work
  terminal Free
  Work -- done --> Free
  Free -- put | done ignore
}
s: Src(d)
d: Busy
input s.go, d.done
"""


def run(source: str, events: str) -> subprocess.CompletedProcess:
    r = compile_source(source)
    assert r.ok, [d.format() for d in r.diagnostics]
    with tempfile.TemporaryDirectory() as tmp:
        wasm, scen = Path(tmp) / "m.wasm", Path(tmp) / "s.events"
        wasm.write_bytes(r.wasm)
        scen.write_text(events, encoding="utf-8")
        return subprocess.run([NODE, str(ROOT / "runtime" / "run.mjs"), str(wasm), str(scen)],
                              capture_output=True, text=True, timeout=30)


@unittest.skipUnless(NODE, "node не найден")
class RuntimeErrors(unittest.TestCase):
    def test_array_bounds(self):
        p = run(ARRAY, "set 2\nset 3\nset 0\n")
        self.assertIn("a[2] = 2", p.stdout)
        self.assertIn("индекс вне границ массива (3)", p.stderr)
        self.assertIn("a[0] = 0", p.stdout, "после ошибки автомат продолжает работу")
        self.assertEqual(p.returncode, 1)

    def test_endless_ping_pong_is_stopped(self):
        p = run(PINGPONG, "a.hit\n")
        self.assertIn("слишком много событий за один шаг", p.stderr)

    def test_dropped_event_is_reported(self):
        p = run(DROPPED, "s.go\nd.done\ns.go\n")
        self.assertIn("строка 1: событие d.put из очереди не обработано", p.stderr)
        self.assertNotIn("строка 3", p.stderr)


if __name__ == "__main__":
    unittest.main()
