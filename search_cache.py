from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class SearchCacheStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.searches_dir = self.base_dir / "searches"
        self.index_path = self.searches_dir / "index.json"
        self.searches_dir.mkdir(parents=True, exist_ok=True)
        # 串行化 index.json 的「读-改-写」，避免并发搜索的 save_search 互相覆盖历史。
        self._write_lock = threading.RLock()

    def save_search(
        self,
        keyword: str,
        result: dict[str, Any],
        source_ids: list[str] | None = None,
        include_disabled: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "keyword": keyword,
            "result": result,
            "source_ids": list(source_ids or []),
            "include_disabled": bool(include_disabled),
            "limit": limit,
        }
        created_at = time.time()
        search_id = self._build_search_id(keyword, payload, created_at)
        search_path = self._resolve_search_path(search_id)
        record = {
            "search_id": search_id,
            "keyword": keyword,
            "created_at": created_at,
            "source_ids": list(source_ids or []),
            "include_disabled": bool(include_disabled),
            "limit": limit,
            "searched_sources": self._search_stat(result, "searched_sources"),
            "successful_sources": self._search_stat(result, "successful_sources"),
            "result_count": self._result_count(result),
            "candidate_group_count": len(result.get("candidate_groups") or []),
            "error_count": len(self._search_errors(result)),
            "path": str(search_path),
        }
        stored = {
            "record": record,
            "result": result,
        }
        # 分片文件路径含 search_id（关键字+时间戳+摘要），实际不冲突，可在锁外写。
        # index.json 的读-改-写则必须串行，否则并发搜索会丢历史记录。
        self._write_json(search_path, stored)

        with self._write_lock:
            index = self._load_index()
            index["searches"] = [
                item
                for item in index["searches"]
                if item.get("search_id") != search_id
            ]
            index["searches"].insert(0, record)
            index["updated_at"] = created_at
            self._write_json(self.index_path, index)
        return record

    def list_searches(self) -> list[dict[str, Any]]:
        index = self._load_index()
        return list(index.get("searches") or [])

    def load_search(self, search_id: str) -> dict[str, Any]:
        path = self._resolve_search_path(search_id)
        if not path.exists():
            raise ValueError("未找到搜索缓存 {search_id}".format(search_id=search_id))
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("搜索缓存损坏：顶层结构不是对象")
        payload.setdefault("record", {})
        payload.setdefault("result", {})
        return payload

    def get_search_result_item(
        self, search_id: str, result_index: int
    ) -> dict[str, Any]:
        payload = self.load_search(search_id)
        result = dict(payload.get("result") or {})
        results = list(self._search_result(result).get("results") or [])
        if result_index < 0 or result_index >= len(results):
            raise ValueError(
                "搜索缓存 {search_id} 中不存在 result_index={index}".format(
                    search_id=search_id,
                    index=result_index,
                )
            )
        item = dict(results[result_index])
        item["result_index"] = result_index
        item["search_id"] = search_id
        return item

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {
                "updated_at": 0,
                "searches": [],
            }
        with open(self.index_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("搜索缓存索引损坏：顶层结构不是对象")
        data.setdefault("updated_at", 0)
        data.setdefault("searches", [])
        return data

    def _build_search_id(
        self, keyword: str, payload: dict[str, Any], created_at: float
    ) -> str:
        digest = hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:10]
        timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime(created_at))
        return "{keyword}-{timestamp}-{digest}".format(
            keyword=self._sanitize_key(keyword),
            timestamp=timestamp,
            digest=digest,
        )

    def _sanitize_key(self, keyword: str) -> str:
        text = "".join(ch if ch.isalnum() else "-" for ch in str(keyword or "").strip())
        text = text.strip("-")
        return text[:24] or "search"

    def _resolve_search_path(self, search_id: str) -> Path:
        normalized = str(search_id or "").strip()
        if not normalized:
            raise ValueError("搜索缓存 ID 不能为空")
        path = self.searches_dir / "{search_id}.json".format(search_id=normalized)
        resolved_parent = path.resolve().parent
        expected_parent = self.searches_dir.resolve()
        if resolved_parent != expected_parent:
            raise ValueError("非法搜索缓存 ID: {search_id}".format(search_id=search_id))
        return path

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    def _search_result(self, result: dict[str, Any]) -> dict[str, Any]:
        nested = result.get("search_result")
        if isinstance(nested, dict):
            return nested
        return result

    def _search_stat(self, result: dict[str, Any], key: str) -> int:
        search_result = self._search_result(result)
        return int(search_result.get(key, 0) or 0)

    def _search_errors(self, result: dict[str, Any]) -> list[Any]:
        return list(self._search_result(result).get("errors") or [])

    def _result_count(self, result: dict[str, Any]) -> int:
        if result.get("candidate_groups") is not None:
            return len(result.get("candidate_groups") or [])
        return len(self._search_result(result).get("results") or [])
