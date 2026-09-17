/**
 * AI 结果的统一渲染。
 *
 * 现在的任务只有两类（dict / explain），但在 UI 上共享同一套底色变量 ——
 * 阅读主题切换时结果面板要跟着变，所以颜色一律走 `--panel / --ink / --rule`，
 * 不写死色值。
 *
 * `translate` / `grammar` / 旧形状的 `explain` 是 M3–M7 的历史结果：
 * 接口已下线，但 ai_logs 里的旧流水仍要能点开回看（需求 5），
 * 所以这里保留了三段**只读**的历史渲染分支。判据是字段形状，不是版本号 ——
 * 记录里没有版本号可用，而且形状本身就是唯一的证据。
 */

import type { ReactNode } from 'react'

import { AudioButton, audioUrl } from './AudioButton'
import { displayPhonetic } from '../lib/phonetic'
import type {
  AiPromptType,
  DictResult,
  ExplainResult,
  GrammarResult,
  LegacyExplainResult,
  TranslateResult,
} from '../lib/types'

const C = {
  panel: 'var(--panel)',
  ink: 'var(--ink)',
  soft: 'var(--ink-soft)',
  faint: 'var(--ink-faint)',
  rule: 'var(--rule)',
  link: 'var(--link)',
}

const SOURCE_LABEL: Record<string, string> = {
  wiktionary: 'Wiktionary',
  missing: '缺',
  error: '取不到',
}

/**
 * 音标行：音标 + 发音。
 *
 * 两个字段的来源是分开的，别混为一谈 ——
 * 音标只认词典（红线 3）；**发音**由后端本地化后从本机吐流，
 * 优先有道美音、拿不到才回退词典录音，前端只看得到 `/api/dict/.../audio`。
 *
 * 所以「有音标」与「有发音」互不蕴含：专名常有录音却没 IPA 记录，
 * 冷僻词常有 IPA 却没人录过。这里两者独立判断，不互相设卡。
 */
export function PhoneticLine({
  phonetic,
  phoneticUk,
  phoneticUs,
  audio,
  audioAccent,
  audioOrigin,
  audioStatus,
  word,
  source,
  pending,
}: {
  phonetic?: string | null
  phoneticUk?: string | null
  phoneticUs?: string | null
  /** 后端给的本机播放地址；null/undefined 表示确认没有录音 */
  audio?: string | null
  /** us | uk | other */
  audioAccent?: string | null
  /** youdao | wiktionary */
  audioOrigin?: string | null
  /** ok|pending|missing，missing 时不画喇叭 */
  audioStatus?: string | null
  /** 兜底拼地址用：老数据没有 audio 字段时按词形重拼 */
  word?: string | null
  source?: string | null
  /** 还在查（词典端点或模型尚未返回）—— 此时说「未收录」会误导用户。 */
  pending?: boolean
}) {
  // 播放地址：新数据用后端给的地址，老缓存（没有 audio 字段）按词形重拼一个。
  // 只有 `audio === null`（后端**明确说没有**）才不给按钮 —— 拿不准就先给，
  // 点下去取不到会自己变成「音频不可用」，比藏起来让人以为坏了要好。
  const src =
    audio === undefined
      ? word
        ? audioUrl(word)
        : null
      : audio

  if (!phonetic) {
    // 这里**刻意不挂 `data-testid="phonetic-line"`**。
    // 那个 testid 的语义是「有音标」，验收脚本靠 `wait_for_selector` 等它出现 ——
    // 如果「没音标」也渲染它，等待会立刻命中一个没有 `data-phonetic` 的节点，
    // 断言直接拿到 None。M7 一开始就是这么写的，把 M3 的验收脚本判成红灯，
    // 而界面其实是对的。锚点的语义必须干净：有就是有。
    return (
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-xs" data-testid="phonetic-missing" style={{ color: C.faint }}>
          {pending ? '音标查询中…' : '词典未收录，暂无音标'}
        </span>
        {/* 音标没有不等于没录音 —— 有道的录音不依赖词典收没收这个词条 */}
        <AudioButton
          src={audio === undefined ? null : audio}
          word={word}
          accent={audioAccent}
          origin={audioOrigin}
          status={audioStatus}
        />
      </div>
    )
  }

  // 英美音标相同时只显示一条，避免并排两个一模一样的
  const same = !phoneticUs || !phoneticUk || phoneticUs === phoneticUk
  // 显示走学习词典的写法（ɹ → r），但 data-phonetic 保留词典原文 ——
  // 那是「音标与词典逐字相等」这条断言的锚点，不能被展示口径污染。
  const shown = displayPhonetic(phonetic)

  return (
    <div
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1"
      data-testid="phonetic-line"
      data-phonetic={phonetic}
      data-phonetic-shown={shown}
      data-source={source ?? ''}
    >
      {same ? (
        <span className="text-base" style={{ color: C.soft }}>
          {shown}
        </span>
      ) : (
        <>
          <span className="text-base" style={{ color: C.soft }}>
            <span className="mr-1 text-[0.7em] uppercase" style={{ color: C.faint }}>
              美
            </span>
            {displayPhonetic(phoneticUs)}
          </span>
          <span className="text-base" style={{ color: C.soft }}>
            <span className="mr-1 text-[0.7em] uppercase" style={{ color: C.faint }}>
              英
            </span>
            {displayPhonetic(phoneticUk)}
          </span>
        </>
      )}
      <AudioButton
        src={src}
        accent={audioAccent}
        origin={audioOrigin}
        status={audioStatus}
        word={word}
      />
      <span className="text-[10px]" style={{ color: C.faint }} data-testid="phonetic-source-note">
        音标来自词典（{SOURCE_LABEL[source ?? ''] ?? source ?? '—'}）
      </span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-3 last:mb-0">
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide" style={{ color: C.faint }}>
        {title}
      </h4>
      <div className="text-sm leading-relaxed" style={{ color: C.ink }}>
        {children}
      </div>
    </section>
  )
}

