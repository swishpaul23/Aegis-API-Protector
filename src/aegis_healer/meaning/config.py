"""Per-field meaning-drift policy from `.aegis/config.toml`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Policy = Literal["block", "flag", "off"]


class MeaningConfig(BaseModel):
    default: Policy = "block"
    fields: dict[str, Policy] = Field(default_factory=dict)

    def policy_for(self, model_key: str, path: str) -> Policy:
        """Exact `model.path` key wins, then the bare path, then the default."""
        return self.fields.get(f"{model_key}.{path}") or self.fields.get(path) or self.default


def load_config(root: Path) -> MeaningConfig:
    path = Path(root) / "config.toml"
    if not path.is_file():
        return MeaningConfig()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ValueError(f"{path.name} is not valid TOML") from None
    table = data.get("meaning")
    if table is None:
        return MeaningConfig()
    try:
        return MeaningConfig.model_validate(table)
    except ValidationError as e:
        # Name the offending key only, never the value.
        loc = e.errors()[0]["loc"]
        key = (
            str(loc[1])
            if len(loc) > 1 and loc[0] == "fields"
            else str(loc[0])
            if loc
            else "meaning"
        )
        raise ValueError(f"invalid meaning policy in {path.name}: {key}") from None
