import pytest
from fakes import m

from aegis_healer.store import StalePatchError
from aegis_healer.store.files import FileStore
from aegis_healer.types import DriftReport, MappingPatch

K = "app.models.User"


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / ".aegis")


def test_save_and_get_active(store):
    store.save_active(K, [m("a", "b")], "one")
    store.save_active(K, [m("c", "d")], "two")
    assert store.active_version(K) == 2
    assert store.get_active(K)[0].source == "c"


def test_rollback_creates_new_version(store):
    store.save_active(K, [m("a", "b")], "one")
    store.save_active(K, [m("c", "d")], "two")
    assert store.rollback(K, 1) == 3
    assert store.get_active(K)[0].source == "a"
    assert [v.version for v in store.history(K)] == [1, 2, 3]


def test_approve_pending_merges(store):
    store.save_active(K, [m("a", "b"), m("x", "y")], "one")
    pid = store.add_pending(K, MappingPatch(mappings=[m("a2", "b")]), DriftReport())
    assert store.resolve_pending(pid, True) == 2
    assert {(x.source, x.target) for x in store.get_active(K)} == {("x", "y"), ("a2", "b")}
    assert store.list_pending() == []


def test_reject_pending(store):
    pid = store.add_pending(K, MappingPatch(mappings=[m("a", "b")]), DriftReport())
    assert store.resolve_pending(pid, False) is None
    assert store.get_pending(pid).status == "rejected"
    assert store.active_version(K) == 0


def test_stale_pending_refused(store):
    pid = store.add_pending(K, MappingPatch(mappings=[m("a", "b")]), DriftReport())
    store.save_active(K, [m("c", "d")], "someone else")
    with pytest.raises(StalePatchError, match="stale; re-run check"):
        store.resolve_pending(pid, True)


def test_model_key_cannot_escape_folder(store, tmp_path):
    store.save_active("../../evil", [m("a", "b")], "x")
    written = [p for p in tmp_path.rglob("*.json")]
    assert written and all((tmp_path / ".aegis") in p.parents for p in written)


def test_no_temp_files_left(store, tmp_path):
    store.save_active(K, [m("a", "b")], "x")
    assert not list(tmp_path.rglob(".tmp-*"))


def test_similar_model_keys_do_not_collide(store):
    store.save_active("a.<locals>.U", [m("a", "b")], "x")
    assert store.get_active("a._locals_.U") == []
    store.save_active("App.User", [m("c", "d")], "x")
    assert store.get_active("app.user") == []


def test_history_never_overwritten_if_current_lost(store):
    store.save_active(K, [m("a", "b")], "first")
    store.save_active(K, [m("c", "d")], "second")
    for f in (store.root / "mappings").glob("*.json"):
        f.unlink()
    assert store.save_active(K, [m("e", "f")], "third") == 3
    assert [v.reason for v in store.history(K)] == ["first", "second", "third"]


def test_odd_pending_file_names_ignored(store):
    (store.root / "pending").mkdir(parents=True)
    (store.root / "pending" / "1e5.json").write_text("{}")
    assert store.add_pending(K, MappingPatch(), DriftReport()) == 1
    assert len(store.list_pending()) == 1
