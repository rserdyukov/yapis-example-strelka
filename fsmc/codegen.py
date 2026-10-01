"""Генерация кода: аннотированная модель → модуль WebAssembly.

Генератор запускается только при нуле ошибок и читает аннотации семантики
(``expr.ty``, ``ref``), ничего не выводя заново (HOWTO, § 2.1).

Раскладка модуля
----------------

* Типы: ``int``, ``money``, ``bool`` → ``i32``; ``string`` → ``i32``-указатель
  на ``[длина: i32][байты UTF-8]`` в линейной памяти.
* Память: адрес 0 оставлен пустым, чтобы нулевой указатель не совпал ни с
  одной строкой. С адреса 8 — **статическая область**: массивы-поля
  экземпляров, локальные массивы подпрограмм, константы-массивы, таблицы
  адресатов ``send`` и очереди событий. Её раскладка вычисляется до генерации
  кода, поэтому все адреса — константы. За ней — строковые литералы
  (data-сегменты), дальше — куча (bump-аллокатор).
* Массив ``T[n]`` — ``n`` слов по 4 байта; индекс проверяется (``$chk``).
  Рекурсия запрещена семантикой (E133), поэтому у каждой подпрограммы не
  больше одной активации и её локальным массивам хватает статической памяти.
* Экземпляр автомата — свой набор глобальных (``$<экз>.state``,
  ``$<экз>.ctx_<поле>``) и своя копия функций действий и обработчиков:
  параметры экземпляра подставляются при компиляции, поэтому адресаты
  ``send`` известны статически. В файле ``machine`` префикс пустой, и
  модуль выглядит как раньше: ``$state``, ``$ctx_<поле>``, ``$on_<событие>``.
* Событие — функция ``<экз>.on_<событие>(параметры) -> i32``. Она выбирает
  ветку по текущему состоянию инструкцией ``br_table`` (таблица переходов, а
  не цепочка сравнений), в ветке проверяет guard'ы сверху вниз, вызывает
  действия и записывает новое состояние. Результат: 0 — переход выполнен,
  1 — событие явно проигнорировано, 2 — не обработано.
* ``send`` кладёт событие в общую очередь FIFO (и ``send self.e`` тоже):
  события обрабатываются в том порядке, в каком посланы, — так причина
  всегда обрабатывается раньше следствия. Экспортированный вход системы
  вызывает обработчик, а потом ``$drain``: пока очередь не пуста, берёт
  событие и передаёт его ``$dispatch`` (``br_table`` по номеру пары
  «экземпляр, событие»). Шагов за один внешний вызов не больше
  ``MAX_STEPS``: бесконечная переписка автоматов остановится с ошибкой.

Импорты хоста (runtime/fsm-host.mjs): ``env.print_str(ptr)``,
``env.print_int(i32)``, ``env.print_end()``; при необходимости
``env.fail(ptr, value)`` — ошибка выполнения (выход за границы массива,
переполнение очереди) и ``env.dropped(pair)`` — событие из очереди никто
не обработал.

Экспорты: ``memory``, ``init``, ``alloc`` (хост кладёт в память строковые
аргументы событий), ``state`` и ``ctx.<поле>`` (с префиксом ``<экз>.`` в
системе), входы ``on_<событие>`` или ``on_<экз>.<событие>``. Описание
машины для хоста — JSON в пользовательской секции ``fsm.meta``.
"""

from __future__ import annotations

import json

from . import ast as A
from .semantic import (BOOL, STRING, InstanceInfo, Routine, System, array_parts, ascii_name,
                       is_array, send_targets)
from .wasm import I32, Data, Func, Global, Import, Module

MOVED, IGNORED, UNHANDLED = 0, 1, 2
DATA_START = 8
QUEUE_CAP = 256  # событий в каждой очереди
MAX_STEPS = 100_000  # событий из очередей за один внешний вызов
SEED = 2463534242 - 2**32  # начальное состояние xorshift32 (как i32)


def wat_id(prefix: str, name: str) -> str:
    """Имя для WAT. Идентификаторы WAT — только ASCII, поэтому кириллические
    имена из программы кодируются: ``баланс`` → ``$ctx_u431_u430...``."""
    return f"${prefix}_{ascii_name(name)}"


def i32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 2**32 if v >= 2**31 else v


class Strings:
    """Пул строковых литералов. Смещения назначает только он (HOWTO, § 5.3):
    длина считается в байтах UTF-8, а не в символах."""

    def __init__(self, start: int = DATA_START) -> None:
        self.offsets: dict[str, int] = {}
        self.end = start

    def intern(self, s: str) -> int:
        if s not in self.offsets:
            self.offsets[s] = self.end
            size = 4 + len(s.encode("utf-8"))
            self.end += (size + 3) & ~3  # выравнивание на 4
        return self.offsets[s]

    def segments(self) -> list[Data]:
        out = []
        for s, off in self.offsets.items():
            data = s.encode("utf-8")
            out.append(Data(off, len(data).to_bytes(4, "little") + data,
                            comment=json.dumps(s, ensure_ascii=False)))
        return out


