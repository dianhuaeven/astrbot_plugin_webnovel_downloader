from __future__ import annotations

import datetime as _datetime
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

try:
    import quickjs
except ImportError:
    quickjs = None


SelectorResolver = Callable[[str], str]


# 纯 JS 宿主 prelude：实现 Legado 风格的 java.* 工具函数。
#
# 关键约束：quickjs 的原生 set_time_limit（用于硬杀死循环）与 add_callable 互斥
# —— C 层规则「Can not call into Python with a time limit set」。因此这里不能再用
# Python 回调，所有 md5/base64/timeFormat/uri 编解码改为纯 JS 实现，context 变量
# 通过 JSON 注入、改动经返回值信封带回。
#
# md5：blueimp 紧凑实现；base64：标准表 + escape/unescape 处理 UTF-8；二者均已与
# Python hashlib/base64 逐字节对拍（含 CJK）。timeFormat：quickjs 的 Date 只有 UTC，
# 故把宿主本地时区偏移（分钟）注入后用 UTC getter 还原本地时间，对齐原 Python 行为。
_JS_HOST_PRELUDE = r"""
var __B64="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function __utf8e(s){return unescape(encodeURIComponent(String(s)));}
function __utf8d(s){return decodeURIComponent(escape(String(s)));}
function __b64encode(str){
  var s=__utf8e(str), out="", i=0;
  while(i<s.length){
    var c1=s.charCodeAt(i++), c2=s.charCodeAt(i++), c3=s.charCodeAt(i++);
    var e1=c1>>2, e2=((c1&3)<<4)|(c2>>4), e3=((c2&15)<<2)|(c3>>6), e4=c3&63;
    if(isNaN(c2)){e3=e4=64;}else if(isNaN(c3)){e4=64;}
    out+=__B64.charAt(e1)+__B64.charAt(e2)+(e3==64?"=":__B64.charAt(e3))+(e4==64?"=":__B64.charAt(e4));
  }
  return out;
}
function __b64idx(ch){var d=__B64.indexOf(ch);return d<0?64:d;}
function __b64decode(str){
  var s=String(str).replace(/[^A-Za-z0-9+/=]/g,""), out="", i=0;
  while(i<s.length){
    var d1=__b64idx(s.charAt(i++)), d2=__b64idx(s.charAt(i++));
    var d3=__b64idx(s.charAt(i++)), d4=__b64idx(s.charAt(i++));
    var c1=(d1<<2)|(d2>>4), c2=((d2&15)<<4)|(d3>>2), c3=((d3&3)<<6)|(d4&63);
    out+=String.fromCharCode(c1);
    if(d3!=64)out+=String.fromCharCode(c2);
    if(d4!=64)out+=String.fromCharCode(c3);
  }
  try{return __utf8d(out);}catch(e){return out;}
}
function add32(a,b){return (a+b)&0xFFFFFFFF;}
function cmn(q,a,b,x,s,t){a=add32(add32(a,q),add32(x,t));return add32((a<<s)|(a>>>(32-s)),b);}
function ff(a,b,c,d,x,s,t){return cmn((b&c)|((~b)&d),a,b,x,s,t);}
function gg(a,b,c,d,x,s,t){return cmn((b&d)|(c&(~d)),a,b,x,s,t);}
function hh(a,b,c,d,x,s,t){return cmn(b^c^d,a,b,x,s,t);}
function ii(a,b,c,d,x,s,t){return cmn(c^(b|(~d)),a,b,x,s,t);}
function md5cycle(x,k){var a=x[0],b=x[1],c=x[2],d=x[3];a=ff(a,b,c,d,k[0],7,-680876936);d=ff(d,a,b,c,k[1],12,-389564586);c=ff(c,d,a,b,k[2],17,606105819);b=ff(b,c,d,a,k[3],22,-1044525330);a=ff(a,b,c,d,k[4],7,-176418897);d=ff(d,a,b,c,k[5],12,1200080426);c=ff(c,d,a,b,k[6],17,-1473231341);b=ff(b,c,d,a,k[7],22,-45705983);a=ff(a,b,c,d,k[8],7,1770035416);d=ff(d,a,b,c,k[9],12,-1958414417);c=ff(c,d,a,b,k[10],17,-42063);b=ff(b,c,d,a,k[11],22,-1990404162);a=ff(a,b,c,d,k[12],7,1804603682);d=ff(d,a,b,c,k[13],12,-40341101);c=ff(c,d,a,b,k[14],17,-1502002290);b=ff(b,c,d,a,k[15],22,1236535329);a=gg(a,b,c,d,k[1],5,-165796510);d=gg(d,a,b,c,k[6],9,-1069501632);c=gg(c,d,a,b,k[11],14,643717713);b=gg(b,c,d,a,k[0],20,-373897302);a=gg(a,b,c,d,k[5],5,-701558691);d=gg(d,a,b,c,k[10],9,38016083);c=gg(c,d,a,b,k[15],14,-660478335);b=gg(b,c,d,a,k[4],20,-405537848);a=gg(a,b,c,d,k[9],5,568446438);d=gg(d,a,b,c,k[14],9,-1019803690);c=gg(c,d,a,b,k[3],14,-187363961);b=gg(b,c,d,a,k[8],20,1163531501);a=gg(a,b,c,d,k[13],5,-1444681467);d=gg(d,a,b,c,k[2],9,-51403784);c=gg(c,d,a,b,k[7],14,1735328473);b=gg(b,c,d,a,k[12],20,-1926607734);a=hh(a,b,c,d,k[5],4,-378558);d=hh(d,a,b,c,k[8],11,-2022574463);c=hh(c,d,a,b,k[11],16,1839030562);b=hh(b,c,d,a,k[14],23,-35309556);a=hh(a,b,c,d,k[1],4,-1530992060);d=hh(d,a,b,c,k[4],11,1272893353);c=hh(c,d,a,b,k[7],16,-155497632);b=hh(b,c,d,a,k[10],23,-1094730640);a=hh(a,b,c,d,k[13],4,681279174);d=hh(d,a,b,c,k[0],11,-358537222);c=hh(c,d,a,b,k[3],16,-722521979);b=hh(b,c,d,a,k[6],23,76029189);a=hh(a,b,c,d,k[9],4,-640364487);d=hh(d,a,b,c,k[12],11,-421815835);c=hh(c,d,a,b,k[15],16,530742520);b=hh(b,c,d,a,k[2],23,-995338651);a=ii(a,b,c,d,k[0],6,-198630844);d=ii(d,a,b,c,k[7],10,1126891415);c=ii(c,d,a,b,k[14],15,-1416354905);b=ii(b,c,d,a,k[5],21,-57434055);a=ii(a,b,c,d,k[12],6,1700485571);d=ii(d,a,b,c,k[3],10,-1894986606);c=ii(c,d,a,b,k[10],15,-1051523);b=ii(b,c,d,a,k[1],21,-2054922799);a=ii(a,b,c,d,k[8],6,1873313359);d=ii(d,a,b,c,k[15],10,-30611744);c=ii(c,d,a,b,k[6],15,-1560198380);b=ii(b,c,d,a,k[13],21,1309151649);a=ii(a,b,c,d,k[4],6,-145523070);d=ii(d,a,b,c,k[11],10,-1120210379);c=ii(c,d,a,b,k[2],15,718787259);b=ii(b,c,d,a,k[9],21,-343485551);x[0]=add32(a,x[0]);x[1]=add32(b,x[1]);x[2]=add32(c,x[2]);x[3]=add32(d,x[3]);}
function md5blk(s){var md5blks=[],i;for(i=0;i<64;i+=4){md5blks[i>>2]=s.charCodeAt(i)+(s.charCodeAt(i+1)<<8)+(s.charCodeAt(i+2)<<16)+(s.charCodeAt(i+3)<<24);}return md5blks;}
function md51(s){var n=s.length,state=[1732584193,-271733879,-1732584194,271733878],i;for(i=64;i<=s.length;i+=64){md5cycle(state,md5blk(s.substring(i-64,i)));}s=s.substring(i-64);var tail=[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0];for(i=0;i<s.length;i++)tail[i>>2]|=s.charCodeAt(i)<<((i%4)<<3);tail[i>>2]|=0x80<<((i%4)<<3);if(i>55){md5cycle(state,tail);for(i=0;i<16;i++)tail[i]=0;}tail[14]=n*8;md5cycle(state,tail);return state;}
var __HEX="0123456789abcdef".split("");
function __rhex(n){var s="",j=0;for(;j<4;j++)s+=__HEX[(n>>(j*8+4))&0x0F]+__HEX[(n>>(j*8))&0x0F];return s;}
function __md5(s){var x=md51(__utf8e(s));for(var i=0;i<x.length;i++)x[i]=__rhex(x[i]);return x.join("");}
function __pad2(n){return (n<10?"0":"")+n;}
function __timeFormat(v){
  var num=Number(v); if(isNaN(num))return String(v);
  var ms=Math.abs(num)>=1e11?num:num*1000;
  var d=new Date(ms + __TZ_OFFSET_MIN*60000);
  return d.getUTCFullYear()+"-"+__pad2(d.getUTCMonth()+1)+"-"+__pad2(d.getUTCDate())+" "+__pad2(d.getUTCHours())+":"+__pad2(d.getUTCMinutes());
}
"""


