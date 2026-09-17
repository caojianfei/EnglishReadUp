/**
 * 音标显示口径。
 *
 * 两个来源的写法都要照顾：
 *
 * **ipa-dict（主音标，现代宽式 IPA）** —— 齿龈近音写作 `ɹ`（`/stɹeɪt/`），
 * 而中国学习者从小到大见的印刷形式是 `r`（`/streɪt/`）—— 牛津、剑桥、
 * 朗文、柯林斯这些学习词典一律写 `r`。同一个音，差别只在「给语言学家看」
 * 还是「给学习者看」。
 *
 * **ECDICT（兜底音标，老式英标）** —— 它用的是上世纪末的排版习惯：
 * 用西里尔字母 `ә`（U+04D9）冒充 `ə`、用 `:` 当长音符号而不是 `ː`、
 * 重音号写在音节前且是撇号 `'` 而不是 `ˈ`。实测确实如此
 * （`resilient` → `ri'ziliәnt`、`the` → `ðә`）。
 *
 * 只改渲染，不动数据：库里存的仍是词典原文，接口返回的也仍是原文
 * （验收脚本正是拿它和词典端点逐字比对，红线 3）。所以这一层随时可以
 * 改成恒等映射退回去，不会污染任何已落库的东西。
 */

const DISPLAY_MAP: [RegExp, string][] = [
  [/ɹ/g, 'r'], // 齿龈近音：学习词典一律写 r
  [/ә/g, 'ə'], // ECDICT 用西里尔字母 ә 冒充中央元音
  [/:/g, 'ː'], // 老式长音号 → 现代 IPA 的长音符
  [/'/g, 'ˈ'], // 老式前置重音号 → 现代 IPA 的主重音
]

export function displayPhonetic(p?: string | null): string | null {
  if (!p) return null
  return DISPLAY_MAP.reduce((s, [re, to]) => s.replace(re, to), p)
}

/** 词条 + 音标「取自哪个原形」的说明，缺一不可地一起展示。 */
export function phoneticNote(lemma?: string | null, of?: string | null): string | null {
  if (!of || !lemma) return null
  if (of.toLowerCase() === lemma.toLowerCase()) return null
  return `音标取自原形 ${of}`
}
