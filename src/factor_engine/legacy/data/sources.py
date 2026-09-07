"""旧版实现：数据源配置、字段目录扫描与规格构造。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ...domain import parse_feature_key
from .model import SourceSpec


INDEX_INNER_CODES = {
    "SSE50": None,
    "CSI300": 3145,
    "CSI500": None,
    "CSI1000": None,
    "CSI2000": None,
}


DEFAULT_EXCLUDE_FIELDS = {
    "DataDate",
    "InnerCode",
    "SecuCode",
    "IfTradingDay",
    "TradingDay",
    "UpdateTime",
    "security_code",
    "trading_day",
    "start_time",
    "filename",
}


DATA_DICT_COLUMNS = ["asset", "freq", "field", "name_cn", "table"]


def fundamental_name(
    field: str,
    column_name: str | None = None,
    quarters: int | None = 1,
    data_code: Any | None = None,
    publ_date_limit: int | None = None,
) -> str:
    """根据基本面参数生成稳定的 source 名称。"""
    # 只把偏离默认值的可选参数编码进名称，保持常用键简短。
    name = str(field)
    if column_name is not None:
        name += f"_{column_name}"
    if quarters is not None and int(quarters) > 1:
        name += f"_{int(quarters)}Q"
    if data_code is not None:
        name += f"_{data_code}"
    if publ_date_limit is not None and int(publ_date_limit) != -180:
        name += f"_PDL{signed_token(int(publ_date_limit))}"
    return name


def signed_token(value: int) -> str:
    """将有符号整数编码为适合名称使用的短标记。"""
    if value < 0:
        return f"m{abs(value)}"
    return f"p{value}"


def minute_data_type(freq: str) -> str:
    """返回分钟频率的默认数据目录类型。"""
    # 配置仅覆盖当前明确支持的标准分钟频率。
    mapping = {
        "1min": "one_minute",
        "5min": "five_minute",
        "15min": "fifteen_minute",
        "30min": "half_hour",
        "60min": "hour",
    }
    if freq not in mapping:
        raise ValueError(f"No default minute data_type for {freq!r}")
    return mapping[freq]


def minute_path(date: Any, data_type: str, path_template: str | None = None) -> Path:
    """根据日期、类型和模板生成分钟 parquet 路径。"""
    date_key = pd.to_datetime(date).strftime("%Y%m%d")
    if path_template is not None:
        return Path(path_template.format(date=date_key, data_type=data_type))
    if data_type == "one_minute":
        return Path(f"/data/cephfs/minute/{data_type}_stat/{date_key}.parquet")
    return Path(f"/data/cephfs/minute/{data_type}/{date_key}.parquet")


def default_source_config() -> dict[str, dict[str, Any]]:
    """生成内置的股票、转债和指数 source 配置。"""
    # 股票和转债日行情字段分别映射到各自的标准宽表。
    config: dict[str, dict[str, Any]] = {}
    for field in (
        "OpenPrice",
        "HighPrice",
        "LowPrice",
        "ClosePrice",
        "PrevClosePrice",
        "DailyReturn",
        "TurnoverVolume",
        "TurnoverValue",
        "TotalShares",
        "NonRestrictedShares",
        "MarketCap",
        "FloatMarketCap",
        "IfSuspended",
        "SuspendedDays",
        "IfSpecialTrade",
        "ListedSector",
        "ListedStatus",
        "IndustryCode",
        "SecondIndustryCode",
        "IndustryCodeNew",
        "SecondIndustryCodeNew",
    ):
        config[f"stk.1d.{field}"] = {
            "asset": "stk",
            "freq": "1d",
            "name": field,
            "source": "ReturnDaily",
            "table": "SmartQuant.ReturnDaily",
            "field": field,
            "params": {},
        }
    for field in (
        "OpenPrice",
        "HighPrice",
        "LowPrice",
        "ClosePrice",
        "PrevClosePrice",
        "DailyReturn",
        "TurnoverVolume",
        "TurnoverValue",
        "TotalShares",
        "NonRestrictedShares",
        "MarketCap",
        "FloatMarketCap",
        "IfSuspended",
        "SuspendedDays",
        "IfSpecialTrade",
        "ListedSector",
        "IndustryCode",
        "SecondIndustryCode",
        "IndustryCodeNew",
        "SecondIndustryCodeNew",
    ):
        config[f"cb.1d.{field}"] = {
            "asset": "cb",
            "freq": "1d",
            "name": field,
            "source": "CBReturnDaily",
            "table": "SmartQuant.CBReturnDaily",
            "field": field,
            "params": {},
        }
    # 补充复权、转债正股映射和行业分类等专用数据源。
    config["stk.1d.adj_factor"] = {
        "asset": "stk",
        "freq": "1d",
        "name": "adj_factor",
        "source": "AdjustFactor",
        "table": "JYDB.DZ_AdjustingFactor",
        "field": "adj_factor",
        "params": {},
    }
    config["cb.1d.underlying_stk_col"] = {
        "asset": "cb",
        "freq": "1d",
        "name": "underlying_stk_col",
        "source": "CBStockMap",
        "table": "JYDB.Bond_ConBDBasicInfo",
        "field": "StockInnerCode",
        "params": {"kind": "col"},
    }
    config["cb.1d.underlying_stk_inner_code"] = {
        "asset": "cb",
        "freq": "1d",
        "name": "underlying_stk_inner_code",
        "source": "CBStockMap",
        "table": "JYDB.Bond_ConBDBasicInfo",
        "field": "StockInnerCode",
        "params": {"kind": "inner_code"},
    }
    config["stk.1d.industry_code.SW2021.L1"] = {
        "asset": "stk",
        "freq": "1d",
        "name": "industry_code.SW2021.L1",
        "source": "ReturnDaily",
        "table": "SmartQuant.ReturnDaily",
        "field": "IndustryCodeNew",
        "params": {},
    }
    # 已知内部代码的指数同时暴露权重与成员掩码字段。
    for index_name, index_inner_code in INDEX_INNER_CODES.items():
        if index_inner_code is None:
            continue
        config[f"stk.1d.index_weight.{index_name}"] = {
            "asset": "stk",
            "freq": "1d",
            "name": f"index_weight.{index_name}",
            "source": "IndexComponentWeight_Choice",
            "table": "SmartQuant.IndexComponentWeight_Choice",
            "field": "Weight",
            "params": {"index_inner_code": index_inner_code, "kind": "index_weight"},
        }
        config[f"stk.1d.is_member.{index_name}"] = {
            "asset": "stk",
            "freq": "1d",
            "name": f"is_member.{index_name}",
            "source": "IndexComponentWeight_Choice",
            "table": "SmartQuant.IndexComponentWeight_Choice",
            "field": "Weight",
            "params": {
                "index_inner_code": index_inner_code,
                "kind": "index_membership",
            },
        }
    return config


def load_source_config(
    source_config: dict[str, Any] | str | Path | None,
) -> dict[str, Any]:
    """加载 data_sources 配置，未指定时读取正式 Catalog 共用的 JSON。"""
    # 未指定配置时优先读取正式 Catalog 配置文件，否则回退到代码内置默认值。
    if source_config is None:
        default_path = (
            Path(__file__).resolve().parents[2] / "data_provider" / "data_sources.json"
        )
        if default_path.exists():
            with default_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        return {"source_tables": [], "sources": default_source_config()}
    if isinstance(source_config, (str, Path)):
        with Path(source_config).open("r", encoding="utf-8") as f:
            return json.load(f)
    return dict(source_config)


def lookup_source_record(
    source_config: dict[str, Any], key: str
) -> dict[str, Any] | None:
    """按完整特征键查找精确 source 记录。"""
    # 同时兼容键值映射和记录列表两种配置格式。
    fk = parse_feature_key(key)
    rows = source_config.get("sources", {})
    if isinstance(rows, dict):
        if fk.key in rows:
            return dict(rows[fk.key])
        return None
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    for row in rows or []:
        row_key = row.get("key") or ".".join(
            str(row.get(part, "")) for part in ("asset", "freq", "name")
        )
        if row_key == fk.key:
            return dict(row)
    return None


def source_spec_from_table_record(
    table_record: dict[str, Any], field: str
) -> SourceSpec:
    """将表级配置和字段名转换为 SourceSpec。"""
    # 表级扩展字段都下沉到 params，供具体 Reader 使用。
    asset = table_record["asset"]
    freq = table_record["freq"]
    params = dict(table_record.get("params", {}))
    for key, value in table_record.items():
        if key not in {
            "asset",
            "freq",
            "name",
            "source",
            "table",
            "fields",
            "params",
            "exclude_fields",
            "asset_axis",
        }:
            params[key] = value
    return SourceSpec(
        asset=asset,
        freq=freq,
        name=field,
        source=table_record.get("source"),
        table=table_record.get("table"),
        field=field,
        params=params,
    )


def build_data_dict(source_config: dict[str, Any], *, reader: Any) -> pd.DataFrame:
    """实时扫描 source_tables 和基本面 item code，生成字段目录 DataFrame。"""
    # 逐表扫描物理字段，并过滤坐标列和内部字段。
    rows: list[dict[str, Any]] = []
    for table_record in source_config.get("source_tables", []) or []:
        table_record = dict(table_record)
        asset = str(table_record.get("asset", ""))
        freq = str(table_record.get("freq", ""))
        table = str(table_record.get("table", ""))
        try:
            fields = scan_source_table_fields(table_record, reader=reader)
        except Exception as exc:
            target = _scan_target(table_record)
            raise RuntimeError(f"Failed to scan {target} for {asset}.{freq}") from exc
        excludes = (
            set(str(value) for value in table_record.get("exclude_fields", ()))
            | DEFAULT_EXCLUDE_FIELDS
        )
        for field in fields:
            field = str(field)
            if _should_exclude_field(field, excludes):
                continue
            rows.append(
                {
                    "asset": asset,
                    "freq": freq,
                    "field": field,
                    "name_cn": "",
                    "table": table,
                }
            )
    # 基本面字段来自独立 item code 目录，并附带中文名。
    try:
        fundamental = scan_fundamental_fields(reader=reader)
    except Exception as exc:
        raise RuntimeError("Failed to scan SmartQuant.Fundamental_ItemCode") from exc
    for row in fundamental:
        rows.append(
            {
                "asset": "stk",
                "freq": "1d",
                "field": str(row["field"]),
                "name_cn": ""
                if pd.isna(row.get("name_cn"))
                else str(row.get("name_cn", "")),
                "table": "Fundamental",
            }
        )
    # 统一列顺序并移除重复扫描记录。
    if not rows:
        return pd.DataFrame(columns=DATA_DICT_COLUMNS)
    df = pd.DataFrame(rows, columns=DATA_DICT_COLUMNS)
    df = df.drop_duplicates(
        subset=["asset", "freq", "field", "table"], keep="first"
    ).reset_index(drop=True)
    return df[DATA_DICT_COLUMNS]


def source_spec_from_data_dict(
    data_dict: pd.DataFrame,
    source_tables: list[dict[str, Any]],
    key: str,
) -> SourceSpec | None:
    """用 data_dict 命中行和 source_tables 配置恢复普通表字段 SourceSpec。"""
    # 字段目录必须在资产、频率、字段和非基本面来源上唯一命中。
    fk = parse_feature_key(key)
    candidates = data_dict[
        (data_dict["asset"].astype(str) == fk.asset)
        & (data_dict["freq"].astype(str) == fk.freq)
        & (data_dict["field"].astype(str) == fk.name)
        & (data_dict["table"].astype(str) != "Fundamental")
    ]
    if candidates.empty:
        return None
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous data_dict match for {fk.key!r}: {candidates.to_dict('records')}"
        )
    row = candidates.iloc[0]
    table = str(row["table"])
    # 再反查唯一表级配置以恢复完整读取参数。
    table_records = [
        dict(record)
        for record in source_tables or []
        if str(record.get("asset")) == fk.asset
        and str(record.get("freq")) == fk.freq
        and str(record.get("table")) == table
    ]
    if len(table_records) != 1:
        raise ValueError(
            f"Expected one source_tables config for {(fk.asset, fk.freq, table)!r}, "
            f"found {len(table_records)}"
        )
    return source_spec_from_table_record(table_records[0], fk.name)


def scan_source_table_fields(table_record: dict[str, Any], *, reader: Any) -> list[str]:
    """根据 table_record 扫描数据库表或 parquet schema 字段。"""
    # 分钟 parquet 使用文件 schema，其余来源查询数据库元数据表。
    source = table_record.get("source")
    if source == "MinuteParquet":
        return scan_minute_parquet_fields(table_record)
    table = table_record.get("table")
    if not table:
        raise ValueError("Table source config requires table")
    schema, table_name = _split_table_name(table)
    sql = f"""
        SELECT COLUMN_NAME AS field
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = '{schema}'
          AND TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
    """
    # 兼容不同数据库驱动返回的字段名大小写。
    rows = reader._read_sql(sql)
    if "field" in rows:
        return [str(value) for value in rows["field"].tolist()]
    if "COLUMN_NAME" in rows:
        return [str(value) for value in rows["COLUMN_NAME"].tolist()]
    if "Column_Name" in rows:
        return [str(value) for value in rows["Column_Name"].tolist()]
    raise ValueError(f"Could not find field column in schema query result for {table}")


def scan_minute_parquet_fields(table_record: dict[str, Any]) -> list[str]:
    """扫描分钟 parquet schema 字段。"""
    # 延迟导入可选依赖，避免普通日频路径强制安装 DuckDB。
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError("scan_minute_parquet_fields requires duckdb") from exc
    # 从配置或频率默认值定位一个样例文件并读取其 schema。
    sample_date = table_record.get("sample_date", "2024-12-31")
    data_type = (
        table_record.get("data_type")
        or table_record.get("params", {}).get("data_type")
        or minute_data_type(table_record["freq"])
    )
    path_template = table_record.get("path_template") or table_record.get(
        "params", {}
    ).get("path_template")
    path = minute_path(sample_date, data_type, path_template)
    sql = f"""
        SELECT column_name AS field
        FROM (
            DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')
        ) AS columns
    """
    rows = duckdb.sql(sql).df()
    return [str(value) for value in rows["field"].tolist()]


def scan_fundamental_fields(*, reader: Any) -> list[dict[str, Any]]:
    """扫描基本面 item code 字段目录，仅用于 search。"""
    # 查询字段中英文名，并兼容驱动返回的列名大小写。
    sql = """
        SELECT ItemName AS field, ItemNameCN AS name_cn
        FROM SmartQuant.Fundamental_ItemCode
    """
    rows = reader._read_sql(sql)
    if rows.empty:
        return []
    field_col = _first_existing_column(rows, ("field", "ItemName", "ITEMNAME"))
    name_col = _first_existing_column(rows, ("name_cn", "ItemNameCN", "ITEMNAMECN"))
    if field_col is None:
        raise ValueError(
            "Could not find field column in Fundamental_ItemCode scan result"
        )
    return [
        {"field": value, "name_cn": rows.iloc[pos][name_col] if name_col else ""}
        for pos, value in enumerate(rows[field_col].tolist())
    ]


def _split_table_name(table: str) -> tuple[str, str]:
    """拆分可选 schema 前缀和数据库表名。"""
    parts = str(table).split(".")
    if len(parts) == 1:
        return "", parts[0]
    return parts[-2], parts[-1]


def source_spec_from_record(key: str, record: dict[str, Any]) -> SourceSpec:
    """将精确 source 配置记录转换为 SourceSpec。"""
    # 未占用的扩展配置统一作为 Reader 参数保留。
    fk = parse_feature_key(key)
    params = dict(record.get("params", {}))
    for param_key, value in record.items():
        if param_key not in {
            "key",
            "asset",
            "freq",
            "name",
            "source",
            "table",
            "field",
            "db_field",
            "params",
            "value_kind",
        }:
            params[param_key] = value
    return SourceSpec(
        asset=fk.asset,
        freq=fk.freq,
        name=fk.name,
        source=record.get("source"),
        table=record.get("table"),
        field=record.get("field") or record.get("db_field") or fk.name,
        params=params,
    )


def _scan_target(table_record: dict[str, Any]) -> str:
    """返回数据源字段扫描所针对的物理位置说明。"""
    # 分钟来源尽量解析到实际 parquet 路径，其余显示数据库表名。
    if table_record.get("source") == "MinuteParquet":
        sample_date = table_record.get("sample_date", "2024-12-31")
        data_type = table_record.get("data_type") or table_record.get("params", {}).get(
            "data_type"
        )
        path_template = table_record.get("path_template") or table_record.get(
            "params", {}
        ).get("path_template")
        if data_type or path_template:
            return f"parquet {minute_path(sample_date, data_type or minute_data_type(table_record['freq']), path_template).as_posix()}"
    return f"source table {table_record.get('table')}"


def _should_exclude_field(field: str, excludes: set[str]) -> bool:
    """判断字段是否为空、显式排除或属于内部字段。"""
    return not field or field in excludes or field.startswith("_")


def _first_existing_column(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    """按候选顺序返回 DataFrame 中第一个存在的列名。"""
    for name in names:
        if name in df.columns:
            return name
    return None