@dataclass(frozen=True)
class JavaScriptRuntimeConfig:
    enabled: bool = True
    # 墙钟硬超时（秒），交给 quickjs 原生 set_time_limit。死循环规则到点抛
    # JSException 中断，不再泄漏工作线程。150ms 对正常 md5/正则规则偏紧，故默认 2s。
    timeout_seconds: float = 2.0
    memory_limit_bytes: int = 8 * 1024 * 1024
    max_stack_size_bytes: int = 512 * 1024


class JavaScriptTimeoutError(RuntimeError):
    """Raised when a JS rule exceeds the configured wall-clock time limit."""


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
        # selector_resolver 仅为兼容历史调用签名而保留：原 java.getString 经它回父进程
        # 查 live 文档，但该回调与原生 set_time_limit 互斥，已停用（java.getString 现抛
        # 「不支持」）。此参数不再被使用。
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
        # 原生墙钟硬超时：能中断 while(true){} 等死循环。注意这与 add_callable
        # 互斥，故本宿主完全不使用 Python 回调，所有 java.* 工具走纯 JS prelude。
        ctx.set_time_limit(max(0.05, float(self.config.timeout_seconds)))

        script = """
        {prelude}
        var __TZ_OFFSET_MIN = {tz_offset_min};
        const __codexPayloadKind = {payload_kind};
        const __codexPayloadText = {payload_text};
        const __codexBaseUrl = {base_url};
        const __codexSourceUrl = {source_url};
        const __ctx = JSON.parse({rule_vars});
        let result = {result_value};
        const baseUrl = __codexBaseUrl;
        const sourceUrl = __codexSourceUrl;
        function __codexGet(key) {{
          const value = __ctx[String(key)];
          return value === undefined || value === null ? "" : String(value);
        }}
        function __codexPut(key, value) {{
          const stored = value === undefined || value === null ? "" : String(value);
          __ctx[String(key)] = stored;
          return stored;
        }}
        const key = __codexGet("key");
        const keyword = __codexGet("keyword") || key;
        const page = __codexGet("page") || "1";
        function __codexUnsupported__(name) {{
          throw new Error(name + " 依赖网络、浏览器或宿主实时文档，当前轻量 JS 宿主不支持");
        }}
        const java = {{
          get: function(key, options) {{
            if (arguments.length > 1) {{ __codexUnsupported__("java.get"); }}
            return __codexGet(String(key));
          }},
          put: function(key, value) {{ return __codexPut(String(key), value); }},
          getString: function() {{ __codexUnsupported__("java.getString"); }},
          md5Encode: function(text) {{ return __md5(String(text)); }},
          timeFormat: function(value) {{ return __timeFormat(String(value)); }},
          base64Encode: function(value) {{ return __b64encode(String(value)); }},
          base64Decode: function(value) {{ return __b64decode(String(value)); }},
          encodeURI: function(value) {{ return encodeURIComponent(String(value)); }},
          decodeURI: function(value) {{ return decodeURIComponent(String(value)); }},
          t2s: function(value) {{ return String(value); }},
          s2t: function(value) {{ return String(value); }},
          log: function() {{ return ""; }},
          toast: function() {{ return ""; }},
          longToast: function() {{ return ""; }},
          ajax: function() {{ __codexUnsupported__("java.ajax"); }},
          post: function() {{ __codexUnsupported__("java.post"); }},
          startBrowserAwait: function() {{ __codexUnsupported__("java.startBrowserAwait"); }},
        }};
        const source = {{
          bookSourceUrl: sourceUrl,
          getKey: function() {{ return sourceUrl; }},
          getVariable: function() {{ return __codexGet("sourceVariable"); }},
          setVariable: function(value) {{ __codexPut("sourceVariable", value); return value; }},
        }};
        const book = {{
          bookUrl: __codexGet("bookUrl"),
          durChapterTitle: __codexGet("durChapterTitle"),
          getVariable: function(key) {{ return __codexGet("book." + String(key || "variable")); }},
          setVariable: function(key, value) {{ __codexPut("book." + String(key || "variable"), value); return value; }},
        }};
        {js_lib}
        function __codexRun__() {{
        {body}
        }}
        JSON.stringify((function() {{
          const __value = __codexRun__();
          return {{
            result: __value === undefined ? "" : __value,
            ctx: __ctx,
          }};
        }})());
        """.format(
            prelude=_JS_HOST_PRELUDE,
            tz_offset_min=self._local_utc_offset_minutes(),
            payload_kind=json.dumps(str(payload_kind or "")),
            payload_text=json.dumps(payload_text),
            base_url=json.dumps(str(base_url or "")),
            source_url=json.dumps(str(source_url or "")),
            rule_vars=json.dumps(json.dumps(context_values, ensure_ascii=False)),
            result_value=self._to_js_value(result),
            js_lib=normalized_js_lib,
            body=self._normalize_function_body(normalized_code),
        )

        try:
            raw_result = ctx.eval(script)
        except Exception as exc:  # quickjs.JSException 及其子类
            if self._is_timeout_error(exc):
                raise JavaScriptTimeoutError(
                    "JS 执行超时（>{seconds}s），疑似死循环或低效规则".format(
                        seconds=self.config.timeout_seconds
                    )
                ) from exc
            raise

        envelope = self._parse_envelope(raw_result)
        updated_ctx = envelope.get("ctx")
        if isinstance(updated_ctx, dict) and rule_context is not None:
            for key, value in updated_ctx.items():
                rule_context[str(key)] = "" if value is None else str(value)

        value = envelope.get("result", "")
        if value in (None, ""):
            return ""
        return value

    def _parse_envelope(self, raw_result: Any) -> dict[str, Any]:
        if raw_result in (None, ""):
            return {"result": "", "ctx": {}}
        try:
            parsed = json.loads(raw_result)
        except Exception:
            return {"result": raw_result, "ctx": {}}
        if isinstance(parsed, dict) and "result" in parsed and "ctx" in parsed:
            return parsed
        # 兼容意外的非信封返回（理论上不会发生）。
        return {"result": parsed, "ctx": {}}

    def _is_timeout_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "interrupt" in text or "time limit" in text or "timeout" in text

    def _local_utc_offset_minutes(self) -> int:
        # 用宿主本地时区把 quickjs 的 UTC-only Date 还原为本地时间，对齐原 Python 行为。
        try:
            offset = _datetime.datetime.now().astimezone().utcoffset()
        except Exception:
            return 0
        if offset is None:
            return 0
        return int(offset.total_seconds() // 60)

    def _normalize_function_body(self, code: str) -> str:
        stripped = str(code or "").strip()
        if not stripped:
            return "return '';"
        if re.search(r"\breturn\b", stripped):
            return stripped

        statements = self._split_top_level_statements(stripped)
        if not statements:
            return "return '';"
        if len(statements) == 1 and self._looks_like_returnable_expression(
            statements[0]
        ):
            return "return ({line});".format(line=statements[0])

        last_statement = statements[-1]
        if self._looks_like_returnable_expression(last_statement):
            prefix = statements[:-1]
            return "\n".join(prefix + ["return ({line});".format(line=last_statement)])
        return stripped + "\nreturn result;"

    def _split_top_level_statements(self, code: str) -> list[str]:
        statements: list[str] = []
        buffer: list[str] = []
        quote_char = ""
        escape = False
        depth_round = 0
        depth_square = 0
        depth_curly = 0
        for char in str(code or ""):
            buffer.append(char)
            if quote_char:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote_char:
                    quote_char = ""
                continue
            if char in ("'", '"', "`"):
                quote_char = char
                continue
            if char == "(":
                depth_round += 1
            elif char == ")":
                depth_round = max(0, depth_round - 1)
            elif char == "[":
                depth_square += 1
            elif char == "]":
                depth_square = max(0, depth_square - 1)
            elif char == "{":
                depth_curly += 1
            elif char == "}":
                depth_curly = max(0, depth_curly - 1)
            elif (
                char == ";"
                and depth_round == 0
                and depth_square == 0
                and depth_curly == 0
            ):
                statement = "".join(buffer).strip().rstrip(";").strip()
                if statement:
                    statements.append(statement)
                buffer = []
        tail = "".join(buffer).strip().rstrip(";").strip()
        if tail:
            statements.append(tail)
        return statements

    def _looks_like_returnable_expression(self, statement: str) -> bool:
        normalized = str(statement or "").strip()
        if not normalized:
            return False
        lowered = normalized.lower()
        blocked_prefixes = (
            "var ",
            "let ",
            "const ",
            "function ",
            "class ",
            "if ",
            "if(",
            "for ",
            "for(",
            "while ",
            "while(",
            "switch ",
            "switch(",
            "try",
            "catch ",
            "catch(",
            "else",
        )
        if lowered.startswith(blocked_prefixes):
            return False
        if normalized.endswith("}"):
            return False
        return True

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

    def _guard_unsupported_code(self, code: str) -> None:
        lowered = str(code or "").lower()
        blocked_tokens = (
            "java.ajax",
            "java.post",
            "java.getstring",
            "startbrowserawait",
            "fetch(",
            "xmlhttprequest",
            "document.",
            "window.",
            "location.",
        )
        if any(token in lowered for token in blocked_tokens):
            raise RuntimeError(
                "JS 规则依赖网络、浏览器或宿主实时文档，当前轻量 JS 宿主不支持"
            )
