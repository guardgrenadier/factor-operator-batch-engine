"""旧版实现：快照固定轴与物化特征的磁盘存储。"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .model import (
    ExecutionScope,
    FeatureArray,
    FeatureDef,
    FeatureMeta,
    FeatureSpace,
    SourceSpec,
    get_freq_step_count,
    normalize_scope_dates,
    parse_feature_key,
    stable_hash,
)


MANIFEST_SCHEMA_VERSION = 1


def _json_default(value: Any) -> Any:
    """将常见非原生类型转换为 JSON 可序列化值。"""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 文件。"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """通过临时文件原子写入 JSON。"""
    # 临时文件与目标同目录，确保最终替换不跨文件系统。
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    os.replace(temp_path, path)


def _write_feature_day(task: tuple[Path, Path, np.ndarray, np.ndarray]) -> None:
    """通过临时文件原子写入单日特征数据和代码。"""
    # 数据和代码分别写入临时文件，成功后依次替换目标。
    data_path, code_path, day_data, day_codes = task
    token = uuid.uuid4().hex
    data_temp = data_path.with_name(f".{data_path.name}.{token}.tmp")
    code_temp = code_path.with_name(f".{code_path.name}.{token}.tmp")
    try:
        with data_temp.open("wb") as f:
            np.save(f, day_data)
        with code_temp.open("wb") as f:
            np.save(f, day_codes)
        os.replace(data_temp, data_path)
        os.replace(code_temp, code_path)
    finally:
        data_temp.unlink(missing_ok=True)
        code_temp.unlink(missing_ok=True)


class FeatureStore:
    """管理固定日期轴、资产轴和物化特征的快照存储。"""

    def __init__(
        self,
        root: str | Path,
    ):
        """初始化 Snapshot 根目录、manifest 和轴缓存。"""
        # 创建根目录并初始化清单、轴缓存与待提交写入记录。
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, Any] = self._load_or_init_manifest()
        self._dates_cache: np.ndarray | None = None
        self._asset_codes_cache: dict[str, np.ndarray] = {}
        self._code_map_cache: dict[str, pd.DataFrame] = {}
        self._pending_feature_writes: dict[Path, tuple[str, bool]] = {}

    @property
    def manifest(self) -> dict[str, Any]:
        """返回当前 Snapshot manifest。"""
        return self._manifest

    @property
    def snapshot_signature(self) -> str:
        """根据固定轴摘要生成 Snapshot 签名。"""
        # 日期轴和各资产代码轴摘要共同决定快照身份。
        snapshot = self._manifest.get("snapshot") or {}
        assets = self._manifest.get("assets") or {}
        return stable_hash(
            snapshot.get("start"),
            snapshot.get("end"),
            snapshot.get("calendar"),
            snapshot.get("dates_hash"),
            {
                asset: record.get("codes_hash")
                for asset, record in sorted(assets.items())
            },
        )

    def _load_or_init_manifest(self) -> dict[str, Any]:
        """加载 schema v1 manifest，不存在时创建空 manifest。"""
        # 已有清单必须使用当前 schema，并补齐允许缺省的顶层字段。
        if self.manifest_path.exists():
            manifest = _read_json(self.manifest_path)
            if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
                raise ValueError(
                    f"{self.manifest_path} is not a v{MANIFEST_SCHEMA_VERSION} snapshot manifest. "
                    "Old snapshot manifests are intentionally unsupported by the new FeatureSpace design."
                )
            manifest.setdefault("snapshot_id", self.root.name)
            manifest.setdefault("snapshot", {})
            manifest.setdefault("assets", {})
            manifest.setdefault("features", {})
            return manifest
        # 新目录先写入最小空清单，后续再初始化固定轴。
        manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "snapshot_id": self.root.name,
            "snapshot": {},
            "assets": {},
            "features": {},
        }
        _write_json(self.manifest_path, manifest)
        return manifest

    def _save_manifest(self) -> None:
        """原子保存当前 manifest。"""
        _write_json(self.manifest_path, self._manifest)

    def init_snapshot(
        self,
        *,
        start: str,
        end: str,
        calendar: str = "cn_a_share",
        assets: tuple[str, ...] = ("stk", "cb", "idx"),
        asset_codes: dict[str, Any] | None = None,
        overwrite: bool = False,
        reader: Any | None = None,
    ) -> None:
        """初始化交易日轴和固定资产轴，并写入 manifest。"""
        # 已初始化快照默认不可覆盖，避免无意改变全部特征的坐标基准。
        if (
            self._manifest.get("snapshot")
            and self._manifest["snapshot"].get("dates_path")
            and not overwrite
        ):
            raise FileExistsError(
                "Snapshot axes already exist; use overwrite=True to rebuild them"
            )

        # 查询、规范化并持久化稳定有序的日期轴。
        dates_df = self._read_calendar_dates(start, end, calendar, reader=reader)
        dates = np.asarray([_date_storage_key(value) for value in dates_df["DataDate"]])
        dates = np.asarray(sorted(pd.unique(dates)))
        if len(dates) == 0:
            raise ValueError(
                f"No trading dates found for {calendar!r} between {start} and {end}"
            )
        dates_hash = stable_hash(dates.tolist())
        dates_path = self.root / "axes" / "dates.npy"
        dates_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(dates_path, dates)

        # 更新日期轴记录并清空所有依赖旧轴的内存缓存。
        self._manifest["snapshot"] = {
            "start": str(start),
            "end": str(end),
            "calendar": str(calendar),
            "dates_path": str(dates_path.relative_to(self.root)),
            "n_dates": int(len(dates)),
            "dates_hash": dates_hash,
        }
        self._manifest["assets"] = {}
        self._dates_cache = dates
        self._asset_codes_cache.clear()
        self._code_map_cache.clear()

        # 资产轴优先使用显式代码，其余从标准日频表自动构建。
        explicit_codes = asset_codes or {}
        for asset in assets:
            if asset in explicit_codes:
                self._write_static_asset_axis(asset, explicit_codes[asset])
            else:
                self._init_asset_axis_from_daily_table(asset, start, end, reader=reader)
        for asset, codes in explicit_codes.items():
            if asset not in self._manifest["assets"]:
                self._write_static_asset_axis(asset, codes)
        self._save_manifest()

    def _read_calendar_dates(
        self, start: str, end: str, calendar: str, *, reader: Any | None
    ) -> pd.DataFrame:
        """读取指定区间内的 A 股交易日。"""
        # 首版只支持固定 A 股交易日历，并按日期升序查询。
        if calendar != "cn_a_share":
            raise NotImplementedError("Only calendar='cn_a_share' is supported")
        sql = f"""
            SELECT DataDate
            FROM SmartQuant.CalenderDay_TradingDay
            WHERE SecuMarket = 83
              AND IfTradingDay = 1
              AND DataDate BETWEEN '{start}' AND '{end}'
            ORDER BY DataDate
        """
        return self._read_sql(sql, reader=reader)

    def _init_asset_axis_from_daily_table(
        self, asset: str, start: str, end: str, *, reader: Any | None
    ) -> None:
        """从日频数据库表提取并初始化资产 InnerCode 轴。"""
        # 各资产类型选择其标准日频表和统一代码映射列。
        if asset == "stk":
            table = "SmartQuant.ReturnDaily"
            sql = f"""
                SELECT DataDate, InnerCode, SecuCode
                FROM {table}
                WHERE DataDate BETWEEN '{start}' AND '{end}'
                  AND IfTradingDay = 1
                ORDER BY DataDate, InnerCode
            """
        elif asset == "cb":
            table = "SmartQuant.CBReturnDaily"
            sql = f"""
                SELECT DataDate, InnerCode, SecuCode
                FROM {table}
                WHERE DataDate BETWEEN '{start}' AND '{end}'
                  AND IfTradingDay = 1
                ORDER BY DataDate, InnerCode
            """
        elif asset == "idx":
            table = "JYDB.QT_IndexQuote"
            sql = f"""
                SELECT TradingDay AS DataDate, InnerCode, InnerCode AS SecuCode
                FROM {table}
                WHERE TradingDay BETWEEN '{start}' AND '{end}'
                ORDER BY TradingDay, InnerCode
            """
        else:
            raise NotImplementedError(
                f"Automatic asset axis initialization is only supported for stk/cb/idx, got {asset!r}"
            )
        # 规范日期、移除无效映射，并建立排序后的唯一内部代码轴。
        code_map = self._read_sql(sql, reader=reader)
        if code_map.empty:
            raise ValueError(
                f"No {asset} codes found in {table} between {start} and {end}"
            )
        code_map = code_map.copy()
        code_map["DataDate"] = code_map["DataDate"].map(_date_storage_key)
        code_map = code_map.dropna(subset=["InnerCode", "SecuCode"])
        codes = np.asarray(sorted(pd.unique(code_map["InnerCode"])))
        self._write_asset_axis(asset, codes, code_map)

    def _write_static_asset_axis(self, asset: str, codes: Any) -> None:
        """根据显式代码列表构造并写入静态资产轴。"""
        dates = self.get_dates()
        codes_arr = np.asarray(sorted(pd.unique(np.asarray(codes))))
        rows = []
        for date in dates:
            for code in codes_arr:
                rows.append({"DataDate": date, "InnerCode": code, "SecuCode": code})
        self._write_asset_axis(asset, codes_arr, pd.DataFrame(rows))

    def _write_asset_axis(
        self, asset: str, codes: np.ndarray, code_map: pd.DataFrame
    ) -> None:
        """写入 InnerCode 轴和 parquet code map，并更新 manifest。"""
        # 资产轴必须唯一，代码数组与逐日映射分别持久化。
        if len(codes) != len(pd.unique(codes)):
            raise ValueError(f"Asset {asset!r} codes contain duplicates")
        asset_dir = self.root / "axes" / "assets" / asset
        asset_dir.mkdir(parents=True, exist_ok=True)
        codes_path = asset_dir / "inner_codes.npy"
        code_map_path = asset_dir / "code_map.parquet"
        np.save(codes_path, codes)
        code_map[["DataDate", "InnerCode", "SecuCode"]].to_parquet(
            code_map_path, engine="pyarrow", index=False
        )
        # 清单记录路径、长度和内容哈希，并同步刷新内存缓存。
        self._manifest["assets"][asset] = {
            "inner_codes_path": str(codes_path.relative_to(self.root)),
            "code_map_path": str(code_map_path.relative_to(self.root)),
            "n_assets": int(len(codes)),
            "codes_hash": stable_hash(codes.tolist()),
        }
        self._asset_codes_cache[asset] = codes
        self._code_map_cache[asset] = code_map[
            ["DataDate", "InnerCode", "SecuCode"]
        ].copy()

    def extend_asset_axis(self, *args: Any, **kwargs: Any) -> None:
        """拒绝隐式扩轴，要求调用方执行显式迁移。"""
        raise NotImplementedError(
            "Asset axis extension must be implemented as an explicit migration"
        )

    def get_dates(self) -> np.ndarray:
        """读取并缓存 Snapshot 日期轴。"""
        self._ensure_snapshot_initialized()
        if self._dates_cache is None:
            path = self.root / self._manifest["snapshot"]["dates_path"]
            self._dates_cache = np.load(path, allow_pickle=True)
        return self._dates_cache

    def get_asset_codes(self, asset: str) -> np.ndarray:
        """读取并缓存指定资产的 InnerCode 轴。"""
        # 内存缓存未命中时按清单路径加载固定代码数组。
        self._ensure_snapshot_initialized()
        if asset in self._asset_codes_cache:
            return self._asset_codes_cache[asset]
        assets = self._manifest.get("assets", {})
        if asset not in assets:
            raise KeyError(f"Asset axis {asset!r} not found in snapshot")
        codes = np.load(
            self.root / assets[asset]["inner_codes_path"], allow_pickle=True
        )
        self._asset_codes_cache[asset] = codes
        return codes

    def get_code_map(self, asset: str) -> pd.DataFrame:
        """读取并缓存指定资产的 parquet code map。"""
        # 对外始终返回副本，避免调用方修改内部缓存。
        self._ensure_snapshot_initialized()
        if asset in self._code_map_cache:
            return self._code_map_cache[asset].copy()
        assets = self._manifest.get("assets", {})
        if asset not in assets:
            raise KeyError(f"Asset axis {asset!r} not found in snapshot")
        code_map = pd.read_parquet(
            self.root / assets[asset]["code_map_path"], engine="pyarrow"
        )
        code_map["DataDate"] = code_map["DataDate"].map(_date_storage_key)
        self._code_map_cache[asset] = code_map
        return code_map.copy()

    def resolve_space(
        self,
        feature_def_or_key: FeatureDef | SourceSpec | str,
        *,
        dates: Any | None = None,
        scope: ExecutionScope | None = None,
    ) -> FeatureSpace:
        """根据特征键和步长规则构造固定 FeatureSpace。"""
        # 兼容日期参数后，从定义或数据源参数推导第三维长度。
        self._ensure_snapshot_initialized()
        scope = self._compat_scope(dates=dates, scope=scope)
        if isinstance(feature_def_or_key, FeatureDef):
            fk = parse_feature_key(feature_def_or_key.key)
            steps = (
                int(feature_def_or_key.steps)
                if feature_def_or_key.steps is not None
                else get_freq_step_count(fk.freq)
            )
        elif isinstance(feature_def_or_key, SourceSpec):
            fk = parse_feature_key(feature_def_or_key.key)
            if (
                feature_def_or_key.source == "Fundamental"
                and "quarters" in feature_def_or_key.params
            ):
                steps = int(feature_def_or_key.params["quarters"])
            else:
                steps = get_freq_step_count(fk.freq)
        else:
            fk = parse_feature_key(feature_def_or_key)
            steps = get_freq_step_count(fk.freq)
        # 日期可按执行范围裁剪，资产轴始终使用快照固定顺序。
        return FeatureSpace(
            asset=fk.asset,
            freq=fk.freq,
            dates=self._scope_dates(scope),
            codes=self.get_asset_codes(fk.asset),
            steps=steps,
        )

    def _compat_scope(
        self, *, dates: Any | None, scope: ExecutionScope | None
    ) -> ExecutionScope | None:
        """把兼容的 dates 参数转换为统一执行范围并拒绝重复指定。"""
        if dates is None:
            return scope
        if scope is not None:
            raise ValueError("Pass either dates or scope, not both")
        return ExecutionScope(read_dates=dates, write_dates=dates)

    def _scope_dates(self, scope: ExecutionScope | None) -> np.ndarray:
        """返回完整 Snapshot 日期或校验后的执行范围日期子集。"""
        # 范围内日期必须全部属于当前快照，但保留请求给出的顺序。
        dates = self.get_dates()
        if scope is None:
            return dates
        requested = np.asarray(normalize_scope_dates(scope.read_dates))
        date_set = set(dates.tolist())
        missing = [date for date in requested.tolist() if date not in date_set]
        if missing:
            raise ValueError(
                f"ExecutionScope read_dates are outside current snapshot: {missing[:5]}"
            )
        return requested

    def to_inner_code(
        self,
        asset: str,
        df: pd.DataFrame,
        *,
        date_col: str = "DataDate",
        code_col: str = "SecuCode",
        errors: str = "raise",
    ) -> pd.DataFrame:
        """使用 code map 将 SecuCode 映射为 Snapshot InnerCode。"""
        # 已是内部代码时直接规范列名，外部代码只支持 SecuCode。
        if code_col == "InnerCode":
            out = df.copy()
            out["InnerCode"] = out[code_col]
            return out
        if code_col != "SecuCode":
            raise ValueError("Only InnerCode and SecuCode input columns are supported")
        # 日期和证券代码共同决定逐日内部代码映射。
        code_map = self.get_code_map(asset)
        map_df = code_map.rename(columns={"SecuCode": code_col})
        out = df.copy()
        out[date_col] = out[date_col].map(_date_storage_key)
        out = out.merge(
            map_df,
            left_on=[date_col, code_col],
            right_on=["DataDate", code_col],
            how="left",
        )
        if "DataDate_y" in out:
            out = out.drop(columns=["DataDate_y"]).rename(
                columns={"DataDate_x": date_col}
            )
        # 未命中记录由 errors 参数决定报错或过滤。
        missing = out["InnerCode"].isna()
        if missing.any():
            if errors == "raise":
                sample = (
                    out.loc[missing, [date_col, code_col]].head(5).to_dict("records")
                )
                raise KeyError(
                    f"Could not map SecuCode to InnerCode for {asset}: {sample}"
                )
            if errors == "ignore":
                out = out.loc[~missing].copy()
            else:
                raise ValueError("errors must be 'raise' or 'ignore'")
        return out

    def has_feature(self, key: str) -> bool:
        """判断 manifest 是否记录指定物化特征。"""
        return parse_feature_key(key).key in self._manifest["features"]

    def feature_dir(self, key: str) -> Path:
        """返回指定特征的持久化目录。"""
        fk = parse_feature_key(key)
        return self.root / "features" / fk.asset / fk.freq / fk.name

    def _staging_feature_dir(self, key: str) -> Path:
        """返回用于分块写入且与目标同级的暂存目录。"""
        final_dir = self.feature_dir(key)
        return final_dir.with_name(f"{final_dir.name}.__tmp__.{uuid.uuid4().hex}")

    def _backup_feature_dir(self, key: str) -> Path:
        """返回用于覆盖提交且与目标同级的备份目录。"""
        final_dir = self.feature_dir(key)
        return final_dir.with_name(f"{final_dir.name}.__old__.{uuid.uuid4().hex}")

    def _is_staging_feature_dir(self, path: Path) -> bool:
        """保守判断路径是否为分块写入的暂存目录。"""
        return ".__tmp__." in Path(path).name

    def rebuild_manifest_from_metadata(self) -> dict[str, Any]:
        """扫描 metadata.json 重建 manifest 中的特征索引。"""
        # 固定轴记录原样保留，只重建物化特征索引。
        manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "snapshot_id": self.root.name,
            "snapshot": self._manifest.get("snapshot", {}),
            "assets": self._manifest.get("assets", {}),
            "features": {},
        }
        base = self.root / "features"
        # 忽略未提交暂存目录和覆盖过程中的备份目录。
        if base.exists():
            for meta_path in base.glob("*/*/*/metadata.json"):
                if any(
                    ".__tmp__." in part or ".__old__." in part
                    for part in meta_path.parts
                ):
                    continue
                meta = _read_json(meta_path)
                feature_meta = FeatureMeta.from_dict(meta["feature_meta"])
                key = parse_feature_key(feature_meta.key).key
                if key in manifest["features"]:
                    raise ValueError(
                        f"Duplicate feature key {key} found while scanning metadata"
                    )
                manifest["features"][key] = str(meta_path.relative_to(self.root))
        self._manifest = manifest
        self._save_manifest()
        return self._manifest

    def write_feature(self, feature: FeatureArray, *, overwrite: bool = False) -> None:
        """将 FeatureArray 写入 Store，默认拒绝覆盖已有特征。"""
        if feature.key in self._manifest["features"] and not overwrite:
            raise FileExistsError(f"Feature {feature.key} already exists")
        self._write_array(feature, base_dir=self.feature_dir(feature.key))

    def begin_feature_write(
        self, feature_def: FeatureDef, *, overwrite: bool = False
    ) -> Path:
        """为分块写入准备暂存目录但暂不提交元数据。"""
        # 同时检查清单和物理目录，避免覆盖未被清单记录的残留数据。
        key = parse_feature_key(feature_def.key).key
        if key in self._manifest["features"] and not overwrite:
            raise FileExistsError(f"Feature {key} already exists")
        if self.feature_dir(key).exists() and not overwrite:
            raise FileExistsError(
                f"Feature directory {self.feature_dir(key)} already exists"
            )
        # 创建唯一暂存目录并登记其目标键和覆盖权限。
        base_dir = self._staging_feature_dir(key)
        (base_dir / "data").mkdir(parents=True, exist_ok=True)
        (base_dir / "code").mkdir(parents=True, exist_ok=True)
        self._pending_feature_writes[base_dir] = (key, bool(overwrite))
        return base_dir

    def abort_feature_write(self, base_dir: Path) -> None:
        """删除失败的分块写入暂存目录。"""
        base = Path(base_dir)
        if not self._is_staging_feature_dir(base):
            raise ValueError(f"Refusing to abort non-staging feature directory: {base}")
        self._pending_feature_writes.pop(base, None)
        shutil.rmtree(base, ignore_errors=True)

    def write_feature_chunk(
        self,
        feature: FeatureArray,
        scope: ExecutionScope,
        *,
        base_dir: Path | None = None,
    ) -> None:
        """仅把执行范围中的写入日期保存到分块目录。"""
        # 建立分块目录和读日期到局部数组位置的映射。
        base = base_dir or self.feature_dir(feature.key)
        data_dir = base / "data"
        code_dir = base / "code"
        data_dir.mkdir(parents=True, exist_ok=True)
        code_dir.mkdir(parents=True, exist_ok=True)
        date_pos = {str(date): i for i, date in enumerate(feature.space.dates)}
        missing = feature.missing_value
        # 每日只保存至少一个 step 有效的资产行，以稀疏方式落盘。
        for date in scope.write_dates:
            if date not in date_pos:
                raise ValueError(
                    f"write date {date!r} is not present in chunk read_dates"
                )
            day = feature.values[date_pos[date]]
            if day.dtype.kind == "b":
                keep = np.ones(day.shape[0], dtype=bool)
            elif _is_nan_missing(missing):
                keep = ~np.all(pd.isna(day), axis=1)
            else:
                keep = ~np.all(day == missing, axis=1)
            _write_feature_day(
                (
                    data_dir / f"{date}.npy",
                    code_dir / f"{date}.npy",
                    day[keep],
                    feature.space.codes[keep],
                )
            )

    def finalize_feature_write(
        self,
        key: str,
        feature_def: FeatureDef,
        *,
        dtype: str = "float64",
        missing_value: Any = np.nan,
        metadata: dict[str, Any] | None = None,
        base_dir: Path | None = None,
    ) -> FeatureMeta:
        """在全部特征分块写完后提交元数据和清单。"""
        # 识别普通目录或受管理的暂存目录，并校验覆盖权限。
        fk = parse_feature_key(key)
        base = Path(base_dir) if base_dir is not None else self.feature_dir(fk.key)
        final_dir = self.feature_dir(fk.key)
        backup_dir: Path | None = None
        is_staging = self._is_staging_feature_dir(base)
        overwrite = False
        if is_staging:
            pending = self._pending_feature_writes.get(base)
            if pending is not None:
                pending_key, overwrite = pending
                if pending_key != fk.key:
                    raise ValueError(
                        f"Staging directory {base} belongs to {pending_key}, not {fk.key}"
                    )
            if fk.key in self._manifest["features"] and not overwrite:
                raise FileExistsError(f"Feature {fk.key} already exists")
            if final_dir.exists():
                if not overwrite:
                    raise FileExistsError(
                        f"Feature directory {final_dir} already exists"
                    )
                backup_dir = self._backup_feature_dir(fk.key)
        # 根据当前快照固定轴生成可校验的特征元数据。
        space = self.resolve_space(feature_def)
        snapshot = self._manifest["snapshot"]
        asset_record = self._manifest["assets"][fk.asset]
        feature_meta = FeatureMeta.from_feature(
            key=fk.key,
            space=space,
            start=snapshot["start"],
            end=snapshot["end"],
            dates_hash=snapshot["dates_hash"],
            codes_hash=asset_record["codes_hash"],
            feature_def=feature_def,
            snapshot_id=self._manifest.get("snapshot_id"),
        )
        payload = {
            "kind": "feature",
            "feature_key": fk.key,
            "asset": fk.asset,
            "freq": fk.freq,
            "name": fk.name,
            "dtype": str(dtype),
            "dims": ["date", "asset", "step"],
            "missing_value": "nan" if _is_nan_missing(missing_value) else missing_value,
            "feature_meta": feature_meta.to_dict(),
            "storage": {
                "format": "standard_by_date",
                "data_dir": "data",
                "code_dir": "code",
            },
            **dict(metadata or {}),
        }
        # 元数据先写入待提交目录，再通过同级重命名原子发布。
        _write_json(base / "metadata.json", payload)
        previous_manifest_record = self._manifest["features"].get(fk.key)
        try:
            if is_staging:
                if backup_dir is not None:
                    os.rename(final_dir, backup_dir)
                os.rename(base, final_dir)
                committed_dir = final_dir
            else:
                committed_dir = base
            self._manifest["features"][fk.key] = str(
                (committed_dir / "metadata.json").relative_to(self.root)
            )
            self._save_manifest()
        except Exception:
            # 发布失败时同时恢复清单记录和原目录状态。
            if previous_manifest_record is None:
                self._manifest["features"].pop(fk.key, None)
            else:
                self._manifest["features"][fk.key] = previous_manifest_record
            if is_staging:
                if backup_dir is not None and backup_dir.exists():
                    if final_dir.exists():
                        if base.exists():
                            shutil.rmtree(base, ignore_errors=True)
                        os.rename(final_dir, base)
                    os.rename(backup_dir, final_dir)
                elif final_dir.exists() and not base.exists():
                    os.rename(final_dir, base)
            raise
        else:
            # 发布成功后移除备份和待处理写入记录。
            if backup_dir is not None:
                shutil.rmtree(backup_dir, ignore_errors=True)
            if is_staging:
                self._pending_feature_writes.pop(base, None)
        return feature_meta

    def _write_array(self, feature: FeatureArray, *, base_dir: Path) -> None:
        """按日期写入数组，全部成功后再提交 metadata 和 manifest。"""
        # 写入前要求调用方数组与当前快照解析出的空间完全一致。
        fk = parse_feature_key(feature.key)
        feature_def = feature.feature_def or FeatureDef.from_key(feature.key)
        space = self.resolve_space(feature_def)
        if feature.space.shape != space.shape or feature.values.shape != space.shape:
            raise ValueError(
                f"Feature {feature.key} shape {feature.values.shape} does not match resolved space {space.shape}"
            )

        data_dir = base_dir / "data"
        code_dir = base_dir / "code"
        data_dir.mkdir(parents=True, exist_ok=True)
        code_dir.mkdir(parents=True, exist_ok=True)
        missing = feature.missing_value

        # 每日过滤整行缺失资产，只保存有效数据及其对应代码。
        for i, date in enumerate(space.dates):
            day = feature.values[i]
            if day.dtype.kind == "b":
                keep = np.ones(day.shape[0], dtype=bool)
            elif _is_nan_missing(missing):
                keep = ~np.all(pd.isna(day), axis=1)
            else:
                keep = ~np.all(day == missing, axis=1)
            day_data = day[keep]
            day_codes = space.codes[keep]
            _write_feature_day(
                (
                    data_dir / f"{date}.npy",
                    code_dir / f"{date}.npy",
                    day_data,
                    day_codes,
                )
            )

        # 数据全部写完后生成元数据并更新清单索引。
        snapshot = self._manifest["snapshot"]
        asset_record = self._manifest["assets"][fk.asset]
        feature_meta = FeatureMeta.from_feature(
            key=feature.key,
            space=space,
            start=snapshot["start"],
            end=snapshot["end"],
            dates_hash=snapshot["dates_hash"],
            codes_hash=asset_record["codes_hash"],
            feature_def=feature_def,
            snapshot_id=self._manifest.get("snapshot_id"),
        )
        metadata = {
            "kind": "feature",
            "feature_key": feature.key,
            "asset": fk.asset,
            "freq": fk.freq,
            "name": fk.name,
            "dtype": str(feature.values.dtype),
            "dims": ["date", "asset", "step"],
            "missing_value": "nan" if _is_nan_missing(missing) else missing,
            "feature_meta": feature_meta.to_dict(),
            "storage": {
                "format": "standard_by_date",
                "data_dir": "data",
                "code_dir": "code",
            },
            **dict(feature.metadata),
        }
        _write_json(base_dir / "metadata.json", metadata)
        self._manifest["features"][feature.key] = str(
            (base_dir / "metadata.json").relative_to(self.root)
        )
        self._save_manifest()
        feature.feature_meta = feature_meta

    def load_feature_def(self, key: str) -> FeatureDef:
        """从物化 metadata 中恢复 FeatureDef。"""
        meta = self._load_feature_metadata(key)
        return FeatureMeta.from_dict(meta["feature_meta"]).feature_def

    def load_feature(
        self,
        key: str,
        *,
        dates: Any | None = None,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """从 Store 加载物化特征数组。"""
        return self._load_array(key, scope=self._compat_scope(dates=dates, scope=scope))

    def _load_feature_metadata(self, key: str) -> dict[str, Any]:
        """根据 manifest 索引读取特征 metadata。"""
        key = parse_feature_key(key).key
        if key not in self._manifest["features"]:
            raise KeyError(f"Feature {key} not found in FeatureStore")
        return _read_json(self.root / self._manifest["features"][key])

    def _load_array(
        self, key: str, *, scope: ExecutionScope | None = None
    ) -> FeatureArray:
        """逐日加载稀疏文件并恢复为完整 FeatureArray。"""
        # 先校验物化元数据与当前快照轴仍然一致。
        fk = parse_feature_key(key)
        meta = self._load_feature_metadata(fk.key)
        feature_meta = FeatureMeta.from_dict(meta["feature_meta"])
        self._validate_feature_meta(feature_meta)
        space = self.resolve_space(feature_meta.feature_def, scope=scope)
        missing = (
            np.nan
            if meta.get("missing_value", "nan") == "nan"
            else meta.get("missing_value")
        )
        dtype = np.dtype(meta.get("dtype", "float64"))
        if _is_nan_missing(missing) and dtype.kind in {"i", "u"}:
            dtype = np.dtype("float64")
        # 以缺失值预填完整数组，并准备代码到固定轴位置的映射。
        values = np.full(space.shape, missing, dtype=dtype)
        code_to_pos = {code: i for i, code in enumerate(space.codes)}
        meta_path = self.root / self._manifest["features"][fk.key]
        data_dir = meta_path.parent / meta.get("storage", {}).get("data_dir", "data")
        code_dir = meta_path.parent / meta.get("storage", {}).get("code_dir", "code")

        # 逐日读取稀疏资产行，校验文件形状后回填固定资产轴。
        for i, date in enumerate(space.dates):
            data_path = data_dir / f"{date}.npy"
            code_path = code_dir / f"{date}.npy"
            if not data_path.exists() or not code_path.exists():
                continue
            day_data = np.load(data_path, allow_pickle=False)
            day_codes = np.load(code_path, allow_pickle=True)
            if day_data.ndim != 2:
                raise ValueError(f"{data_path} must be N_saved x S")
            if day_data.shape[0] != len(day_codes):
                raise ValueError(f"{data_path} first dimension must match {code_path}")
            if day_data.shape[1] != space.steps:
                raise ValueError(
                    f"{data_path} step dimension {day_data.shape[1]} does not match {space.steps}"
                )
            positions = np.asarray(
                [code_to_pos.get(code, -1) for code in day_codes], dtype=int
            )
            if np.any(positions < 0):
                code = day_codes[int(np.where(positions < 0)[0][0])]
                raise ValueError(
                    f"Code {code!r} in {code_path} not found in current asset axis {fk.asset}"
                )
            values[i, positions, :] = day_data
        return FeatureArray(
            key=fk.key,
            values=values,
            space=space,
            feature_def=feature_meta.feature_def,
            feature_meta=feature_meta,
            missing_value=missing,
            metadata=meta,
        )

    def _validate_feature_meta(self, feature_meta: FeatureMeta) -> None:
        """校验物化特征是否与当前 Snapshot 固定轴一致。"""
        # 分别构造当前轴期望摘要与物化时实际摘要进行整体比较。
        snapshot = self._manifest["snapshot"]
        asset_record = self._manifest["assets"].get(feature_meta.asset)
        if asset_record is None:
            raise KeyError(
                f"Asset axis {feature_meta.asset!r} not found in current snapshot"
            )
        expected = {
            "start": snapshot["start"],
            "end": snapshot["end"],
            "n_dates": int(snapshot["n_dates"]),
            "dates_hash": snapshot["dates_hash"],
            "n_assets": int(asset_record["n_assets"]),
            "codes_hash": asset_record["codes_hash"],
            "steps": self.resolve_space(feature_meta.feature_def).steps,
        }
        actual = {
            "start": feature_meta.start,
            "end": feature_meta.end,
            "n_dates": feature_meta.n_dates,
            "dates_hash": feature_meta.dates_hash,
            "n_assets": feature_meta.n_assets,
            "codes_hash": feature_meta.codes_hash,
            "steps": feature_meta.steps,
        }
        if actual != expected:
            raise ValueError(
                f"Feature {feature_meta.key} was written against a different snapshot axis"
            )

    def import_array(
        self,
        key: str,
        values: np.ndarray,
        *,
        dates: Any | None = None,
        codes: Any | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> FeatureArray:
        """将外部数组对齐到 Snapshot 轴后写入 Store。"""
        # 统一三维形状和可表示缺失值的 dtype，并保留非标准 step 数。
        fk = parse_feature_key(key)
        arr = np.asarray(values)
        if arr.ndim == 2:
            arr = arr[:, :, None]
        if arr.ndim != 3:
            raise ValueError(f"import_array values must be 2D or 3D, got {arr.shape}")
        arr, missing_value = _array_with_safe_missing(arr)
        default_steps = get_freq_step_count(fk.freq)
        feature_steps = int(arr.shape[2])
        steps = feature_steps if feature_steps != default_steps else None
        feature_def = FeatureDef.from_key(
            fk.key, steps=steps, metadata={"origin": "import_array"}
        )
        space = self.resolve_space(feature_def)

        # 显式日期或代码轴按位置散布到快照完整坐标。
        if dates is not None:
            date_pos = _positions(
                space.dates,
                np.asarray([_date_storage_key(value) for value in dates]),
                "date",
            )
            aligned = np.full(
                (space.n_dates, arr.shape[1], arr.shape[2]),
                missing_value,
                dtype=arr.dtype,
            )
            aligned[date_pos] = arr
            arr = aligned
        if codes is not None:
            code_pos = _positions(space.codes, np.asarray(codes), "code")
            aligned = np.full(
                (arr.shape[0], space.n_assets, arr.shape[2]),
                missing_value,
                dtype=arr.dtype,
            )
            aligned[:, code_pos, :] = arr
            arr = aligned
        if arr.shape != space.shape:
            raise ValueError(
                f"import_array shape {arr.shape} does not match resolved space {space.shape}"
            )
        # 最终形状校验通过后封装并复用标准写入流程。
        feature = FeatureArray(
            key=fk.key,
            values=arr,
            space=space,
            feature_def=feature_def,
            missing_value=missing_value,
            metadata=metadata or {},
        )
        self.write_feature(feature, overwrite=overwrite)
        return feature

    def import_dataframe(
        self,
        key: str,
        df: pd.DataFrame,
        *,
        date_col: str = "DataDate",
        code_col: str = "InnerCode",
        value_col: str = "value",
        step_col: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> FeatureArray:
        """将外部长表对齐到 Snapshot 轴后写入 Store。"""
        # 先把外部证券代码统一映射为快照内部代码。
        fk = parse_feature_key(key)
        aligned_df = self.to_inner_code(
            fk.asset, df, date_col=date_col, code_col=code_col, errors="raise"
        )
        # 从数据推导 step 轴，并据此解析完整目标空间。
        step_values = np.asarray(
            [0] if step_col is None else sorted(pd.unique(aligned_df[step_col]))
        )
        steps = int(len(step_values))
        default_steps = get_freq_step_count(fk.freq)
        feature_def = FeatureDef.from_key(
            fk.key,
            steps=steps if steps != default_steps else None,
            metadata={"origin": "import_dataframe"},
        )
        space = self.resolve_space(feature_def)
        # 将长表有效记录按三个坐标位置散布进预分配数组。
        values = np.full(space.shape, np.nan, dtype=float)
        date_pos = {date: i for i, date in enumerate(space.dates)}
        code_pos = {code: i for i, code in enumerate(space.codes)}
        step_pos = {step: i for i, step in enumerate(step_values)}
        dates = aligned_df[date_col].map(_date_storage_key).map(date_pos)
        codes = aligned_df["InnerCode"].map(code_pos)
        steps = (
            pd.Series(0, index=aligned_df.index)
            if step_col is None
            else aligned_df[step_col].map(step_pos)
        )
        valid = dates.notna() & codes.notna() & steps.notna()
        values[
            dates.loc[valid].to_numpy(dtype=int),
            codes.loc[valid].to_numpy(dtype=int),
            steps.loc[valid].to_numpy(dtype=int),
        ] = aligned_df.loc[valid, value_col].to_numpy()
        feature = FeatureArray(
            key=fk.key,
            values=values,
            space=space,
            feature_def=feature_def,
            metadata=metadata or {},
        )
        self.write_feature(feature, overwrite=overwrite)
        return feature

    def _ensure_snapshot_initialized(self) -> None:
        """确认 Snapshot 日期轴已经初始化。"""
        if not self._manifest.get("snapshot") or not self._manifest["snapshot"].get(
            "dates_path"
        ):
            raise RuntimeError(
                "Snapshot is not initialized. Call FeatureStore.init_snapshot(...) first."
            )

    def _read_sql(self, sql: str, *, reader: Any | None = None) -> pd.DataFrame:
        """复用 SourceReader 的 ConnectorX 边界执行 SQL。"""
        if reader is not None:
            return reader._read_sql(sql)
        from .smartquant import SmartQuantSourceReader

        return SmartQuantSourceReader()._read_sql(sql)


def _date_storage_key(value: Any) -> str:
    """将日期转换为文件名使用的 YYYYMMDD 格式。"""
    try:
        return pd.to_datetime(value).strftime("%Y%m%d")
    except Exception:
        return str(value).replace("-", "")[:8]


def _is_nan_missing(value: Any) -> bool:
    """判断缺失值配置是否为 NaN。"""
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False


def _array_with_safe_missing(arr: np.ndarray) -> tuple[np.ndarray, Any]:
    """为外部数组选择可安全表示缺失值的 dtype 和 sentinel。"""
    if arr.dtype.kind in {"i", "u"}:
        return arr.astype(float), np.nan
    if arr.dtype.kind == "b":
        return arr, False
    return arr, np.nan


def _positions(
    space_values: np.ndarray, input_values: np.ndarray, label: str
) -> list[int]:
    """将输入轴值映射到目标轴位置，遇到未知值时显式报错。"""
    pos_map = {value: i for i, value in enumerate(space_values)}
    positions: list[int] = []
    for value in input_values:
        if value not in pos_map:
            raise ValueError(f"{label} {value!r} not found in target axis")
        positions.append(pos_map[value])
    return positions
