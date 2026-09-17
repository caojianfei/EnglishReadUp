"""LLM 客户端：OpenAI 兼容端点 + 严格 JSON 输出。

为什么不用 `openai` SDK：
  这个项目只用到 `/chat/completions` 一个接口，httpx 已经是依赖（抓取在用），
  直接写省掉一个包和它的版本约束。行为完全可控。

三条硬约束（docs/DESIGN.md §4 红线 4）：
  1. **所有调用都落 ai_logs** —— 落库在 ai.py 的编排层做，这里只负责调用与计时。
  2. **按 input_hash 缓存** —— 同样在编排层，缓存的是结果不是日志。
  3. **改 prompt 必须升 PROMPT_VERSION** —— 常量在 ai.py。

JSON 可靠性：
  DeepSeek 支持 `response_format={"type": "json_object"}`，但仍有小概率
  返回带 markdown 围栏或前后缀说明的文本，所以解析走「层层剥离」而不是裸 json.loads。

流式（M5）：
  `chat_json` 接一个可选的 `on_delta`。给了它就走 SSE 拉取，边收边回调，
  但**返回值与落库路径与一次性调用完全一致** —— 调用方（services/ai.py）不需要
  知道自己走的是哪条路。这样「流式」不会变成第二套 AI 编排。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import settings

log = logging.getLogger("english.llm")

# 单例客户端：复用连接池。httpx.Client 本身线程安全，可被多个请求线程共用。
_client_lock = threading.Lock()
_client: httpx.Client | None = None

# ```json ... ``` / ``` ... ```
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


class LLMError(RuntimeError):
    """LLM 不可用（网络、鉴权、限流）。调用方负责降级提示。"""


class _HTTPStatus(Exception):
    """内部用：把「上游返回了非 200」从连接类异常里分出来，好套同一套重试规则。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}：{body}")
        self.status = status
        self.body = body


@dataclass
class LLMResult:
    content: dict[str, Any]
    raw: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    base_url=settings.llm_base_url.rstrip("/"),
                    timeout=httpx.Timeout(90.0, connect=15.0),
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                )
    return _client


def extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里尽力抠出一个 JSON 对象。

    依次尝试：裸解析 → 剥 markdown 围栏 → 截取第一个 `{` 到最后一个 `}`。
    全部失败才抛 LLMError（由上层决定是否重试）。
    """
    text = (raw or "").strip()
    if not text:
        raise LLMError("模型返回空内容")

    for candidate in (text, _FENCE.sub(r"\1", text).strip()):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(text[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise LLMError(f"模型未返回合法 JSON：{text[:200]}")


def _post_once(payload: dict) -> tuple[str, dict, dict]:
    """一次性拉取。返回 (正文文本, choice, usage)。"""
    r = get_client().post("/chat/completions", json=payload)
    if r.status_code != 200:
        raise _HTTPStatus(r.status_code, r.text[:300])
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or "", choice, data.get("usage") or {}


def _post_stream(
    payload: dict, on_delta: Callable[[str], None]
) -> tuple[str, dict, dict]:
    """SSE 拉取。逐块回调 `on_delta`，最后拼成完整文本返回。

    只把 `delta.content` 当正文：DeepSeek 的思考过程走 `delta.reasoning_content`，
    它是模型的自言自语，不是答案，拼进去会让 JSON 解析在后面白跑一趟。
    但**要显示**——「有东西在动」正是流式存在的理由，所以思考片段照样回调，
    只是不拼进正文。调用方按事件类型自己决定怎么用。

    这里刻意不返回 token 计时中间态，usage 由末尾那个 `include_usage` 块给。
    """
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    parts: list[str] = []
    finish: str | None = None
    usage: dict = {}
    model: str | None = None

    with get_client().stream("POST", "/chat/completions", json=payload) as r:
        if r.status_code != 200:
            # 必须把 body 读出来才拿得到错误详情，也给连接一个正常关闭的机会
            r.read()
            raise _HTTPStatus(r.status_code, r.text[:300])
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue          # 空行是帧分隔，`:` 开头是注释/心跳
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue          # 半截帧/心跳，跳过而不是整体失败
            if obj.get("model"):
                model = obj["model"]
            if obj.get("usage"):
                usage = obj["usage"]
            ch = (obj.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                parts.append(piece)
                on_delta(piece)
            elif delta.get("reasoning_content"):
                # 思考片段：只报「上游有动静」，所以回调空串。前端把它当成心跳，
                # 用来把「已等待 12 秒」从死气沉沉改成「模型正在想」。
                on_delta("")

    choice = {"finish_reason": finish}
    if model:
        choice["model"] = model
    return "".join(parts), choice, usage


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    retries: int = 1,
    model: str | None = None,
    validate: Callable[[dict], None] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_retry: Callable[[], None] | None = None,
) -> LLMResult:
    """请求一次对话补全并解析成 dict。

    `retries` 覆盖三类可恢复故障：网络异常、模型偶发吐出非法 JSON、
    以及**解析成功但内容为空**（由 `validate` 判定）。后两类重试时会在 user
    末尾追加一句更硬的格式要求。

    `validate` 为什么要放在这一层：它必须和「解析失败」走同一条重试路径。
    放到调用方去判，就只能「失败即报错」，白白浪费掉重试机会 ——
    而 deepseek-flash 偶尔真会回一个 `{"lemma": "..."}` 这种合法但全空的
    JSON（M3 实测，见 `services/ai.py` 的 `_substantive`）。

    `on_delta` 给了就走流式：每收到一段正文回调一次。返回值与非流式调用完全
    同构，所以调用方不必为流式写第二套逻辑。重试前会调 `on_retry()`，
    前端据此清空已经画出来的半截文本（否则第二次的输出会接在第一次后面）。
    """
    if not settings.llm_api_key:
        raise LLMError("未配置 LLM_API_KEY（backend/.env）")

    use_model = model or settings.llm_model
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err: Exception | None = None

    # 截断时会自动扩大上限重试，所以预算要可变。
    # deepseek-flash 会先输出 reasoning token 再输出正文，同样长度的答案
    # 需要的预算比普通模型更宽 —— 这是 M3 实测踩到的坑（见下方 truncated 分支）。
    cur_max = max_tokens

    for attempt in range(retries + 1):
        payload = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": cur_max,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        t0 = time.perf_counter()
        try:
            if on_delta is None:
                raw, choice, usage = _post_once(payload)
            else:
                raw, choice, usage = _post_stream(payload, on_delta)
        except _HTTPStatus as e:
            latency = int((time.perf_counter() - t0) * 1000)
            # 4xx 里除了 429 都是不可重试的（鉴权错、模型名错、额度）
            if e.status != 429 and 400 <= e.status < 500:
                raise LLMError(f"LLM 返回 {e.status}：{e.body}") from e
            last_err = LLMError(f"HTTP {e.status}：{e.body}")
            log.warning("LLM HTTP %s（第 %d 次）：%s", e.status, attempt + 1, e.body)
            if on_retry is not None:
                on_retry()
            continue
        except Exception as e:  # noqa: BLE001 —— 连接层任何异常都算可重试
            last_err = e
            log.warning("LLM 网络异常（第 %d 次，%dms）：%s", attempt + 1,
                        int((time.perf_counter() - t0) * 1000), e)
            if on_retry is not None:
                on_retry()
            continue

        latency = int((time.perf_counter() - t0) * 1000)
        truncated = choice.get("finish_reason") == "length"
        try:
            content = extract_json(raw)
            if validate is not None:
                validate(content)
        except (LLMError, ValueError) as e:
            last_err = e
            log.warning(
                "JSON 解析/校验失败（第 %d/%d 次，截断=%s，out_tokens=%s）：%s",
                attempt + 1, retries + 1, truncated,
                usage.get("completion_tokens"), raw[:200],
            )
            if attempt < retries:
                # 输出被截断时，病根是 max_tokens 不够 —— 原样重试会再次截断。
                # 把预算翻倍，同时收紧措辞（双管齐下，一次就好）。
                if truncated:
                    cur_max = min(cur_max * 2, 8000)
                    log.info("max_tokens 提升到 %d 后重试", cur_max)
                messages = [
                    *messages[:2],
                    {
                        "role": "user",
                        "content": user + "\n\n只输出一个 JSON 对象，不要任何解释或代码块标记。",
                    },
                ]
                if on_retry is not None:
                    on_retry()
            continue

        warnings: list[str] = []
        if truncated:
            warnings.append("输出被 max_tokens 截断，结果可能不完整")

        return LLMResult(
            content=content,
            raw=raw,
            model=choice.get("model") or usage.get("model") or use_model,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            latency_ms=latency,
            finish_reason=choice.get("finish_reason"),
            warnings=warnings,
        )

    raise LLMError(f"LLM 调用失败：{last_err}")


def ping() -> dict[str, Any]:
    """健康检查用：探一次极小的 JSON 请求。"""
    t0 = time.perf_counter()
    try:
        # max_tokens 不能给太小：deepseek-flash 会先花掉 reasoning token，
        # 20 的预算会被思考过程吃光，正文是空的（M3 实测）。
        res = chat_json(
            "You reply only with JSON.",
            'Return {"ok": true}',
            max_tokens=300,
            retries=1,
        )
        return {
            "ok": bool(res.content.get("ok")),
            "model": res.model,
            "latency_ms": res.latency_ms or int((time.perf_counter() - t0) * 1000),
            "tokens": res.tokens_in + res.tokens_out,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def model_name() -> str:
    return settings.llm_model


__all__ = ["LLMError", "LLMResult", "chat_json", "extract_json", "model_name", "ping"]
