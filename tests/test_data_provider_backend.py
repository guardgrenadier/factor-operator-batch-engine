"""覆盖 DuckDB 后端 Arrow 分批迭代与资源释放行为的测试。"""

from __future__ import annotations

import duckdb

from factor_engine.data_provider.backend import DuckDBBackend


def test_iter_arrow_falls_back_and_closes_resources(monkeypatch) -> None:
    """验证 iter_arrow 回退到 record batch reader 时会关闭 reader 与连接。"""
    # 构造模拟的 DuckDB 连接与 record batch reader。
    state = {"reader_closed": False, "connection_closed": False}
    batch = object()

    class Reader:
        """模拟逐批返回结果并记录关闭状态的 record batch reader。"""

        def __iter__(self):
            """迭代产出一个预制的结果批次。"""
            yield batch

        def close(self):
            """记录 reader 已被关闭。"""
            state["reader_closed"] = True

    class Connection:
        """按 DuckDB 连接接口返回模拟结果的桩对象。"""

        def register(self, name, frame):
            """空实现：登记内存表时不做任何操作。"""
            pass

        def execute(self, sql):
            """执行 SQL 并返回自身以便链式取结果。"""
            return self

        def fetch_record_batch(self, batch_size):
            """校验批次大小后返回模拟的 record batch reader。"""
            assert batch_size == 3
            return Reader()

        def close(self):
            """记录连接已被关闭。"""
            state["connection_closed"] = True

    # 消费第一个批次后关闭迭代器，随后校验资源释放状态。
    monkeypatch.setattr(duckdb, "connect", lambda **kwargs: Connection())
    batches = DuckDBBackend(arrow_batch_rows=3).iter_arrow("SELECT 1")

    assert next(batches) is batch
    batches.close()

    assert state == {"reader_closed": True, "connection_closed": True}
