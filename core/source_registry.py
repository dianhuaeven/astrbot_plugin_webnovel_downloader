from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .source_models import (
    REGISTRY_SCHEMA_VERSION,
    build_source_summary,
    normalize_book_source,
    parse_source_payload,
)


class SourceRegistry:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.sources_dir = self.base_dir / "sources"
        self.raw_dir = self.sources_dir / "raw"
        self.normalized_dir = self.sources_dir / "normalized"
        self.registry_path = self.sources_dir / "registry.json"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        # 串行化 registry.json 的「读-改-写」序列，避免管理员导入、bootstrap 导入、
        # 启停/删除在多线程（命令线程池、bootstrap 线程、后台 worker）下相互覆盖。
        # 读路径（list/load）依赖 _write_json 的 tmp+os.replace 原子换名，无需加锁。
        self._write_lock = threading.RLock()

    def import_sources_from_text(
        self,
        raw_text: str,
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, Any]:
        payload = parse_source_payload(raw_text)
        # 解析在锁外完成（纯计算）；从加载注册表到落盘的整段 RMW 必须在锁内串行。
        with self._write_lock:
            return self._import_parsed_sources(payload, progress_callback)

    def _import_parsed_sources(
        self,
        payload: List[Dict[str, Any]],
        progress_callback: Optional[Any],
    ) -> Dict[str, Any]:
        registry = self._load_registry()
        imported: List[Dict[str, Any]] = []
        warnings: List[str] = []
        total = len(payload)

        for index, raw_source in enumerate(payload, start=1):
            normalized = normalize_book_source(raw_source)
            source_id = normalized["source_id"]
            updated_at = time.time()

            # 只写 normalized 分片（load_normalized_source 会读它）。
            # raw 原文副本此前从不被读取，仅在导入时写、删除时清，纯属冗余写盘，
            # 占了导入耗时的一半，故不再写出。
            self._write_json(
                self.normalized_dir / "{source_id}.json".format(source_id=source_id),
                normalized,
                fsync=False,
            )

            summary = build_source_summary(normalized, updated_at).to_dict()
            registry["sources"][source_id] = summary
            imported.append(summary)
            if summary.get("issues"):
                warnings.append(
                    "{name}: {issues}".format(
                        name=summary.get("name", source_id),
                        issues="；".join(summary.get("issues", [])),
                    )
                )
            # 大批量导入时每隔若干条回报进度，便于调用方显示「正在导入 N/总数」。
            if progress_callback is not None and (
                index == total or index % 50 == 0
            ):
                try:
                    progress_callback(index, total)
                except Exception:
                    pass

        registry["updated_at"] = time.time()
        self._write_json(self.registry_path, registry)
        return {
            "imported_count": len(imported),
            "supported_search_count": sum(
                1 for item in imported if item.get("supports_search")
            ),
            "supported_download_count": sum(
                1 for item in imported if item.get("supports_download")
            ),
            "warnings": warnings,
            "sources": imported,
        }

    def list_sources(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        sources = sorted(
            registry["sources"].values(),
            key=lambda item: (item.get("enabled") is not True, item.get("name", "")),
        )
        if enabled_only:
            sources = [item for item in sources if item.get("enabled")]
        return sources

    def load_enabled_source_summaries(
        self,
        source_ids: Optional[Iterable[str]] = None,
        include_disabled: bool = False,
    ) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        selected_ids = set(source_ids or [])
        result: List[Dict[str, Any]] = []
        for source_id, summary in registry["sources"].items():
            if selected_ids and source_id not in selected_ids:
                continue
            if not include_disabled and not summary.get("enabled", False):
                continue
            result.append(summary)
        return result

    def get_source_summary(self, source_id: str) -> Dict[str, Any]:
        registry = self._load_registry()
        try:
            return registry["sources"][source_id]
        except KeyError as exc:
            raise ValueError(
                "未找到书源 {source_id}".format(source_id=source_id)
            ) from exc

    def load_normalized_source(self, source_id: str) -> Dict[str, Any]:
        path = self.normalized_dir / "{source_id}.json".format(source_id=source_id)
        if not path.exists():
            raise ValueError("未找到书源 {source_id}".format(source_id=source_id))
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_enabled_sources(
        self,
        source_ids: Optional[Iterable[str]] = None,
        include_disabled: bool = False,
    ) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        selected_ids = set(source_ids or [])
        result: List[Dict[str, Any]] = []
        for source_id, summary in registry["sources"].items():
            if selected_ids and source_id not in selected_ids:
                continue
            if not include_disabled and not summary.get("enabled", False):
                continue
            result.append(self.load_normalized_source(source_id))
        return result

    def set_enabled(self, source_id: str, enabled: bool) -> Dict[str, Any]:
        with self._write_lock:
            registry = self._load_registry()
            if source_id not in registry["sources"]:
                raise ValueError(
                    "未找到书源 {source_id}".format(source_id=source_id)
                )
            registry["sources"][source_id]["enabled"] = bool(enabled)
            registry["sources"][source_id]["updated_at"] = time.time()
            self._write_json(self.registry_path, registry)

            normalized = self.load_normalized_source(source_id)
            normalized["enabled"] = bool(enabled)
            normalized["last_imported_at"] = time.time()
            self._write_json(
                self.normalized_dir / "{source_id}.json".format(source_id=source_id),
                normalized,
            )
            return registry["sources"][source_id]

    def remove_source(self, source_id: str) -> Dict[str, Any]:
        with self._write_lock:
            registry = self._load_registry()
            if source_id not in registry["sources"]:
                raise ValueError(
                    "未找到书源 {source_id}".format(source_id=source_id)
                )
            removed = registry["sources"].pop(source_id)
            registry["updated_at"] = time.time()
            self._write_json(self.registry_path, registry)

            for directory in (self.raw_dir, self.normalized_dir):
                path = directory / "{source_id}.json".format(source_id=source_id)
                self._unlink_if_exists(path)
            return removed

    def _load_registry(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "updated_at": 0,
                "sources": {},
            }
        with open(self.registry_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("书源注册表损坏：顶层结构不是对象")
        data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
        data.setdefault("updated_at", 0)
        data.setdefault("sources", {})
        return data

    def _write_json(self, path: Path, payload: Any, fsync: bool = True) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # 批量导入时对每个分片文件 fsync 会拖慢到几秒级（每文件一次刷盘）。
            # 这些分片可由最终 registry.json 重建，故批量写入时跳过 fsync，
            # 只在收尾写 registry 时强制刷盘以保证可恢复。
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    def _unlink_if_exists(self, path: Path) -> None:
        for attempt in range(5):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
