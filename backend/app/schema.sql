-- EnglishReadUp — SQLite DDL
--
-- 核心原则（docs/DESIGN.md §4）：原文结构（articles/blocks/images）与
-- 学习痕迹（highlights/ai_logs/vocab/grammar_points）物理分离。
-- 重抓文章只重建前一组，学习痕迹必须原样保留。
--
-- 与 DESIGN.md §4.1 的三处刻意修正：
--   1. blocks 增加 block_uid（内容指纹）与 is_stale。
--      重抓时按 block_uid 复用原有行 id，学习痕迹的 block_id 引用才不断。
--      消失的块标记 is_stale=1 而非删除（删除会 CASCADE 掉 highlights）。
--   2. blocks 不设 UNIQUE(article_id, order_index)。order_index 只是排序提示，
--      与 is_stale 共存时无法保持唯一。
--   3. ai_logs 不设 UNIQUE(input_hash)（M3 修正）。
--      需求 5 是「AI 协助过的内容全部记录」，而同一输入会被反复划过。
--      改成「input_hash 上建普通索引 + 查询时取最新一条复用结果」：
--      结果仍然缓存（不重复烧 token），但每一次调用都有独立流水。
--      与 DESIGN §4.4 的「按 input_hash 缓存」并不冲突 —— 缓存的是结果，不是日志。
--   4. 生词本拆成两表（M4）：vocab 一词一行（音标/释义跨篇复用），
--      vocab_sources 记录「在哪篇、哪句划过」。合在一起的话，
--      一个词在两篇文章里出现过就必然要在 vocab 里占两行，音标与释义要重新查一遍。
--   5. grammar_points 的唯一索引不在本文件里建，见该表下方的说明。
--   6. 复习排期（M5）不另建表，直接给 vocab 补 srs_* 列，另加 review_logs 存流水。
--      一个词只可能有一套当前排期，拆表只会多一次 JOIN；流水反过来必须独立成表。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 原文结构

