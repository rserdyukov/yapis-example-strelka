"""Мост для playground: компилятор, вызываемый из JavaScript через Pyodide.

Возвращает один JSON: так на границе Python/JS не нужно разбираться с
преобразованием PyProxy, а двоичный модуль передаётся в base64.
Этот же код проверяется обычными тестами в CPython (tests/test_web.py).
"""

from __future__ import annotations

import base64
import json
import time

from . import ast as A
from . import compile_source, dict_loader


def compile_json(source: str, frontend: str = "antlr", files: str = "{}") -> str:
    """``files`` — JSON «имя файла → текст» для import (модули примера)."""
    started = time.perf_counter()
    r = compile_source(source, frontend, dict_loader(json.loads(files)))
    elapsed = (time.perf_counter() - started) * 1000
    out = {
        "ok": r.ok,
        "frontend": frontend,
        "ms": round(elapsed, 1),
        "diagnostics": [d.to_dict() for d in r.diagnostics],
        "mermaid": r.mermaid,
        "wat": r.wat,
        "wasm": base64.b64encode(r.wasm).decode("ascii"),
        "meta": r.meta,
        "ast": dump_ast(r.program) if r.program is not None else "",
        "stats": _stats(r),
    }
    return json.dumps(out, ensure_ascii=False)


def _stats(r) -> dict:
    if r.model is None:
        return {}
    models = r.model.models
    return {"machines": len(models), "instances": len(r.model.instances),
            "states": sum(len(m.states) for m in models),
            "events": sum(len(m.events) for m in models),
            "actions": sum(len(m.actions) for m in models),
            "transitions": sum(len(m.program.transitions) for m in models),
            "wat_lines": r.wat.count("\n"), "wasm_bytes": len(r.wasm)}


def dump_ast(node, indent: int = 0) -> str:
    """AST в виде дерева: имя узла и его поля без позиций и аннотаций."""
    pad = "  " * indent
    if isinstance(node, list):
        return "\n".join(dump_ast(x, indent) for x in node)
    if isinstance(node, (A.Name, A.TypeRef)):
        return f"{pad}{node.text}"
    if isinstance(node, (A.IntLit, A.StrLit, A.BoolLit, A.Ref, A.Unary, A.Binary, A.Index,
                         A.CallExpr, A.ArrayLit)):
        return f"{pad}{type(node).__name__}  {A.show_expr(node)}"
    lines = [f"{pad}{type(node).__name__}"]
    for name, value in vars(node).items():
        if name in ("line", "col", "ty", "ref", "slot", "end_slot", "type_ty", "target_ty",
                    "array_ty", "ret_type") or value is None or value == [] or value is False:
            continue
        if isinstance(value, (bool, str, int)):
            lines.append(f"{pad}  {name}: {value}")
            continue
        simple = isinstance(value, (A.Name, A.TypeRef))
        if simple:
            lines.append(f"{pad}  {name}: {dump_ast(value).strip()}")
        elif isinstance(value, list) and all(isinstance(x, A.Name) for x in value):
            lines.append(f"{pad}  {name}: " + ", ".join(x.text for x in value))
        else:
            lines.append(f"{pad}  {name}:")
            lines.append(dump_ast(value, indent + 2))
    return "\n".join(lines)
