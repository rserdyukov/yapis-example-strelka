"""Абстрактное синтаксическое дерево языка FSM.

Оба фронтенда (ANTLR и Lark) строят именно эти узлы, поэтому всё, что идёт
после разбора, — семантика, генерация кода, диаграммы — от выбора парсера не
зависит. Тест test_frontends сравнивает деревья обоих фронтендов через ``==``.

Позиции (line, col) считаются с единицы. Поля, которые заполняет семантический
анализатор (``ty``, ``ref``), объявлены с ``compare=False``: аннотации не
должны влиять на сравнение деревьев.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class Name:
    text: str
    line: int
    col: int


@dataclass
class TypeRef:
    """Тип: ``int``, массив фиксированной длины ``int[52]`` (``size``) или
    автомат — тип параметра-ссылки на экземпляр: ``Table``, ``traffic.Light``."""

    name: str
    line: int
    col: int
    size: Optional[int] = None
    module: Optional[str] = None

    @property
    def text(self) -> str:
        head = f"{self.module}.{self.name}" if self.module else self.name
        return head if self.size is None else f"{head}[{self.size}]"


# ---------------------------------------------------------------- выражения


@dataclass
class IntLit:
    value: int
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class StrLit:
    value: str
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class BoolLit:
    value: bool
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Ref:
    """Имя в выражении: поле контекста, константа, параметр действия или
    имя, связанное образцом события, локальная переменная, параметр
    экземпляра. Что именно — решает семантика и записывает в ``ref`` =
    (вид, данные). ``module`` — квалификатор: ``cards.ACE``."""

    name: str
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)
    ref: Optional[tuple] = field(default=None, compare=False)
    module: Optional[str] = None


@dataclass
class Index:
    """Элемент массива ``deck[i]``, ``cards.RANKS[r]``."""

    name: Name
    index: "Expr"
    module: Optional[Name]
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)
    ref: Optional[tuple] = field(default=None, compare=False)
    array_ty: Optional[str] = field(default=None, compare=False)


@dataclass
class CallExpr:
    """Вызов функции в выражении: ``score(h)``, ``cards.rank(c)``, ``len(s)``.
    ``ref`` — описание функции (FuncInfo) или имя встроенной."""

    name: Name
    args: list["Expr"]
    module: Optional[Name]
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)
    ref: Optional[object] = field(default=None, compare=False)


@dataclass
class ArrayLit:
    """``[1, 2, 3]`` — только в значении константы-массива."""

    items: list["Expr"]
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Unary:
    op: str  # "-" | "not"
    operand: "Expr"
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Binary:
    op: str  # + - * / % == != < <= > >= and or
    left: "Expr"
    right: "Expr"
    line: int
    col: int
    ty: Optional[str] = field(default=None, compare=False)


Expr = Union[IntLit, StrLit, BoolLit, Ref, Index, CallExpr, ArrayLit, Unary, Binary]


# ---------------------------------------------------------------- операторы


@dataclass
class Assign:
    target: Name
    value: Expr
    line: int
    col: int
    index: Optional[Expr] = None  # ``deck[i] = v``
    ref: Optional[tuple] = field(default=None, compare=False)
    target_ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Var:
    """Локальная переменная ``var x: int = 0`` — живёт до конца блока."""

    name: Name
    type: TypeRef
    init: Optional[Expr]
    line: int
    col: int
    slot: Optional[str] = field(default=None, compare=False)  # уникальное имя локальной
    type_ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Say:
    """``say`` печатает строку целиком; ``write`` — без перевода строки."""

    items: list[Expr]
    line: int
    col: int
    newline: bool = True


@dataclass
class If:
    cond: Expr
    then: list["Stmt"]
    else_: Optional[list["Stmt"]]
    line: int
    col: int


@dataclass
class For:
    """``for i in a .. b { }`` — i от a до b-1; границы вычисляются один раз,
    переменная цикла неизменяема, поэтому цикл всегда завершается."""

    var: Name
    start: Expr
    end: Expr
    body: list["Stmt"]
    line: int
    col: int
    slot: Optional[str] = field(default=None, compare=False)
    end_slot: Optional[str] = field(default=None, compare=False)


@dataclass
class Return:
    value: Expr
    line: int
    col: int


@dataclass
class Send:
    """``send table.bet(10)``, ``send seats[i].card(c)``, ``send self.next`` —
    поставить событие в очередь экземпляра. Обработается после текущего шага."""

    target: Name
    index: Optional[Expr]
    event: Name
    args: list[Expr]
    line: int
    col: int
    ref: Optional[tuple] = field(default=None, compare=False)


Stmt = Union[Var, Assign, Say, If, For, Return, Send, "Call"]


# ---------------------------------------------------------------- объявления


@dataclass
class Const:
    name: Name
    type: TypeRef
    value: Expr
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Field:
    name: Name
    type: TypeRef
    init: Optional[Expr]
    ty: Optional[str] = field(default=None, compare=False)


@dataclass
class Param:
    """Параметр. ``byref`` — ``var a: int[5]``: массив передаётся по ссылке и
    его можно менять; без ``var`` параметр только для чтения."""

    name: Name
    type: TypeRef
    byref: bool = False


@dataclass
class EventDecl:
    name: Name
    params: list[Param]


@dataclass
class Action:
    name: Name
    params: list[Param]
    body: list[Stmt]


@dataclass
class Func:
    """Чистая функция: читает контекст, но не меняет его и ничего не печатает."""

    name: Name
    params: list[Param]
    ret: TypeRef
    body: list[Stmt]
    ret_type: Optional[str] = field(default=None, compare=False)


@dataclass
class Import:
    name: Name


@dataclass
class Interface:
    """Набор событий. Автомат подходит под интерфейс, если объявляет все его
    события с теми же типами параметров (структурная типизация, как в Go)."""

    name: Name
    events: list[EventDecl]


@dataclass
class InputRef:
    """``input you.fold`` или ``input table`` — какие события система
    принимает извне."""

    instance: Name
    event: Optional[Name]


@dataclass
class Instance:
    """Экземпляр автомата в system: ``bot: Bot(1)``, ``l: traffic.Light``."""

    name: Name
    machine: Name
    module: Optional[Name]
    args: list[Expr]


@dataclass
class Pattern:
    """Образец параметра события: ``c``, ``_`` или ``s: money``."""

    name: Name
    type: Optional[TypeRef]


@dataclass
class Trigger:
    """``pin(c)``. ``patterns is None`` — событие записано без скобок."""

    event: Name
    patterns: Optional[list[Pattern]]


@dataclass
class Call:
    """Вызов действия: в переходе (``/ greet``) или оператором (``show(h)``).
    ``module`` — действие из модуля: ``poker.show(h)``."""

    name: Name
    args: list[Expr]
    module: Optional[Name] = None
    ref: Optional[object] = field(default=None, compare=False)


@dataclass
class Transition:
    source: Name
    triggers: list[Trigger]
    guard: Optional[Expr]
    ordered: bool  # записан ли ``else``: явный порядок проверки guard'ов
    target: Optional[Name]  # None — переход в себя
    actions: list[Union[Call, Send]]
    ignore: bool
    line: int
    col: int
    more: list[Name] = field(default_factory=list)  # `A | B -- e`: B и далее

    @property
    def sources(self) -> list[Name]:
        return [self.source] + self.more


@dataclass
class Program:
    name: Name
    consts: list[Const] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    events: list[EventDecl] = field(default_factory=list)
    states: list[Name] = field(default_factory=list)
    initial: list[Name] = field(default_factory=list)
    terminal: list[Name] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    funcs: list[Func] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)  # параметры экземпляра в system


@dataclass
class Unit:
    """Файл ``module`` (библиотека) или ``system`` (композиция автоматов).
    Файл ``machine`` по-прежнему разбирается в ``Program``."""

    kind: str  # "module" | "system"
    name: Name
    imports: list[Import] = field(default_factory=list)
    consts: list[Const] = field(default_factory=list)
    funcs: list[Func] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    interfaces: list[Interface] = field(default_factory=list)
    machines: list[Program] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    inputs: list[InputRef] = field(default_factory=list)


# ---------------------------------------------------------------- печать


_PREC = {"or": 1, "and": 2, "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
         "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}


def show_expr(e: Expr, parent: int = 0) -> str:
    """Выражение обратно в текст — для диаграмм и сообщений об ошибках."""
    if isinstance(e, IntLit):
        return str(e.value)
    if isinstance(e, StrLit):
        return '"' + e.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(e, BoolLit):
        return "true" if e.value else "false"
    if isinstance(e, Ref):
        return f"{e.module}.{e.name}" if e.module else e.name
    if isinstance(e, Index):
        head = f"{e.module.text}.{e.name.text}" if e.module else e.name.text
        return f"{head}[{show_expr(e.index)}]"
    if isinstance(e, ArrayLit):
        return "[" + ", ".join(show_expr(x) for x in e.items) + "]"
    if isinstance(e, CallExpr):
        head = f"{e.module.text}.{e.name.text}" if e.module else e.name.text
        return f"{head}({', '.join(show_expr(a) for a in e.args)})"
    if isinstance(e, Unary):
        prec = 3 if e.op == "not" else 7
        inner = show_expr(e.operand, prec)
        text = f"not {inner}" if e.op == "not" else f"-{inner}"
        return f"({text})" if parent > prec else text
    prec = _PREC[e.op]
    text = f"{show_expr(e.left, prec)} {e.op} {show_expr(e.right, prec + 1)}"
    return f"({text})" if prec < parent else text