function Chip({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'accent' }) {
  return (
    <span
      className="inline-block rounded px-1.5 py-0.5 text-xs"
      style={{
        background: C.panel,
        border: `1px solid ${C.rule}`,
        color: tone === 'accent' ? C.link : C.soft,
      }}
    >
      {children}
    </span>
  )
}

// ---------------------------------------------------------------- 各任务视图

function DictView({ r }: { r: DictResult }) {
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-lg font-semibold" style={{ color: C.ink }}>
          {r.lemma}
        </span>
        {r.pos ? <Chip tone="accent">{r.pos}</Chip> : null}
        {r.register ? <Chip>{r.register}</Chip> : null}
      </div>

      <div className="mb-3">
        <PhoneticLine
          phonetic={r.phonetic}
          phoneticUk={r.phonetic_uk}
          phoneticUs={r.phonetic_us}
          audio={r.audio}
          audioAccent={r.audio_accent}
          audioOrigin={r.audio_origin}
          audioStatus={r.audio_status}
          word={r.phonetic_of || r.lemma}
          source={r.phonetic_source}
        />
      </div>

      {r.meaning_cn ? (
        <Section title="释义">
          <p className="font-medium">{r.meaning_cn}</p>
          {r.meaning_en ? (
            <p className="mt-0.5 text-xs" style={{ color: C.soft }}>
              {r.meaning_en}
            </p>
          ) : null}
        </Section>
      ) : null}

      {r.meaning_in_ctx ? (
        <Section title="在这句里的意思">
          <p style={{ color: C.ink }}>{r.meaning_in_ctx}</p>
        </Section>
      ) : null}

      {r.example ? (
        <Section title="例句">
          <p className="font-serif">{r.example}</p>
        </Section>
      ) : null}

      {r.collocations.length ? (
        <Section title="搭配">
          <div className="flex flex-wrap gap-1.5">
            {r.collocations.map((c, i) => (
              <Chip key={i}>{c}</Chip>
            ))}
          </div>
        </Section>
      ) : null}

      {r.note ? (
        <Section title="提示">
          <p style={{ color: C.soft }}>{r.note}</p>
        </Section>
      ) : null}
    </div>
  )
}

/**
 * 判断一条 explain 记录是不是 M8 之前的老形状。
 *
 * 用字段探测而不是看 `prompt_version`：老记录的版本号五花八门
 * （m3.2 / m4.0 / m4.1…），而 `structure` / `clauses` / `grammar_notes`
 * 这三个字段只在老形状里出现，判起来没有歧义。
 */
function isLegacyExplain(r: unknown): r is LegacyExplainResult {
  const o = (r ?? {}) as Record<string, unknown>
  return o.structure !== undefined || o.clauses !== undefined || o.grammar_notes !== undefined
}

