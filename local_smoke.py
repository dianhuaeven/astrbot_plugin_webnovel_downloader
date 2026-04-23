from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .plugin_renderer import ToolRenderConfig, ToolResultRenderer
from .plugin_runtime import build_plugin_runtime
from .text_loader import load_text_argument


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在本地直接导入书源、列书源、搜书，不依赖 AstrBot。"
    )
    parser.add_argument(
        "--data-dir",
        default=".webnovel-local-smoke",
        help="本地运行目录，默认写到当前目录下的 .webnovel-local-smoke",
    )
    parser.add_argument(
        "--source-json",
        default="",
        help="书源 JSON，支持 URL、文件路径或原始 JSON 文本；留空则复用现有 registry.json",
    )
    parser.add_argument("--keyword", default="", help="可选，导入后立即搜索这个关键词")
    parser.add_argument(
        "--source-ids-json",
        default="",
        help="可选，JSON 数组或逗号分隔的 source_id 列表，用于缩小搜索范围",
    )
    parser.add_argument("--limit", type=int, default=20, help="搜索结果条数上限")
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="搜索时包含已禁用书源",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="导入后额外输出一页书源摘要，便于先看兼容性",
    )
    parser.add_argument(
        "--enabled-only",
        action="store_true",
        help="与 --list-sources 一起使用，只列启用书源",
    )
    parser.add_argument("--list-limit", type=int, default=20, help="列书源分页大小")
    parser.add_argument("--list-offset", type=int, default=0, help="列书源分页偏移")
    parser.add_argument("--max-workers", type=int, default=3, help="正文下载并发数")
    parser.add_argument(
        "--search-max-workers",
        type=int,
        default=6,
        help="搜书并发数",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=20.0,
        help="请求超时秒数，必须大于 0",
    )
    parser.add_argument(
        "--search-request-timeout",
        type=float,
        default=20.0,
        help="搜书请求超时秒数，必须大于 0",
    )
    parser.add_argument(
        "--search-time-budget",
        type=float,
        default=45.0,
        help="搜书总时间预算秒数，必须大于 0",
    )
    parser.add_argument(
        "--use-env-proxy",
        action="store_true",
        help="沿用当前进程的 http_proxy/https_proxy/no_proxy 环境变量",
    )
    parser.add_argument(
        "--max-tool-response-chars",
        type=int,
        default=2800,
        help="本地摘要最大字符数",
    )
    parser.add_argument(
        "--max-tool-preview-items",
        type=int,
        default=8,
        help="本地摘要最多内联多少条预览",
    )
    parser.add_argument(
        "--max-tool-preview-text",
        type=int,
        default=180,
        help="本地摘要里单段文本的最大长度",
    )
    return parser


def _parse_string_list(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime = build_plugin_runtime(
        data_dir,
        {
            "max_workers": args.max_workers,
            "search_max_workers": args.search_max_workers,
            "request_timeout": args.request_timeout,
            "search_request_timeout": args.search_request_timeout,
            "search_time_budget": args.search_time_budget,
            "use_env_proxy": bool(args.use_env_proxy),
        },
    )
    reports_dir = data_dir / "reports"
    renderer = ToolResultRenderer(
        reports_dir,
        runtime.source_registry,
        runtime.manager,
        ToolRenderConfig(
            max_tool_response_chars=max(800, int(args.max_tool_response_chars)),
            max_tool_preview_items=max(1, int(args.max_tool_preview_items)),
            max_tool_preview_text=max(60, int(args.max_tool_preview_text)),
        ),
    )

    payload: dict[str, object] = {
        "data_dir": str(data_dir),
        "registry_path": str(runtime.source_registry.registry_path),
    }
    if args.source_json:
        source_text = load_text_argument(
            args.source_json,
            runtime.manager.config.user_agent,
            runtime.manager.config.request_timeout,
            runtime.manager.config.default_encoding,
            runtime.manager.config.use_env_proxy,
        )
        import_result = runtime.source_registry.import_sources_from_text(source_text)
        payload["import"] = json.loads(renderer.render_import_summary(import_result))
    elif not runtime.source_registry.registry_path.exists():
        raise ValueError("未提供 --source-json，且当前 data dir 下不存在 registry.json")

    if args.list_sources:
        sources = runtime.source_registry.list_sources(bool(args.enabled_only))
        sources = runtime.source_health_store.enrich_sources(sources)
        payload["sources"] = json.loads(
            renderer.render_sources_summary(
                sources,
                bool(args.enabled_only),
                max(1, int(args.list_limit)),
                max(0, int(args.list_offset)),
            )
        )

    if args.keyword:
        search_result = runtime.search_service.search(
            args.keyword,
            _parse_string_list(args.source_ids_json) or None,
            max(1, int(args.limit)),
            bool(args.include_disabled),
        )
        payload["search"] = json.loads(renderer.render_search_summary(search_result))

    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run_smoke(args)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
