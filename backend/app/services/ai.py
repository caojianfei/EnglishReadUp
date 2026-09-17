"""AI 编排：把「选中一段英文」翻译成「一次可缓存的 LLM 调用」。

现在只有一种任务（docs/DESIGN.md §6）：**选段【解析】**。
界面上曾经并列的「翻译 / 精读解析 / 语法点」已经合成一件事；而**划词查词**
（原来的 dict 任务）在 M10 也退出了这里 —— 音标与释义改由本地词典库提供
（`api/dictionary.py`），不再经过模型。

  explain  选中一段 → 【解析】：译文 + 主干/附加信息拆解 + 可复用语法要点

一次解析同时回答「说什么」（translation）与「怎么读」（skeleton / parts），
长难句的重点是**主干 + 挂靠关系**：先给骨架，再说清每块附加成分挂在谁身上。
`grammar_points` 是其中「换个句子也用得上」的那一小节，单独抽出来自动进语法清单
（services/grammar.persist_points）；其余字段只进 ai_logs 流水，不进任何清单 ——
清单要的是可复用的结构，不是逐句讲解。

两条铁律的实现位置：
  红线 3（音标只能来自词典）
      音标已经完全不经模型：划词由本地词典库（ECDICT + ipa-dict）提供，
      `explain` 的结果里根本没有任何音标字段。
  红线 4（改 prompt 必须升版本）
      下面每个 prompt 的版本号汇总在 PROMPT_VERSION。改任何一句提示词，
      都必须把它 +1，否则老缓存会冒充新结果返回。

缓存的粒度是「一次调用」而不是「一次选择」：
  input_hash = sha1(prompt_type|version|model|规范化输入)
  同一段文字反复解析，第二次直接读 ai_logs 里的 output_json，不烧 token。
  但每一次调用仍会留下一条流水（source=cache），需求 5 要的就是完整痕迹。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Callable

from ..db import now_iso, read, tx
from . import grammar, llm

log = logging.getLogger("english.ai")

# 改了任何 prompt 或**结果内容**就把这段 +1（红线 4）。
# m3.2 → m3.3：新增内容实质校验（空结果不再算成功），必须让旧的空结果失效。
# m3.3 → m4.0：dict 增加 worth 字段（生词本的「值得记」判定），
#              同时结果要落生词本 / 语法清单 —— 两者都改变了「同一次调用的产出」。
# m4.0 → m4.1：音标改为「只要 lemma 与划到的形态不同就用原形的音标」。
#              这是结果内容的改变（原来可能挂着变形词的读音），必须让 m4.0 的缓存失效。
# m4.1 → m8.0：**三操作合并为【解析】**。explain 的输出从
#              「summary/structure/clauses/grammar_notes」整体换成
#              「translation + skeleton + parts + grammar_points」，
#              translate / grammar 两个任务与端点下线。
#              产出变了就必须升版本，否则旧缓存会冒充新结构返回。
#              （M5–M7 没改过 prompt，所以版本号一直停在 m4.1。）
#
# M10 删掉 dict 任务（划词改查本地词典）时**刻意没有升版本**：红线 4 的口径是
# 「prompt 或产出变了才升」，而 explain 的提示词与产出逐字未变 —— 升一次版本会让
# 全部历史解析缓存作废、白烧一遍 token，换不来任何东西。谁将来改了 explain
# 的任何一个字，再在这里加一行并 +1。
PROMPT_VERSION = "m8.0"

MAX_INPUT_CHARS = 6000     # 单次送进去的正文上限，超了截断并告警
MAX_CONTEXT_CHARS = 1200   # 上下文（前后块）上限


# ---------------------------------------------------------------- 内容实质校验
#
# `extract_json` 只保证「是个 JSON 对象」，不保证里面有东西。M3 冷启动实测
# 撞到过一次：deepseek-flash 回了一个 `{"lemma": "straight"}` —— 合法、能解析,
# 于是被当成成功结果写进 ai_logs 并进了缓存，用户看到的是一张只有词头、
# 没有释义的空卡片。更糟的是它已进缓存，用户重划多少次都是同一张空卡片。
#
# 所以每个任务都要说清「什么才算有内容」：不达标就当失败走重试；
# 重试用完仍不达标就报 502。宁可明确报错，也不要留一条空结果在缓存里。
_SUBSTANCE: dict[str, tuple[str, ...]] = {
    # 解析的核心产出是译文。结构拆解（skeleton / parts）允许为空 ——
    # 划到的可能只是一个短语，本来就没有句子主干可拆，不能因此判成失败。
    "explain": ("translation",),
}


def _has_content(v: Any) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return False


def substantive(prompt_type: str) -> Callable[[dict], None]:
    """给 `llm.chat_json` 用的校验器：内容空到没法用就抛 ValueError 触发重试。"""
    keys = _SUBSTANCE.get(prompt_type, ())

    def _check(content: dict) -> None:
        if not isinstance(content, dict):
            raise ValueError("模型返回的不是 JSON 对象")
        if keys and not any(_has_content(content.get(k)) for k in keys):
            raise ValueError(f"模型返回内容为空（{'/'.join(keys)} 全空）")

    return _check


def _usable(prompt_type: str, parsed: Any) -> bool:
    """缓存里的旧结果也要过这一关：空结果不能被当成命中，否则永远自愈不了。"""
    if not isinstance(parsed, dict):
        return False
    try:
        substantive(prompt_type)(parsed)
    except ValueError:
        return False
    return True


def _cleaned_ok(prompt_type: str, clean: dict[str, Any]) -> bool:
    """清洗之后再看一眼 —— 收敛阶段也可能把内容弄没。"""
    keys = _SUBSTANCE.get(prompt_type, ())
    return bool(keys) and any(_has_content(clean.get(k)) for k in keys)


# ---------------------------------------------------------------- prompt

SYS_EXPLAIN = (
    "你是英语精读老师，服务中国学习者。解析一段英文：先给出准确、通顺的中文译文，"
    "再拆解句子结构 —— 先指出主干（谁 + 做什么 + 什么），再逐条说明附加成分"
    "（从句、非谓语、介词短语、插入语等）分别修饰或补充哪个成分、起什么作用。"
    "长难句要点明难点在哪。讲解用中文，术语中英对照。不要编造原文没有的语法现象；"
    "没有值得讲的点就返回空数组。你只输出 JSON。"
)


def _explain_user(text: str, context: str) -> str:
    return f"""请解析下面这段英文。

