"""Infer a Pydantic model from sample JSON and render it as Python source.

API keys are untrusted, so they never reach the source as code: field names must be
safe identifiers, and original keys go in as repr()'d string aliases.
"""

from __future__ import annotations

import keyword
import re
from datetime import datetime
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_ident(key: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", key)
    if not name or name[0].isdigit():
        name = f"f_{name}"
    if keyword.iskeyword(name) or name in {"model_config", "model_fields"}:
        name = f"{name}_"
    return name


def class_name(key: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", key)
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not name or name[0].isdigit() or not _IDENT.match(name):
        name = f"Model{safe_ident(name)}"
    return name


def _is_datetime(s: str) -> bool:
    if len(s) < 10 or s[4:5] != "-":
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class _Gen:
    def __init__(self) -> None:
        self.classes: dict[str, list[str]] = {}
        self.needs_field = False
        self.needs_datetime = False
        self.needs_any = False

    def scalar(self, values: list[Any]) -> str:
        present = [v for v in values if v is not None]
        optional = len(present) < len(values)
        if not present:
            self.needs_any = True
            return "Any | None"
        kinds = set()
        for v in present:
            if isinstance(v, bool):
                kinds.add("bool")
            elif isinstance(v, int):
                kinds.add("int")
            elif isinstance(v, float):
                kinds.add("float")
            elif isinstance(v, str):
                kinds.add("datetime" if _is_datetime(v) else "str")
            elif isinstance(v, list):
                kinds.add("list")
            else:
                kinds.add("other")
        if kinds == {"int", "float"}:
            kinds = {"float"}
        if kinds == {"datetime", "str"}:
            kinds = {"str"}
        if len(kinds) != 1 or kinds & {"list", "other"}:
            if kinds == {"list"}:
                inner = [x for v in present for x in v]
                t = (
                    f"list[{self.scalar(inner)}]"
                    if inner and not any(isinstance(x, dict | list) for x in inner)
                    else "list[Any]"
                )
                if t == "list[Any]":
                    self.needs_any = True
            else:
                self.needs_any = True
                t = "Any"
        else:
            t = kinds.pop()
            if t == "datetime":
                self.needs_datetime = True
        return f"{t} | None" if optional else t

    def model(self, name: str, records: list[dict[str, Any]]) -> str:
        name = self._unique(name)
        self.classes[name] = []  # reserve the name before children
        keys: list[str] = []
        for r in records:
            keys += [k for k in r if k not in keys]
        lines = []
        used: set[str] = set()
        for key in keys:
            values = [r.get(key) for r in records]
            missing = any(key not in r for r in records)
            dicts = [v for v in values if isinstance(v, dict)]
            if dicts and len(dicts) == len([v for v in values if v is not None]):
                t = self.model(class_name(key), dicts)
                if len(dicts) < len(values):
                    t += " | None"
            else:
                t = self.scalar(values)
            field = safe_ident(key)
            while field in used:
                field += "_"
            used.add(field)
            optional = missing or t.endswith("| None")
            if optional and not t.endswith("| None"):
                t += " | None"
            if field != key:
                self.needs_field = True
                default = "None, " if optional else ""
                lines.append(f"    {field}: {t} = Field({default}alias={key!r})")
            else:
                lines.append(f"    {field}: {t}{' = None' if optional else ''}")
        self.classes[name] = lines or ["    pass"]
        return name

    def _unique(self, name: str) -> str:
        base, n = name, 2
        while name in self.classes:
            name, n = f"{base}{n}", n + 1
        return name


def generate_models(records: list[dict[str, Any]], name: str = "Model") -> str:
    if not records or not all(isinstance(r, dict) for r in records):
        raise ValueError("need at least one JSON object to infer a model")
    gen = _Gen()
    gen.model(class_name(name), records)
    imports = ["from __future__ import annotations", ""]
    if gen.needs_datetime:
        imports.append("from datetime import datetime")
    if gen.needs_any:
        imports.append("from typing import Any")
    if gen.needs_datetime or gen.needs_any:
        imports.append("")
    pyd = "BaseModel, Field" if gen.needs_field else "BaseModel"
    imports.append(f"from pydantic import {pyd}")
    out = ['"""Generated by `aegis init`. Edit freely: this is your contract."""', "", *imports]
    # Children were registered after parents; emit children first.
    for cls in reversed(list(gen.classes)):
        out += ["", "", f"class {cls}(BaseModel):", *gen.classes[cls]]
    return "\n".join(out) + "\n"
