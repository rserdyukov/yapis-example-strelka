"""Семантический анализ программы на FSM.

Единица компиляции — файл одного из трёх видов:

* ``machine`` — один автомат (как банкомат);
* ``module`` — библиотека: константы, функции, действия, интерфейсы и
  шаблоны автоматов; кода не порождает, подключается через ``import``;
* ``system`` — композиция: шаблоны автоматов, их экземпляры и связи между
  ними. Экземпляры обмениваются событиями через ``send``.

Проходы:

1. **Загрузка модулей.** ``import`` разрешается загрузчиком (по умолчанию —
   файлы рядом с исходником); циклический импорт — ошибка.
2. **Таблицы символов и типы.** Каждое выражение получает тип (``expr.ty``),
   каждое имя — ссылку на символ (``ref``). Генератор кода читает только
   эти аннотации и ничего не выводит заново (HOWTO, § 2.1).
3. **Свойства автоматов.** Проверки, ради которых язык существует: они
   доказывают свойства графа переходов, а не типы. Обход в ширину.
4. **Свойства системы.** Экземпляры, соответствие интерфейсам, кто кому
   посылает события; рекурсия в графе вызовов запрещена — так каждый шаг
   автомата гарантированно завершается.

Коды проверок E1NN совпадают с номерами строк таблицы из карточки задачи
(раздел 5), дополнительные проверки нумеруются с 113. Список — в README.md.
"""

from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import ast as A
from .diagnostics import Diagnostics, SyntaxFailure

INT, MONEY, STRING, BOOL = "int", "money", "string", "bool"
TYPES = {INT, MONEY, STRING, BOOL}
NUMERIC = {INT, MONEY}
SELF, ANY = "self", "any"
MAX_ARRAY = 4096

# Встроенные функции выражений: имя -> (чистая ли). random меняет состояние
# генератора, поэтому её нельзя звать из func и guard'ов.
BUILTINS = {"len": True, "str": True, "code": True, "chr": True, "min": True, "max": True,
            "random": False}
BUILTIN_PROCS = {"srand"}

Loader = Callable[[str], Optional[tuple[str, str]]]


# ---------------------------------------------------------------- типы
# Тип — строка: "int", массив "int[52]", ссылка на экземпляр "@poker.Seat"
# или массив ссылок "@poker.Seat[2]".


_ID_OK = re.compile(r"^[A-Za-z0-9_]+$")


def ascii_name(name: str) -> str:
    """Имя для WAT. Идентификаторы WAT — только ASCII, поэтому кириллические
    имена из программы кодируются: ``баланс`` → ``u431_u430...``."""
    if _ID_OK.match(name):
        return name
    return "_".join(f"u{ord(ch):x}" if not _ID_OK.match(ch) else ch for ch in name)


def array_parts(t: Optional[str]) -> Optional[tuple[str, int]]:
    if t and t.endswith("]") and "[" in t:
        elem, n = t[:-1].rsplit("[", 1)
        return elem, int(n)
    return None


def is_array(t: Optional[str]) -> bool:
    return array_parts(t) is not None


def is_ref(t: Optional[str]) -> bool:
    return bool(t) and t.startswith("@")


def assignable(target: str, source: str) -> bool:
    """int неявно расширяется до money; обратно — нельзя. Для массивов —
    поэлементно при равной длине."""
    if target == source or (target == MONEY and source == INT):
        return True
    ta, sa = array_parts(target), array_parts(source)
    return bool(ta and sa and ta[1] == sa[1] and assignable(ta[0], sa[0]))


def show_type(t: str) -> str:
    return t[1:].split(".", 1)[-1] if is_ref(t) else t


# ---------------------------------------------------------------- модель


@dataclass
class EventInfo:
    index: int
    decl: A.EventDecl

    @property
    def param_types(self) -> list[str]:
        return [p.type.name for p in self.decl.params]


@dataclass(eq=False)
class Routine:
    """Функция или действие. Уровня автомата (``model`` задан) — код
    порождается для каждого экземпляра; уровня модуля — один раз."""

    node: object  # A.Func | A.Action
    kind: str  # "func" | "action"
    unit: "UnitInfo"
    model: Optional["Model"] = None
    locals: list[str] = field(default_factory=list)
    frame: int = 0  # байт под локальные массивы на теневом стеке
    arrays: dict[str, int] = field(default_factory=dict)  # слот -> смещение в кадре
    calls: set = field(default_factory=set)
    used: bool = False
    param_types: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.node.name.text

    @property
    def ret(self) -> Optional[str]:
        return getattr(self.node, "ret_type", None)  # у action нет

    def new_local(self, name: str, ty: str) -> str:
        slot = f"$l{len(self.locals)}_{ascii_name(name)}"
        self.locals.append(slot)
        arr = array_parts(ty)
        if arr:
            self.arrays[slot] = self.frame
            self.frame += 4 * arr[1]
        return slot


@dataclass
class Model:
    """Аннотированный автомат: вход генератора кода и построителя диаграмм."""

    program: A.Program
    unit: "UnitInfo" = None
    states: list[str] = field(default_factory=list)
    events: dict[str, EventInfo] = field(default_factory=dict)
    actions: dict[str, Routine] = field(default_factory=dict)
    funcs: dict[str, Routine] = field(default_factory=dict)
    consts: dict[str, A.Const] = field(default_factory=dict)
    fields: dict[str, A.Field] = field(default_factory=dict)
    params: list[A.Param] = field(default_factory=list)
    param_types: list[Optional[str]] = field(default_factory=list)
    initial: str = ""
    terminal: list[str] = field(default_factory=list)
    # (состояние, событие) -> переходы в порядке проверки; `any` уже раскрыт
    table: dict[tuple[str, str], list[A.Transition]] = field(default_factory=dict)
    sends: list[A.Send] = field(default_factory=list)
    # Итоги анализа графа: недостижимые (E102) и ловушки (E103)
    unreachable: set[str] = field(default_factory=set)
    trapped: set[str] = field(default_factory=set)
    paths: dict[str, list[str]] = field(default_factory=dict)  # кратчайшие пути из initial

    @property
    def name(self) -> str:
        return self.program.name.text

    def state_index(self, name: str) -> int:
        return self.states.index(name)


@dataclass(eq=False)
class UnitInfo:
    name: str
    kind: str  # "machine" | "module" | "system"
    node: object
    diags: Diagnostics
    file: str = ""
    imports: dict[str, "UnitInfo"] = field(default_factory=dict)
    consts: dict[str, A.Const] = field(default_factory=dict)
    funcs: dict[str, Routine] = field(default_factory=dict)
    actions: dict[str, Routine] = field(default_factory=dict)
    interfaces: dict[str, A.Interface] = field(default_factory=dict)
    machines: dict[str, Model] = field(default_factory=dict)
    loading: bool = False


@dataclass
class InstanceInfo:
    name: str  # "" — единственный автомат файла machine
    model: Model
    # Аргументы по параметрам шаблона: выражение (значение), имя экземпляра
    # (ссылка) или список имён (массив ссылок).
    args: list = field(default_factory=list)
    node: Optional[A.Instance] = None


@dataclass
class System:
    kind: str
    name: str
    main: UnitInfo
    units: dict[str, UnitInfo]
    models: list[Model]
    instances: list[InstanceInfo] = field(default_factory=list)
    inputs: list[tuple[str, str]] = field(default_factory=list)
    uses_queue: bool = False

    @property
    def inputs_resolved(self) -> list[tuple["InstanceInfo", str]]:
        by_name = {i.name: i for i in self.instances}
        return [(by_name[n], e) for n, e in self.inputs]

    @property
    def model(self) -> Optional[Model]:
        """Автомат файла machine (для простых случаев и обратной совместимости)."""
        return self.instances[0].model if self.kind == "machine" and self.instances else None

    @property
    def routines(self) -> list[Routine]:
        out = []
        for u in [self.main, *self.units.values()]:
            out += list(u.funcs.values()) + list(u.actions.values())
        for m in self.models:
            out += list(m.funcs.values()) + list(m.actions.values())
        return out


