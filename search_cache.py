from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


class SearchCacheStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.searches_dir = self.base_dir / "searches"
        self.index_path = self.searches_dir / "index.json"
        self.searches_dir.mkdir(parents=True, exist_ok=True)

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
        record = {
            "search_id": search_id,
            "keyword": keyword,
            "created_at": created_at,
            "source_ids": list(source_ids or []),
            "include_disabled": bool(include_disabled),
            "limit": limit,
            "searched_sources": result.get("searched_sources", 0),
            "successful_sources": result.get("successful_sources", 0),
            "result_count": len(result.get("results") or []),
            "error_count": len(result.get("errors") or []),
            "path": str(self.searches_dir / "{search_id}.json".format(search_id=search_id)),
        }
        stored = {
            "record": record,
            "result": result,
        }
        self._write_json(Path(record["path"]), stored)

        index = self._load_index()
        index["searches"] = [item for item in index["searches"] if item.get("search_id") != search_id]
        index["searches"].insert(0, record)
        index["updated_at"] = created_at
        self._write_json(self.index_path, index)
        return record

    def list_searches(self) -> list[dict[str, Any]]:
        index = self._load_index()
        return list(index.get("searches") or [])

    def load_search(self, search_id: str) -> dict[str, Any]:
        path = self.searches_dir / "{search_id}.json".format(search_id=search_id)
        if not path.exists():
            raise ValueError("未找到搜索缓存 {search_id}".format(search_id=search_id))
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("搜索缓存损坏：顶层结构不是对象")
        payload.setdefault("record", {})
        payload.setdefault("result", {})
        return payload

    def get_search_result_item(self, search_id: str, result_index: int) -> dict[str, Any]:
        payload = self.load_search(search_id)
        results = list(payload.get("result", {}).get("results") or [])
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

    def _build_search_id(self, keyword: str, payload: dict[str, Any], created_at: float) -> str:
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

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
