"""Диаграмма автомата в Mermaid (stateDiagram-v2) — усложнение из карточки, п. 9.

Строится из той же модели, что и код, поэтому показывает автомат таким, каким
его понял компилятор. Состояния с ошибками E102/E103 подсвечиваются.

У системы каждый экземпляр — составное состояние (рамка); ``send`` между
экземплярами — стрелка между рамками с именем события. У модуля рисуются
его шаблоны автоматов.
"""

from __future__ import annotations

from . import ast as A
from .ast import show_expr
from .semantic import Model, System, send_targets


def _label(text: str) -> str:
    # В подписи перехода Mermaid ломается на `:`, `;`, `#` и угловых скобках
    # (сущности &lt; он тоже обрезает). Заменяем их похожими символами Unicode:
    # подпись читается так же, а разбор не ломается.
    for ascii_, uni in (("<=", "≤"), (">=", "≥"), ("!=", "≠"),
                        ("<", "‹"), (">", "›"), (":", "∶"),
                        (";", ","), ("#", "♯")):
        text = text.replace(ascii_, uni)
    return text


def _call_name(c) -> str:
    if isinstance(c, A.Send):
        return f"send {c.target.text}.{c.event.text}"
    return f"{c.module.text}.{c.name.text}" if c.module else c.name.text


def _machine(model: Model, prefix: str = "", pad: str = "    ") -> list[str]:
    """Переходы одного автомата. ``prefix`` делает имена состояний разных
    экземпляров различимыми: ``table_Deal``."""
    def sid(s: str) -> str:
        return f"{prefix}{s}"

    lines = []
    if model.initial:
        lines.append(f"{pad}[*] --> {sid(model.initial)}")
    for term in model.terminal:
        lines.append(f"{pad}{sid(term)} --> [*]")
    if prefix:
        for s in model.states:
            lines.append(f"{pad}state \"{s}\" as {sid(s)}")
    # Переходы с одинаковыми концами и подписью, но разными событиями
    # (`cancel | timeout`) сливаются в одну стрелку: `cancel | timeout / eject`.
    arrows: dict[tuple, list[str]] = {}
    for (state, event), ts in model.table.items():
        for t in ts:
            if t.ignore:
                continue
            target = t.target.text if t.target else state
            if target not in model.states:
                continue
            tail = ""
            if t.guard is not None:
                tail += f" [{show_expr(t.guard)}]"
            elif t.ordered:
                tail += " [else]"
            if t.actions:
                tail += " / " + ", ".join(_call_name(c) for c in t.actions)
            trig = "any" if any(tr.event.text == "any" for tr in t.triggers) else event
            events = arrows.setdefault((state, target, id(t), tail), [])
            if trig not in events:
                events.append(trig)
    for (state, target, _, tail), events in arrows.items():
        lines.append(f"{pad}{sid(state)} --> {sid(target)}: "
                     f"{_label(' | '.join(events) + tail)}")
    # Состояние без единой стрелки иначе не попало бы на диаграмму.
    drawn = {s for s, t, _, _ in arrows} | {t for _, t, _, _ in arrows}
    for s in model.states:
        if s not in drawn and s != model.initial and s not in model.terminal and not prefix:
            lines.append(f"{pad}{s}")
    bad = model.unreachable | model.trapped
    if bad:
        lines.append(f"{pad}class " + ",".join(sid(s) for s in model.states if s in bad)
                     + " bad")
    return lines


def mermaid(system: System) -> str:
    lines = ["stateDiagram-v2"]
    if system.kind == "machine":
        lines += _machine(system.model)
    else:
        groups = ([(i.name, i.model, i) for i in system.instances] if system.kind == "system"
                  else [(m.name, m, None) for m in system.main.machines.values()])
        for name, model, _ in groups:
            title = name if model.name == name else f"{name}: {model.name}"
            lines.append(f"    state \"{title}\" as {name} {{")
            lines += _machine(model, f"{name}_", "        ")
            lines.append("    }")
        links: dict[tuple[str, str], list[str]] = {}
        for name, model, inst in groups:
            if inst is None:
                continue
            for s in model.sends:
                for target in send_targets(inst, s):
                    events = links.setdefault((name, target), [])
                    if target != name and s.event.text not in events:
                        events.append(s.event.text)
        for (name, target), events in links.items():
            if events:
                lines.append(f"    {name} --> {target}: {_label(', '.join(events))}")
    if any(m.unreachable | m.trapped for m in system.models):
        lines.insert(1, "    classDef bad fill:#fde2e1,stroke:#c0392b,color:#7b1d14")
    return "\n".join(lines) + "\n"
