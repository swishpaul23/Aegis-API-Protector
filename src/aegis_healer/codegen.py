"""Infer a Pydantic model from sample JSON and render it as Python source.

API keys are untrusted, so they never reach the source as code: field and class names
are sanitised identifiers that can't shadow a type or each other, and original keys
only appear as repr()'d string aliases.
"""

from __future__ import annotations

import builtins
import keyword
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

# Names a generated field or class must never take: builtins (str, int, list…),
# names we import, and Pydantic's reserved namespace.
RESERVED = set(dir(builtins)) | {"BaseModel", "Field", "Any", "datetime", "annotations"}
_DT = TypeAdapter(datetime)


def safe_ident(key: str) -> str:
    """A field name for an API key: ASCII identifier, no leading underscore, not reserved."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", key).lstrip("_")
    if not name or name[0].isdigit() or name.startswith("model_"):
        name = f"f_{name}"
    if keyword.iskeyword(name) or name in RESERVED:
        name = f"{name}_"
    return name


def class_name(key: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", key)
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not name or name[0].isdigit():
        name = f"Model{name}"
    if name in RESERVED or keyword.iskeyword(name):
        name = f"{name}Model"
    return name


def _is_datetime(s: str) -> bool:
    # Require a date shape first: Pydantic also parses numeric strings as timestamps.
    if len(s) < 10 or s[4:5] != "-" or not s[:4].isdigit():
        return False
    try:
        _DT.validate_python(s)
    except ValidationError:
        return False
    return True


@dataclass
class _Field:
    key: str
    type: str
    optional: bool


class _Gen:
    def __init__(self) -> None:
        self.classes: dict[str, list[_Field]] = {}
        self.needs_datetime = False
        self.needs_any = False

    def _any(self) -> str:
        self.needs_any = True
        return "Any"

    def scalar(self, values: list[Any]) -> str:
        present = [v for v in values if v is not None]
        if not present:
            return self._any()
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
            else:
                kinds.add("other")
        if kinds == {"int", "float"}:
            kinds = {"float"}
        if kinds == {"datetime", "str"}:
            kinds = {"str"}
        if len(kinds) != 1 or "other" in kinds:
            return self._any()
        t = kinds.pop()
        if t == "datetime":
            self.needs_datetime = True
        return t

    def field_type(self, key: str, values: list[Any]) -> str:
        present = [v for v in values if v is not None]
        if present and all(isinstance(v, dict) for v in present):
            return self.model(class_name(key), present)
        if present and all(isinstance(v, list) for v in present):
            items = [x for v in present for x in v]
            real = [x for x in items if x is not None]
            if real and all(isinstance(x, dict) for x in real):
                inner = self.model(class_name(key) + "Item", real)
            elif real and not any(isinstance(x, dict | list) for x in real):
                inner = self.scalar(real)
            else:
                inner = self._any()
            if len(real) < len(items) and inner != "Any":
                inner += " | None"
            return f"list[{inner}]"
        return self.scalar(values)

    def model(self, name: str, records: list[dict[str, Any]]) -> str:
        base, n = name, 2
        while name in self.classes:
            name, n = f"{base}{n}", n + 1
        self.classes[name] = []  # reserve the name before children
        keys: list[str] = []
        for r in records:
            keys += [k for k in r if k not in keys]
        fields = []
        for key in keys:
            values = [r.get(key) for r in records]
            optional = any(key not in r or r[key] is None for r in records)
            fields.append(_Field(key=key, type=self.field_type(key, values), optional=optional))
        self.classes[name] = fields
        return name

    def render(self) -> list[str]:
        out: list[str] = []
        class_names = set(self.classes)
        # Children were registered after parents; emit children first.
        for cls in reversed(list(self.classes)):
            out += ["", "", f"class {cls}(BaseModel):"]
            used: set[str] = set()
            lines = []
            for f in self.classes[cls]:
                name = safe_ident(f.key)
                while name in used or name in class_names:
                    name += "_"
                used.add(name)
                t = f.type
                if f.optional and t != "Any" and not t.endswith("| None"):
                    t += " | None"
                if name != f.key:
                    arg = "None, " if f.optional else ""
                    lines.append(f"    {name}: {t} = Field({arg}alias={f.key!r})")
                else:
                    lines.append(f"    {name}: {t}{' = None' if f.optional else ''}")
            out += lines or ["    pass"]
        return out


def generate_models(records: list[dict[str, Any]], name: str = "Model") -> str:
    if not records or not all(isinstance(r, dict) for r in records):
        raise ValueError("need at least one JSON object to infer a model")
    gen = _Gen()
    gen.model(class_name(name), records)
    body = gen.render()
    uses_field = any("= Field(" in line for line in body)
    imports = ["from __future__ import annotations", ""]
    if gen.needs_datetime:
        imports.append("from datetime import datetime")
    if gen.needs_any:
        imports.append("from typing import Any")
    if gen.needs_datetime or gen.needs_any:
        imports.append("")
    imports.append(f"from pydantic import {'BaseModel, Field' if uses_field else 'BaseModel'}")
    header = ['"""Generated by `aegis init`. Edit freely: this is your contract."""', "", *imports]
    return "\n".join(header + body) + "\n"
