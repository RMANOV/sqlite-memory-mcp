import sys
import types

from schema import init_db


def test_init_db_prepares_vec_tables_without_backfilling(tmp_path, monkeypatch):
    calls: list[str] = []
    fake_vec = types.ModuleType("vec_search")
    fake_vec.VEC_AVAILABLE = True
    fake_vec.init_vec_table = lambda conn: calls.append("entity-table")
    fake_vec.init_task_vec_table = lambda conn: calls.append("task-table")
    fake_vec.backfill_task_embeddings = lambda conn: calls.append("backfill")
    monkeypatch.setitem(sys.modules, "vec_search", fake_vec)

    init_db(str(tmp_path / "memory.db"))

    assert calls == ["entity-table", "task-table"]
