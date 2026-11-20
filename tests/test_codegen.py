import ast
import importlib.util
import itertools
import random
import sys

import pytest
from fixtures.payloads import PHASE1_V2, V1

from aegis_healer.codegen import generate_models

_n = itertools.count()


def _load(src, tmp_path):
    name = f"gen_{next(_n)}"
    path = tmp_path / f"{name}.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return vars(mod)


def test_generated_model_validates_its_sample(tmp_path):
    ns = _load(generate_models([V1], "User"), tmp_path)
    user = ns["User"].model_validate(V1)
    assert user.address.city == "Vancouver"
    assert "datetime" in generate_models([V1], "User")


def test_phase1_nested_and_types(tmp_path):
    src = generate_models([PHASE1_V2], "Profile")
    ns = _load(src, tmp_path)
    p = ns["Profile"].model_validate(PHASE1_V2)
    assert p.contactInfo.mailingAddress.postalCode == "62704"
    assert "signupTimestamp: int" in src


def test_optional_when_missing_in_some_records():
    src = generate_models([{"a": 1, "b": "x"}, {"a": 2}], "M")
    assert "b: str | None = None" in src


def test_hostile_keys_cannot_inject_code(tmp_path):
    evil = {"x): pass\nimport os\nos.system('echo pwned')  #": 1, "class": "k", "1st": 2}
    src = generate_models([evil], "M")
    tree = ast.parse(src)
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
    assert {n.module for n in imports} <= {"__future__", "pydantic", "datetime", "typing"}
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "system"
    ]
    ns = _load(src, tmp_path)
    m = ns["M"].model_validate(evil)
    assert m.class_ == "k" and m.f_1st == 2


@pytest.mark.parametrize(
    "records",
    [
        [{"str": "a", "x": 1}, {"x": 2}],  # optional field named like a builtin type
        [{"int": None, "list": [1], "Field": 1, "datetime": "2024-01-01T00:00:00Z", "Any": 2}],
        [{"_private": 1, "__x": 2, "名前": "x", "model_dump": 1, "model_validate": 2}],
        [{"user": {"a": 1}, "User": 3}],  # field name equal to a generated class
        [{"base_model": {"a": 1}, "field": {"b": 2}}],  # classes named BaseModel / Field
        [{"items": [{"id": 1}, {"id": 2, "qty": 3}]}],  # list of objects with varying keys
        [{"t": "2024-01-01T10"}, {"t": "2024-01-01T10:00:00 +05:30"}],  # odd date strings
        [{"meta": {"a": 1}}, {"meta": None}],  # nested object sometimes null
    ],
)
def test_generated_model_imports_and_validates_own_data(tmp_path, records):
    ns = _load(generate_models(records, "Root"), tmp_path)
    for r in records:
        ns["Root"].model_validate(r)


def test_list_of_objects_gets_a_model():
    src = generate_models([{"items": [{"id": 1}]}], "Order")
    assert "class ItemsItem(BaseModel)" in src and "items: list[ItemsItem]" in src


def _random_value(rng, depth):
    kind = rng.choice(
        ["int", "float", "str", "bool", "none", "date", "dict", "list"][: 8 if depth < 3 else 6]
    )
    if kind == "int":
        return rng.randint(-5, 5)
    if kind == "float":
        return rng.random()
    if kind == "str":
        return rng.choice(["a", "", "1", "x y", "</t>"])
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "none":
        return None
    if kind == "date":
        return "2024-05-06T07:08:09Z"
    if kind == "dict":
        return _random_record(rng, depth + 1)
    return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]


KEYS = [
    "a",
    "b",
    "str",
    "list",
    "_x",
    "User",
    "model_x",
    "class",
    "1st",
    "名",
    "a-b",
    "a_b",
    "Field",
]


def _random_record(rng, depth=0):
    return {k: _random_value(rng, depth) for k in rng.sample(KEYS, rng.randint(1, 5))}


def test_random_shapes_round_trip(tmp_path):
    rng = random.Random(7)
    for _ in range(150):
        records = [_random_record(rng) for _ in range(rng.randint(1, 3))]
        src = generate_models(records, "Root")
        try:
            ns = _load(src, tmp_path)
            for r in records:
                ns["Root"].model_validate(r)
        except Exception as exc:
            raise AssertionError(f"{records}\n{src}") from exc