CREATE TABLE IF NOT EXISTS articles (
  id              INTEGER PRIMARY KEY,
  url             TEXT NOT NULL,
  canonical_url   TEXT UNIQUE NOT NULL,
  title           TEXT,
  source_domain   TEXT,
  sitename        TEXT,
  author          TEXT,
  published_at    TEXT,
  lang            TEXT DEFAULT 'en',
  word_count      INTEGER DEFAULT 0,
  difficulty      REAL,
  cover_path      TEXT,
  raw_html_path   TEXT,
  extractor_pass  TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending|fetching|done|failed
  error_msg       TEXT,
  fetched_at      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS blocks (
  id          INTEGER PRIMARY KEY,
  article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  block_uid   TEXT NOT NULL,          -- sha1(type|text|origin_url)，重抓时用它复用 id
  order_index INTEGER NOT NULL,
  type        TEXT NOT NULL,          -- heading|para|image|quote|caption|list|table|video
                                      -- 另有 title：文章标题本身也是一块，见 ingest.title_block
  text        TEXT,                   -- 纯文本，划词偏移的坐标系（不可变）
  html        TEXT,                   -- 块内原始片段，保留粗体/斜体/链接
  level       INTEGER,                -- heading 的 h1..h6
  image_id    INTEGER REFERENCES images(id) ON DELETE SET NULL,
  is_stale    INTEGER NOT NULL DEFAULT 0,  -- 1 = 本次重抓未出现，保留供旧痕迹引用
  UNIQUE(article_id, block_uid)
);
CREATE INDEX IF NOT EXISTS idx_blocks_article ON blocks(article_id, order_index);

CREATE TABLE IF NOT EXISTS images (
  id          INTEGER PRIMARY KEY,
  article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  origin_url  TEXT NOT NULL,
  local_path  TEXT,                   -- 相对 data/ 的路径，下载失败为 NULL
  sha256      TEXT,
  ext         TEXT,
  width       INTEGER,
  height      INTEGER,
  alt         TEXT,
  caption     TEXT,
  download_status TEXT DEFAULT 'ok',  -- ok|failed
  UNIQUE(article_id, origin_url)
);
CREATE INDEX IF NOT EXISTS idx_images_article ON images(article_id);

-- ---------------------------------------------------------------- 学习痕迹

-- 划词 / 选段。创建即落库（需求 5：AI 协助过的内容全部记录，不靠用户手动收藏）。
CREATE TABLE IF NOT EXISTS highlights (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  block_id      INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  start_offset  INTEGER NOT NULL,     -- 相对 blocks.text 的字符偏移，坐标系只有这一套
  end_offset    INTEGER NOT NULL,
  kind          TEXT NOT NULL,        -- word|phrase|sentence|paragraph
  surface       TEXT NOT NULL,        -- 选中的原样文字
  context_before TEXT,
  context_after  TEXT,
  note          TEXT,
  color         TEXT DEFAULT 'yellow',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlight_block ON highlights(block_id);
CREATE INDEX IF NOT EXISTS idx_highlight_article ON highlights(article_id);

-- AI 调用流水。每调用一次写一条（含缓存命中），output_json 存结构化结果。
CREATE TABLE IF NOT EXISTS ai_logs (
  id             INTEGER PRIMARY KEY,
  article_id     INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id   INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  prompt_type    TEXT NOT NULL,       -- 现在只写 explain（划词查词典不再经模型）；dict|translate|grammar|summarize 都是历史类型，旧流水仍要能回看
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  input_hash     TEXT NOT NULL,       -- sha1(type|version|model|归一化输入)
  input_text     TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  latency_ms     INTEGER,
  source         TEXT,                -- dict_api|llm|cache
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_hash ON ai_logs(input_hash);
CREATE INDEX IF NOT EXISTS idx_ai_article ON ai_logs(article_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_highlight ON ai_logs(highlight_id);

-- 生词本。音标与释义都来自本地词典库（红线 3）—— M10 起划词不再经模型。
--
-- UNIQUE(lemma)：一个词只占一行，跨文章共用一份音标与释义（不重复查）。
-- 代价是「这个词在哪几篇、哪句里划过」放不进这一行 —— 那部分在 vocab_sources，
-- 一篇一条（M4）。「按文章筛生词」和「单篇汇总清单」都走那张表。
CREATE TABLE IF NOT EXISTS vocab (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER REFERENCES articles(id) ON DELETE SET NULL,  -- 首次入册在哪儿
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  lemma         TEXT NOT NULL,
  surface       TEXT NOT NULL,
  phonetic_uk   TEXT,
  phonetic_us   TEXT,
  phonetic_source TEXT,               -- ipa|ecdict|missing，缺失时不显示而非编造
  phonetic_of   TEXT,                 -- 音标取自哪个原形（划 invading 时是 invade）
  pos           TEXT,
  meaning_cn    TEXT,
  meaning_en    TEXT,
  -- 下两列是「只有模型才给得出」的字段（语境义 / 例句）。M10 起不再产生新值，
  -- 列保留是因为历史行还在、前端也仍在按条件渲染它们。
  meaning_in_ctx TEXT,
  example       TEXT,
  note          TEXT,                 -- 用户自己的批注（可编辑）
  status        TEXT NOT NULL DEFAULT 'new',  -- new|learning|known，M4 用户可改
  worth         INTEGER NOT NULL DEFAULT 1,   -- 模型判定值得记=1；0 只是排序靠后，不隐藏
  seen_count    INTEGER NOT NULL DEFAULT 1,   -- 被划过几次

  -- ---- 间隔复习（M5）。八个列全部由 services/srs.py 独写，别处不改。
  srs_due       TEXT,                          -- 下次到期（UTC ISO）；NULL = 还没排期
  srs_state     TEXT NOT NULL DEFAULT 'new',   -- new|learning|review
  srs_step      INTEGER NOT NULL DEFAULT 0,    -- learning 阶段走到第几步（IN_LEARN_STEPS 的下标）
  srs_interval  REAL NOT NULL DEFAULT 0,       -- 当前间隔（天）；learning 阶段为 0
  srs_ease      REAL NOT NULL DEFAULT 2.5,     -- 难度因子，1.3~3.2
  srs_reps      INTEGER NOT NULL DEFAULT 0,    -- 成功复习次数
  srs_lapses    INTEGER NOT NULL DEFAULT 0,    -- 在 review 阶段答错（again）的次数
  srs_last_at   TEXT,                          -- 上次复习时间
  srs_suspended INTEGER NOT NULL DEFAULT 0,    -- 1 = 用户主动暂停，不进队列

  created_at    TEXT NOT NULL,
  updated_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vocab_lemma ON vocab(lemma);
CREATE INDEX IF NOT EXISTS idx_vocab_article ON vocab(article_id);
-- 生词本列表默认按「值得记 → 划过次数 → id」排（services/vocab.py 的 _ORDERS），走这条。
CREATE INDEX IF NOT EXISTS idx_vocab_status ON vocab(status, worth DESC, id DESC);

-- 生词的出现记录：一个词在一篇文章里被划过一次算一条。
-- 与 vocab 分表的原因见上：vocab 存「这个词是什么」，这里存「它在哪儿出现过」。
-- 文章被删时本表跟着删（CASCADE），vocab 行本身保留（article_id 置 NULL）——
-- 词是学习成果，不该因为原文没了就丢掉。
CREATE TABLE IF NOT EXISTS vocab_sources (
  id            INTEGER PRIMARY KEY,
  vocab_id      INTEGER NOT NULL REFERENCES vocab(id) ON DELETE CASCADE,
  article_id    INTEGER REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  surface       TEXT NOT NULL,        -- 当时划到的形态（invading / invade 各算一条）
  sentence      TEXT,                 -- 当时的句子，生词表要带上
  meaning_in_ctx TEXT,                -- 当时那句中模型给的语境义（一词多义时按句不同）
  created_at    TEXT NOT NULL,
  UNIQUE(vocab_id, article_id, surface)
);
CREATE INDEX IF NOT EXISTS idx_vocab_src_vocab ON vocab_sources(vocab_id);
CREATE INDEX IF NOT EXISTS idx_vocab_src_article ON vocab_sources(article_id);

-- 复习流水（M5）。每评一次写一条，永不修改、永不删除。
--
-- 为什么不把「上次评分、上次间隔」拍进 vocab 就够了：间隔复习的价值一半在
-- 「我什么时候答错过、答错了几次」。只有当前状态的话，答错一次就看不见了，
-- 也没法回头算留存率。代价是这张表会长，但它只是文本行，不值钱。
CREATE TABLE IF NOT EXISTS review_logs (
  id            INTEGER PRIMARY KEY,
  vocab_id      INTEGER NOT NULL REFERENCES vocab(id) ON DELETE CASCADE,
  grade         TEXT NOT NULL,        -- again|hard|good|easy
  prev_state    TEXT NOT NULL,
  next_state    TEXT NOT NULL,
  prev_interval REAL,                 -- 评分前的间隔（天）
  next_interval REAL,                 -- 评分后的间隔（天）
  ease_after    REAL NOT NULL,
  was_lapse     INTEGER NOT NULL DEFAULT 0,  -- 这次算不算「忘了」（review → again）
  due_after     TEXT NOT NULL,
  elapsed_ms    INTEGER,              -- 这张卡看了多久，用来发现「秒过 = 没在读」
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_vocab ON review_logs(vocab_id, id);
CREATE INDEX IF NOT EXISTS idx_review_created ON review_logs(created_at);
-- 复习队列的取卡条件（未暂停 + 已到期），见 services/srs.py。
CREATE INDEX IF NOT EXISTS idx_vocab_due ON vocab(srs_suspended, srs_due);

CREATE TABLE IF NOT EXISTS grammar_points (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  category      TEXT,
  pattern       TEXT,
  explanation_cn TEXT NOT NULL,
  source_sentence TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grammar_article ON grammar_points(article_id);
-- 同篇同结构只留一条：解析任务被重复调用（换一处划线、命中缓存复用）
-- 时不应该把同一个语法点在清单里刷成好几行。
-- services/grammar.py 是「先按 `article_id + COALESCE(pattern,'')` 查、再插入或补空」，
-- 与这条索引的键必须是同一个表达式 —— 改一边就要改另一边，否则查到"没有"却插不进去。
CREATE UNIQUE INDEX IF NOT EXISTS idx_grammar_dedupe
  ON grammar_points(article_id, COALESCE(pattern, ''));


-- 发音音频的本地化台账（M7；M10 起一个词一行一口音）。
--
-- 主键是 (word, accent) —— **一个词 × 一个口音 = 一个可下载对象**。
-- 历史上是一词一行、只存一个口音；英美双口音进来后，如果靠加列解决就会变成
-- path_uk / status_uk / origin_uk / path_us / status_us … 十来个新列。
-- 复合主键既不用把列翻倍，也更贴近「每次下载只针对一个口音」这件事。
--
-- 音频**不预热**：点喇叭才下载（services/audio.py::ensure），所以表里大多数行
-- 是在用户第一次点发音时写进来的，而不是划词时。
-- 状态：ok 已本地化 / missing 确认没有（负缓存，永不重试）/ pending 还没成
CREATE TABLE IF NOT EXISTS audio_cache (
  word       TEXT NOT NULL,         -- 归一化后的词形
  accent     TEXT NOT NULL,         -- **请求**的口音：uk|us
  status     TEXT NOT NULL,         -- ok|missing|pending
  origin     TEXT,                  -- youdao|wiktionary
  got_accent TEXT,                  -- **实际**拿到的口音：有道拿不到时回退到词典录音，
                                    -- 可能不是请求的那个，界面要照实标（别把英音标成美音）
  path       TEXT,                  -- 本地文件名（data/audio/ 下，内容哈希命名）
  bytes      INTEGER,
  sha256     TEXT,
  attempts   INTEGER NOT NULL DEFAULT 0,
  note       TEXT,                  -- 最后一次尝试的结论，排查用
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (word, accent)
);
CREATE INDEX IF NOT EXISTS idx_audio_status ON audio_cache(status, fetched_at);

CREATE TABLE IF NOT EXISTS article_progress (
  article_id  INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  last_block_id INTEGER,
  scroll_pct  REAL,
  read_seconds INTEGER DEFAULT 0,
  updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- 全文检索

CREATE VIRTUAL TABLE IF NOT EXISTS fts_blocks USING fts5(
  text, article_id UNINDEXED, block_id UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_vocab USING fts5(
  lemma, meaning_cn, example, vocab_id UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_ai USING fts5(
  input_text, output_json, ai_log_id UNINDEXED, tokenize='porter unicode61'
);