待解析内容：
{text}

前后文（供判断指代与语气）：
{context or "（无）"}

返回这个 JSON：
{{
  "translation": "整段的中文翻译，地道通顺，不添加原文没有的信息",
  "summary_cn": "这段话在说什么，一句话，不超过 40 字",
  "skeleton": "主干：用原文片段拼出句子骨架（主语 + 谓语 + 宾语 / 表语），例如「Resistance was the only thing」",
  "parts": [
    {{"text": "附加成分的原文片段", "role": "它是什么：定语从句 / 状语 / 非谓语 / 插入语 / 同位语 等",
      "attach": "它修饰或补充哪个成分（写清挂靠位置）",
      "explain": "起什么作用，不超过 25 字"}}
  ],
  "key_phrases": [
    {{"phrase": "原文中的短语", "meaning_cn": "中文意思", "note": "用法或搭配提示"}}
  ],
  "grammar_points": [
    {{"pattern": "可复用的结构模板，例如「not until ... did ...」",
      "category": "时态 / 语态 / 从句 / 非谓语 / 倒装 / 虚拟语气 / 介词 / 搭配 / 其他 之一",
      "explain_cn": "中文讲解，2-3 句，说清规则与含义",
      "evidence": "原文中体现该结构的片段",
      "similar": ["同类例句，最多 1 个"]}}
  ],
  "difficulty": "CEFR 难度：A2 / B1 / B2 / C1 / C2 之一",
  "hard_note": "长难句的难点：主干被什么隔开、哪一层嵌套最深；不难就填空字符串"
}}

要求：
- **主干与附加信息是重点**：读长难句卡住的地方通常不是单词，而是「主干在哪、
  这坨东西挂在谁身上」。parts 只列附加成分（主干本身不重复列），没有就返回空数组；
  按重要性排序，最多 4 条；attach 必须指向一个具体成分，不能写「整句」。
