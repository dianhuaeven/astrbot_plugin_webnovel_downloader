from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .core.download_manager import NovelDownloadManager, RuntimeConfig
from .core.rule_engine import RuleEngine, RuleEngineConfig
from .core.search_service import SearchService, SearchServiceConfig
from .core.source_downloader import SourceDownloadConfig, SourceDownloadService
from .core.source_registry import SourceRegistry
from .clean_rule_store import CleanRuleRepositoryStore


@dataclass
class PluginRuntime:
    manager: NovelDownloadManager
    source_registry: SourceRegistry
    clean_rule_store: CleanRuleRepositoryStore
    search_service: SearchService
    source_download_service: SourceDownloadService


def _parse_positive_float(settings: dict, key: str, default: float) -> float:
    value = float(settings.get(key, default))
    if value <= 0:
        raise ValueError(
            "配置项 {key} 必须大于 0，当前值: {value}".format(key=key, value=value)
        )
    return value


def build_plugin_runtime(base_dir: str | Path, config: dict | None = None) -> PluginRuntime:
    settings = config or {}
    plugin_data_dir = Path(base_dir)
    plugin_data_dir.mkdir(parents=True, exist_ok=True)

    runtime_config = RuntimeConfig(
        max_workers=int(settings.get("max_workers", 6)),
        request_timeout=_parse_positive_float(settings, "request_timeout", 20.0),
        use_env_proxy=bool(settings.get("use_env_proxy", False)),
        max_retries=int(settings.get("max_retries", 3)),
        retry_backoff=float(settings.get("retry_backoff", 1.6)),
        journal_fsync=bool(settings.get("journal_fsync", False)),
        default_encoding=str(settings.get("default_encoding", "")).strip(),
        preview_chars=int(settings.get("preview_chars", 4000)),
        auto_assemble=bool(settings.get("auto_assemble", True)),
        cleanup_journal_after_assemble=bool(
            settings.get("cleanup_journal_after_assemble", False)
        ),
        user_agent=str(settings.get("user_agent", "")).strip() or RuntimeConfig().user_agent,
    )
    manager = NovelDownloadManager(plugin_data_dir, runtime_config)
    source_registry = SourceRegistry(plugin_data_dir)
    clean_rule_store = CleanRuleRepositoryStore(plugin_data_dir)
    engine = RuleEngine(
        RuleEngineConfig(
            request_timeout=runtime_config.request_timeout,
            user_agent=runtime_config.user_agent,
            use_env_proxy=runtime_config.use_env_proxy,
            clean_rule_store=clean_rule_store,
        )
    )
    search_service = SearchService(
        source_registry,
        engine,
        SearchServiceConfig(
            max_workers=max(1, min(8, int(settings.get("max_workers", 6)))),
            time_budget_seconds=_parse_positive_float(settings, "search_time_budget", 45.0),
        ),
    )
    source_download_service = SourceDownloadService(
        source_registry,
        engine,
        manager,
        SourceDownloadConfig(
            max_workers=max(1, min(8, int(settings.get("max_workers", 6)))),
        ),
    )
    return PluginRuntime(
        manager=manager,
        source_registry=source_registry,
        clean_rule_store=clean_rule_store,
        search_service=search_service,
        source_download_service=source_download_service,
    )
