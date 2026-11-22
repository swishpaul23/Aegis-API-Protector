"""Case schema for the real-API eval, plus the stable dev/test split."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

FieldType = Literal["string", "integer", "number", "boolean", "datetime"]


class ExpectedMap(BaseModel):
    target: str  # dotted path in the old model
    source: str  # dotted path in the new payload
    # how the old value is represented in the new payload
    convert: Literal["none", "int_str", "int_float", "epoch_iso"] = "none"


class Case(BaseModel):
    id: str
    api: str
    source: dict[str, str]  # repo, old_ref, new_ref, spec_path, schema
    kind: Literal["rename", "move", "retype", "split-merge", "unmappable"]
    old_fields: dict[str, FieldType]
    new_fields: dict[str, FieldType]
    expected: Literal["repair", "refuse"]
    maps: list[ExpectedMap]
    label_status: Literal["drafted", "confirmed"] = "drafted"
    ambiguous: bool = False
    notes: str = ""

    @model_validator(mode="after")
    def _labels_consistent(self) -> Case:
        if self.expected == "refuse" and self.maps:
            raise ValueError("refuse cases must have empty maps")
        if self.expected == "repair" and not self.ambiguous and not self.maps:
            raise ValueError("unambiguous repair cases need non-empty maps")
        if self.kind == "unmappable" and self.expected != "refuse":
            raise ValueError("unmappable cases must have expected='refuse'")
        return self


def split_of(case_id: str) -> Literal["dev", "test"]:
    """Stable ~30% dev / 70% test split derived from the id."""
    n = int.from_bytes(hashlib.sha256(case_id.encode()).digest()[:8], "big")
    return "dev" if n % 100 < 30 else "test"


def load_cases(dir: Path) -> list[Case]:
    return [Case.model_validate_json(p.read_text()) for p in sorted(Path(dir).glob("*.json"))]


def save_case(case: Case, dir: Path) -> Path:
    path = Path(dir) / f"{case.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return path
