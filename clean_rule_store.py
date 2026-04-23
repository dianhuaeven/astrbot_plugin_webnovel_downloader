from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable


class CleanRuleRepositoryStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.clean_rules_dir = self.base_dir / "clean_rules"
        self.repos_dir = self.clean_rules_dir / "repos"
        self.index_path = self.clean_rules_dir / "index.json"
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    def import_rules_from_text(
        self,
        raw_text: str,
        repo_name: str = "",
        source_ref: str = "",
    ) -> dict[str, Any]:
        rules, skipped_rules = self._parse_rules(raw_text)
        imported_at = time.time()
        name = (repo_name or source_ref or "未命名净化仓库").strip()
        repo_id = self._build_repo_id(name, source_ref, raw_text)
        payload = {
            "repo_id": repo_id,
            "name": name,
            "source_ref": str(source_ref or "").strip(),
            "imported_at": imported_at,
            "rule_count": len(rules),
            "enabled_rule_count": sum(1 for item in rules if item.get("enabled", True)),
            "scoped_rule_count": sum(1 for item in rules if item.get("scope")),
            "skipped_rule_count": len(skipped_rules),
            "skipped_rules": skipped_rules,
            "rules": rules,
        }
        repo_path = self.repos_dir / "{repo_id}.json".format(repo_id=repo_id)
        self._write_json(repo_path, payload)

        index = self._load_index()
        record = {
            "repo_id": repo_id,
            "name": name,
            "source_ref": payload["source_ref"],
            "imported_at": imported_at,
            "rule_count": payload["rule_count"],
            "enabled_rule_count": payload["enabled_rule_count"],
            "scoped_rule_count": payload["scoped_rule_count"],
            "skipped_rule_count": payload["skipped_rule_count"],
            "path": str(repo_path),
        }
        index["repos"] = [
            item for item in index["repos"] if item.get("repo_id") != repo_id
        ]
        index["repos"].insert(0, record)
        index["updated_at"] = imported_at
        self._write_json(self.index_path, index)
        return record

    def list_repositories(self) -> list[dict[str, Any]]:
        return list(self._load_index().get("repos") or [])

    def load_applicable_cleaners(self, source: dict[str, Any]) -> list[tuple[str, str]]:
        candidates = self._source_match_candidates(source)
        cleaners: list[tuple[str, str]] = []
        seen_cleaners: set[tuple[str, str]] = set()
        for record in self.list_repositories():
            repo_payload = self._load_repo_payload(record["repo_id"])
            for rule in repo_payload.get("rules") or []:
                if not rule.get("enabled", True):
                    continue
                scope = list(rule.get("scope") or [])
                if scope and not self._scope_matches(scope, candidates):
                    continue
                pattern = str(rule.get("pattern") or "")
                if not pattern:
                    continue
                if not rule.get("is_regex", True):
                    pattern = re.escape(pattern)
                cleaner = (pattern, str(rule.get("replacement") or ""))
                # The same repository may be re-imported, and multiple repositories may
                # intentionally share a rule; apply each exact cleaner only once.
                if cleaner in seen_cleaners:
                    continue
                seen_cleaners.add(cleaner)
                cleaners.append(cleaner)
        return cleaners

    def _load_repo_payload(self, repo_id: str) -> dict[str, Any]:
        path = self.repos_dir / "{repo_id}.json".format(repo_id=repo_id)
        if not path.exists():
            raise ValueError("未找到净化规则仓库 {repo_id}".format(repo_id=repo_id))
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("净化规则仓库损坏：顶层结构不是对象")
        payload.setdefault("rules", [])
        return payload

    def _source_match_candidates(self, source: dict[str, Any]) -> list[str]:
        values = [
            source.get("source_id", ""),
            source.get("name", ""),
            source.get("source_url", ""),
            source.get("group", ""),
            source.get("clean_rule_url", ""),
        ]
        candidates = [
            str(item).strip().lower() for item in values if str(item or "").strip()
        ]
        return candidates

    def _scope_matches(
        self, scope_tokens: Iterable[str], candidates: list[str]
    ) -> bool:
        haystack = "\n".join(candidates)
        for raw_token in scope_tokens:
            token = str(raw_token or "").strip()
            if not token:
                continue
            lowered = token.lower()
            if lowered.startswith("re:"):
                try:
                    if re.search(token[3:], haystack, flags=re.I):
                        return True
                except re.error:
                    continue
                continue
            if lowered in haystack:
                return True
        return False

    def _parse_rules(
        self, raw_text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        text = str(raw_text or "").strip()
        if not text:
            raise ValueError("净化规则仓库内容不能为空")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

        if parsed is not None:
            return self._parse_json_rules(parsed)
        return self._parse_text_rules(text)

    def _parse_json_rules(
        self, payload: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        items: list[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            candidate = (
                payload.get("rules")
                or payload.get("items")
                or payload.get("data")
                or payload.get("replaceRules")
            )
            if not isinstance(candidate, list):
                raise ValueError(
                    "净化规则 JSON 必须是数组，或包含 rules/items/data/replaceRules 数组"
                )
            items = candidate
        else:
            raise ValueError("净化规则 JSON 顶层必须是对象或数组")

        rules: list[dict[str, Any]] = []
        skipped_rules: list[dict[str, str]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or item.get("regex") or "").strip()
            if not pattern:
                continue
            name = str(
                item.get("name")
                or item.get("title")
                or "rule-{index}".format(index=index)
            ).strip()
            replacement = str(
                item.get("replacement")
                or item.get("replace")
                or item.get("replaceText")
                or ""
            )
            if replacement.strip().startswith("@js:"):
                skipped_rules.append(
                    {"name": name, "reason": "JS replacement 当前不支持"}
                )
                continue
            scope_content = item.get("scopeContent")
            scope_title = item.get("scopeTitle")
            if scope_title is True and scope_content is not True:
                skipped_rules.append(
                    {"name": name, "reason": "仅标题净化规则当前不会应用到正文"}
                )
                continue
            scope = self._normalize_scope(
                item.get("scope")
                or item.get("match")
                or item.get("source")
                or item.get("sources")
                or item.get("site")
            )
            rules.append(
                {
                    "name": name,
                    "group": str(
                        item.get("group") or item.get("category") or ""
                    ).strip(),
                    "pattern": pattern,
                    "replacement": replacement,
                    "is_regex": bool(
                        item.get("isRegex", item.get("regexEnabled", True))
                    ),
                    "enabled": bool(item.get("enabled", item.get("isEnabled", True))),
                    "scope": scope,
                }
            )
        if not rules:
            raise ValueError("净化规则仓库中没有可用规则")
        return rules, skipped_rules

    def _parse_text_rules(
        self, text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        rules: list[dict[str, Any]] = []
        for index, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("##"):
                continue
            if "##" in stripped:
                pattern, replacement = stripped.split("##", 1)
            else:
                pattern, replacement = stripped, ""
            pattern = pattern.strip()
            if not pattern:
                continue
            rules.append(
                {
                    "name": "rule-{index}".format(index=index),
                    "group": "",
                    "pattern": pattern,
                    "replacement": replacement,
                    "is_regex": True,
                    "enabled": True,
                    "scope": [],
                }
            )
        if not rules:
            raise ValueError("净化规则文本中没有可用规则")
        return rules, []

    def _normalize_scope(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
        text = str(value).strip()
        if not text:
            return []
        parts = re.split(r"[\n|,;；]+", text)
        return [part.strip() for part in parts if part.strip()]

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {
                "updated_at": 0,
                "repos": [],
            }
        with open(self.index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("净化规则仓库索引损坏：顶层结构不是对象")
        payload.setdefault("updated_at", 0)
        payload.setdefault("repos", [])
        return payload

    def _build_repo_id(self, name: str, source_ref: str, raw_text: str) -> str:
        normalized_source_ref = str(source_ref or "").strip()
        # Use a stable identity so re-importing the same repository updates it instead of
        # stacking duplicate rule packs forever.
        stable_ref = (
            normalized_source_ref
            or hashlib.sha1(str(raw_text or "").encode("utf-8")).hexdigest()
        )
        digest = hashlib.sha1(
            json.dumps(
                {
                    "name": name,
                    "source_ref": stable_ref,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:10]
        slug = (
            re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", name).strip("-").lower()
            or "clean-rules"
        )
        return "{slug}-{digest}".format(slug=slug[:24], digest=digest)

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
