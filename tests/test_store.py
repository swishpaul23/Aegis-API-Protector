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
