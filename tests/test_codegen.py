import ast
import importlib.util
import itertools
import sys

from fixtures.payloads import PHASE1_V2, V1

from aegis_healer.codegen import generate_models

_n = itertools.count()


def _load(src, tmp_path):
    name = f"gen_{next(_n)}"
    path = tmp_path / f"{name}.py"
    path.write_text(src)
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
