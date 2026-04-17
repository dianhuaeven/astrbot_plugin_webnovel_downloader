from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .core.book_resolution_service import BookResolutionService
from .core.download_manager import NovelDownloadManager, RuntimeConfig
from .core.download_orchestrator import DownloadOrchestrator
from .core.rule_engine import RuleEngine, RuleEngineConfig
from .core.search_service import SearchService, SearchServiceConfig
from .core.source_health_store import SourceHealthStore
from .core.source_probe_service import SourceProbeService, SourceProbeServiceConfig
from .core.source_downloader import SourceDownloadConfig, SourceDownloadService
from .core.source_registry import SourceRegistry
from .clean_rule_store import CleanRuleRepositoryStore


@dataclass
class PluginRuntime:
    manager: NovelDownloadManager
    source_registry: SourceRegistry
    clean_rule_store: CleanRuleRepositoryStore
    source_health_store: SourceHealthStore
    source_probe_service: SourceProbeService
    search_service: SearchService
    book_resolution_service: BookResolutionService
    source_download_service: SourceDownloadService
    download_orchestrator: DownloadOrchestrator


def _parse_positive_float(settings: dict, key: str, default: float) -> float:
    value = float(settings.get(key, default))
    if value <= 0:
        raise ValueError(
            "配置项 {key} 必须大于 0，当前值: {value}".format(key=key, value=value)
        )
    return value


def _parse_positive_int(settings: dict, key: str, default: int) -> int:
    value = int(settings.get(key, default))
    if value <= 0:
        raise ValueError(
            "配置项 {key} 必须大于 0，当前值: {value}".format(key=key, value=value)
        )
    return value


def _parse_string_list(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        text = str(value or "").strip()
        items = [line.strip() for line in text.splitlines() if line.strip()]
    if not items:
        return tuple(default)
    return tuple(items)


def build_plugin_runtime(base_dir: str | Path, config: dict | None = None) -> PluginRuntime:
    settings = config or {}
    plugin_data_dir = Path(base_dir)
    plugin_data_dir.mkdir(parents=True, exist_ok=True)
    search_request_timeout = _parse_positive_float(
        settings,
        "search_request_timeout",
        _parse_positive_float(settings, "request_timeout", 20.0),
    )
    search_max_workers = int(settings.get("search_max_workers", settings.get("max_workers", 6)))
    if search_max_workers <= 0:
        raise ValueError(
            "配置项 search_max_workers 必须大于 0，当前值: {value}".format(
                value=search_max_workers
            )
        )

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
    source_health_store = SourceHealthStore(plugin_data_dir / "source_health.json")
    download_engine = RuleEngine(
        RuleEngineConfig(
            request_timeout=runtime_config.request_timeout,
            user_agent=runtime_config.user_agent,
            use_env_proxy=runtime_config.use_env_proxy,
            clean_rule_store=clean_rule_store,
        )
    )
    search_engine = RuleEngine(
        RuleEngineConfig(
            request_timeout=search_request_timeout,
            user_agent=runtime_config.user_agent,
            use_env_proxy=runtime_config.use_env_proxy,
            clean_rule_store=clean_rule_store,
        )
    )
    search_service = SearchService(
        source_registry,
        search_engine,
        SearchServiceConfig(
            max_workers=search_max_workers,
            time_budget_seconds=_parse_positive_float(settings, "search_time_budget", 45.0),
            health_path=plugin_data_dir / "search_source_health.json",
        ),
    )
    source_probe_service = SourceProbeService(
        source_registry,
        search_engine,
        source_health_store,
        SourceProbeServiceConfig(
            max_workers=_parse_positive_int(settings, "probe_max_workers", 2),
            probe_keywords=_parse_string_list(
                settings.get("probe_keywords"),
                ("诡秘之主", "斗破苍穹", "凡人修仙传"),
            ),
        ),
    )
    source_download_service = SourceDownloadService(
        source_registry,
        download_engine,
        manager,
        SourceDownloadConfig(
            max_workers=max(1, min(8, int(settings.get("max_workers", 6)))),
        ),
    )
    book_resolution_service = BookResolutionService(
        source_registry,
        search_service,
        source_health_store,
    )
    download_orchestrator = DownloadOrchestrator(
        book_resolution_service,
        source_download_service,
    )
    return PluginRuntime(
        manager=manager,
        source_registry=source_registry,
        clean_rule_store=clean_rule_store,
        source_health_store=source_health_store,
        source_probe_service=source_probe_service,
        search_service=search_service,
        book_resolution_service=book_resolution_service,
        source_download_service=source_download_service,
        download_orchestrator=download_orchestrator,
    )
