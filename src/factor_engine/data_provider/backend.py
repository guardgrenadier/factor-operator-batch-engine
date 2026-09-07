"""物理查询后端与 SQL 转义、诊断计数辅助工具。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote_plus

import pandas as pd


DEFAULT_OB_HOST = "192.168.55.161"
DEFAULT_OB_PORT = 2883
DEFAULT_ARROW_BATCH_ROWS = 100_000_000


@dataclass(frozen=True)
class OceanBaseBackend:
    """只负责执行 OceanBase SQL 的中性连接边界。"""

    user: str | None = None
    password: str | None = None
    host: str | None = None
    port: int | None = None

    def query(self, sql: str) -> pd.DataFrame:
        """用拼接好的凭据构造连接串并执行 SQL 返回 DataFrame。"""

        import connectorx

        config = _ob_config()
        user = self.user or config.get("OB_USER")
        password = self.password or config.get("OB_PASSWORD")
        if not user or not password:
            raise RuntimeError("OB_USER and OB_PASSWORD are required")
        host = self.host or config.get("OB_HOST") or DEFAULT_OB_HOST
        port = int(self.port or config.get("OB_PORT") or DEFAULT_OB_PORT)
        service_user = user if "@service:ocean" in user else f"{user}@service:ocean"
        url = f"mysql://{quote_plus(service_user)}:{quote_plus(password)}@{host}:{port}"
        return connectorx.read_sql(url, sql, return_type="pandas")


@dataclass(frozen=True)
class DuckDBBackend:
    """为一次 parquet 查询创建并关闭短生命周期 DuckDB 连接。"""

    threads: int = 8
    arrow_batch_rows: int = DEFAULT_ARROW_BATCH_ROWS

    def query(
        self,
        sql: str,
        *,
        tables: Mapping[str, pd.DataFrame] | None = None,
        threads: int | None = None,
    ) -> pd.DataFrame:
        """在内存 DuckDB 上执行一条 SQL，可注册内存表，结束后关闭连接。"""

        import duckdb

        connection = duckdb.connect(database=":memory:")
        try:
            for name, frame in (tables or {}).items():
                connection.register(name, frame)
            connection.execute("SET enable_progress_bar=false")
            connection.execute(f"SET threads={max(1, int(threads or self.threads))}")
            return connection.execute(sql).df()
        finally:
            connection.close()

    def iter_arrow(
        self,
        sql: str,
        *,
        tables: Mapping[str, pd.DataFrame] | None = None,
        threads: int | None = None,
        batch_rows: int | None = None,
    ) -> Iterator[Any]:
        """在短生命周期连接上逐批返回 Arrow RecordBatch。"""

        import duckdb

        connection = duckdb.connect(database=":memory:")
        reader = None
        try:
            for name, frame in (tables or {}).items():
                connection.register(name, frame)
            connection.execute("SET enable_progress_bar=false")
            connection.execute(f"SET threads={max(1, int(threads or self.threads))}")
            size = max(
                1,
                int(self.arrow_batch_rows if batch_rows is None else batch_rows),
            )
            result = connection.execute(sql)
            to_arrow_reader = getattr(result, "to_arrow_reader", None)
            reader = (to_arrow_reader or result.fetch_record_batch)(size)
            yield from reader
        finally:
            try:
                if reader is not None:
                    reader.close()
            finally:
                connection.close()

    def parquet_fields(self, path: Path) -> tuple[str, ...]:
        """读取单个 parquet 文件的字段清单（列名元组）。"""

        rows = self.query(
            "SELECT column_name FROM ("
            f"DESCRIBE SELECT * FROM read_parquet({sql_literal(path.as_posix())})"
            ") columns"
        )
        return tuple(str(value) for value in rows[rows.columns[0]].tolist())


def sql_identifier(value: str) -> str:
    """校验并返回反引号包裹的安全 SQL 标识符，拒绝非法字符。"""

    text = str(value)
    if (
        not text
        or not (text[0].isalpha() or text[0] == "_")
        or not all(char.isalnum() or char == "_" for char in text)
    ):
        raise ValueError(f"Invalid SQL identifier {value!r}")
    return f"`{text}`"


def sql_table(value: str) -> str:
    """将 schema.table 形式的表名逐段转义并拼接为安全 SQL 表引用。"""

    return ".".join(sql_identifier(part) for part in str(value).split("."))


def duckdb_identifier(value: str) -> str:
    """以双引号包裹并转义内部双引号，生成 DuckDB 安全列/别名标识符。"""

    return '"' + str(value).replace('"', '""') + '"'


def sql_literal(value: Any) -> str:
    """将任意值转为单引号包裹的 SQL 字面量，并把内部单引号翻倍转义。"""

    return "'" + str(value).replace("'", "''") + "'"


def sql_literal_list(values: Sequence[Any]) -> str:
    """将序列逐个转义后拼接为 DuckDB 列表字面量（方括号包裹）。"""

    return "[" + ", ".join(sql_literal(value) for value in values) + "]"


def integer_list(values: Sequence[Any]) -> str:
    """将资产轴代码序列拼接为逗号分隔的整数字符串，空序列报错。"""

    items = tuple(str(int(value)) for value in values)
    if not items:
        raise ValueError("Asset axis must not be empty")
    return ", ".join(items)


def column(frame: pd.DataFrame, name: str) -> str:
    """按名称（忽略大小写）在结果帧中唯一定位列名，否则报错。"""

    matches = [item for item in frame.columns if str(item).lower() == name.lower()]
    if len(matches) != 1:
        raise ValueError(f"Backend result must contain one {name!r} column")
    return str(matches[0])


def measured_query(
    query: Callable[[], pd.DataFrame],
    emit: Callable[..., None],
    *,
    operation: str,
    dataset: str,
    fields: Sequence[Any] = (),
    domain: Any | None = None,
) -> pd.DataFrame:
    """执行一次物理查询并统一记录诊断。"""

    started = perf_counter()
    coordinates = (
        {"start": domain.dates[0], "end": domain.dates[-1]}
        if domain is not None
        else {}
    )
    try:
        rows = query()
    except Exception as exc:
        emit(
            operation=operation,
            dataset=dataset,
            status="error",
            fields=[str(value) for value in fields],
            elapsed_ms=round((perf_counter() - started) * 1000, 3),
            error=type(exc).__name__,
            **coordinates,
        )
        raise
    emit(
        operation=operation,
        dataset=dataset,
        status="ok",
        fields=[str(value) for value in fields],
        rows=len(rows),
        bytes=int(rows.memory_usage(index=True, deep=True).sum()),
        elapsed_ms=round((perf_counter() - started) * 1000, 3),
        **coordinates,
    )
    return rows


def measured_arrow(
    query: Callable[[], Iterator[Any]],
    emit: Callable[..., None],
    *,
    operation: str,
    dataset: str,
    fields: Sequence[Any] = (),
    domain: Any | None = None,
) -> Iterator[Any]:
    """消费 Arrow batch 流并统一记录整次物理查询诊断。"""

    started = perf_counter()
    row_count = byte_count = 0
    coordinates = (
        {"start": domain.dates[0], "end": domain.dates[-1]}
        if domain is not None
        else {}
    )
    batches = None
    try:
        batches = query()
        for batch in batches:
            row_count += batch.num_rows
            byte_count += batch.nbytes
            yield batch
    except Exception as exc:
        emit(
            operation=operation,
            dataset=dataset,
            status="error",
            fields=[str(value) for value in fields],
            elapsed_ms=round((perf_counter() - started) * 1000, 3),
            error=type(exc).__name__,
            **coordinates,
        )
        raise
    else:
        emit(
            operation=operation,
            dataset=dataset,
            status="ok",
            fields=[str(value) for value in fields],
            rows=row_count,
            bytes=byte_count,
            elapsed_ms=round((perf_counter() - started) * 1000, 3),
            **coordinates,
        )
    finally:
        close = getattr(batches, "close", None) if batches is not None else None
        if close is not None:
            close()


def _ob_config() -> dict[str, Any]:
    """合并环境变量与仓库根 .env，返回 OceanBase 连接配置字典。"""

    try:
        from dotenv import dotenv_values
    except ImportError:
        values: Mapping[str, Any] = {}
    else:
        repository = Path(__file__).resolve().parents[3]
        values = dotenv_values(repository / ".env")
    return {
        key: os.getenv(key) or values.get(key)
        for key in ("OB_USER", "OB_PASSWORD", "OB_HOST", "OB_PORT")
    }
