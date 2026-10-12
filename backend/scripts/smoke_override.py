"""词典释义纠错验收：用户修正叠在离线词典之上（dict_overrides）。

跑法（后端需已在运行）：
    python scripts/smoke_override.py
    python scripts/smoke_override.py --base http://127.0.0.1:8011 --word lucid

验收重点：
  * 修正后查词拿到修正值，`meaning_cn_dict` 仍是词典原文，`meaning_edited` 为真；
  * 修正对生词本生效（新入册直接带修正值；已入册的行同步改写）；
  * 生词本里改释义 = 全局修正，清空 = 恢复词典原文（两边一起恢复）；
  * 词典未收录的词不能纠错（404）；带斜杠的词走 body / query，不受路径影响。

脚本会自己收尾：修正全部恢复，本脚本新入册的生词删掉。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

passed = 0
failed: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print(f"  ✓ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        failed.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))
    return cond


def head(t: str) -> None:
    print(f"\n{t}")


def main() -> None:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--word", default="lucid", help="拿来纠错的词（应是原形）")
    args = ap.parse_args()
    c = httpx.Client(base_url=args.base, timeout=30.0)
    word = args.word.lower()
    fixed = "【纠错测试】清楚易懂的"
    created_vocab: int | None = None

    def get(w: str, **qs: object) -> dict:
        r = c.get("/api/dict", params={"word": w, "enroll": 0, **qs})
        r.raise_for_status()
        return r.json()

    try:
        head("[A] 词典原文")
        base = get(word)
        if not check(base["source"] == "ecdict", "测试词在词典里", base["source"]):
            sys.exit(2)
        if base["meaning_edited"]:
            c.delete("/api/dict/override", params={"word": word})
            base = get(word)
        original = base["meaning_cn"]
        check(bool(original), "有中文释义", original[:30])
        check(base["meaning_cn_dict"] == original and not base["meaning_edited"],
              "未修正时 meaning_cn == meaning_cn_dict")

        head("[B] 修正后查词")
        r = c.put("/api/dict/override", json={"word": word, "meaning_cn": f"  {fixed}  "})
        check(r.status_code == 200, "PUT /api/dict/override", f"HTTP {r.status_code}")
        after = get(word)
        check(after["meaning_cn"] == fixed, "查词返回修正值（首尾空白已去）", after["meaning_cn"])
        check(after["meaning_cn_dict"] == original, "meaning_cn_dict 仍是词典原文")
        check(after["meaning_edited"] is True, "meaning_edited = true")
        check(c.get(f"/api/dict/{word.upper()}", params={"enroll": 0}).json()["meaning_cn"] == fixed,
              "大小写不同也命中修正")

        head("[C] 入册带上修正值")
        arts = c.get("/api/articles?limit=1").json()["items"]
        if arts:
            aid = arts[0]["id"]
            existed = c.get("/api/vocab", params={"q": word, "limit": 100}).json()["items"]
            existed = [v for v in existed if v["lemma"].lower() == word]
            e = get(word, article_id=aid, enroll=1)
            receipt = e.get("enrolled") or {}
            check(bool(receipt.get("enrolled")), "入册成功", str(receipt))
            vid = receipt.get("vocab_id")
            if receipt.get("created"):
                created_vocab = vid
            v = c.get(f"/api/vocab/{vid}").json()
            check(v["meaning_cn"] == fixed, "生词本里是修正值",
                  f"{'已有' if existed else '新'}行：{v['meaning_cn']}")
            check(v["meaning_edited"] is True, "生词本 meaning_edited = true")
            hits = c.get("/api/search", params={"q": "纠错测试"}).json()
            check(any(h["id"] == vid for h in hits["vocab"]), "检索能按修正值搜到")

            head("[D] 生词本改释义 = 全局修正，清空 = 恢复")
            r = c.patch(f"/api/vocab/{vid}", json={"meaning_cn": fixed + "2"})
            check(r.status_code == 200 and r.json()["meaning_cn"] == fixed + "2", "PATCH 改释义")
            check(get(word)["meaning_cn"] == fixed + "2", "查词同步成生词本里的修正")
            r = c.patch(f"/api/vocab/{vid}", json={"meaning_cn": ""})
            check(r.status_code == 200 and r.json()["meaning_cn"] == original,
                  "PATCH 清空 → 生词本恢复词典原文", str(r.json().get("meaning_cn"))[:30])
            check(r.json()["meaning_edited"] is False, "生词本 meaning_edited = false")
            back = get(word)
            check(back["meaning_cn"] == original and not back["meaning_edited"],
                  "查词也恢复词典原文")

            head("[E] DELETE 恢复同步到生词本")
            c.put("/api/dict/override", json={"word": word, "meaning_cn": fixed})
            r = c.delete("/api/dict/override", params={"word": word})
            check(r.status_code == 200 and r.json()["meaning_cn"] == original,
                  "DELETE /api/dict/override 返回词典原文")
            check(c.get(f"/api/vocab/{vid}").json()["meaning_cn"] == original,
                  "生词本那一行一起恢复")
        else:
            print("  （没有文章，跳过入册相关检查）")

        head("[F] 边界")
        r = c.put("/api/dict/override", json={"word": "qzxqzxqzx", "meaning_cn": "x"})
        check(r.status_code == 404, "未收录的词不能纠错", f"HTTP {r.status_code}")
        r = c.put("/api/dict/override", json={"word": word, "meaning_cn": "   "})
        check(r.status_code == 422, "空白释义被拒", f"HTTP {r.status_code}")
        slash = get("and/or")
        if slash["source"] == "ecdict":
            r = c.put("/api/dict/override", json={"word": "and/or", "meaning_cn": fixed})
            check(r.status_code == 200 and get("and/or")["meaning_cn"] == fixed, "带斜杠的词可纠错")
            r = c.delete("/api/dict/override", params={"word": "and/or"})
            check(r.status_code == 200 and not get("and/or")["meaning_edited"], "带斜杠的词可恢复")
        else:
            print("  （词典里没有 and/or，跳过斜杠检查）")
    finally:
        c.delete("/api/dict/override", params={"word": word})
        if created_vocab is not None:
            c.delete(f"/api/vocab/{created_vocab}")

    print(f"\n{'=' * 56}")
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for f in failed:
        print(f"  - {f}")
    c.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