@dataclass
class Ctx:
    """Где анализируется выражение или оператор."""

    unit: UnitInfo
    model: Optional[Model] = None
    routine: Optional[Routine] = None
    pure: bool = False  # тело func или guard: никаких побочных эффектов
    const: bool = False  # константное выражение: без вызовов функций
    loop_vars: frozenset = frozenset()


# ---------------------------------------------------------------- анализатор


class Analyzer:
    def __init__(self, root, diags: Diagnostics, loader: Optional[Loader] = None,
                 frontend: str = "antlr"):
        self.root = root
        self.diags = diags
        self.d = diags
        self.loader = loader
        self.frontend = frontend
        self.units: dict[str, UnitInfo] = {}
        self.models: list[Model] = []
        self.uses_queue = False

    # ================================================================ вход

    def run(self) -> System:
        main = self._unit_info(self.root, self.diags, "")
        self.main = main
        main.loading = True
        self._load_imports(main)
        main.loading = False
        units = list(self.units.values()) + [main]

        for u in units:
            self._unit_decls(u)
        for u in units:
            for m in u.machines.values():
                self._machine_decls(m)
        for u in units:
            self._unit_bodies(u)
            for m in u.machines.values():
                self._machine_bodies(m)

        sysm = System(main.kind, main.name, main, self.units, self.models)
        if main.kind == "machine":
            m = main.machines[main.name]
            sysm.instances = [InstanceInfo("", m)]
            sysm.inputs = [("", e) for e in m.events]
        elif main.kind == "system":
            self._system(main, sysm)
        for u in self.units.values():
            self._no_instances(u)
        self._recursion(sysm.routines)
        sysm.uses_queue = self.uses_queue
        return sysm

    def _enter(self, unit: UnitInfo) -> None:
        self.d = unit.diags

    # ================================================================ модули

    def _unit_info(self, node, diags: Diagnostics, file: str) -> UnitInfo:
        if isinstance(node, A.Program):
            info = UnitInfo(node.name.text, "machine", node, diags, file)
            info.machines[node.name.text] = self._new_model(node, info)
        else:
            info = UnitInfo(node.name.text, node.kind, node, diags, file)
        return info

    def _new_model(self, program: A.Program, unit: UnitInfo) -> Model:
        m = Model(program, unit)
        self.models.append(m)
        return m

    def _load_imports(self, info: UnitInfo) -> None:
        self._enter(info)
        for imp in info.node.imports:
            name = imp.name.text
            if name in info.imports:
                continue
            mod = self.units.get(name)
            if mod is None:
                mod = self._load(name, imp)
                if mod is None:
                    continue
                self.units[name] = mod
                mod.loading = True
                self._load_imports(mod)
                mod.loading = False
                self._enter(info)
            elif mod.loading:
                self.d.error("E130", f"циклический импорт модуля '{name}'",
                             imp.name.line, imp.name.col)
                continue
            info.imports[name] = mod

    def _load(self, name: str, imp: A.Import) -> Optional[UnitInfo]:
        found = self.loader(name) if self.loader else None
        if found is None:
            self.d.error("E130", f"модуль '{name}' не найден (ищется файл {name}.fsm)",
                         imp.name.line, imp.name.col)
            return None
        source, filename = found
        mdiags = self.diags.for_file(filename)
        from . import get_frontend
        try:
            node = get_frontend(self.frontend).parse(source, mdiags)
        except SyntaxFailure:
            return None
        if not isinstance(node, A.Unit) or node.kind != "module":
            self.d.error("E130", f"файл {filename} — не модуль: он должен начинаться "
                         f"с `module {name}`", imp.name.line, imp.name.col)
            return None
        if node.name.text != name:
            self.d.error("E130", f"в файле {filename} объявлен модуль '{node.name.text}', "
                         f"а не '{name}'", imp.name.line, imp.name.col)
            return None
        return self._unit_info(node, mdiags, filename)

    def _module(self, ctx: Ctx, mod: A.Name) -> Optional[UnitInfo]:
        unit = ctx.unit.imports.get(mod.text)
        if unit is None:
            self.d.error("E130", f"модуль '{mod.text}' не импортирован (добавьте `import "
                         f"{mod.text}`)", mod.line, mod.col)
        return unit

    # ================================================================ объявления

    def _unique(self, names: list[A.Name], what: str) -> dict[str, A.Name]:
        seen: dict[str, A.Name] = {}
        for n in names:
            if n.text in seen:
                self.d.error("E113", f"{what} '{n.text}' объявлено повторно "
                             f"(первое объявление в строке {seen[n.text].line})", n.line, n.col)
            else:
                seen[n.text] = n
        return seen

    def _check_type(self, t: A.TypeRef, unit: UnitInfo, *, array: bool = False,
                    ref: bool = False) -> Optional[str]:
        """Тип из объявления. ``array`` — допустим массив, ``ref`` — ссылка на
        экземпляр (тип — интерфейс или автомат)."""
        if t.module is None and t.name in TYPES:
            base = t.name
        elif ref:
            base = self._ref_type(t, unit)
            if base is None:
                return None
        else:
            self.d.error("E115", f"неизвестный тип '{t.text}', допустимы: "
                         + ", ".join(sorted(TYPES)), t.line, t.col)
            return None
        if t.size is None:
            return base
        if not array:
            self.d.error("E134", f"массив {t.text} здесь недопустим", t.line, t.col)
            return None
        if not 1 <= t.size <= MAX_ARRAY:
            self.d.error("E134", f"длина массива должна быть от 1 до {MAX_ARRAY}, "
                         f"указано {t.size}", t.line, t.col)
            return None
        return f"{base}[{t.size}]"

    def _ref_type(self, t: A.TypeRef, unit: UnitInfo) -> Optional[str]:
        owner = unit
        if t.module is not None:
            owner = unit.imports.get(t.module)
            if owner is None:
                self.d.error("E130", f"модуль '{t.module}' не импортирован", t.line, t.col)
                return None
        if t.name in owner.interfaces or t.name in owner.machines:
            return f"@{owner.name}.{t.name}"
        pool = list(TYPES) + list(owner.interfaces) + list(owner.machines)
        self.d.error("E115", f"неизвестный тип '{t.text}': ожидается {', '.join(sorted(TYPES))}"
                     " или интерфейс/автомат" + _hint(t.name, pool), t.line, t.col)
        return None

    def ref_events(self, t: str) -> dict[str, A.EventDecl]:
        """События типа-ссылки ``@модуль.Имя``: интерфейса или автомата."""
        unit_name, name = t[1:].split("[")[0].split(".", 1)
        unit = self._main_unit(unit_name)
        if name in unit.interfaces:
            return {e.name.text: e for e in unit.interfaces[name].events}
        return {e.name.text: e for e in unit.machines[name].program.events}

    def _main_unit(self, name: str) -> UnitInfo:
        return self.units.get(name) or self.main

    def _signature(self, r: Routine, unit: UnitInfo) -> None:
        node = r.node
        self._unique([p.name for p in node.params], f"параметр {node.name.text}")
        r.param_types = [self._check_type(p.type, unit, array=True) for p in node.params]
        for pr, ty in zip(node.params, r.param_types):
            if pr.byref and ty and not is_array(ty):
                self.d.error("E134", f"var-параметр {pr.name.text}: по ссылке передаются "
                             "только массивы", pr.name.line, pr.name.col)
            elif pr.byref and r.kind == "func":
                self.d.error("E132", f"функция {node.name.text} не может менять аргументы: "
                             f"var-параметр {pr.name.text} допустим только в action",
                             pr.name.line, pr.name.col)
        if r.kind == "func":
            node.ret_type = self._check_type(node.ret, unit)

    def _event_types(self, events: list[A.EventDecl], unit: UnitInfo, what: str) -> None:
        names = self._unique([e.name for e in events], what)
        for e in events:
            if names.get(e.name.text) is not e.name:
                continue
            self._unique([pr.name for pr in e.params], f"параметр события {e.name.text}")
            for pr in e.params:
                self._check_type(pr.type, unit)

    def _unit_decls(self, u: UnitInfo) -> None:
        """Имена уровня module/system: константы, функции, действия,
        интерфейсы, шаблоны автоматов."""
        self._enter(u)
        if u.kind == "machine":
            return
        node = u.node
        self._unique([c.name for c in node.consts] + [f.name for f in node.funcs]
                     + [a.name for a in node.actions] + [i.name for i in node.interfaces]
                     + [m.name for m in node.machines] + [i.name for i in node.instances],
                     "имя")
        for f in node.funcs:
            u.funcs.setdefault(f.name.text, Routine(f, "func", u))
        for a in node.actions:
            u.actions.setdefault(a.name.text, Routine(a, "action", u))
        for i in node.interfaces:
            u.interfaces.setdefault(i.name.text, i)
        for m in node.machines:
            if m.name.text not in u.machines:
                u.machines[m.name.text] = self._new_model(m, u)
        for i in node.interfaces:
            self._event_types(i.events, u, f"событие интерфейса {i.name.text}")
        for r in list(u.funcs.values()) + list(u.actions.values()):
            self._signature(r, u)
        # Константы: значение видит только константы, объявленные ВЫШЕ.
        scope: dict = {}
        for c in node.consts:
            self._const(c, scope, Ctx(u, const=True))
            u.consts.setdefault(c.name.text, c)
            scope.setdefault(c.name.text, ("const", c.ty, c))

    def _const(self, c: A.Const, scope: dict, ctx: Ctx) -> None:
        ty = self._check_type(c.type, ctx.unit, array=True)
        c.ty = ty
        vt = self._expr(c.value, scope, ctx, what="значение константы", init=True)
        if ty and vt and not assignable(ty, vt):
            self.d.error("E115", f"константа {c.name.text}: ожидается {ty}, получено {vt}",
                         c.name.line, c.name.col)

    def _unit_bodies(self, u: UnitInfo) -> None:
        self._enter(u)
        for r in list(u.funcs.values()) + list(u.actions.values()):
            self._routine(r, self._unit_scope(u), Ctx(u))

    def _unit_scope(self, u: UnitInfo) -> dict:
        return {n: ("const", c.ty, c) for n, c in u.consts.items()}

    def _no_instances(self, u: UnitInfo) -> None:
        if u.kind == "module":
            self._enter(u)
            for i in u.node.instances:
                self.d.error("E136", "экземпляры автоматов объявляются только в system",
                             i.name.line, i.name.col)
            for i in u.node.inputs:
                self.d.error("E138", "input объявляется только в system",
                             i.instance.line, i.instance.col)

    # ================================================================ автомат: объявления

    def _machine_decls(self, m: Model) -> None:
        self._enter(m.unit)
        p, u = m.program, m.unit
        m.states = list(self._unique(p.states, "состояние"))
        if not p.states:
            self.d.error("E112", "не объявлено ни одного состояния (states ...)",
                         p.name.line, p.name.col)

        # E112: ровно одно начальное, минимум одно терминальное.
        if not p.initial:
            self.d.error("E112", "начальное состояние не объявлено", p.name.line, p.name.col)
        elif len(p.initial) > 1:
            extra = p.initial[1]
            self.d.error("E112", "начальное состояние должно быть ровно одно, объявлено "
                         + str(len(p.initial)), extra.line, extra.col)
        if not p.terminal:
            self.d.error("E112", "терминальное состояние не объявлено", p.name.line, p.name.col)
        for n in p.initial + p.terminal:
            self._state_ref(m, n, "объявлено необъявленное состояние")
        if p.initial and p.initial[0].text in m.states:
            m.initial = p.initial[0].text
        m.terminal = [n.text for n in p.terminal if n.text in m.states]

        self._event_types(p.events, u, "событие")
        seen = set()
        for e in p.events:
            if e.name.text not in seen:
                seen.add(e.name.text)
                m.events[e.name.text] = EventInfo(len(m.events), e)

        # Параметры экземпляра, константы и поля — одно пространство имён.
        self._unique([pr.name for pr in p.params] + [c.name for c in p.consts]
                     + [f.name for f in p.fields], "имя константы или поля")
        m.params = p.params
        m.param_types = [self._check_type(pr.type, u, array=True, ref=True) for pr in p.params]

        names = self._unique([a.name for a in p.actions] + [f.name for f in p.funcs],
                             "действие или функция")
        for a in p.actions:
            if names.get(a.name.text) is a.name:
                m.actions[a.name.text] = Routine(a, "action", u, m)
        for f in p.funcs:
            if names.get(f.name.text) is f.name:
                m.funcs[f.name.text] = Routine(f, "func", u, m)
        for r in list(m.funcs.values()) + list(m.actions.values()):
            self._signature(r, u)

    def _state_ref(self, m: Model, n: A.Name, what: str) -> bool:
        if n.text in m.states:
            return True
        self.d.error("E101", f"{what} '{n.text}'" + _hint(n.text, m.states), n.line, n.col)
        return False

    # ================================================================ области видимости
    # Область видимости — словарь имя -> (вид, тип, данные). Вид определяет,
    # как генератор загрузит значение: константа (подстановка или адрес
    # массива), поле (global), параметр или локальная (local), параметр
    # экземпляра (подстановка аргумента), ссылка на экземпляр (только send).

    def _const_scope(self, m: Model) -> dict:
        scope = self._unit_scope(m.unit)
        for name, c in m.consts.items():
            scope[name] = ("const", c.ty, c)
        return scope

    def _param_scope(self, m: Model) -> dict:
        scope = self._const_scope(m)
        for i, (pr, ty) in enumerate(zip(m.params, m.param_types)):
            scope[pr.name.text] = ("iref" if is_ref(ty) else "iparam", ty, i)
        return scope

    def _base_scope(self, m: Model) -> dict:
        scope = self._param_scope(m)
        for name, f in m.fields.items():
            scope[name] = ("field", f.ty, name)
        return scope

    # ================================================================ автомат: тела

    def _machine_bodies(self, m: Model) -> None:
        self._enter(m.unit)
        p, ctx = m.program, Ctx(m.unit, m, const=True)
        for c in p.consts:
            self._const(c, self._const_scope(m), ctx)
            m.consts.setdefault(c.name.text, c)
        for f in p.fields:
            ty = self._check_type(f.type, m.unit, array=True)
            f.ty = ty
            if f.init is not None:
                vt = self._expr(f.init, self._param_scope(m), ctx,
                                what="начальное значение поля", init=True)
                if ty and vt and not assignable(ty, vt):
                    self.d.error("E115", f"поле {f.name.text}: ожидается {ty}, получено {vt}",
                                 f.name.line, f.name.col)
            m.fields.setdefault(f.name.text, f)
        for r in list(m.funcs.values()) + list(m.actions.values()):
            self._routine(r, self._base_scope(m), Ctx(m.unit, m))
        self._transitions(m)
        self._graph(m)

    # ================================================================ функции и действия

    def _routine(self, r: Routine, scope: dict, ctx: Ctx) -> None:
        scope = dict(scope)
        for i, (pr, ty) in enumerate(zip(r.node.params, r.param_types)):
            scope[pr.name.text] = ("refparam" if pr.byref else "param", ty, i)
        ctx = Ctx(ctx.unit, ctx.model, r, pure=r.kind == "func")
        self._block(r.node.body, scope, ctx)
        if r.kind == "func" and not _returns(r.node.body):
            n = r.node.name
            self.d.error("E131", f"функция {n.text}: не на всех путях выполнения есть return",
                         n.line, n.col)

    def _block(self, body: list, scope: dict, ctx: Ctx) -> None:
        scope = dict(scope)  # локальные переменные живут до конца блока
        for s in body:
            if isinstance(s, A.Var):
                self._var(s, scope, ctx)
            elif isinstance(s, A.Assign):
                self._assign(s, scope, ctx)
            elif isinstance(s, A.Say):
                if ctx.pure:
                    self._impure(s, "say/write")
                for e in s.items:
                    t = self._expr(e, scope, ctx)
                    if t and (is_array(t) or is_ref(t)):
                        self.d.error("E134", f"нельзя напечатать значение типа {show_type(t)}",
                                     e.line, e.col)
            elif isinstance(s, A.If):
                self._condition(s.cond, scope, ctx, "условие if")
                self._block(s.then, scope, ctx)
                if s.else_ is not None:
                    self._block(s.else_, scope, ctx)
            elif isinstance(s, A.For):
                self._for(s, scope, ctx)
            elif isinstance(s, A.Return):
                self._return(s, scope, ctx)
            elif isinstance(s, A.Send):
                self._send(s, scope, ctx)
            elif isinstance(s, A.Call):
                self._call(s, scope, ctx)

    def _impure(self, node, what: str) -> None:
        self.d.error("E132", f"{what} — побочный эффект: в func и guard недопустим",
                     node.line if hasattr(node, "line") else node.name.line,
                     node.col if hasattr(node, "col") else node.name.col)

    def _declare_local(self, name: A.Name, ty: Optional[str], scope: dict, ctx: Ctx):
        if name.text in scope:
            self.d.error("E113", f"имя '{name.text}' уже объявлено в этой области видимости",
                         name.line, name.col)
        slot = ctx.routine.new_local(name.text, ty or INT)
        scope[name.text] = ("local", ty, slot)
        return slot

    def _var(self, s: A.Var, scope: dict, ctx: Ctx) -> None:
        ty = self._check_type(s.type, ctx.unit, array=True)
        if s.init is not None:
            vt = self._expr(s.init, scope, ctx, what="начальное значение переменной",
                            init=is_array(ty))
            if ty and vt and not assignable(ty, vt):
                self.d.error("E115", f"переменная {s.name.text}: ожидается {ty}, получено {vt}",
                             s.line, s.col)
        s.type_ty = ty
        s.slot = self._declare_local(s.name, ty, scope, ctx)

    def _for(self, s: A.For, scope: dict, ctx: Ctx) -> None:
        for e in (s.start, s.end):
            t = self._expr(e, scope, ctx, what="граница цикла")
            if t is not None and t != INT:
                self.d.error("E115", f"граница цикла должна иметь тип int, получено {t}",
                             e.line, e.col)
        inner = dict(scope)
        s.slot = self._declare_local(s.var, INT, inner, ctx)
        s.end_slot = ctx.routine.new_local(s.var.text + "_end", INT)
        body_ctx = Ctx(ctx.unit, ctx.model, ctx.routine, ctx.pure, ctx.const,
                       ctx.loop_vars | {s.slot})
        self._block(s.body, inner, body_ctx)

    def _return(self, s: A.Return, scope: dict, ctx: Ctx) -> None:
        vt = self._expr(s.value, scope, ctx)
        r = ctx.routine
        if r is None or r.kind != "func":
            self.d.error("E131", "return допустим только в func: действие ничего не "
                         "возвращает", s.line, s.col)
            return
        if r.ret and vt and not assignable(r.ret, vt):
            self.d.error("E131", f"функция {r.name} возвращает {r.ret}, а не {vt}",
                         s.value.line, s.value.col)

    def _assign(self, s: A.Assign, scope: dict, ctx: Ctx) -> None:
        vt = self._expr(s.value, scope, ctx)
        it = self._expr(s.index, scope, ctx, what="индекс") if s.index is not None else None
        sym = scope.get(s.target.text)
        if sym is None:
            pool = [n for n, v in scope.items() if v[0] in ("field", "local")]
            self.d.error("E110", f"присваивание несуществующему полю '{s.target.text}'"
                         + _hint(s.target.text, pool), s.target.line, s.target.col)
            return
        kind, ty, data = sym
        s.ref = (kind, data)
        if kind == "local" and data in ctx.loop_vars:
            self.d.error("E115", f"нельзя присвоить значение переменной цикла "
                         f"'{s.target.text}'", s.target.line, s.target.col)
            return
        if kind == "refparam" and s.index is None:
            self.d.error("E134", f"массив '{s.target.text}' нельзя присвоить целиком: "
                         "присваивайте элементы", s.line, s.col)
            return
        if kind not in ("field", "local", "refparam"):
            what = {"const": "константе", "param": "параметру", "iparam": "параметру",
                    "iref": "параметру"}[kind]
            hint = " (массив можно передать как var-параметр)" if is_array(ty) else ""
            self.d.error("E115", f"нельзя присвоить значение {what} '{s.target.text}': "
                         "изменять можно только поля контекста" + hint,
                         s.target.line, s.target.col)
            return
        if kind == "field" and ctx.pure:
            self._impure(s, f"присваивание полю {s.target.text}")
            return
        if s.index is not None:
            arr = array_parts(ty)
            if arr is None:
                self.d.error("E134", f"'{s.target.text}' — не массив, индексировать нельзя",
                             s.target.line, s.target.col)
                return
            if it is not None and it != INT:
                self.d.error("E134", f"индекс должен иметь тип int, получено {it}",
                             s.index.line, s.index.col)
            s.target_ty = ty
            ty = arr[0]
        elif is_array(ty):
            self.d.error("E134", f"массив '{s.target.text}' нельзя присвоить целиком: "
                         "присваивайте элементы", s.line, s.col)
            return
        if ty and vt and not assignable(ty, vt):
            self.d.error("E115", f"полю {s.target.text} типа {ty} присваивается {vt}",
                         s.line, s.col)

    def _condition(self, e: A.Expr, scope: dict, ctx: Ctx, what: str,
                   code: str = "E115") -> None:
        ty = self._expr(e, scope, ctx, what=what)
        if ty is not None and ty != BOOL:
            self.d.error(code, f"{what} должно иметь тип bool, получено {ty}", e.line, e.col)

    # ================================================================ вызовы

    def _resolve_routine(self, name: A.Name, module: Optional[A.Name], ctx: Ctx,
                         kind: str) -> Optional[Routine]:
        """Найти функцию (kind="func") или действие ("action"). None — не найдено
        (сообщение не выдаётся: его формулирует вызывающий)."""
        if module is not None:
            unit = self._module(ctx, module)
            if unit is None:
                return False
            pools = [unit.funcs if kind == "func" else unit.actions]
        else:
            pools = []
            if ctx.model is not None:
                pools.append(ctx.model.funcs if kind == "func" else ctx.model.actions)
            pools.append(ctx.unit.funcs if kind == "func" else ctx.unit.actions)
        for pool in pools:
            if name.text in pool:
                return pool[name.text]
        return None

    def _args(self, callee: str, params: list[A.Param], types: list[Optional[str]],
              args: list, arg_types: list, name: A.Name, code: str = "E114") -> None:
        if len(arg_types) != len(params):
            self.d.error(code, f"{callee} ожидает {len(params)} аргумент(а), "
                         f"передано {len(arg_types)}", name.line, name.col)
            return
        for arg, ty, pr, pt in zip(args, arg_types, params, types):
            if ty and pt and not (ty == pt if pr.byref else assignable(pt, ty)):
                self.d.error(code, f"{callee}: параметр {pr.name.text} ожидает "
                             f"{show_type(pt)}, получено {show_type(ty)}", arg.line, arg.col)
            elif pr.byref and not (isinstance(arg, A.Ref) and arg.ref
                                   and arg.ref[0] in ("field", "local", "refparam")):
                self.d.error(code, f"{callee}: var-параметр {pr.name.text} требует изменяемый "
                             "массив — поле, переменную или var-параметр", arg.line, arg.col)

    def _call(self, call: A.Call, scope: dict, ctx: Ctx) -> None:
        """Вызов действия: в переходе или оператором в блоке."""
        types = [self._expr(a, scope, ctx, what="аргумент действия") for a in call.args]
        if call.module is None and call.name.text in BUILTIN_PROCS:
            call.ref = ("builtin", call.name.text)
            if ctx.pure:
                self._impure(call.name, call.name.text)
            if len(types) != 1 or (types[0] and types[0] != INT):
                self.d.error("E114", "srand ожидает один аргумент int",
                             call.name.line, call.name.col)
            return
        r = self._resolve_routine(call.name, call.module, ctx, "action")
        if r is False:
            return
        if r is None:
            if self._resolve_routine(call.name, call.module, ctx, "func"):
                self.d.error("E109", f"'{call.name.text}' — функция, а не действие: её "
                             "значение нужно использовать в выражении",
                             call.name.line, call.name.col)
                return
            pool = list(ctx.model.actions) if ctx.model else list(ctx.unit.actions)
            self.d.error("E109", f"неизвестное действие '{call.name.text}'"
                         + _hint(call.name.text, pool), call.name.line, call.name.col)
            return
        call.ref = ("routine", r)
        r.used = True
        if ctx.pure:
            self._impure(call.name, f"вызов действия {call.name.text}")
        if ctx.routine is not None:
            ctx.routine.calls.add(r)
        self._args(f"действие {call.name.text}", r.node.params, r.param_types,
                   call.args, types, call.name)

    def _send(self, s: A.Send, scope: dict, ctx: Ctx) -> None:
        types = [self._expr(a, scope, ctx, what="аргумент события") for a in s.args]
        it = self._expr(s.index, scope, ctx, what="индекс") if s.index is not None else None
        if ctx.pure:
            self._impure(s, "send")
            return
        m = ctx.model
        if m is None:
            self.d.error("E135", "send допустим только внутри автомата", s.line, s.col)
            return
        self.uses_queue = True
        target = s.target.text
        if target == SELF:
            events = {n: e.decl for n, e in m.events.items()}
            s.ref = ("self",)
            if s.index is not None:
                self.d.error("E135", "self — не массив", s.target.line, s.target.col)
        else:
            sym = scope.get(target)
            if sym is None or sym[0] != "iref":
                self.d.error("E135", f"'{target}' — не ссылка на экземпляр: в send "
                             "указывается self или параметр автомата типа интерфейс/автомат"
                             + _hint(target, [n for n, v in scope.items() if v[0] == "iref"]),
                             s.target.line, s.target.col)
                return
            _, ty, idx = sym
            if is_array(ty) != (s.index is not None):
                need = "нужен индекс: " + target + "[i]" if is_array(ty) else "это не массив"
                self.d.error("E135", f"'{target}': {need}", s.target.line, s.target.col)
                return
            if it is not None and it != INT:
                self.d.error("E134", f"индекс должен иметь тип int, получено {it}",
                             s.index.line, s.index.col)
            events = self.ref_events(ty)
            s.ref = ("iref", idx)
        decl = events.get(s.event.text)
        if decl is None:
            self.d.error("E135", f"у '{target}' нет события '{s.event.text}'"
                         + _hint(s.event.text, events), s.event.line, s.event.col)
            s.ref = None
            return
        self._args(f"событие {s.event.text}", decl.params,
                   [p.type.name for p in decl.params], s.args, types, s.event, "E135")
        m.sends.append(s)

    # ================================================================ выражения

    def _expr(self, e, scope: dict, ctx: Ctx, what: str = "выражение",
              init: bool = False) -> Optional[str]:
        """Вывести тип, записать его в ``e.ty``. None — ошибка уже выдана."""
        ty = self._infer(e, scope, ctx, what, init)
        e.ty = ty
        return ty

    def _lookup(self, name: str, module: Optional[str], line: int, col: int,
                scope: dict, ctx: Ctx, what: str):
        if module is not None:
            unit = self._module(ctx, A.Name(module, line, col))
            if unit is None:
                return None
            c = unit.consts.get(name)
            if c is None:
                self.d.error("E110", f"в модуле {module} нет константы '{name}'"
                             + _hint(name, unit.consts), line, col)
                return None
            return ("const", c.ty, c)
        sym = scope.get(name)
        if sym is None:
            self.d.error("E110", f"{what} ссылается на несуществующее имя '{name}'"
                         + _hint(name, list(scope)), line, col)
        return sym

    def _infer(self, e, scope: dict, ctx: Ctx, what: str, init: bool) -> Optional[str]:
        if isinstance(e, A.IntLit):
            return INT
        if isinstance(e, A.StrLit):
            return STRING
        if isinstance(e, A.BoolLit):
            return BOOL
        if isinstance(e, A.ArrayLit):
            return self._array_lit(e, scope, ctx, what, init)
        if isinstance(e, A.Ref):
            sym = self._lookup(e.name, e.module, e.line, e.col, scope, ctx, what)
            if sym is None:
                return None
            kind, ty, data = sym
            if kind == "iref":
                self.d.error("E135", f"'{e.name}' — ссылка на экземпляр: её можно "
                             "использовать только в send", e.line, e.col)
                return None
            e.ref = (kind, data)
            return ty
        if isinstance(e, A.Index):
            sym = self._lookup(e.name.text, e.module.text if e.module else None,
                               e.name.line, e.name.col, scope, ctx, what)
            it = self._expr(e.index, scope, ctx, what)
            if sym is None:
                return None
            kind, ty, data = sym
            arr = array_parts(ty)
            if arr is None or kind == "iref":
                self.d.error("E134" if kind != "iref" else "E135",
                             f"'{e.name.text}' — не массив значений, индексировать нельзя",
                             e.line, e.col)
                return None
            if it is not None and it != INT:
                self.d.error("E134", f"индекс должен иметь тип int, получено {it}",
                             e.index.line, e.index.col)
            e.ref = (kind, data)
            e.array_ty = ty
            return arr[0]
        if isinstance(e, A.CallExpr):
            return self._call_expr(e, scope, ctx, what)
        if isinstance(e, A.Unary):
            t = self._expr(e.operand, scope, ctx, what)
            if t is None:
                return None
            if e.op == "not":
                if t != BOOL:
                    return self._bad(e, f"операция not требует bool, получено {t}")
                return BOOL
            if t not in NUMERIC:
                return self._bad(e, f"унарный минус требует число, получено {t}")
            return t
        lt = self._expr(e.left, scope, ctx, what)
        rt = self._expr(e.right, scope, ctx, what)
        if lt is None or rt is None:
            return None
        return self._binary(e, lt, rt)

    def _array_lit(self, e: A.ArrayLit, scope, ctx, what, init) -> Optional[str]:
        types = [self._expr(x, scope, ctx, what) for x in e.items]
        if not init:
            self.d.error("E134", "литерал массива допустим только в начальном значении "
                         "константы, поля или переменной", e.line, e.col)
            return None
        if not e.items:
            self.d.error("E134", "пустой литерал массива", e.line, e.col)
            return None
        if any(t is None for t in types):
            return None
        elem = MONEY if set(types) == NUMERIC else types[0]
        for x, t in zip(e.items, types):
            if not assignable(elem, t) or is_array(t) or is_ref(t):
                self.d.error("E134", f"элементы массива должны быть одного типа: {elem} и {t}",
                             x.line, x.col)
                return None
        return f"{elem}[{len(e.items)}]"

    def _call_expr(self, e: A.CallExpr, scope: dict, ctx: Ctx, what: str) -> Optional[str]:
        types = [self._expr(a, scope, ctx, what) for a in e.args]
        if ctx.const:
            self.d.error("E115", f"{what}: в константном выражении нельзя вызывать функции",
                         e.line, e.col)
            return None
        r = None
        if e.module is not None or e.name.text not in BUILTINS:
            r = self._resolve_routine(e.name, e.module, ctx, "func")
            if r is False:
                return None
        if r is None and e.module is None and e.name.text in BUILTINS:
            return self._builtin(e, types, ctx)
        if r is None:
            if self._resolve_routine(e.name, e.module, ctx, "action"):
                self.d.error("E114", f"'{e.name.text}' — действие: оно ничего не возвращает "
                             "и вызывается отдельным оператором", e.line, e.col)
                return None
            pool = list(BUILTINS) + list(ctx.unit.funcs) + (list(ctx.model.funcs)
                                                             if ctx.model else [])
            self.d.error("E110", f"{what} вызывает несуществующую функцию '{e.name.text}'"
                         + _hint(e.name.text, pool), e.line, e.col)
            return None
        e.ref = ("routine", r)
        r.used = True
        if ctx.routine is not None:
            ctx.routine.calls.add(r)
        self._args(f"функция {e.name.text}", r.node.params, r.param_types, e.args, types,
                   e.name)
        return r.ret

    def _builtin(self, e: A.CallExpr, types: list, ctx: Ctx) -> Optional[str]:
        name = e.name.text
        e.ref = ("builtin", name)
        if not BUILTINS[name] and ctx.pure:
            self._impure(e, f"вызов {name}")
        if any(t is None for t in types):
            return None
        sig = {"len": 1, "str": 1, "code": 2, "chr": 1, "min": 2, "max": 2, "random": 1}[name]
        if len(types) != sig:
            return self._bad(e, f"{name} ожидает {sig} аргумент(а), передано {len(types)}",
                             "E114")
        if name == "len":
            if types[0] == STRING or is_array(types[0]):
                return INT
            return self._bad(e, f"len определена для строк и массивов, получено "
                             f"{show_type(types[0])}", "E114")
        if name == "str":
            if types[0] in TYPES:
                return STRING
            return self._bad(e, f"str: нельзя превратить в строку {show_type(types[0])}",
                             "E114")
        if name == "code":
            if types == [STRING, INT]:
                return INT
            return self._bad(e, "code(строка, позиция) ожидает string и int", "E114")
        if name == "random":
            if types[0] == INT:
                return INT
            return self._bad(e, "random(n) ожидает int", "E114")
        if name == "chr":
            if types[0] == INT:
                return STRING
            return self._bad(e, "chr(код) ожидает int", "E114")
        if all(t in NUMERIC for t in types):  # min, max
            return MONEY if MONEY in types else INT
        return self._bad(e, f"{name} определена для чисел", "E114")

    def _binary(self, e: A.Binary, lt: str, rt: str) -> Optional[str]:
        op = e.op
        if is_array(lt) or is_array(rt):
            return self._bad(e, f"операция {op} не определена для массивов")
        if op in ("and", "or"):
            if lt == BOOL and rt == BOOL:
                return BOOL
            return self._bad(e, f"операция {op} требует bool, получено {lt} и {rt}")
        if op in ("==", "!="):
            if lt == rt or {lt, rt} == NUMERIC:
                return BOOL
            return self._bad(e, f"нельзя сравнивать {lt} и {rt}")
        if op in ("<", "<=", ">", ">="):
            if lt in NUMERIC and rt in NUMERIC:
                return BOOL
            return self._bad(e, f"операция {op} определена для чисел, получено {lt} и {rt}")
        if op == "+" and lt == STRING and rt == STRING:
            return STRING  # склейка строк
        # Арифметика. money — деньги: их можно складывать между собой и
        # умножать на int, но не умножать деньги на деньги.
        if lt not in NUMERIC or rt not in NUMERIC:
            return self._bad(e, f"операция {op} определена для чисел, получено {lt} и {rt}")
        if op in ("+", "-"):
            return MONEY if MONEY in (lt, rt) else INT
        if op == "*":
            if lt == MONEY and rt == MONEY:
                return self._bad(e, "нельзя умножать money на money")
            return MONEY if MONEY in (lt, rt) else INT
        # / и %
        if lt == INT and rt == MONEY:
            return self._bad(e, "нельзя делить int на money")
        if lt == MONEY and rt == MONEY:
            return INT if op == "/" else MONEY
        return lt

    def _bad(self, e, message: str, code: str = "E115") -> None:
        self.d.error(code, message, e.line, e.col)
        return None

    # ================================================================ переходы

    def _transitions(self, m: Model) -> None:
        explicit: dict[tuple[str, str], list[A.Transition]] = {}
        from_any: dict[str, list[A.Transition]] = {}
        wildcard: dict[Optional[str], list[A.Transition]] = {}
        for t in m.program.transitions:
            any_src = t.source.text == ANY
            if any(n.text == ANY for n in t.more) or (any_src and t.more):
                n = t.more[0]
                self.d.error("E137", "`any` уже означает «любое состояние»: другие состояния "
                             "рядом с ним не перечисляются", n.line, n.col)
            srcs = [n for n in t.sources if n.text != ANY]
            ok = [n.text for n in srcs
                  if self._state_ref(m, n, "переход из необъявленного состояния")]
            src_ok = any_src or bool(ok)
            if t.target is not None:
                self._state_ref(m, t.target, "переход ведёт в необъявленное состояние")
            # Guard и действия проверяются один раз на переход, даже если
            # событий несколько (`cancel | timeout`). Поэтому образцы всех
            # событий обязаны связывать одни и те же имена с одними и теми же
            # позициями и типами — иначе у guard'а был бы разный смысл.
            scopes = []
            for trig in t.triggers:
                if trig.event.text == ANY:
                    if trig.patterns is not None or len(t.triggers) > 1:
                        self.d.error("E137", "`any` в триггере означает «любое прочее "
                                     "событие»: без образца и без других событий",
                                     trig.event.line, trig.event.col)
                        continue
                    scopes.append((trig, self._base_scope(m)))
                    for src in ([None] if any_src else ok):
                        wildcard.setdefault(src, []).append(t)
                    continue
                scope = self._trigger(m, t, trig)
                if scope is None:
                    continue
                scopes.append((trig, scope))
                if not src_ok:
                    continue
                if any_src:
                    from_any.setdefault(trig.event.text, []).append(t)
                for src in ok:
                    explicit.setdefault((src, trig.event.text), []).append(t)
            if not scopes:
                continue
            bound = [{k: v for k, v in s.items() if v[0] == "param"} for _, s in scopes]
            for (trig, _), b in zip(scopes[1:], bound[1:]):
                if b != bound[0]:
                    self.d.error("E116", f"образцы событий {scopes[0][0].event.text} и "
                                 f"{trig.event.text} связывают разные имена: у перехода "
                                 "с несколькими событиями они должны совпадать",
                                 trig.event.line, trig.event.col)
            scope = scopes[0][1]
            ctx = Ctx(m.unit, m)
            if t.guard is not None:
                self._condition(t.guard, scope, Ctx(m.unit, m, pure=True), "guard",
                                code="E111")
            for call in t.actions:
                if isinstance(call, A.Send):
                    self._send(call, scope, ctx)
                else:
                    self._call(call, scope, ctx)

        # Раскрытие `any`. Названное событие важнее «любого», своё состояние
        # важнее «любого» (как умолчание у родителя в иерархических автоматах):
        #   S -- e   →   any -- e   →   S -- any   →   any -- any.
        # Следующий слой добавляется, только пока в цепочке нет перехода без
        # guard'а: дальше он всё равно не сработал бы.
        m.table = {}
        for s in m.states:
            for e in m.events:
                chain = list(explicit.get((s, e), []))
                for layer in (from_any.get(e, []), wildcard.get(s, []), wildcard.get(None, [])):
                    if any(t.guard is None for t in chain):
                        break
                    chain += layer
                if chain:
                    m.table[(s, e)] = chain
        self._determinism(m)

    def _trigger(self, m: Model, t: A.Transition, trig: A.Trigger) -> dict | None:
        """Проверить образец события и вернуть область видимости перехода."""
        info = m.events.get(trig.event.text)
        if info is None:
            self.d.error("E104", f"неизвестное событие '{trig.event.text}'"
                         + _hint(trig.event.text, m.events), trig.event.line, trig.event.col)
            return None
        params = info.decl.params
        scope = self._base_scope(m)
        # Событие с параметрами записывается только с образцом: `withdraw(_)`,
        # а не `withdraw`. Так пропущенный аргумент не проходит молча.
        patterns = trig.patterns if trig.patterns is not None else []
        if len(patterns) != len(params):
            sig = ", ".join(p.type.name for p in params) or "без параметров"
            self.d.error("E105", f"{trig.event.text} ожидает {len(params)} "
                         f"параметр(а) ({sig}), в образце {len(patterns)}",
                         trig.event.line, trig.event.col)
            return scope
        for i, (pat, decl) in enumerate(zip(patterns, params)):
            if pat.type is not None and pat.type.name != decl.type.name:
                self.d.error("E105", f"{trig.event.text} ожидает {decl.type.name}, "
                             f"получено {pat.type.name}", pat.type.line, pat.type.col)
            if pat.name.text == "_":
                continue
            if pat.name.text in scope:
                self.d.error("E113", f"имя образца '{pat.name.text}' совпадает с полем или "
                             "константой", pat.name.line, pat.name.col)
                continue
            scope[pat.name.text] = ("param", decl.type.name, i)
        return scope

    def _determinism(self, m: Model) -> None:
        for (state, event), ts in m.table.items():
            unguarded = [t for t in ts if t.guard is None]
            # E106: два перехода без guard'а — какой сработает, неизвестно.
            for t in unguarded[1:]:
                self.d.error("E106", f"недетерминизм: '{state}' -- {event} без guard'а "
                             f"определён повторно (первый — строка {unguarded[0].line})",
                             t.line, t.col)
            if unguarded:
                first = ts.index(unguarded[0])
                for t in ts[first + 1:]:
                    if t.guard is not None:
                        self.d.warning("W117", f"переход '{state}' -- {event} никогда не "
                                       "сработает: выше стоит переход без guard'а", t.line, t.col)
            guarded = [t for t in ts if t.guard is not None]
            # W107: несколько guard'ов без явного порядка.
            for t in guarded[1:]:
                if not t.ordered:
                    self.d.warning("W107", f"guard'ы '{state}' -- {event} могут перекрываться, "
                                   "применяется первый подходящий; напишите else [...], "
                                   "чтобы задать порядок явно", t.line, t.col)
            if ts and not unguarded:
                t = ts[-1]
                self.d.warning("W118", f"в '{state}' событие {event} может остаться "
                               "необработанным: у всех переходов есть guard, "
                               "добавьте переход с else", t.line, t.col)

    # ================================================================ граф

    def edges(self, m: Model) -> dict[str, list[tuple[str, str]]]:
        """Граф переходов: состояние -> [(событие, цель)]. Guard'ы не
        учитываются: считаем, что каждый может выполниться (консервативно)."""
        g: dict[str, list[tuple[str, str]]] = {s: [] for s in m.states}
        for (state, event), ts in m.table.items():
            for t in ts:
                if t.ignore:
                    continue
                target = t.target.text if t.target else state
                if target in m.states:
                    g[state].append((event, target))
        return g

    def _graph(self, m: Model) -> None:
        g = self.edges(m)
        where = {n.text: n for n in m.program.states}

        # W108: каждое событие обработано в каждом состоянии или явно
        # проигнорировано. Сообщение группирует события одного состояния.
        for s in m.states:
            missing = [e for e in m.events if (s, e) not in m.table]
            if missing:
                n = where[s]
                self.d.warning("W108", f"в '{s}' не обработаны события: {', '.join(missing)} "
                               f"(добавьте переход или `{s} -- ... ignore`)", n.line, n.col)

        # E102: достижимость из начального — обход в ширину.
        if m.initial:
            m.paths = _bfs(m.initial, g)
            for s in m.states:
                if s not in m.paths:
                    m.unreachable.add(s)
                    n = where[s]
                    self.d.error("E102", f"состояние '{s}' недостижимо из начального "
                                 f"'{m.initial}'", n.line, n.col)

        # E103: из каждого состояния достижимо терминальное — обход в ширину
        # по обращённому графу от всех терминальных сразу.
        if m.terminal:
            reverse: dict[str, list[tuple[str, str]]] = {s: [] for s in m.states}
            for s, out in g.items():
                for ev, t in out:
                    reverse[t].append((ev, s))
            back = set()
            for term in m.terminal:
                back |= set(_bfs(term, reverse))
            for s in m.states:
                if s not in back:
                    m.trapped.add(s)
                    n = where[s]
                    self.d.error("E103", f"из '{s}' нет пути в терминальное состояние "
                                 f"({', '.join(m.terminal)})", n.line, n.col)

        # Действие считается вызванным и из перехода с ошибкой: предупреждать
        # о нём поверх ошибки — лишний шум.
        named = {c.name.text for t in m.program.transitions for c in t.actions
                 if isinstance(c, A.Call) and c.module is None}
        for name, a in m.actions.items():
            if not a.used and name not in named:
                self.d.warning("W120", f"действие '{name}' нигде не вызывается",
                               a.node.name.line, a.node.name.col)

    # ================================================================ система

    def _system(self, u: UnitInfo, sysm: System) -> None:
        self._enter(u)
        node = u.node
        if not node.instances:
            self.d.error("E136", "в системе нет ни одного экземпляра автомата "
                         "(имя: Автомат(аргументы))", node.name.line, node.name.col)
        insts: dict[str, InstanceInfo] = {}
        for i in node.instances:
            model = self._instance_machine(u, i)
            if model is not None and i.name.text not in insts:
                insts[i.name.text] = InstanceInfo(i.name.text, model, node=i)
        for info in insts.values():
            self._instance_args(u, info, insts)
        sysm.instances = list(insts.values())

        if node.inputs:
            inputs = []
            for ref in node.inputs:
                info = insts.get(ref.instance.text)
                if info is None:
                    self.d.error("E138", f"input: нет экземпляра '{ref.instance.text}'"
                                 + _hint(ref.instance.text, insts),
                                 ref.instance.line, ref.instance.col)
                    continue
                if ref.event is None:
                    inputs += [(info.name, e) for e in info.model.events]
                elif ref.event.text in info.model.events:
                    inputs.append((info.name, ref.event.text))
                else:
                    self.d.error("E138", f"input: у {info.name} нет события '{ref.event.text}'"
                                 + _hint(ref.event.text, info.model.events),
                                 ref.event.line, ref.event.col)
            sysm.inputs = list(dict.fromkeys(inputs))
        else:
            sysm.inputs = [(i.name, e) for i in sysm.instances for e in i.model.events]

        # W139: событие, которое никто не посылает и которого нет в input.
        if node.inputs:
            sent = set(sysm.inputs)
            for info in sysm.instances:
                for s in info.model.sends:
                    for target in send_targets(info, s):
                        sent.add((target, s.event.text))
            for info in sysm.instances:
                dead = [e for e in info.model.events if (info.name, e) not in sent]
                if dead:
                    n = info.node.name
                    self.d.warning("W139", f"событие(я) {', '.join(dead)} экземпляра "
                                   f"{info.name} никто не посылает и их нет в input",
                                   n.line, n.col)

    def _instance_machine(self, u: UnitInfo, i: A.Instance) -> Optional[Model]:
        owner = u
        if i.module is not None:
            owner = u.imports.get(i.module.text)
            if owner is None:
                self.d.error("E130", f"модуль '{i.module.text}' не импортирован",
                             i.module.line, i.module.col)
                return None
        model = owner.machines.get(i.machine.text)
        if model is None:
            self.d.error("E136", f"неизвестный автомат '{i.machine.text}'"
                         + _hint(i.machine.text, owner.machines), i.machine.line, i.machine.col)
        return model

    def _instance_args(self, u: UnitInfo, info: InstanceInfo, insts: dict) -> None:
        i, m = info.node, info.model
        if len(i.args) != len(m.params):
            self.d.error("E136", f"автомат {m.name} ожидает {len(m.params)} аргумент(а), "
                         f"передано {len(i.args)}", i.machine.line, i.machine.col)
            return
        ctx = Ctx(u, const=True)
        for arg, pr, ty in zip(i.args, m.params, m.param_types):
            if ty is None:
                info.args.append(None)
                continue
            if not is_ref(ty):
                vt = self._expr(arg, self._unit_scope(u), ctx, what="аргумент экземпляра",
                                init=is_array(ty))
                if vt and not assignable(ty, vt):
                    self.d.error("E136", f"{m.name}: параметр {pr.name.text} ожидает {ty}, "
                                 f"получено {vt}", arg.line, arg.col)
                info.args.append(arg)
                continue
            arr = array_parts(ty)
            items = arg.items if isinstance(arg, A.ArrayLit) else None
            if arr and (items is None or len(items) != arr[1]):
                self.d.error("E136", f"{m.name}: параметр {pr.name.text} ожидает список из "
                             f"{arr[1]} экземпляров [a, b, ...]", arg.line, arg.col)
                info.args.append(None)
                continue
            names = []
            for x in (items if arr else [arg]):
                target = insts.get(x.name) if isinstance(x, A.Ref) and not x.module else None
                if target is None:
                    self.d.error("E136", f"{m.name}: параметр {pr.name.text} ожидает "
                                 "экземпляр автомата" + (_hint(x.name, insts)
                                                         if isinstance(x, A.Ref) else ""),
                                 x.line, x.col)
                    names = None
                    break
                missing = self._conforms(target.model, ty)
                if missing:
                    self.d.error("E136", f"{target.name} ({target.model.name}) не подходит "
                                 f"под {show_type(ty)}: {missing}", x.line, x.col)
                names.append(target.name)
            info.args.append(None if names is None else (names if arr else names[0]))

    def _conforms(self, m: Model, ty: str) -> str:
        """Пусто — автомат подходит под тип; иначе — чего не хватает.
        Интерфейс проверяется структурно: все его события с теми же типами."""
        unit_name, name = ty[1:].split("[")[0].split(".", 1)
        if name in self._main_unit(unit_name).machines:
            ok = m.unit.name == unit_name and m.name == name
            return "" if ok else f"ожидается автомат {name}"
        problems = []
        for ev in self.ref_events(ty).values():
            own = m.events.get(ev.name.text)
            want = [p.type.name for p in ev.params]
            if own is None:
                problems.append(f"нет события {ev.name.text}")
            elif own.param_types != want:
                problems.append(f"{ev.name.text}({', '.join(own.param_types)}) вместо "
                                f"{ev.name.text}({', '.join(want)})")
        return "; ".join(problems)

    # ================================================================ рекурсия

    def _recursion(self, routines: list[Routine]) -> None:
        """E133: граф вызовов функций и действий ацикличен. Вместе с
        ограниченным for это гарантирует, что каждый шаг автомата завершается."""
        color: dict[Routine, int] = {}
        reported: set = set()

        def visit(r: Routine, path: list[Routine]) -> None:
            color[r] = 1
            for c in r.calls:
                if color.get(c) == 1:
                    cycle = path[path.index(c):] + [c] if c in path else [r, c]
                    key = frozenset(id(x) for x in cycle)
                    if key not in reported:
                        reported.add(key)
                        n = c.node.name
                        self.d = c.unit.diags
                        self.d.error("E133", "рекурсия запрещена: "
                                     + " → ".join(x.name for x in cycle)
                                     + " (шаг автомата обязан завершаться)", n.line, n.col)
                elif c not in color:
                    visit(c, path + [c])
            color[r] = 2

        for r in routines:
            if r not in color:
                visit(r, [r])


