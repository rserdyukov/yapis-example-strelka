"""fsmc — учебный компилятор языка конечных автоматов FSM в WebAssembly.

Конвейер (HOWTO, § 2.1):

    исходник ─► фронтенд (ANTLR или Lark) ─► AST ─► семантика ─► генератор ─► WAT + .wasm
                        ошибки? стоп                  ошибки? стоп

Публичный вход — ``compile_source``: его зовут CLI, тесты и playground.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import ast
from .diagnostics import Diagnostic, Diagnostics, SyntaxFailure

__version__ = "0.2.0"
FRONTENDS = ("antlr", "lark")


def get_frontend(name: str):
    """Фронтенды импортируются лениво: для Lark не нужен ANTLR runtime и наоборот."""
    if name == "antlr":
        from . import frontend_antlr as fe
    elif name == "lark":
        from . import frontend_lark as fe
    else:
        raise ValueError(f"неизвестный фронтенд {name!r}, доступны: {', '.join(FRONTENDS)}")
    return fe


@dataclass
class Result:
    diagnostics: list[Diagnostic]
    program: object = None  # ast.Program или ast.Unit
    model: object = None  # semantic.System
    wat: str = ""
    wasm: bytes = b""
    meta: dict = field(default_factory=dict)
    mermaid: str = ""

    @property
    def ok(self) -> bool:
        return not any(d.level == "error" for d in self.diagnostics)


def parse(source: str, frontend: str = "antlr"):
    diags = Diagnostics()
    try:
        return get_frontend(frontend).parse(source, diags), diags
    except SyntaxFailure:
        return None, diags


def file_loader(directory) -> Callable[[str], Optional[tuple[str, str]]]:
    """Загрузчик модулей: ``import cards`` → файл ``cards.fsm`` в каталоге."""
    from pathlib import Path

    def load(name: str):
        path = Path(directory) / f"{name}.fsm"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8"), path.name
    return load


def dict_loader(files: dict[str, str]) -> Callable[[str], Optional[tuple[str, str]]]:
    """Загрузчик модулей из словаря «имя файла → текст» (playground, тесты)."""
    def load(name: str):
        key = f"{name}.fsm"
        return (files[key], key) if key in files else None
    return load


def compile_source(source: str, frontend: str = "antlr", loader=None) -> Result:
    """Скомпилировать исходник. ``loader(имя_модуля)`` возвращает (текст,
    имя файла) или None — так разрешаются ``import``."""
    from .codegen import generate
    from .diagram import mermaid
    from .semantic import analyze

    program, diags = parse(source, frontend)
    if program is None:
        return Result(diags.sorted())
    model = analyze(program, diags, loader, frontend)
    result = Result(diags.sorted(), program=program, model=model)
    # Диаграмма строится и при семантических ошибках: по ней их легче понять.
    result.mermaid = mermaid(model)
    if diags.has_errors:
        return result
    if model.kind == "module":
        return result  # модуль — библиотека: кода не порождает
    module, result.meta = generate(model)
    result.wat = module.to_wat()
    result.wasm = module.to_wasm()
    return result
