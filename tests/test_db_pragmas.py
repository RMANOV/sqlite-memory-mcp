from db_utils import get_conn


def test_get_conn_uses_bounded_wal_autocheckpoint(tmp_path):
    with get_conn(str(tmp_path / "memory.db")) as conn:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
