"""M3 前置探测：LLM 端点连通性 + 模型名可用性 + 词典服务形态。

回答三个问题：
  1. .env 里的 LLM_API_KEY 是否能通过鉴权？
  2. `deepseek-flash` 这个模型名是否真实存在？
  3. 词典服务（Wiktionary）能否拿到音标，形态是什么？

注意：dictionaryapi.dev 已于 2026-09-14 实测整体 522 / 读超时，
音标来源已改为 Wiktionary action=parse（见 app/services/dictionary.py）。
本脚本第 4 步直接调用词典服务，不再手拼 URL。

用 `--list-models` 只探模型清单，不动正文。
用 `--word X` 指定探测词，默认 resilient。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402


def hr(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def probe_models() -> None:
    hr(f"[1] 模型清单 GET {settings.llm_base_url}/models")
    try:
        r = httpx.get(
            f"{settings.llm_base_url}/models",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=20,
        )
        print(f"HTTP {r.status_code}")
        if r.status_code != 200:
            print(r.text[:600])
            return
        ids = [m.get("id") for m in r.json().get("data", [])]
        print("模型:", json.dumps(ids, ensure_ascii=False))
        want = settings.llm_model
        print(f"当前配置的 `{want}` 存在？", want in ids)
    except Exception as e:  # noqa: BLE001
        print(f"请求异常: {type(e).__name__}: {e}")


def probe_chat(model: str) -> None:
    hr(f"[2] 对话 POST /chat/completions  model={model}")
    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "只回四个字：可以用了"}],
                # 实测：deepseek-flash 会先吐推理 token，预算太小会整条被截断
                "max_tokens": 300,
            },
            timeout=120,
        )
        ms = (time.perf_counter() - t0) * 1000
        print(f"HTTP {r.status_code}   {ms:.0f}ms")
        if r.status_code == 200:
            body = r.json()
            ch = body["choices"][0]
            print("content:", ch["message"]["content"])
            print("finish_reason:", ch.get("finish_reason"))
            print("usage:", json.dumps(body.get("usage", {}), ensure_ascii=False))
        else:
            print(r.text[:600])
    except Exception as e:  # noqa: BLE001
        print(f"请求异常: {type(e).__name__}: {e}")


def probe_json_mode(model: str) -> None:
    hr(f"[3] JSON 模式 POST /chat/completions  model={model}")
    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "只输出 JSON，不要任何解释。"},
                    {
                        "role": "user",
                        "content": '把 "resilient" 的词性和中文释义给我，'
                        '格式 {"pos":"","meaning_cn":""}',
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 800,
            },
            timeout=120,
        )
        ms = (time.perf_counter() - t0) * 1000
        print(f"HTTP {r.status_code}   {ms:.0f}ms")
        if r.status_code == 200:
            body = r.json()
            ch = body["choices"][0]
            print("raw:", ch["message"]["content"][:400])
            print("finish_reason:", ch.get("finish_reason"))
            try:
                print("parsed:", json.loads(ch["message"]["content"]))
            except Exception as e:  # noqa: BLE001
                print(f"JSON 解析失败: {e}")
        else:
            print(r.text[:600])
    except Exception as e:  # noqa: BLE001
        print(f"请求异常: {type(e).__name__}: {e}")


def probe_dict(word: str = "resilient") -> None:
    hr(f"[4] 词典服务（Wiktionary）word={word}   base={settings.dict_api_base}")
    from app.services import dictionary  # 延迟导入，避免污染上层

    words = [word, "Beijing", "unprecedented", "invading", "invade"]
    for w in words:
        t0 = time.perf_counter()
        try:
            got = dictionary.lookup(w, refresh=True)
            ms = (time.perf_counter() - t0) * 1000
            print(
                f"  {w:<16} source={got.get('source'):<10} "
                f"phonetic={str(got.get('phonetic')):<18} "
                f"us={str(got.get('audio_us'))[:48]:<50} {ms:.0f}ms"
            )
        except Exception as e:  # noqa: BLE001
            print(f"  {w:<16} 异常 {type(e).__name__}: {e}")
    print("\n注：变形词（-ing/-ed）在 Wiktionary 往往没有独立音标页，")
    print("    ai.py 会用模型给出的 lemma 回查一次原形（词形兜底）。")


def main() -> None:
    args = sys.argv[1:]
    model = settings.llm_model
    if "--model" in args:
        model = args[args.index("--model") + 1]
    word = "resilient"
    if "--word" in args:
        word = args[args.index("--word") + 1]

    probe_models()
    probe_dict(word)
    if "--list-models" not in args:
        probe_chat(model)
        probe_json_mode(model)
    print("\n完成。")


if __name__ == "__main__":
    main()