- 若待解析内容只是一个单词或短语（没有完整句子结构），skeleton 填空字符串，
  重点给 translation / key_phrases / grammar_points。
- grammar_points 只收**可复用的结构**（换个句子也用得上），最多 2 条；
  某个词在本句的临时用法、基础常识（名词可数、主谓一致）不要收；没有就返回空数组。
- key_phrases 最多 3 条，同一个短语只说一次。
- 硬性长度限制（输出越长越慢，一次解析实测要 10 秒以上，必须压住）：
  summary_cn ≤ 40 字；每条 explain ≤ 25 字；explain_cn ≤ 80 字；
  hard_note ≤ 40 字。整个 JSON 控制在 900 字以内。
- 不要输出 JSON 以外的任何文字。"""


PROMPTS: dict[str, dict[str, Any]] = {
    "explain": {
        "system": SYS_EXPLAIN,
        "build": _explain_user,
        "temperature": 0.2,
        # 解析要一次给全：译文 + 主干 + 附加成分 + 语法要点，比原来的 explain
        # 多了一节 grammar_points，预算相应上调（截断会自动翻倍重试，见 llm.py）。
        "max_tokens": 4600,
    },
}


# ---------------------------------------------------------------- 缓存键

def _norm(s: str | None) -> str:
    """归一化：折叠空白。换行差异不该造成两次调用。"""
    return re.sub(r"\s+", " ", (s or "")).strip()


def input_hash(prompt_type: str, text: str, context: str | None = None,
               model: str | None = None) -> str:
    raw = "|".join([
        prompt_type,
        PROMPT_VERSION,
        model or llm.model_name(),
        _norm(text),
        _norm(context),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _find_cached(conn, h: str, highlight_id: int | None) -> dict | None:
    if highlight_id is None:
        row = conn.execute(
            "SELECT * FROM ai_logs WHERE input_hash = ? AND highlight_id IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (h,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM ai_logs WHERE input_hash = ? AND highlight_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (h, highlight_id),
        ).fetchone()
    return dict(row) if row else None


def _latest_output(conn, h: str) -> str | None:
    """只看结果，不管是谁划的 —— 跨文章复用同一段解析结果。"""
    row = conn.execute(
        "SELECT output_json FROM ai_logs WHERE input_hash = ? ORDER BY id DESC LIMIT 1",
        (h,),
    ).fetchone()
    return row["output_json"] if row else None


def _save_log(
    conn, *, article_id: int, highlight_id: int | None, prompt_type: str,
    h: str, input_text: str, output_json: str, model: str, tokens_in: int = 0,
    tokens_out: int = 0, latency_ms: int = 0, source: str,
) -> int:
    # 痕迹可能在「_guard 检查通过」和「写日志」之间被用户删掉。
    # ai_logs 的 ON DELETE SET NULL 只作用于已存在的行，拦不住 INSERT 去引用
    # 一个刚消失的 id —— 那会直接抛 IntegrityError，把整次 AI 结果一起弄丢。
    # 需求 5 要求每次调用都留档，所以宁可把关联置空，也不丢记录。
    if highlight_id is not None:
        alive = conn.execute(
            "SELECT 1 FROM highlights WHERE id = ?", (highlight_id,)
        ).fetchone()
        if alive is None:
            highlight_id = None

    cur = conn.execute(
        "INSERT INTO ai_logs (article_id, highlight_id, prompt_type, prompt_version,"
        " model, input_hash, input_text, output_json, tokens_in, tokens_out,"
        " latency_ms, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (article_id, highlight_id, prompt_type, PROMPT_VERSION, model, h,
         input_text[:MAX_INPUT_CHARS], output_json, tokens_in, tokens_out,
         latency_ms, source, now_iso()),
    )
    log_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO fts_ai (input_text, output_json, ai_log_id) VALUES (?,?,?)",
        (input_text[:20000], output_json[:20000], log_id),
    )
    return log_id


def _post_persist(
    conn, *, prompt_type: str, article_id: int, highlight_id: int | None,
    result: dict[str, Any], sentence: str = "",
) -> dict[str, Any] | None:
    """把一次成功的解析结果同步进语法清单（需求 6）。

    「生词本」那条线已经跟着 dict 任务一起搬走了 —— 单词的入册现在由
    `api/dictionary.py` 在查词时顺手完成（走 `vocab.enroll`）。

    复用路径（source=cache）**也要走一遍**：要记的是「我确实解析过这句话」，
    这件事的发生与模型有没有被真的调用无关。persist_points 本身幂等，重复无害。
    """
    try:
        n = grammar.persist_points(
            conn,
            article_id=article_id,
            highlight_id=highlight_id,
            sentence=sentence,
            points=_as_list(result.get("grammar_points")),
        )
        return {"points": n}
    except Exception as e:  # noqa: BLE001
        # 入册失败不能把已经拿到手的结果一起丢掉 —— 结果已经落进 ai_logs，
        # 这里只记日志，让接口照常返回（痕迹的完整性优先于清单的即时性）。
        log.warning("写入语法清单失败（%s）：%s", prompt_type, e)
    return None


# ---------------------------------------------------------------- 结果规范化
def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip() if not isinstance(v, str) else v.strip()


def _clean_explain_result(content: dict) -> dict:
    def rows(key: str, keys: tuple[str, ...], limit: int) -> list[dict]:
        out = []
        for r in _as_list(content.get(key))[:limit]:
            if isinstance(r, dict):
                item = {k: _as_str(r.get(k)) for k in keys}
                if any(item.values()):
                    out.append(item)
        return out

    # grammar_points 与 grammar.persist_points 的入参形状一致（多一个 similar），
    # 因为它就是清单的数据源 —— 这里收敛干净，入册那边就不用再做防御。
    points = []
    for p in _as_list(content.get("grammar_points"))[:3]:
        if not isinstance(p, dict):
            continue
        pattern = _as_str(p.get("pattern"))
        explain_cn = _as_str(p.get("explain_cn"))
        # explanation_cn 是 NOT NULL：没有讲解的条目落不了库，也不必展示
        if not explain_cn:
            continue
        points.append({
            "pattern": pattern,
            "category": _as_str(p.get("category")),
            "explain_cn": explain_cn,
            "evidence": _as_str(p.get("evidence")),
            "similar": [_as_str(x) for x in _as_list(p.get("similar")) if _as_str(x)][:3],
        })

    return {
        "translation": _as_str(content.get("translation")),
        "summary_cn": _as_str(content.get("summary_cn")),
        "skeleton": _as_str(content.get("skeleton")),
        "parts": rows("parts", ("text", "role", "attach", "explain"), 6),
        "key_phrases": rows("key_phrases", ("phrase", "meaning_cn", "note"), 6),
        "grammar_points": points,
        "difficulty": _as_str(content.get("difficulty")),
        "hard_note": _as_str(content.get("hard_note")),
    }


CLEANERS = {
    "explain": _clean_explain_result,
}


# ---------------------------------------------------------------- 主入口

def run(
    prompt_type: str,
    *,
    article_id: int,
    text: str,
    context: str | None = None,
    highlight_id: int | None = None,
    force: bool = False,
    on_delta: Callable[[str], None] | None = None,
    on_retry: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """执行一次 AI 任务（带缓存与流水）。

    返回：
      {prompt_type, result, source: 'llm'|'cache', model, cached, tokens_in,
       tokens_out, latency_ms, ai_log_id, prompt_version, warnings}

    `on_delta` / `on_retry` 只影响「等的时候能看到什么」，不影响任何结果：
    给了就说明调用方要流式（见 api/ai.py 的 SSE 端点），清洗、落库、缓存、
    入册全部还是这条路。**刻意不做第二套流式编排** —— 两套迟早分叉，
    而分叉的地方（缓存、入册）恰恰都是红线所在。
    缓存命中时不会有任何 delta，调用方据此直接展示结果即可。
    """
    if prompt_type not in PROMPTS:
        raise ValueError(f"未知的任务类型：{prompt_type}")

    spec = PROMPTS[prompt_type]
    text = (text or "").strip()
    if not text:
        raise ValueError("待处理内容为空")

    warnings: list[str] = []
    if len(text) > MAX_INPUT_CHARS:
        warnings.append(f"内容超过 {MAX_INPUT_CHARS} 字，已截断")
        text = text[:MAX_INPUT_CHARS]
    ctx = (context or "")[:MAX_CONTEXT_CHARS]

    model = llm.model_name()

    # 缓存键带上 context：同一句话在不同上下文里解析不同（指代、语气都会变）
    h = input_hash(prompt_type, text, ctx, model)

    # ---- 缓存命中：不再调 LLM，但要留痕 ----
    # 两种命中分得很清：
    #   a) 同一个划词重复点开 → 已有它自己的流水，直接返回，不新增（否则点十次刷十条）
    #   b) 换了篇/换了划词但内容相同 → 复用结果并补一条 source=cache 的流水，
    #      这样「在这篇文章里我确实让 AI 解析过这句」这件事有记录（需求 5）
    if not force:
        with tx() as conn:
            mine = _find_cached(conn, h, highlight_id)
            if mine is not None:
                cached_result = json.loads(mine["output_json"])
                if not _usable(prompt_type, cached_result):
                    # 历史里躺着一条空结果（见 _SUBSTANCE 的说明）：不能算命中，
                    # 否则用户重划多少次都是同一张空卡片。放行去真调模型。
                    log.warning("缓存里的结果不可用，改为重新调用：%s", h[:12])
                    mine = None
            if mine is not None:
                # source 一律报 cache：这是「本次没有调用模型」的语义。
                # 记录里原本的 source（llm/cache）代表那条结果的产地，不是本次的。
                return {
                    "prompt_type": prompt_type,
                    "result": cached_result,
                    "source": "cache",
                    "model": mine["model"],
                    "cached": True,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "latency_ms": 0,
                    "ai_log_id": mine["id"],
                    "prompt_version": mine["prompt_version"],
                    # 这条是「同一个划词重复点开」，上一次已经入过册了，不再重复写
                    "enrolled": None,
                    "warnings": warnings,
                }
            reused = _latest_output(conn, h)
            if reused is not None and not _usable(prompt_type, json.loads(reused)):
                log.warning("可复用的历史结果不可用，改为重新调用：%s", h[:12])
                reused = None
            if reused is not None:
                log_id = _save_log(
                    conn,
                    article_id=article_id,
                    highlight_id=highlight_id,
                    prompt_type=prompt_type,
                    h=h,
                    input_text=f"{text}\n---\n{ctx}" if ctx else text,
                    output_json=reused,
                    model=llm.model_name(),
                    source="cache",
                )
                enrolled = _post_persist(
                    conn,
                    prompt_type=prompt_type,
                    article_id=article_id,
                    highlight_id=highlight_id,
                    result=json.loads(reused),
                    sentence=text,
                )
                return {
                    "prompt_type": prompt_type,
                    "result": json.loads(reused),
                    "source": "cache",
                    "model": llm.model_name(),
                    "cached": True,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "latency_ms": 0,
                    "ai_log_id": log_id,
                    "prompt_version": PROMPT_VERSION,
                    "enrolled": enrolled,
                    "warnings": warnings,
                }

    # ---- 真正调用 ----
    user = spec["build"](text, ctx)

    t0 = time.perf_counter()
    res = llm.chat_json(
        spec["system"],
        user,
        temperature=spec["temperature"],
        max_tokens=spec["max_tokens"],
        validate=substantive(prompt_type),
        on_delta=on_delta,
        on_retry=on_retry,
    )
    warnings.extend(res.warnings)

    clean = CLEANERS[prompt_type](res.content)

    # 保险丝：校验器看的是模型原始 JSON，清洗后仍要再确认结果真的可用。
    # cleaner 与 substantive 的字段口径未来可能分叉（比如改了字段名），这里兜住。
    if not _cleaned_ok(prompt_type, clean):
        raise llm.LLMError(f"{prompt_type} 结果为空，已重试仍无有效内容")

    with tx() as conn:
        log_id = _save_log(
            conn,
            article_id=article_id,
            highlight_id=highlight_id,
            prompt_type=prompt_type,
            h=h,
            input_text=f"{text}\n---\n{ctx}" if ctx else text,
            output_json=json.dumps(clean, ensure_ascii=False),
            model=res.model,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            latency_ms=res.latency_ms,
            source="llm",
        )
        enrolled = _post_persist(
            conn,
            prompt_type=prompt_type,
            article_id=article_id,
            highlight_id=highlight_id,
            result=clean,
            sentence=text,
        )

    return {
        "prompt_type": prompt_type,
        "result": clean,
        "source": "llm",
        "model": res.model,
        "cached": False,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "latency_ms": res.latency_ms or int((time.perf_counter() - t0) * 1000),
        "ai_log_id": log_id,
        "prompt_version": PROMPT_VERSION,
        "enrolled": enrolled,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 痕迹查询

def list_logs(
    article_id: int | None = None,
    highlight_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if article_id is not None:
        where.append("article_id = ?")
        params.append(article_id)
    if highlight_id is not None:
        where.append("highlight_id = ?")
        params.append(highlight_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM ai_logs {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT id, article_id, highlight_id, prompt_type, prompt_version, model,"
            f" input_text, output_json, tokens_in, tokens_out, latency_ms, source,"
            f" created_at FROM ai_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = json.loads(d.pop("output_json") or "{}")
        except json.JSONDecodeError:
            d["result"] = {}
            d.pop("output_json", None)
        items.append(d)
    return {"total": total, "items": items}


def delete_log(log_id: int, *, with_highlight: bool = False) -> dict:
    """删除一条解析流水（划词误操作的清理口）。

    **物理删除，不做软删除**。划词是「选中即翻译」，手一抖就会多一条记录 ——
    用户要的是「这条不该存在」，而不是「这条藏起来」。删了它，同一段文字下次
    再被划中会重新调模型、重新落一条流水；「删了就是删了」比「删了还留着」
    好解释。

    记录与缓存是同一行（`_find_cached` 靠这张表），所以删除会顺带清掉缓存：
    可接受 —— 那本来就是一次真实调用的产出，删除是用户的明确意愿。

    `fts_ai` 没有触发器（索引由 `_save_log` 手写），两处必须在同一个事务里
    成对删：只删 ai_logs 会在 FTS 里留下搜不到也管不着的孤儿行。

    **高亮**（`with_highlight=True` 时）：这条记录是它那处划线的**最后一处
    引用**才一并删除。记录删光后，那处划线就成了一处没有任何解释的孤痕
    （还占着清单的统计）；但同一处划线可能挂着多条记录
    （重跑解析会再落一条），只要还有别的记录靠它指回原文，就不能动它。
    生词本条目始终不动：词是学习成果，跟这次调用无关。

    返回 `{"deleted": ..., "highlight_deleted": ...}`，让调用方按真实结果刷新
    本地状态 —— 前端会乐观预判高亮的去留，但未必和后端一致。
    """
    with tx() as conn:
        row = conn.execute(
            "SELECT highlight_id FROM ai_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row is None:
            return {"deleted": False, "highlight_deleted": None}

        conn.execute("DELETE FROM fts_ai WHERE ai_log_id = ?", (log_id,))
        conn.execute("DELETE FROM ai_logs WHERE id = ?", (log_id,))

        deleted_highlight = None
        hid = row["highlight_id"]
        if with_highlight and hid is not None:
            left = conn.execute(
                "SELECT COUNT(*) c FROM ai_logs WHERE highlight_id = ?", (hid,)
            ).fetchone()["c"]
            if left == 0:
                conn.execute("DELETE FROM highlights WHERE id = ?", (hid,))
                deleted_highlight = int(hid)
        return {"deleted": True, "highlight_deleted": deleted_highlight}


def stats() -> dict:
    with read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(tokens_in+tokens_out),0) t,"
            " COALESCE(SUM(source='llm'),0) llm_calls,"
            " COALESCE(SUM(source='cache'),0) cache_hits"
            " FROM ai_logs"
        ).fetchone()
        hi = conn.execute("SELECT COUNT(*) c FROM highlights").fetchone()["c"]
    return {
        "ai_logs": int(row["c"]),
        "llm_calls": int(row["llm_calls"]),
        "cache_hits": int(row["cache_hits"]),
        "tokens_total": int(row["t"]),
        "highlights": int(hi),
        "prompt_version": PROMPT_VERSION,
    }


__all__ = [
    "PROMPT_VERSION",
    "delete_log",
    "input_hash",
    "list_logs",
    "run",
    "stats",
]
