from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

try:
    import quickjs
except ImportError:
    quickjs = None


SelectorResolver = Callable[[str], str]


@dataclass(frozen=True)
class JavaScriptRuntimeConfig:
    enabled: bool = True
    timeout_ms: int = 150
    memory_limit_bytes: int = 8 * 1024 * 1024
    max_stack_size_bytes: int = 512 * 1024


class JavaScriptRuntime:
    def __init__(self, config: JavaScriptRuntimeConfig | None = None):
        self.config = config or JavaScriptRuntimeConfig()

    @property
    def available(self) -> bool:
        return bool(self.config.enabled and quickjs is not None)

    def evaluate(
        self,
        code: str,
        *,
        result: Any = "",
        payload_kind: str = "",
        payload: Any = None,
        rule_context: dict[str, Any] | None = None,
        selector_resolver: SelectorResolver | None = None,
        js_lib: str = "",
        base_url: str = "",
        source_url: str = "",
    ) -> Any:
        if not self.available:
            raise RuntimeError("当前环境未安装 quickjs，无法执行纯 JS 规则")

        normalized_code = str(code or "").strip()
        self._guard_unsupported_code(normalized_code)
        normalized_js_lib = str(js_lib or "").strip()
        if normalized_js_lib:
            self._guard_unsupported_code(normalized_js_lib)

        payload_text = self._payload_text(payload)
        context_values = {
            str(key): str(value) for key, value in dict(rule_context or {}).items()
        }

        ctx = quickjs.Context()
        ctx.set_memory_limit(max(1024 * 1024, int(self.config.memory_limit_bytes)))
        ctx.set_max_stack_size(max(64 * 1024, int(self.config.max_stack_size_bytes)))
        ctx.add_callable("py_get", lambda key: str(context_values.get(str(key), "")))
        ctx.add_callable(
            "py_put",
            lambda key, value: self._store_context_value(context_values, key, value),
        )
        ctx.add_callable(
            "py_get_string",
            lambda expression: self._resolve_selector(selector_resolver, expression),
        )
        ctx.add_callable(
            "py_md5", lambda text: hashlib.md5(str(text).encode("utf-8")).hexdigest()
        )
        ctx.add_callable("py_time_format", self._time_format)

        script = """
        const __codexPayloadKind = {payload_kind};
        const __codexPayloadText = {payload_text};
        const __codexBaseUrl = {base_url};
        const __codexSourceUrl = {source_url};
        const __codexRuleVars = JSON.parse({rule_vars});
        let result = {result_value};
        const baseUrl = __codexBaseUrl;
        const sourceUrl = __codexSourceUrl;
        const java = {{
          get: function(key) {{ return py_get(String(key)); }},
          put: function(key, value) {{
            py_put(String(key), value === undefined || value === null ? "" : String(value));
            return value;
          }},
          getString: function(expression) {{ return py_get_string(String(expression)); }},
          md5Encode: function(text) {{ return py_md5(String(text)); }},
          timeFormat: function(value) {{ return py_time_format(String(value)); }},
        }};
        {js_lib}
        function __codexRun__() {{
        {body}
        }}
        JSON.stringify((function() {{
          const __value = __codexRun__();
          return __value === undefined ? "" : __value;
        }})());
        """.format(
            payload_kind=json.dumps(str(payload_kind or "")),
            payload_text=json.dumps(payload_text),
            base_url=json.dumps(str(base_url or "")),
            source_url=json.dumps(str(source_url or "")),
            rule_vars=json.dumps(json.dumps(context_values, ensure_ascii=False)),
            result_value=self._to_js_value(result),
            js_lib=normalized_js_lib,
            body=self._normalize_function_body(normalized_code),
        )
        raw_result = ctx.eval(script)
        for key, value in context_values.items():
            if rule_context is not None:
                rule_context[key] = value
        if raw_result in (None, ""):
            return ""
        try:
            return json.loads(raw_result)
        except Exception:
            return raw_result

    def _normalize_function_body(self, code: str) -> str:
        stripped = str(code or "").strip()
        if not stripped:
            return "return '';"
        if "return" in stripped:
            return stripped
        lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
        if not lines:
            return "return '';"
        if len(lines) == 1 and not lines[0].endswith(";"):
            return "return ({line});".format(line=lines[0])
        last_line = lines[-1].rstrip().rstrip(";")
        prefix = lines[:-1]
        if prefix:
            return "\n".join(prefix + ["return ({line});".format(line=last_line)])
        return "return ({line});".format(line=last_line)

    def _to_js_value(self, value: Any) -> str:
        if isinstance(value, (dict, list, tuple)):
            return "JSON.parse({payload})".format(
                payload=json.dumps(json.dumps(value, ensure_ascii=False))
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return json.dumps(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        return json.dumps(str(value))

    def _payload_text(self, payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (dict, list)):
            return json.dumps(payload, ensure_ascii=False)
        if hasattr(payload, "get"):
            try:
                return str(payload.get() or "")
            except Exception:
                return str(payload)
        return str(payload)

    def _resolve_selector(
        self,
        selector_resolver: SelectorResolver | None,
        expression: str,
    ) -> str:
        if selector_resolver is None:
            return ""
        return str(selector_resolver(str(expression or "")) or "")

    def _store_context_value(
        self,
        context_values: dict[str, str],
        key: Any,
        value: Any,
    ) -> str:
        normalized_key = str(key or "")
        normalized_value = "" if value is None else str(value)
        context_values[normalized_key] = normalized_value
        return normalized_value

    def _time_format(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            numeric = float(text)
        except Exception:
            return text
        timestamp_seconds = numeric / 1000.0 if abs(numeric) >= 1e11 else numeric
        return datetime.fromtimestamp(timestamp_seconds).strftime("%Y-%m-%d %H:%M")

    def _guard_unsupported_code(self, code: str) -> None:
        lowered = str(code or "").lower()
        blocked_tokens = (
            "java.ajax",
            "fetch(",
            "xmlhttprequest",
            "document.",
            "window.",
            "location.",
        )
        if any(token in lowered for token in blocked_tokens):
            raise RuntimeError("JS 规则依赖网络或浏览器能力，当前轻量 JS 宿主不支持")