/** 【解析】结果（M8）：译文 → 主干 → 附加成分 → 语法要点。 */
function ParseView({ r }: { r: ExplainResult }) {
  return (
    <div data-testid="parse-result">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-xs" style={{ color: C.faint }}>
          难度
        </span>
        <Chip tone="accent">{r.difficulty || '—'}</Chip>
        {r.summary_cn ? (
          <span className="text-xs" style={{ color: C.soft }}>
            {r.summary_cn}
          </span>
        ) : null}
      </div>

      {r.translation ? (
        <Section title="译文">
          <p className="font-serif">{r.translation}</p>
        </Section>
      ) : null}

      {/* 主干与附加成分是解析的重点，紧跟在译文后面 —— 读者卡住的地方在这 */}
      {r.skeleton ? (
        <Section title="主干">
          <p className="font-serif font-medium" data-testid="parse-skeleton">
            {r.skeleton}
          </p>
        </Section>
      ) : null}

      {r.parts.length ? (
        <Section title="附加成分">
          <ul className="space-y-2">
            {r.parts.map((p, i) => (
              <li
                key={i}
                style={{ borderLeft: `2px solid ${C.rule}`, paddingLeft: 8 }}
              >
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  {p.role ? <Chip tone="accent">{p.role}</Chip> : null}
                  <span className="font-serif text-sm">{p.text}</span>
                </div>
                {p.attach ? (
                  <p className="mt-0.5 text-xs" style={{ color: C.soft }}>
                    挂在 <span style={{ color: C.link, fontWeight: 500 }}>{p.attach}</span>
                  </p>
                ) : null}
                {p.explain ? (
                  <p className="mt-0.5 text-xs" style={{ color: C.soft }}>
                    {p.explain}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {r.hard_note ? (
        <Section title="难在哪">
          <p style={{ color: C.soft }}>{r.hard_note}</p>
        </Section>
      ) : null}

      {r.key_phrases.length ? (
        <Section title="短语">
          <ul className="space-y-1.5">
            {r.key_phrases.map((p, i) => (
              <li key={i}>
                <span className="font-medium font-serif">{p.phrase}</span>
                <span className="mx-1.5" style={{ color: C.faint }}>
                  —
                </span>
                <span>{p.meaning_cn}</span>
                {p.note ? (
                  <span className="block text-xs" style={{ color: C.soft }}>
                    {p.note}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {r.grammar_points.length ? (
        <Section title="语法要点">
          <ul className="space-y-3">
            {r.grammar_points.map((p, i) => (
              <li
                key={i}
                className="rounded p-2.5"
                style={{ background: C.panel, border: `1px solid ${C.rule}` }}
              >
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <Chip tone="accent">{p.category || '语法'}</Chip>
                  <span className="text-sm font-medium" style={{ color: C.ink }}>
                    {p.pattern}
                  </span>
                </div>
                <p className="text-sm leading-relaxed">{p.explain_cn}</p>
                {p.evidence ? (
                  <p
                    className="mt-1.5 font-serif text-xs"
                    style={{ color: C.soft, borderLeft: `2px solid ${C.rule}`, paddingLeft: 8 }}
                  >
                    {p.evidence}
                  </p>
                ) : null}
                {p.similar.length ? (
                  <p className="mt-1 text-xs" style={{ color: C.faint }}>
                    同类：{p.similar.join(' / ')}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  )
}

/** M3–M7 的历史解析结果：只读回看，不再产生新的。 */
function LegacyExplainView({ r }: { r: LegacyExplainResult }) {
  return (
    <div data-testid="parse-legacy">
      <p
        className="mb-3 rounded px-2 py-1 text-[11px]"
        style={{ background: C.panel, color: C.faint, border: `1px solid ${C.rule}` }}
      >
        旧版解析（当时的「精读解析」）—— 只读回看，重跑一次会得到新版【解析】。
      </p>

      <div className="mb-3 flex items-center gap-2">
        <span className="text-xs" style={{ color: C.faint }}>
          难度
        </span>
        <Chip tone="accent">{r.difficulty || '—'}</Chip>
      </div>

      {r.summary_cn ? (
        <Section title="这段在说什么">
          <p>{r.summary_cn}</p>
        </Section>
      ) : null}

      {r.translation ? (
        <Section title="译文">
          <p className="font-serif">{r.translation}</p>
        </Section>
      ) : null}

      {(r.structure ?? []).length ? (
        <Section title="句子成分">
          <ul className="space-y-1.5">
            {(r.structure ?? []).map((s, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 pt-[2px]">
                  <Chip tone="accent">{s.part}</Chip>
                </span>
                <span>
                  <span className="font-serif">{s.text}</span>
                  {s.note ? (
                    <span className="block text-xs" style={{ color: C.soft }}>
                      {s.note}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {(r.clauses ?? []).length ? (
        <Section title="从句 / 结构">
          <ul className="space-y-2">
            {(r.clauses ?? []).map((c, i) => (
              <li key={i}>
                <div className="flex items-baseline gap-2">
                  <Chip>{c.role}</Chip>
                  <span className="font-serif text-sm">{c.text}</span>
                </div>
                {c.explain ? (
                  <p className="mt-0.5 text-xs" style={{ color: C.soft }}>
                    {c.explain}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {(r.key_phrases ?? []).length ? (
        <Section title="短语">
          <ul className="space-y-1.5">
            {(r.key_phrases ?? []).map((p, i) => (
              <li key={i}>
                <span className="font-medium font-serif">{p.phrase}</span>
                <span className="mx-1.5" style={{ color: C.faint }}>
                  —
                </span>
                <span>{p.meaning_cn}</span>
                {p.note ? (
                  <span className="block text-xs" style={{ color: C.soft }}>
                    {p.note}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {(r.grammar_notes ?? []).length ? (
        <Section title="值得记的语法">
          <ul className="list-disc space-y-1 pl-4">
            {(r.grammar_notes ?? []).map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  )
}

function ExplainView({ r }: { r: ExplainResult | LegacyExplainResult }) {
  if (isLegacyExplain(r)) return <LegacyExplainView r={r} />
  return <ParseView r={r} />
}

/** 历史任务「翻译」：只读回看。 */
function TranslateView({ r }: { r: TranslateResult }) {
  return (
    <div>
      <Section title="译文">
        <p className="font-serif">{r.translation}</p>
      </Section>
      {r.notes.length ? (
        <Section title="翻译处理">
          <ul className="space-y-1.5">
            {r.notes.map((n, i) => (
              <li key={i}>
                <span className="font-medium">{n.point}</span>
                <span className="block text-xs" style={{ color: C.soft }}>
                  {n.explain}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  )
}

/** 历史任务「语法点」：只读回看（那些点已经在语法清单里）。 */
function GrammarView({ r }: { r: GrammarResult }) {
  if (!r.points.length) {
    return (
      <p className="text-sm" style={{ color: C.soft }}>
        这段没有值得单独讲的语法结构。
      </p>
    )
  }
  return (
    <ul className="space-y-3">
      {r.points.map((p, i) => (
        <li key={i} className="rounded p-2.5" style={{ background: C.panel, border: `1px solid ${C.rule}` }}>
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <Chip tone="accent">{p.category || '语法'}</Chip>
            <span className="text-sm font-medium" style={{ color: C.ink }}>
              {p.pattern}
            </span>
          </div>
          <p className="text-sm leading-relaxed">{p.explain_cn}</p>
          {p.evidence ? (
            <p className="mt-1.5 font-serif text-xs" style={{ color: C.soft, borderLeft: `2px solid ${C.rule}`, paddingLeft: 8 }}>
              {p.evidence}
            </p>
          ) : null}
          {p.similar.length ? (
            <p className="mt-1 text-xs" style={{ color: C.faint }}>
              同类：{p.similar.join(' / ')}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

export default function AiResultView({
  type,
  result,
}: {
  type: AiPromptType
  result: unknown
}) {
  if (!result) return null
  switch (type) {
    case 'dict':
      return <DictView r={result as DictResult} />
    case 'explain':
      return <ExplainView r={result as ExplainResult | LegacyExplainResult} />
    // 以下三类是历史记录（接口已下线），只在历史列表里回看
    case 'translate':
      return <TranslateView r={result as TranslateResult} />
    case 'grammar':
      return <GrammarView r={result as GrammarResult} />
    default:
      return null
  }
}

export const AI_LABEL: Record<AiPromptType, string> = {
  dict: '划词释义',
  explain: '解析',
  translate: '翻译',
  grammar: '语法点',
}