class Generator:
    def __init__(self, system: System):
        self.sys = system
        self.mod = Module()
        self.labels = 0
        self.inst: InstanceInfo | None = None  # чей код генерируется
        self.routine: Routine | None = None
        self.needed: set[str] = set()  # runtime-функции, которые понадобились
        self.imports: set[str] = set()
        self.static_end = DATA_START
        self.static_data: list[tuple[int, object]] = []  # (адрес, значения или ссылка)
        self.addr: dict = {}
        self.pairs: list[tuple[InstanceInfo, str]] = []
        self.max_args = 0
        self.queue = 0  # адрес очереди событий; 0 — send не используется

    # ================================================================ вход

    def run(self) -> tuple[Module, dict]:
        sysm, mod = self.sys, self.mod
        insts = sysm.instances
        mod.header = [f"{'Автомат' if sysm.kind == 'machine' else 'Система'} {sysm.name}: "
                      "сгенерировано fsmc."]
        for inst in insts:
            who = f"{inst.name} ({inst.model.name})" if inst.name else "Состояния"
            mod.header.append(f"{who}: " + ", ".join(f"{i}={s}" for i, s in
                                                     enumerate(inst.model.states)))
        mod.imports = [
            Import("env", "print_str", "$print_str", [I32], []),
            Import("env", "print_int", "$print_int", [I32], []),
            Import("env", "print_end", "$print_end", [], []),
        ]
        for inst in insts:
            for e in inst.model.events:
                self.pairs.append((inst, e))
                self.max_args = max(self.max_args, len(inst.model.events[e].decl.params))
        self._layout()
        self.strings = Strings((self.static_end + 7) & ~7)

        for inst in insts:
            p = self._p(inst)
            mod.globals.append(Global(f"${p}state", 0,
                                      comment=f"текущее состояние {inst.name}".rstrip()))
            for name, f in inst.model.fields.items():
                if not is_array(f.ty):
                    mod.globals.append(Global(wat_id(f"{p}ctx", name), 0,
                                              comment=f"поле {name}: {f.type.text}"))

        mod.funcs.append(self._str_eq())
        mod.funcs.append(self._alloc_func())
        for unit in [*sysm.units.values(), sysm.main]:
            for r in list(unit.funcs.values()) + list(unit.actions.values()):
                mod.funcs.append(self._routine(r, None))
        for inst in insts:
            for r in list(inst.model.funcs.values()) + list(inst.model.actions.values()):
                mod.funcs.append(self._routine(r, inst))
            for name in inst.model.events:
                mod.funcs.append(self._event(inst, name))
        mod.funcs.append(self._init())
        if sysm.uses_queue:
            self._need("drain")
            for inst, e in sysm.inputs_resolved:
                mod.funcs.append(self._input(inst, e))
        self._runtime()

        mod.exports.append(("memory", "memory", "$memory"))
        mod.exports.append(("init", "func", "$init"))
        mod.exports.append(("alloc", "func", "$alloc"))
        for inst in insts:
            p, xp = self._p(inst), self._xp(inst)
            mod.exports.append((f"{xp}state", "global", f"${p}state"))
            for name, f in inst.model.fields.items():
                if not is_array(f.ty):
                    mod.exports.append((f"{xp}ctx.{name}", "global", wat_id(f"{p}ctx", name)))
        for inst, e in sysm.inputs_resolved:
            fn = (wat_id(f"in.{self._p(inst)}on", e) if sysm.uses_queue
                  else wat_id(f"{self._p(inst)}on", e))
            mod.exports.append((self._export(inst, e), "func", fn))

        for name, params, results in (("fail", [I32, I32], []), ("dropped", [I32], [])):
            if name in self.imports:
                mod.imports.append(Import("env", name, f"${name}", params, results))

        # Куча начинается сразу за строками. Глобальная $heap объявляется
        # последней: к этому моменту все литералы уже в пуле.
        static = self._static_segments()
        heap = (self.strings.end + 7) & ~7
        mod.memory_pages = max(1, (heap + 65535) // 65536)
        mod.globals.append(Global("$heap_base", heap, mutable=False, comment="начало кучи"))
        mod.globals.append(Global("$heap", heap, comment="указатель bump-аллокатора"))
        if "random" in self.needed:
            mod.globals.append(Global("$seed", SEED, comment="состояние xorshift32"))
        mod.data = static + self.strings.segments()

        meta = self._meta()
        mod.custom.append(("fsm.meta", json.dumps(meta, ensure_ascii=False).encode("utf-8")))
        return mod, meta

    # ================================================================ имена

    def _p(self, inst: InstanceInfo | None) -> str:
        """Префикс WAT-имён экземпляра: ``table.``; у файла machine — пустой."""
        return f"{ascii_name(inst.name)}." if inst and inst.name else ""

    def _xp(self, inst: InstanceInfo) -> str:
        """Префикс экспортов экземпляра (имена экспортов — любой UTF-8)."""
        return f"{inst.name}." if inst.name else ""

    def _export(self, inst: InstanceInfo, event: str) -> str:
        return f"on_{self._xp(inst)}{event}"

    def _routine_name(self, r: Routine) -> str:
        kind = "fn" if r.kind == "func" else "act"
        if r.model is None:  # подпрограмма модуля: `/` не встречается в именах экземпляров
            return wat_id(f"{ascii_name(r.unit.name)}/{kind}", r.name)
        if self.sys.kind == "machine" and r.kind == "action":
            return wat_id("act", r.name)  # как раньше: $act_<имя>
        return wat_id(f"{self._p(self.inst)}{kind}", r.name)

    def _label(self, base: str) -> str:
        self.labels += 1
        return f"${ascii_name(base)}_{self.labels}"

    def _need(self, name: str) -> None:
        self.needed.add(name)

    def _fail(self, f: Func, message: str, value=None) -> None:
        """Ошибка выполнения: хост бросает исключение с текстом и числом."""
        self.imports.add("fail")
        f.emit("i32.const", self.strings.intern(message))
        if value is None:
            f.emit("i32.const", 0)
        else:
            f.emit("local.get", value)
        f.emit("call", "$fail")

    # ================================================================ статическая память

    def _static(self, nbytes: int) -> int:
        addr = self.static_end
        self.static_end += (nbytes + 3) & ~3
        return addr

    def _layout(self) -> None:
        """Раскладка статической области — до генерации, чтобы адреса были
        константами в коде."""
        sysm = self.sys
        consts = []
        for unit in [*sysm.units.values(), sysm.main]:
            consts += unit.consts.values()
        for m in sysm.models:
            consts += m.consts.values()
        for c in consts:
            arr = array_parts(c.ty)
            if arr:
                self.addr[id(c)] = self._static(4 * arr[1])
                self.static_data.append((self.addr[id(c)], ("const", c, None)))
        for unit in [*sysm.units.values(), sysm.main]:
            for r in list(unit.funcs.values()) + list(unit.actions.values()):
                if r.frame:
                    self.addr[(id(r), None)] = self._static(r.frame)
        for inst in sysm.instances:
            m = inst.model
            for name, f in m.fields.items():
                arr = array_parts(f.ty)
                if arr:
                    self.addr[(inst.name, "field", name)] = self._static(4 * arr[1])
            for i, ty in enumerate(m.param_types):
                arg = inst.args[i] if i < len(inst.args) else None
                if not is_array(ty) or arg is None or isinstance(arg, (str, list)):
                    continue
                if isinstance(arg, A.Ref):  # константа-массив системы
                    self.addr[(inst.name, "iparam", i)] = self.addr[id(arg.ref[1])]
                else:
                    a = self._static(4 * array_parts(ty)[1])
                    self.addr[(inst.name, "iparam", i)] = a
                    self.static_data.append((a, ("expr", arg, inst)))
            for r in list(m.funcs.values()) + list(m.actions.values()):
                if r.frame:
                    self.addr[(id(r), inst.name)] = self._static(r.frame)
            for s in m.sends:
                if s.index is not None:
                    targets = send_targets(inst, s)
                    a = self._static(4 * len(targets))
                    self.addr[(inst.name, id(s))] = a
                    ids = [self._pair(t, s.event.text) for t in targets]
                    self.static_data.append((a, ("ints", ids, None)))
        if sysm.uses_queue:
            self.queue = self._static(8 + QUEUE_CAP * 4 * (1 + self.max_args))

    def _pair(self, inst_name: str, event: str) -> int:
        for i, (inst, e) in enumerate(self.pairs):
            if inst.name == inst_name and e == event:
                return i
        raise KeyError((inst_name, event))

    def _static_segments(self) -> list[Data]:
        out = []
        for addr, (kind, data, inst) in self.static_data:
            if kind == "ints":
                values = data
            else:
                expr = data.value if kind == "const" else data
                values = [self._materialize(v) for v in self._eval(expr, inst)]
            payload = b"".join(i32(v).to_bytes(4, "little", signed=True) for v in values)
            if any(payload):
                out.append(Data(addr, payload, comment=f"массив из {len(values)} элементов"))
        return out

    def _materialize(self, v) -> int:
        if isinstance(v, str):
            return self.strings.intern(v)
        return i32(int(v))

    def _eval(self, e, inst):
        """Значение константного выражения при компиляции: int, str, bool или
        список. Используется для данных массивов-констант."""
        if isinstance(e, (A.IntLit, A.BoolLit, A.StrLit)):
            return e.value
        if isinstance(e, A.ArrayLit):
            return [self._eval(x, inst) for x in e.items]
        if isinstance(e, A.Ref):
            kind, data = e.ref
            if kind == "const":
                return self._eval(data.value, inst)
            if kind == "iparam":
                return self._eval(inst.args[data], inst)
            raise ValueError(f"не константа: {e.name}")
        if isinstance(e, A.Index):
            return self._eval(A.Ref(e.name.text, 0, 0, ref=e.ref), inst)[self._eval(e.index,
                                                                                     inst)]
        if isinstance(e, A.Unary):
            v = self._eval(e.operand, inst)
            return (not v) if e.op == "not" else i32(-v)
        lv, rv = self._eval(e.left, inst), self._eval(e.right, inst)
        op = e.op
        if op == "and":
            return lv and rv
        if op == "or":
            return lv or rv
        if isinstance(lv, str):
            return {"+": lambda: lv + rv, "==": lambda: lv == rv, "!=": lambda: lv != rv}[op]()
        if op in ("/", "%"):
            if rv == 0:
                return 0
            q = abs(lv) // abs(rv) * (1 if (lv < 0) == (rv < 0) else -1)
            return i32(q if op == "/" else lv - q * rv)
        return {"+": lambda: i32(lv + rv), "-": lambda: i32(lv - rv), "*": lambda: i32(lv * rv),
                "==": lambda: lv == rv, "!=": lambda: lv != rv, "<": lambda: lv < rv,
                "<=": lambda: lv <= rv, ">": lambda: lv > rv, ">=": lambda: lv >= rv}[op]()

    # ================================================================ описание для хоста

    def _meta(self) -> dict:
        sysm = self.sys
        instances = []
        for inst in sysm.instances:
            m, p, xp = inst.model, self._p(inst), self._xp(inst)
            ctx = []
            for n, f in m.fields.items():
                item = {"name": n, "type": f.ty}
                if is_array(f.ty):
                    item["addr"] = self.addr[(inst.name, "field", n)]
                else:
                    item["export"] = f"{xp}ctx.{n}"
                ctx.append(item)
            instances.append({"name": inst.name, "machine": m.name, "states": m.states,
                              "initial": m.initial, "terminal": m.terminal,
                              "state": f"{xp}state", "context": ctx})
        events = []
        for inst, e in sysm.inputs_resolved:
            decl = inst.model.events[e].decl
            events.append({"name": f"{self._xp(inst)}{e}", "instance": inst.name, "event": e,
                           "export": self._export(inst, e),
                           "params": [{"name": p.name.text, "type": p.type.name}
                                      for p in decl.params]})
        meta = {"machine": sysm.name, "kind": sysm.kind, "events": events,
                "instances": instances,
                "pairs": [f"{self._xp(inst)}{e}" for inst, e in self.pairs],
                "status": {"moved": MOVED, "ignored": IGNORED, "unhandled": UNHANDLED}}
        if sysm.kind == "machine":
            first = instances[0]
            meta.update({k: first[k] for k in ("states", "initial", "terminal", "context")})
        return meta

    # ================================================================ runtime на WAT

    def _str_eq(self) -> Func:
        """Сравнение строк: длины, затем побайтно. Написано на WAT, а не
        импортировано из JS: так модуль не зависит от хоста в логике."""
        f = Func("$str_eq", [("$a", I32), ("$b", I32)], [I32],
                 comment="runtime: равенство строк [len][bytes]")
        f.local("$i")
        f.local("$n")
        e = f.emit
        e("local.get", "$a"); e("i32.load")
        e("local.tee", "$n")
        e("local.get", "$b"); e("i32.load")
        e("i32.ne")
        e("if", None)
        e("i32.const", 0); e("return")
        e("end")
        e("block", "$done")
        e("loop", "$next")
        e("local.get", "$i"); e("local.get", "$n"); e("i32.ge_u"); e("br_if", "$done")
        e("local.get", "$a"); e("local.get", "$i"); e("i32.add"); e("i32.load8_u", 4)
        e("local.get", "$b"); e("local.get", "$i"); e("i32.add"); e("i32.load8_u", 4)
        e("i32.ne")
        e("if", None)
        e("i32.const", 0); e("return")
        e("end")
        e("local.get", "$i"); e("i32.const", 1); e("i32.add"); e("local.set", "$i")
        e("br", "$next")
        e("end")
        e("end")
        e("i32.const", 1)
        return f

    def _alloc_func(self) -> Func:
        """Bump-аллокатор: хост просит память под строковый аргумент события."""
        f = Func("$alloc", [("$size", I32)], [I32],
                 comment="runtime: bump-аллокатор, выравнивание 8, рост памяти по страницам")
        f.local("$ptr")
        e = f.emit
        e("global.get", "$heap"); e("local.set", "$ptr")
        e("global.get", "$heap"); e("local.get", "$size"); e("i32.add")
        e("i32.const", 7); e("i32.add"); e("i32.const", -8); e("i32.and")
        e("global.set", "$heap")
        e("comment", "не хватает памяти — добавить страницы по 64 КиБ")
        e("block", "$ok")
        e("loop", "$grow")
        e("global.get", "$heap"); e("memory.size"); e("i32.const", 65536); e("i32.mul")
        e("i32.le_u"); e("br_if", "$ok")
        e("i32.const", 1); e("memory.grow"); e("i32.const", -1); e("i32.eq")
        e("if", None)
        e("unreachable")
        e("end")
        e("br", "$grow")
        e("end")
        e("end")
        e("local.get", "$ptr")
        return f

    def _runtime(self) -> None:
        """Вспомогательные функции — только те, что понадобились. Одни зовут
        другие (drain → dispatch → ...), поэтому собираем до неподвижной точки."""
        done: set[str] = set()
        while self.needed - done:
            name = sorted(self.needed - done)[0]
            done.add(name)
            self.mod.funcs.append(getattr(self, f"_rt_{name}")())

    def _loop_bytes(self, f: Func, n: str, body) -> None:
        """for $i in 0 .. n: body() — общий цикл вспомогательных функций."""
        e = f.emit
        f.local("$i")
        e("i32.const", 0); e("local.set", "$i")
        e("block", "$done")
        e("loop", "$next")
        e("local.get", "$i"); e("local.get", n); e("i32.ge_u"); e("br_if", "$done")
        body()
        e("local.get", "$i"); e("i32.const", 1); e("i32.add"); e("local.set", "$i")
        e("br", "$next")
        e("end")
        e("end")

    def _rt_chk(self) -> Func:
        f = Func("$chk", [("$i", I32), ("$n", I32)], [I32],
                 comment="runtime: проверка индекса массива")
        f.emit("local.get", "$i"); f.emit("local.get", "$n"); f.emit("i32.ge_u")
        f.emit("if", None)
        self._fail(f, "индекс вне границ массива", "$i")
        f.emit("end")
        f.emit("local.get", "$i")
        return f

    def _rt_memcpy(self) -> Func:
        f = Func("$memcpy", [("$dst", I32), ("$src", I32), ("$n", I32)], [],
                 comment="runtime: копирование n байт")
        e = f.emit

        def body():
            e("local.get", "$dst"); e("local.get", "$i"); e("i32.add")
            e("local.get", "$src"); e("local.get", "$i"); e("i32.add"); e("i32.load8_u")
            e("i32.store8")
        self._loop_bytes(f, "$n", body)
        return f

    def _rt_fill(self) -> Func:
        f = Func("$fill", [("$dst", I32), ("$n", I32), ("$v", I32)], [],
                 comment="runtime: заполнить n слов значением")
        e = f.emit

        def body():
            e("local.get", "$dst"); e("local.get", "$i"); e("i32.const", 4); e("i32.mul")
            e("i32.add"); e("local.get", "$v"); e("i32.store")
        self._loop_bytes(f, "$n", body)
        return f

    def _rt_concat(self) -> Func:
        self._need("memcpy")
        f = Func("$concat", [("$a", I32), ("$b", I32)], [I32],
                 comment="runtime: склейка строк в новую строку на куче")
        f.local("$la"); f.local("$lb"); f.local("$r")
        e = f.emit
        e("local.get", "$a"); e("i32.load"); e("local.set", "$la")
        e("local.get", "$b"); e("i32.load"); e("local.set", "$lb")
        e("local.get", "$la"); e("local.get", "$lb"); e("i32.add"); e("i32.const", 4)
        e("i32.add"); e("call", "$alloc"); e("local.set", "$r")
        e("local.get", "$r"); e("local.get", "$la"); e("local.get", "$lb"); e("i32.add")
        e("i32.store")
        e("local.get", "$r"); e("i32.const", 4); e("i32.add")
        e("local.get", "$a"); e("i32.const", 4); e("i32.add"); e("local.get", "$la")
        e("call", "$memcpy")
        e("local.get", "$r"); e("i32.const", 4); e("i32.add"); e("local.get", "$la")
        e("i32.add")
        e("local.get", "$b"); e("i32.const", 4); e("i32.add"); e("local.get", "$lb")
        e("call", "$memcpy")
        e("local.get", "$r")
        return f

    def _rt_itoa(self) -> Func:
        self._need("memcpy")
        f = Func("$itoa", [("$n", I32)], [I32],
                 comment="runtime: число → строка (цифры пишутся с конца буфера)")
        for v in ("$u", "$buf", "$p", "$r", "$len"):
            f.local(v)
        e = f.emit
        e("local.get", "$n")
        e("local.get", "$n"); e("i32.const", 0); e("i32.sub")
        e("local.get", "$n"); e("i32.const", 0); e("i32.ge_s")
        e("select"); e("local.set", "$u")
        e("i32.const", 16); e("call", "$alloc"); e("local.tee", "$buf")
        e("i32.const", 16); e("i32.add"); e("local.set", "$p")
        e("loop", "$digit")
        e("local.get", "$p"); e("i32.const", 1); e("i32.sub"); e("local.tee", "$p")
        e("local.get", "$u"); e("i32.const", 10); e("i32.rem_u"); e("i32.const", 48)
        e("i32.add"); e("i32.store8")
        e("local.get", "$u"); e("i32.const", 10); e("i32.div_u"); e("local.tee", "$u")
        e("br_if", "$digit")
        e("end")
        e("local.get", "$n"); e("i32.const", 0); e("i32.lt_s")
        e("if", None)
        e("local.get", "$p"); e("i32.const", 1); e("i32.sub"); e("local.tee", "$p")
        e("i32.const", 45); e("i32.store8")
        e("end")
        e("local.get", "$buf"); e("i32.const", 16); e("i32.add"); e("local.get", "$p")
        e("i32.sub"); e("local.set", "$len")
        e("local.get", "$len"); e("i32.const", 4); e("i32.add"); e("call", "$alloc")
        e("local.tee", "$r"); e("local.get", "$len"); e("i32.store")
        e("local.get", "$r"); e("i32.const", 4); e("i32.add"); e("local.get", "$p")
        e("local.get", "$len"); e("call", "$memcpy")
        e("local.get", "$r")
        return f

    def _rt_chr(self) -> Func:
        f = Func("$chr", [("$c", I32)], [I32],
                 comment="runtime: строка из одного байта с кодом c (0..255)")
        f.local("$r")
        e = f.emit
        e("i32.const", 5); e("call", "$alloc"); e("local.tee", "$r")
        e("i32.const", 1); e("i32.store")
        e("local.get", "$r"); e("local.get", "$c"); e("i32.store8", 4)
        e("local.get", "$r")
        return f

    def _rt_code(self) -> Func:
        self._need("chk")
        f = Func("$code", [("$s", I32), ("$i", I32)], [I32],
                 comment="runtime: байт строки по номеру, с проверкой границ")
        e = f.emit
        e("local.get", "$s")
        e("local.get", "$i"); e("local.get", "$s"); e("i32.load"); e("call", "$chk")
        e("i32.add"); e("i32.load8_u", 4)
        return f

    def _rt_minmax(self, name: str, cmp: str) -> Func:
        f = Func(f"${name}", [("$a", I32), ("$b", I32)], [I32], comment=f"runtime: {name}")
        f.emit("local.get", "$a"); f.emit("local.get", "$b")
        f.emit("local.get", "$a"); f.emit("local.get", "$b"); f.emit(cmp)
        f.emit("select")
        return f

    def _rt_min(self) -> Func:
        return self._rt_minmax("min", "i32.lt_s")

    def _rt_max(self) -> Func:
        return self._rt_minmax("max", "i32.gt_s")

    def _rt_random(self) -> Func:
        f = Func("$random", [("$n", I32)], [I32],
                 comment="runtime: xorshift32, число от 0 до n-1; воспроизводимо после srand")
        f.local("$x")
        e = f.emit
        e("local.get", "$n"); e("i32.const", 0); e("i32.le_s")
        e("if", None)
        self._fail(f, "random(n): n должно быть больше нуля", "$n")
        e("end")
        e("global.get", "$seed"); e("local.set", "$x")
        for op, k in (("i32.shl", 13), ("i32.shr_u", 17), ("i32.shl", 5)):
            e("local.get", "$x"); e("local.get", "$x"); e("i32.const", k); e(op)
            e("i32.xor"); e("local.set", "$x")
        e("local.get", "$x"); e("global.set", "$seed")
        e("local.get", "$x"); e("local.get", "$n"); e("i32.rem_u")
        return f

    def _rt_srand(self) -> Func:
        self._need("random")
        f = Func("$srand", [("$v", I32)], [], comment="runtime: задать зерно генератора")
        f.emit("i32.const", SEED); f.emit("local.get", "$v"); f.emit("local.get", "$v")
        f.emit("select"); f.emit("global.set", "$seed")
        return f

    # ---------------------------------------------------------------- очереди событий
    # Очередь — кольцевой буфер в статической памяти: [голова][хвост][записи],
    # запись — [пара][арг0..аргK]. Голова и хвост растут неограниченно, место
    # записи — по модулю ёмкости.

    def _entry(self) -> int:
        return 4 * (1 + self.max_args)

    def _rt_push(self) -> Func:
        args = [(f"$a{k}", I32) for k in range(self.max_args)]
        f = Func("$push", [("$q", I32), ("$pair", I32)] + args, [],
                 comment="runtime: поставить событие в очередь")
        f.local("$t"); f.local("$at")
        e = f.emit
        e("local.get", "$q"); e("i32.load", 4); e("local.tee", "$t")
        e("local.get", "$q"); e("i32.load"); e("i32.sub"); e("i32.const", QUEUE_CAP)
        e("i32.ge_u")
        e("if", None)
        f.emit("i32.const", QUEUE_CAP); f.emit("local.set", "$at")
        self._fail(f, "очередь событий переполнена, ёмкость", "$at")
        e("end")
        e("local.get", "$q"); e("i32.const", 8); e("i32.add")
        e("local.get", "$t"); e("i32.const", QUEUE_CAP); e("i32.rem_u")
        e("i32.const", self._entry()); e("i32.mul"); e("i32.add"); e("local.set", "$at")
        e("local.get", "$at"); e("local.get", "$pair"); e("i32.store")
        for k in range(self.max_args):
            e("local.get", "$at"); e("local.get", f"$a{k}"); e("i32.store", 4 + 4 * k)
        e("local.get", "$q"); e("local.get", "$t"); e("i32.const", 1); e("i32.add")
        e("i32.store", 4)
        return f

    def _rt_pop(self) -> Func:
        f = Func("$pop", [("$q", I32)], [I32],
                 comment="runtime: адрес записи из головы очереди или 0, если пусто")
        f.local("$h")
        e = f.emit
        e("local.get", "$q"); e("i32.load"); e("local.tee", "$h")
        e("local.get", "$q"); e("i32.load", 4); e("i32.eq")
        e("if", None)
        e("i32.const", 0); e("return")
        e("end")
        e("local.get", "$q"); e("local.get", "$h"); e("i32.const", 1); e("i32.add")
        e("i32.store")
        e("local.get", "$q"); e("i32.const", 8); e("i32.add")
        e("local.get", "$h"); e("i32.const", QUEUE_CAP); e("i32.rem_u")
        e("i32.const", self._entry()); e("i32.mul"); e("i32.add")
        return f

    def _rt_drain(self) -> Func:
        self._need("pop")
        self._need("dispatch")
        self.imports.add("dropped")
        f = Func("$drain", [], [], comment="runtime: обработать очередь событий до конца")
        f.local("$at"); f.local("$steps")
        e = f.emit
        e("block", "$empty")
        e("loop", "$next")
        e("i32.const", self.queue); e("call", "$pop"); e("local.tee", "$at")
        e("i32.eqz"); e("br_if", "$empty")
        e("local.get", "$steps"); e("i32.const", 1); e("i32.add"); e("local.tee", "$steps")
        e("i32.const", MAX_STEPS); e("i32.gt_u")
        e("if", None)
        self._fail(f, "слишком много событий за один шаг: автоматы бесконечно "
                      "пересылают события друг другу?", "$steps")
        e("end")
        e("local.get", "$at"); e("i32.load")
        for k in range(self.max_args):
            e("local.get", "$at"); e("i32.load", 4 + 4 * k)
        e("call", "$dispatch")
        e("i32.const", UNHANDLED); e("i32.eq")
        e("if", None)
        e("local.get", "$at"); e("i32.load"); e("call", "$dropped")
        e("end")
        e("br", "$next")
        e("end")
        e("end")
        return f

    def _rt_dispatch(self) -> Func:
        args = [(f"$a{k}", I32) for k in range(self.max_args)]
        f = Func("$dispatch", [("$pair", I32)] + args, [I32],
                 comment="runtime: событие из очереди → обработчик экземпляра")
        e = f.emit
        labels = [self._label(f"pair{i}") for i in range(len(self.pairs))]
        bad = self._label("bad")
        e("block", bad)
        for lbl in reversed(labels):
            e("block", lbl)
        e("local.get", "$pair")
        e("br_table", labels, bad)
        for lbl, (inst, ev) in zip(labels, self.pairs):
            e("end")
            e("comment", f"{self._xp(inst)}{ev}")
            for k in range(len(inst.model.events[ev].decl.params)):
                e("local.get", f"$a{k}")
            e("call", wat_id(f"{self._p(inst)}on", ev))
            e("return")
        e("end")
        e("i32.const", UNHANDLED)
        return f

    def _input(self, inst: InstanceInfo, event: str) -> Func:
        """Вход системы: обработать внешнее событие, затем очередь."""
        decl = inst.model.events[event].decl
        params = [(wat_id("e", p.name.text), I32) for p in decl.params]
        f = Func(wat_id(f"in.{self._p(inst)}on", event), params, [I32],
                 comment=f"вход {self._xp(inst)}{event}: обработчик, затем очередь")
        f.local("$status")
        for n, _ in params:
            f.emit("local.get", n)
        f.emit("call", wat_id(f"{self._p(inst)}on", event))
        f.emit("local.set", "$status")
        f.emit("call", "$drain")
        f.emit("local.get", "$status")
        return f

    # ================================================================ init

    def _init(self) -> Func:
        f = Func("$init", [], [], comment="сброс: начальное состояние и контекст")
        self.inst, self.routine = None, None
        f.emit("global.get", "$heap_base")
        f.emit("global.set", "$heap")
        if "random" in self.needed:
            f.emit("i32.const", SEED)
            f.emit("global.set", "$seed")
        if self.queue:
            f.emit("i32.const", self.queue); f.emit("i32.const", 0); f.emit("i32.store")
            f.emit("i32.const", self.queue); f.emit("i32.const", 0); f.emit("i32.store", 4)
        for inst in self.sys.instances:
            self.inst = inst
            p = self._p(inst)
            f.emit("i32.const", inst.model.state_index(inst.model.initial))
            f.emit("global.set", f"${p}state")
            for name, fld in inst.model.fields.items():
                if is_array(fld.ty):
                    self._init_array(f, self.addr[(inst.name, "field", name)], fld.ty, fld.init)
                    continue
                if fld.init is not None:
                    self._expr(f, fld.init, {})
                else:
                    self._default(f, fld.ty)
                f.emit("global.set", wat_id(f"{p}ctx", name))
        self.inst = None
        return f

    def _default(self, f: Func, ty: str) -> None:
        f.emit("i32.const", self.strings.intern("") if ty == STRING else 0)

    def _init_array(self, f: Func, addr: int, ty: str, init) -> None:
        """Начальное значение массива: литерал, копия другого массива или
        значения по умолчанию."""
        elem, n = array_parts(ty)
        if isinstance(init, A.ArrayLit):
            for k, item in enumerate(init.items):
                f.emit("i32.const", addr + 4 * k)
                self._expr(f, item, [])
                f.emit("i32.store")
        elif init is not None:
            self._need("memcpy")
            f.emit("i32.const", addr)
            self._addr(f, init, [])
            f.emit("i32.const", 4 * n)
            f.emit("call", "$memcpy")
        else:
            self._need("fill")
            f.emit("i32.const", addr)
            f.emit("i32.const", n)
            self._default(f, elem)
            f.emit("call", "$fill")

    # ================================================================ функции и действия

    def _routine(self, r: Routine, inst: InstanceInfo | None) -> Func:
        self.inst, self.routine = inst, r
        node = r.node
        params = [(wat_id("a", p.name.text), I32) for p in node.params]
        results = [I32] if r.kind == "func" else []
        where = f" ({inst.name})" if inst and inst.name else ""
        f = Func(self._routine_name(r), params, results,
                 comment=f"{'func' if r.kind == 'func' else 'action'} {r.name}{where}")
        for slot in r.locals:
            if slot not in r.arrays:
                f.local(slot)
        self._block(f, node.body, [n for n, _ in params])
        if r.kind == "func":
            f.emit("unreachable")  # все пути заканчиваются return (E131)
        self.routine = None
        return f

    def _block(self, f: Func, body: list, params: list[str]) -> None:
        for s in body:
            if isinstance(s, A.Var):
                if is_array(s.type_ty):
                    self._init_array(f, self._local_addr(s.slot), s.type_ty, s.init)
                    continue
                if s.init is not None:
                    self._expr(f, s.init, params)
                else:
                    self._default(f, s.type_ty)
                f.emit("local.set", s.slot)
            elif isinstance(s, A.Assign):
                self._assign(f, s, params)
            elif isinstance(s, A.Say):
                for item in s.items:
                    self._print(f, item, params)
                if s.newline:
                    f.emit("call", "$print_end")
            elif isinstance(s, A.If):
                self._expr(f, s.cond, params)
                f.emit("if", None)
                self._block(f, s.then, params)
                if s.else_:
                    f.emit("else")
                    self._block(f, s.else_, params)
                f.emit("end")
            elif isinstance(s, A.For):
                self._for(f, s, params)
            elif isinstance(s, A.Return):
                self._expr(f, s.value, params)
                f.emit("return")
            elif isinstance(s, A.Send):
                self._send(f, s, params)
            elif isinstance(s, A.Call):
                self._call(f, s, params)

    def _for(self, f: Func, s: A.For, params) -> None:
        # Границы вычисляются один раз; переменная цикла неизменяема (E115),
        # поэтому цикл завершается не более чем за end - start итераций.
        brk, cont = self._label("for_end"), self._label("for_next")
        f.local(s.end_slot)
        self._expr(f, s.start, params)
        f.emit("local.set", s.slot)
        self._expr(f, s.end, params)
        f.emit("local.set", s.end_slot)
        f.emit("block", brk)
        f.emit("loop", cont)
        f.emit("local.get", s.slot); f.emit("local.get", s.end_slot); f.emit("i32.ge_s")
        f.emit("br_if", brk)
        self._block(f, s.body, params)
        f.emit("local.get", s.slot); f.emit("i32.const", 1); f.emit("i32.add")
        f.emit("local.set", s.slot)
        f.emit("br", cont)
        f.emit("end")
        f.emit("end")

    def _assign(self, f: Func, s: A.Assign, params) -> None:
        kind, data = s.ref
        if s.index is not None:
            self._element(f, s.target.text, kind, data, s.index, params, s.target_ty)
            self._expr(f, s.value, params)
            f.emit("i32.store")
            return
        self._expr(f, s.value, params)
        if kind == "field":
            f.emit("global.set", wat_id(f"{self._p(self.inst)}ctx", data))
        else:
            f.emit("local.set", data)

    def _print(self, f: Func, e, params: list[str]) -> None:
        if e.ty == STRING:
            self._expr(f, e, params)
            f.emit("call", "$print_str")
        elif e.ty == BOOL:
            # bool печатается словом: select между двумя строками.
            self._bool_str(f, e, params)
            f.emit("call", "$print_str")
        else:
            self._expr(f, e, params)
            f.emit("call", "$print_int")

    def _bool_str(self, f: Func, e, params) -> None:
        f.emit("i32.const", self.strings.intern("true"))
        f.emit("i32.const", self.strings.intern("false"))
        self._expr(f, e, params)
        f.emit("select")

    def _call(self, f: Func, call: A.Call, params) -> None:
        kind, data = call.ref
        if kind == "builtin":  # srand
            self._need("srand")
            self._need("random")
            self._expr(f, call.args[0], params)
            f.emit("call", "$srand")
            return
        for arg, pr in zip(call.args, data.node.params):
            self._arg(f, arg, data.param_types[data.node.params.index(pr)], params)
        f.emit("call", self._routine_name(data))

    def _arg(self, f: Func, arg, ty: str, params) -> None:
        if is_array(ty):
            self._addr(f, arg, params)  # массив передаётся адресом
        else:
            self._expr(f, arg, params)

    def _send(self, f: Func, s: A.Send, params) -> None:
        inst = self.inst
        targets = send_targets(inst, s)
        f.emit("i32.const", self.queue)
        if s.index is None:
            f.emit("i32.const", self._pair(targets[0], s.event.text))
        else:
            self._need("chk")
            f.emit("i32.const", self.addr[(inst.name, id(s))])
            self._expr(f, s.index, params)
            f.emit("i32.const", len(targets))
            f.emit("call", "$chk")
            f.emit("i32.const", 4); f.emit("i32.mul"); f.emit("i32.add")
            f.emit("i32.load")
        for arg in s.args:
            self._expr(f, arg, params)
        for _ in range(self.max_args - len(s.args)):
            f.emit("i32.const", 0)
        self._need("push")
        f.emit("call", "$push")

    # ================================================================ выражения
    # Контракт (HOWTO, § 2.4): после кода выражения на стеке ровно одно i32.
    # ``params`` — WAT-имена параметров текущей функции по номеру.

    _ARITH = {"+": "i32.add", "-": "i32.sub", "*": "i32.mul",
              "/": "i32.div_s", "%": "i32.rem_s"}
    _CMP = {"==": "i32.eq", "!=": "i32.ne", "<": "i32.lt_s", "<=": "i32.le_s",
            ">": "i32.gt_s", ">=": "i32.ge_s"}

    def _local_addr(self, slot: str) -> int:
        r = self.routine
        base = self.addr[(id(r), self.inst.name if r.model is not None else None)]
        return base + r.arrays[slot]

    def _addr(self, f: Func, e, params) -> None:
        """Адрес массива: поле, локальная, параметр, константа, параметр экземпляра."""
        kind, data = e.ref
        if kind == "field":
            f.emit("i32.const", self.addr[(self.inst.name, "field", data)])
        elif kind == "local":
            f.emit("i32.const", self._local_addr(data))
        elif kind in ("param", "refparam"):
            f.emit("local.get", params[data])
        elif kind == "const":
            f.emit("i32.const", self.addr[id(data)])
        else:  # iparam
            f.emit("i32.const", self.addr[(self.inst.name, "iparam", data)])

    def _element(self, f: Func, name: str, kind, data, index, params, ty: str) -> None:
        """Адрес элемента массива с проверкой индекса."""
        self._need("chk")
        self._addr(f, A.Ref(name, 0, 0, ref=(kind, data)), params)
        self._expr(f, index, params)
        f.emit("i32.const", array_parts(ty)[1])
        f.emit("call", "$chk")
        f.emit("i32.const", 4); f.emit("i32.mul"); f.emit("i32.add")

    def _expr(self, f: Func, e, params) -> None:
        if isinstance(e, A.IntLit):
            f.emit("i32.const", i32(e.value))
        elif isinstance(e, A.BoolLit):
            f.emit("i32.const", int(e.value))
        elif isinstance(e, A.StrLit):
            f.emit("i32.const", self.strings.intern(e.value))
        elif isinstance(e, A.Ref):
            kind, data = e.ref
            if is_array(e.ty):
                self._addr(f, e, params)
            elif kind == "const":
                # Константа подставляется своим выражением: оно содержит
                # только литералы и константы, объявленные выше.
                self._expr(f, data.value, params)
            elif kind == "iparam":
                self._expr(f, self.inst.args[data], params)
            elif kind == "field":
                f.emit("global.get", wat_id(f"{self._p(self.inst)}ctx", data))
            elif kind == "local":
                f.emit("local.get", data)
            else:
                f.emit("local.get", params[data])
        elif isinstance(e, A.Index):
            kind, data = e.ref
            self._element(f, e.name.text, kind, data, e.index, params, e.array_ty)
            f.emit("i32.load")
        elif isinstance(e, A.CallExpr):
            self._call_expr(f, e, params)
        elif isinstance(e, A.Unary):
            if e.op == "not":
                self._expr(f, e.operand, params)
                f.emit("i32.eqz")
            else:
                f.emit("i32.const", 0)
                self._expr(f, e.operand, params)
                f.emit("i32.sub")
        elif e.op in ("and", "or"):
            # Короткое вычисление (HOWTO, § 3.5): правый операнд не
            # вычисляется, если результат уже известен.
            self._expr(f, e.left, params)
            f.emit("if", None, I32)
            if e.op == "and":
                self._expr(f, e.right, params)
                f.emit("else")
                f.emit("i32.const", 0)
            else:
                f.emit("i32.const", 1)
                f.emit("else")
                self._expr(f, e.right, params)
            f.emit("end")
        elif e.left.ty == STRING:
            self._expr(f, e.left, params)
            self._expr(f, e.right, params)
            if e.op == "+":
                self._need("concat")
                f.emit("call", "$concat")
            else:
                f.emit("call", "$str_eq")
                if e.op == "!=":
                    f.emit("i32.eqz")
        else:
            self._expr(f, e.left, params)
            self._expr(f, e.right, params)
            f.emit(self._ARITH.get(e.op) or self._CMP[e.op])

    def _call_expr(self, f: Func, e: A.CallExpr, params) -> None:
        kind, data = e.ref
        if kind == "routine":
            for arg, ty in zip(e.args, data.param_types):
                self._arg(f, arg, ty, params)
            f.emit("call", self._routine_name(data))
            return
        a = e.args
        if data == "len":
            if is_array(a[0].ty):
                f.emit("i32.const", array_parts(a[0].ty)[1])
            else:
                self._expr(f, a[0], params)
                f.emit("i32.load")
        elif data == "chr":
            self._need("chr")
            self._expr(f, a[0], params)
            f.emit("call", "$chr")
        elif data == "str":
            if a[0].ty == STRING:
                self._expr(f, a[0], params)
            elif a[0].ty == BOOL:
                self._bool_str(f, a[0], params)
            else:
                self._need("itoa")
                self._expr(f, a[0], params)
                f.emit("call", "$itoa")
        else:  # code, min, max, random
            self._need(data)
            for arg in a:
                self._expr(f, arg, params)
            f.emit("call", f"${data}")

    # ================================================================ события

    def _event(self, inst: InstanceInfo, event: str) -> Func:
        self.inst, self.routine = inst, None
        m, p = inst.model, self._p(inst)
        info = m.events[event]
        params = [(wat_id("e", pr.name.text), I32) for pr in info.decl.params]
        names = [n for n, _ in params]
        f = Func(wat_id(f"{p}on", event), params, [I32],
                 comment=f"событие {self._xp(inst)}{event}: 0 — переход, 1 — игнор, "
                         "2 — не обработано")
        # Состояния, в которых событие упомянуто, получают свою ветку;
        # остальные ведут br_table сразу в $unhandled.
        handled = [s for s in m.states if (s, event) in m.table]
        labels = {s: self._label(f"in_{s}") for s in handled}
        unhandled = self._label("unhandled")

        f.emit("block", unhandled)
        for s in reversed(handled):
            f.emit("block", labels[s])
        f.emit("global.get", f"${p}state")
        f.emit("br_table", [labels.get(s, unhandled) for s in m.states], unhandled)
        for s in handled:
            f.emit("end")
            f.emit("comment", f"{s} -- {event}")
            for t in m.table[(s, event)]:
                self._transition(f, t, names, p)
            if not f.body or f.body[-1][0] != "return":
                f.emit("br", unhandled)
        f.emit("end")
        f.emit("i32.const", UNHANDLED)
        return f

    def _transition(self, f: Func, t: A.Transition, params: list[str], p: str) -> None:
        if t.guard is not None:
            self._expr(f, t.guard, params)
            f.emit("if", None)
        if t.ignore:
            f.emit("i32.const", IGNORED)
            f.emit("return")
        else:
            # send из действия только ставит событие в очередь: оно
            # обработается после этого шага, уже в новом состоянии.
            for call in t.actions:
                if isinstance(call, A.Send):
                    self._send(f, call, params)
                else:
                    self._call(f, call, params)
            if t.target is not None:
                f.emit("i32.const", self.inst.model.state_index(t.target.text))
                f.emit("global.set", f"${p}state")
            f.emit("i32.const", MOVED)
            f.emit("return")
        if t.guard is not None:
            f.emit("end")


def generate(system: System) -> tuple[Module, dict]:
    return Generator(system).run()
