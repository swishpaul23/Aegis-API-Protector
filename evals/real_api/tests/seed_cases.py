"""Hand-written seed cases used by runner tests."""

from evals.real_api.cases import Case, ExpectedMap

_SRC = {"repo": "seed", "old_ref": "a", "new_ref": "b", "spec_path": "-", "schema": "-"}

SEEDS: list[Case] = [
    Case(
        id="seed-rename",
        api="seed",
        source=_SRC,
        kind="rename",
        old_fields={"id": "integer", "full_name": "string", "email": "string"},
        new_fields={"id": "integer", "fullName": "string", "email": "string"},
        expected="repair",
        maps=[ExpectedMap(target="full_name", source="fullName")],
    ),
    Case(
        id="seed-move-retype",
        api="seed",
        source=_SRC,
        kind="move",
        old_fields={"user_id": "integer", "name": "string", "email": "string"},
        new_fields={"account.user_id": "string", "name": "string", "email": "string"},
        expected="repair",
        maps=[ExpectedMap(target="user_id", source="account.user_id", convert="int_str")],
    ),
    Case(
        id="seed-unmappable",
        api="seed",
        source=_SRC,
        kind="unmappable",
        old_fields={"id": "integer", "name": "string", "score": "number"},
        new_fields={"id": "integer", "name": "string"},
        expected="refuse",
        maps=[],
    ),
]