def send_targets(info: InstanceInfo, s: A.Send) -> list[str]:
    """Имена экземпляров, которым может уйти этот send из экземпляра info."""
    if s.ref is None:
        return []
    if s.ref[0] == "self":
        return [info.name]
    arg = info.args[s.ref[1]] if s.ref[1] < len(info.args) else None
    if arg is None:
        return []
    return arg if isinstance(arg, list) else [arg]


def _returns(body: list) -> bool:
    """Заканчивается ли каждый путь блока оператором return."""
    for s in body:
        if isinstance(s, A.Return):
            return True
        if isinstance(s, A.If) and s.else_ is not None and _returns(s.then) \
                and _returns(s.else_):
            return True
    return False


def _bfs(start: str, g: dict[str, list[tuple[str, str]]]) -> dict[str, list[str]]:
    """Достижимые вершины и для каждой — кратчайший путь событий до неё."""
    paths = {start: []}
    queue = deque([start])
    while queue:
        s = queue.popleft()
        for ev, t in g.get(s, []):
            if t not in paths:
                paths[t] = paths[s] + [ev]
                queue.append(t)
    return paths


def _closest(name: str, pool) -> str | None:
    found = difflib.get_close_matches(name, list(pool), n=1, cutoff=0.75)
    return found[0] if found else None


def _hint(name: str, pool) -> str:
    c = _closest(name, pool)
    return f" — возможно, '{c}'?" if c else ""


def analyze(root, diags: Diagnostics, loader: Optional[Loader] = None,
            frontend: str = "antlr") -> System:
    return Analyzer(root, diags, loader, frontend).run()
