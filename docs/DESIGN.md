# 英语阅读提升 — 系统设计文档

> 输入一篇文章 URL，程序完成抓取、本地化、结构化入库，用户在其中精读并留下可检索的学习痕迹，
> 最终按篇产出「生词 + 语法」汇总清单。

---

## 1. 目标与非目标

### 目标

| # | 能力 | 说明 |
|---|---|---|
| G1 | 文章抓取 | 输入 URL，抓正文 + 元数据 + 图片，最大化还原原文信息 |
| G2 | 完整阅读 | 本地阅读器，图片离线可见，排版可控 |
| G3 | 划词翻译 | 选中单词 → 释义 + 英美音标 + 例句 |
| G4 | 段落精读 | 选中句子/段落 → 一次【解析】：译文 + 主干/附加成分拆解 + 可复用语法要点 |
| G5 | 痕迹留档 | 所有 AI 交互永久记录，可回看、可全文检索 |
| G6 | 汇总清单 | 按篇输出生词表 + 语法点清单，可导出 |

### 非目标（明确不做）

- 不做公开分享站点，不做多用户，不做内容分发
- 不做爬虫平台（不批量抓全站，只处理用户主动提交的单篇 URL）
- 第一版不做移动端适配

---

## 2. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 运行时 | Python 3.13 | 正文提取生态最强 |
| 依赖管理 | `uv`（环境落在 `backend/.venv`） | 建环境与装依赖快；`uv run` 自动使用 `.venv`，不必 activate。**依赖清单权威仍是 `backend/requirements.txt`**，不引入 `pyproject.toml`，避免两份真相 |
| Web 框架 | FastAPI + uvicorn | 异步、自带 OpenAPI、SSE 流式简单 |
| 抓取 | `httpx`（异步） + `playwright`（客户端渲染站点 / 热链图床） | 静态优先，渲染兜底 |
| 正文提取 | `trafilatura` + `selectolax` | 通用兜底用 trafilatura，实际以容器选择器 / `__NEXT_DATA__` / 渲染 DOM 为主（见 §5.1） |
| 存储 | SQLite + FTS5 | 单文件、零运维、内置全文检索 |
| 数据校验 | Pydantic v2 | 直接用作 LLM 结构化输出的契约 |
| LLM | `openai` SDK → **DeepSeek**（模型 `deepseek-flash`） | OpenAI 兼容端点，可无痛切换厂商 |
| 词典 | **本地词典库**：ECDICT（释义/词性/考纲标签/词频，811 MB）+ ipa-dict（英美双音标，4.7 MB），只读 SQLite 与内存表 | 划词零外网、毫秒级、不花钱、不外泄。M3–M9 曾是 Wiktionary 联网音标 + LLM 释义，M10 整体本地化（§6.1） |
| LLM 客户端 | `httpx` 直连 OpenAI 兼容端点 | 不引 `openai` 包，少一层依赖 |
| 前端 | React 19 + Vite + TypeScript + Tailwind | 阅读器需要精细的 DOM 与选区控制 |
| 数据请求 | TanStack Query | 缓存与重试 |
| 流式 | SSE | **M5 已交付**（§6.6）：复用 `llm.chat_json(on_delta)`，终帧的 `result` 与一次性端点逐字相同 |

### 2.1 配置项

全部从环境变量读取，写入 `backend/.env`（不进版本库）。

```
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
ECDICT_DB=                                          # 缺省 = <DATA_DIR>/stardict.db（§6.1.1）
IPA_DICT_DIR=                                       # 缺省 = <DATA_DIR>（放 ipa_en_*.txt）
DICT_AUTODOWNLOAD=true                              # 启动自检 + 后台补齐缺失的词典数据（§6.1.1）
DICT_API_BASE=https://en.wiktionary.org/w/api.php   # **仅**录音回退用（§6.1.2）
DICT_AUDIO_BASE=https://dict.youdao.com/dictvoice   # 发音录音（M7）
AUDIO_ACCENT=uk                                     # 默认口音 uk|us（M10 双槽）
DATA_DIR=./data
SERVE_FRONTEND=true        # 由后端同进程托管前端构建产物（§7.8）
FETCH_UA=Mozilla/5.0 ... (自定义，标识用途)
FETCH_RATE_LIMIT=2.0       # 每域名最小请求间隔（秒）
FETCH_TIMEOUT=25
```

---

## 3. 目录结构

```
English/
├── docs/
│   ├── DESIGN.md                  # 本文件：设计与决策的唯一权威
│   └── USAGE.md                   # 使用手册：怎么装、怎么起、怎么用、怎么排查
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI 入口 + lifespan 初始化 + /media 静态图片
│   │   ├── config.py              # pydantic-settings，读 backend/.env
│   │   ├── db.py                  # 短连接 + WAL + FTS5 初始化 + 各里程碑迁移
│   │   ├── schema.sql             # DDL（唯一权威定义）
│   │   ├── schemas.py             # Pydantic 请求/响应契约
│   │   ├── api/
│   │   │   ├── articles.py        # 提交抓取、列表、详情、重抓、删除、进度、汇总、导出
│   │   │   ├── search.py          # M4：三组全文检索（正文/生词/AI）+ 前缀补全
│   │   │   ├── traces.py          # M3：痕迹（划词/选段）增删改查
│   │   │   ├── dictionary.py      # M3：词典音标 + 发音元信息；M7：音频字节端点
│   │   │   ├── vocab.py           # M4：生词本读改删、语法清单、导出
│   │   │   ├── review.py          # M5：复习队列、评分、暂停/恢复、统计、流水
│   │   │   └── ai.py              # M3：解析、日志查询；M5：SSE 流式端点
│   │   └── services/
│   │       ├── fetcher.py         # 抓取 → blocks（渲染 / Next.js / 容器 / trafilatura）
│   │       ├── ingest.py          # 落库 + 重抓保痕 + FTS 重建
│   │       ├── jobs.py            # 后台抓取线程池
│   │       ├── export.py          # blocks → Markdown；M4：清单 Markdown + Anki CSV
│   │       ├── dictionary.py      # M3：词典 API（音标 / 词典侧录音候选）
│   │       ├── audio.py           # M7：发音（有道优先 + 回退 + 本地化 + 节流熔断）
│   │       ├── llm.py             # M3：OpenAI 兼容客户端；M5：流式分支
│   │       ├── ai.py              # M3：编排（缓存 + 流水 + 入册）；prompt 与 PROMPT_VERSION 都在这
│   │       ├── vocab.py           # M4：生词本入册与查询（一词一行 + 出现记录）
│   │       ├── grammar.py         # M4：语法点入册与归一（M8 起来源是解析的 grammar_points）
│   │       ├── summarize.py       # M4：单篇汇总聚合（不调模型）
│   │       ├── srs.py             # M5：间隔复习调度（纯函数 + 队列 + 统计）
│   │       └── search.py          # M4：查询串清洗 + 三组检索 + 片段列选择
│   ├── data/
│   │   ├── english.db             # 库（含 english.db-wal / -shm）
│   │   ├── raw/{article_id}.html  # 原始 HTML 兜底
│   │   ├── assets/{article_id}/{sha1(url)[:16]}.{ext}
│   │   ├── audio/{sha256(bytes)[:16]}.{mp3|ogg|wav|flac|webm}   # M7：本地化发音，内容哈希命名
│   │   ├── exports/                # 导出样例
│   │   ├── m0/ m2_shots/ … m6_shots/   # 各里程碑脚本产物，与服务端数据分开
│   ├── requirements.txt
│   ├── .env.example
│   └── scripts/
│       ├── fetch_once.py          # M0 命令行：抓取 + 落 Markdown/JSON 供人工验收
│       ├── dev_check.py           # 自检：建库、打印表与路由
│       ├── dev_routes.py          # 打印路由明细（排查注册问题）
│       ├── dev_clean_tmp.py       # 清理 data/ 下的中间产物
│       ├── smoke_api.py           # M1/M2：ASGI 传输端到端冒烟
│       ├── smoke_ai.py            # M3：AI 编排端到端（含四条红线）
│       ├── smoke_m4.py            # M4：生词本 / 清单 / 导出 / 三组检索
│       ├── smoke_m5.py            # M5：调度算法 / 队列 / 评分 / SSE
│       ├── smoke_m7.py            # M7：发音源 / 回退 / 本地化 / 节流熔断
│       ├── smoke_http.py          # 真实 uvicorn 进程端到端
│       ├── smoke_ui.py            # M2 浏览器验收
│       ├── smoke_ui_m3.py         # M3 浏览器验收（划词 → 高亮区间重建 + 喇叭）
│       ├── smoke_ui_m4.py         # M4 浏览器验收（三页 + 深链回跳 + 音标显示层）
│       ├── smoke_ui_m5.py         # M5 浏览器验收（复习卡 + 流式增量）
│       ├── diag_*.py              # 浮层/词卡/深链等问题的一次性诊断
│       └── probe_*.py             # 站点诊断：正文在哪 / 图片在哪 / 能否直连
└── frontend/                       # React 19 + Vite 6 + Tailwind 4（M2 已建）
    ├── package.json                # 依赖用 npm 管理，原因见 §11
    ├── vite.config.ts              # /api 与 /media 反代到 127.0.0.1:8010（开发同源）
    ├── dist/                       # npm run build 产物，由后端同进程直发（§7.8）；不入版本库
    └── src/
        ├── main.tsx                # QueryClient + BrowserRouter
        ├── App.tsx                 # 主题根节点 + 路由 + 偏好持久化
        ├── index.css               # 三套主题变量 + 阅读排版 + 控件样式 + 定位闪烁
        ├── pages/
        │   ├── Home.tsx            # URL 提交、抓取轮询、文章库
        │   ├── Reader.tsx          # 正文渲染 + 进度采集/还原 + 图片放大 + 深链定位
        │   ├── Vocab.tsx           # M4：生词本（筛选/编辑/导出）
        │   ├── Review.tsx          # M5：间隔复习（卡片 + 四档评分 + 键盘操作）
        │   ├── Summary.tsx         # M4：单篇清单（生词表 + 语法 + 划了没解析 + 导出）
        │   └── Search.tsx          # M4：三组检索，点开跳回原文
        ├── components/
        │   ├── AppNav.tsx          # M4/M5：顶部导航（生词总数 + 待复习张数徽标）
        │   ├── AudioButton.tsx     # M7：发音按钮（只认本机地址，不消费外链）
        │   ├── BlockRenderer.tsx   # blocks → DOM；caption 并进 figure；data-focus 闪烁
        │   ├── SelectionPopover.tsx# M3：划词浮层（词卡 / 三个动作按钮）
        │   ├── AiPanel.tsx         # M3：右侧解析面板 + 历史列表；M5：流式增量区
        │   └── Toolbar.tsx         # 主题/字号/行距/栏宽/图片开关/进度条
        └── lib/
            ├── api.ts              # 后端契约封装 + localStorage 离线兜底
            ├── types.ts            # 与 backend/app/schemas.py 一一对应
            ├── prefs.ts            # 阅读偏好（只落 localStorage）
            ├── phonetic.ts         # M4：音标显示层归一（ɹ → r，只改渲染）
            ├── stream.ts           # M5：SSE 帧解析（fetch + ReadableStream，不用 EventSource）
            ├── selection.ts        # M3：选区读偏移 / 高亮切分（M4 改为按边界切段）
            └── useReadingProgress.ts  # 阅读位置与时长采集、上报
```

M0–M7 全部完成。§11 的待定项里，P4 的「间隔复习」与「SSE 流式」已在 M5 落地，
「发音换源 + 本地化」在 M7 落地；剩下的都是新功能或优化（难度算法、浏览器插件、
Anki .apkg、本地词库等）。

---

## 4. 数据模型

核心原则：**原文结构（articles / blocks / images）与学习痕迹（highlights / ai_logs / vocab / grammar_points）物理分离。**
好处是重新抓取或改版渲染不会丢学习记录，删除标注也不影响原文。

### 4.1 DDL

```sql
-- 文章
CREATE TABLE articles (
  id              INTEGER PRIMARY KEY,
  url             TEXT NOT NULL,
  canonical_url   TEXT UNIQUE NOT NULL,
  title           TEXT,
  source_domain   TEXT,
  author          TEXT,
  published_at    TEXT,
  lang            TEXT DEFAULT 'en',
  word_count      INTEGER,
  difficulty      REAL,              -- 难度评分，可后置计算
  cover_path      TEXT,
  raw_html_path   TEXT,
  status          TEXT NOT NULL,     -- pending|fetching|done|failed
  error_msg       TEXT,
  fetched_at      TEXT,
  created_at      TEXT NOT NULL
);

-- 正文块（渲染与锚定的最小单元）
CREATE TABLE blocks (
  id          INTEGER PRIMARY KEY,
  article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  block_uid   TEXT NOT NULL,         -- 内容指纹，重抓时用它复用 id（见 §4.3）
  order_index INTEGER NOT NULL,
  type        TEXT NOT NULL,         -- heading|para|image|quote|caption|list|table|video
                                     -- 另有 title（M9）：文章标题本身也是一块，order_index=0
  text        TEXT,                  -- 纯文本，划词偏移的坐标系
  html        TEXT,                  -- 块内原始片段，用于保留粗体/斜体/链接
  level       INTEGER,               -- heading 的 h1..h6
  image_id    INTEGER REFERENCES images(id) ON DELETE SET NULL,
  is_stale    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(article_id, block_uid)
);

-- 图片资产
CREATE TABLE images (
  id          INTEGER PRIMARY KEY,
  article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  origin_url  TEXT NOT NULL,
  local_path  TEXT,                  -- 相对 data/ 的路径，下载失败为 NULL
  sha256      TEXT,
  ext         TEXT,
  width       INTEGER,
  height      INTEGER,
  alt         TEXT,
  caption     TEXT,
  download_status TEXT DEFAULT 'ok',  -- ok|failed
  UNIQUE(article_id, origin_url)      -- 重抓时按 origin_url 复用 id
);

-- 学习痕迹：锚点
CREATE TABLE highlights (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  block_id      INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  start_offset  INTEGER NOT NULL,     -- 相对 blocks.text 的字符偏移
  end_offset    INTEGER NOT NULL,
  kind          TEXT NOT NULL,        -- word|phrase|sentence|paragraph
  surface       TEXT NOT NULL,        -- 实际选中的文本
  context_before TEXT,                -- 前后各 60 字符，检索与重建上下文用
  context_after  TEXT,
  note          TEXT,                 -- 用户手动批注
  color         TEXT DEFAULT 'yellow',
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_highlight_block ON highlights(block_id);
CREATE INDEX idx_highlight_article ON highlights(article_id);

-- AI 交互原始留档（需求 4 的落点）
CREATE TABLE ai_logs (
  id             INTEGER PRIMARY KEY,
  article_id     INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id   INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  prompt_type    TEXT NOT NULL,       -- 现在只有 dict|explain；translate|grammar|summarize 是 M3–M7 的历史类型，旧流水仍要能回看
  prompt_version TEXT NOT NULL,       -- 改 prompt 后据此选择性重算
  model          TEXT NOT NULL,
  input_hash     TEXT NOT NULL,       -- sha1(prompt_type|version|model|input)
  input_text     TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  latency_ms     INTEGER,
  source         TEXT,                -- llm|cache（本次结果怎么来的）
  created_at     TEXT NOT NULL,
  UNIQUE(input_hash)
);

-- 生词本：一词一行，音标与释义跨文章复用
CREATE TABLE vocab (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER REFERENCES articles(id) ON DELETE SET NULL,  -- 首次入册那篇
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  lemma         TEXT NOT NULL,        -- 原形（入册的唯一键）
  surface       TEXT NOT NULL,        -- 首次划到的形态
  phonetic_uk   TEXT,
  phonetic_us   TEXT,
  phonetic_source TEXT,               -- wiktionary|missing，缺失时前端不显示而非编造
  phonetic_of   TEXT,                 -- 音标取自哪个原形（sitting → sit）
  pos           TEXT,
  meaning_cn    TEXT,
  meaning_en    TEXT,
  meaning_in_ctx TEXT,                -- 语境义
  example       TEXT,
  note          TEXT,                 -- 用户自己的批注
  status        TEXT NOT NULL DEFAULT 'new',   -- new|learning|known（用户主观标记）
  worth         INTEGER NOT NULL DEFAULT 1,    -- 1 值得记 / 0 不必记（只影响排序与筛选）
  seen_count    INTEGER NOT NULL DEFAULT 1,
  -- ↓ M5 间隔复习：算法进度，服务端独写，跟 vocab.status 故意并行（见 §11）
  srs_due       TEXT,                 -- 到期时间（ISO8601，本地时区），NULL = 暂不排期
  srs_state     TEXT NOT NULL DEFAULT 'new',   -- new|learning|review
  srs_step      INTEGER NOT NULL DEFAULT 0,    -- learning 阶段走到第几步（0/1）
  srs_interval  REAL NOT NULL DEFAULT 0,       -- 当前间隔（天；learning 阶段为 0）
  srs_ease      REAL NOT NULL DEFAULT 2.5,     -- 难度因子，边界 1.3 ~ 3.2
  srs_reps      INTEGER NOT NULL DEFAULT 0,    -- **成功**复习次数（毕业 +1，again 不加）
  srs_lapses    INTEGER NOT NULL DEFAULT 0,    -- 从 review 掉回 learning 的次数
  srs_last_at   TEXT,                 -- 上次评分时间
  srs_suspended INTEGER NOT NULL DEFAULT 0,    -- 1 = 暂停（不进出题队列，不影响生词本）
  created_at    TEXT NOT NULL,
  updated_at    TEXT
);
CREATE UNIQUE INDEX idx_vocab_lemma ON vocab(lemma);
CREATE INDEX idx_vocab_article ON vocab(article_id);
-- idx_vocab_due 同理不在这里建：它依赖 srs_suspended/srs_due 两列，
-- 老库要先 ALTER TABLE 补完列，才能建索引（由 db._migrate_m5() 负责）
-- idx_vocab_status / idx_grammar_dedupe 刻意**不在这里建**（详见下面两处说明），
-- 交给 db.py 的 _migrate_m4()：老库缺列、或有重复行时，直接 CREATE 会让
-- init_db 整体崩掉，服务起不来

-- 出现记录：一篇一词一条。这才是「按文章筛生词」和「单篇清单」的依据 ——
-- vocab.article_id 只是「第一次入册时在哪篇」，不能拿来筛
CREATE TABLE vocab_sources (
  id            INTEGER PRIMARY KEY,
  vocab_id      INTEGER NOT NULL REFERENCES vocab(id) ON DELETE CASCADE,
  article_id    INTEGER REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  surface       TEXT NOT NULL,
  sentence      TEXT,                 -- 当时划到的那一句
  meaning_in_ctx TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(vocab_id, article_id, surface)
);

-- 复习流水（M5）：每次点档位写一条，只增不改。这是「复习效果」的唯一凭据，
-- 生词本进度是从这里算出来的（srs_* 是缓存），删卡片不删流水
CREATE TABLE review_logs (
  id            INTEGER PRIMARY KEY,
  vocab_id      INTEGER NOT NULL REFERENCES vocab(id) ON DELETE CASCADE,
  grade         TEXT NOT NULL,        -- again|hard|good|easy
  prev_state    TEXT, next_state    TEXT,
  prev_interval REAL, next_interval REAL,
  ease_after    REAL,
  was_lapse     INTEGER NOT NULL DEFAULT 0,  -- 这次是不是从 review 掉回 learning
  due_after     TEXT,                 -- 评完之后推到什么时候
  elapsed_ms    INTEGER,              -- 从出示卡片到点档位的耗时
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_review_logs_vocab ON review_logs(vocab_id, created_at);
CREATE INDEX idx_review_logs_time ON review_logs(created_at);

-- 发音音频的本地化台账（M7；M10 起一词一行一口音）。
--
-- 主键是 (word, accent) —— **一个词 × 一个口音 = 一个可下载对象**。
-- 历史是一词一行、只存一个口音；英美双口音进来后，若靠加列解决就会变成
-- path_uk / status_uk / origin_uk / path_us / status_us … 十来个新列。
-- 复合主键既不用把列翻倍，也更贴近「每次下载只针对一个口音」这件事。
--
-- 音频**不预热**：点喇叭才下载（services/audio.py::ensure），所以表里大多数行
-- 是在用户第一次点发音时写进来的，而不是划词时。
-- 状态：ok 已本地化 / missing 确认没有（负缓存，永不重试）/ pending 还没成
CREATE TABLE audio_cache (
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
CREATE INDEX idx_audio_status ON audio_cache(status, fetched_at);

-- 语法点
CREATE TABLE grammar_points (
  id            INTEGER PRIMARY KEY,
  article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  highlight_id  INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
  category      TEXT,                 -- 从句|非谓语|倒装|虚拟语气|时态|搭配|其他
  pattern       TEXT,                 -- 结构模板
  explanation_cn TEXT NOT NULL,
  source_sentence TEXT,
  created_at    TEXT NOT NULL
);
-- 同篇同结构只留一条。**不在 schema.sql 里建**：老库可能有重复行，
-- 直接 CREATE UNIQUE INDEX 会让 init_db 整体崩掉，交给 db._migrate_m4()
-- 「先去重，再建索引」
-- CREATE UNIQUE INDEX idx_grammar_dedupe ON grammar_points(article_id, COALESCE(pattern, ''));

-- 阅读进度
CREATE TABLE article_progress (
  article_id  INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  last_block_id INTEGER,
  scroll_pct  REAL,
  read_seconds INTEGER DEFAULT 0,
  updated_at  TEXT NOT NULL
);

-- 全文检索
CREATE VIRTUAL TABLE fts_blocks USING fts5(
  text, article_id UNINDEXED, block_id UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE fts_vocab USING fts5(
  lemma, meaning_cn, example, vocab_id UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE fts_ai USING fts5(
  input_text, output_json, ai_log_id UNINDEXED, tokenize='porter unicode61'
);
```

> 完整 DDL 的唯一权威定义在 `backend/app/schema.sql`，本节为同步摘录。
> 修改表结构时改 schema.sql，并回填本节的差异说明。

### 4.2 为什么偏移量锚在 blocks.text 上

- `blocks.text` 是**纯文本、不含标记**，字符偏移稳定可复现
- 渲染时按该块的 highlights 区间排序后切分成 `[text, <mark>, text, <mark>]` 序列重建 DOM
- 计算新选区偏移时，把 Range 内所有文本节点长度累加，加上 Range 起点在首节点内的偏移
- 好处：高亮是**可推导的**，而不是存在 HTML 里的，样式改了也不丢

### 4.3 重抓为什么不伤学习痕迹（M1 已实现）

`highlights.block_id` 指向 `blocks.id`，而重抓会重建 blocks。
如果按「先全删再全插」实现，`ON DELETE CASCADE` 会把所有学习痕迹一起删掉 ——
这正违反红线 1。机制如下：

```
block_uid = sha1(类型 | 内容指纹 | 同内容出现序号)
  ├─ 文本块：(type, 归一化后的 text)  +  第 n 次出现
  └─ 图片块：(image, origin_url 的 path)  +  第 n 次出现

重抓时
  ├─ uid 命中旧行 → UPDATE 内容与顺序，**id 保持不变**（痕迹仍指向同一段原文）
  ├─ uid 是新块   → INSERT
  └─ 旧 uid 未出现 → is_stale=1、order_index=-1，**不删除**
```

- 出现序号是必需的：同一篇文章里两段完全相同的文字（或同一张图出现两次）
  会算出同一个指纹，加序号才能保证 `UNIQUE(article_id, block_uid)` 成立。
- 渲染层只取 `is_stale=0` 的块；stale 块留在库里，供旧标注继续解析上下文。
- `images` 同理按 `origin_url` 复用 id；不再被引用的图片行才物理删除
  （先把 `blocks.image_id` 置 NULL 再删，避免外键悬空）。
- 因此 `blocks` **不能**有 `UNIQUE(article_id, order_index)`：
  stale 块的 `order_index` 统一为 -1，唯一约束会直接冲突。
- **标题块（M9）走同一条路**：`type='title'`、`order_index=0`、`text=articles.title`，
  重抓命中 uid 就复用原行 id。所以「标题上的痕迹跨重抓保留」不是额外机制，
  而是它本来就在这套坐标系里（见 §7.9）。

M1 已用冒烟测试验证：重抓同一篇后 22/22 个 block id 全部复用。

---

## 5. 抓取与解析流程

```
URL
 ├─ 1. 规范化 canonical_url（去 utm 参数、统一末尾斜杠）→ 查重
 ├─ 2. httpx GET（带 Referer、真实 UA、每域名 1 req/s 限速）
 ├─ 3. 原始 HTML 落盘 data/raw/{id}.html；同时解析 __NEXT_DATA__（拿图集与图注）
 ├─ 4. 正文提取（按优先级，详见 §5.1 结论一）
 │     ├─ a. 站点在 RENDER_DOMAINS / --render → Playwright 渲染后取正文容器
 │     ├─ b. 有 __NEXT_DATA__ → 取 detailData.data.content
 │     ├─ c. 命中 per-domain 正文容器 → 直接拆容器 DOM（不再过 trafilatura）
 │     └─ d. trafilatura 通用提取（精度优先 → 召回降级）
 ├─ 5. 按文档顺序把正文拆成 blocks
 │     ├─ h1..h6            → heading
 │     ├─ p                 → para（空段落保留为图片占位锚）
 │     ├─ blockquote        → quote
 │     ├─ figure/figcaption → image + caption
 │     ├─ img / graphic     → image（裸标签，漏了会静默丢图）
 │     ├─ video             → video（只记 URL）
 │     └─ ul/ol / table     → list / table
 ├─ 6. 装饰图过滤（分享图标 / logo / 二维码 / 埋点像素）→ 丢弃并计数
 ├─ 7. 图片本地化
 │     ├─ 候选 URL 优先级：srcset 最大档 > data-src > data-original > src
 │     ├─ 下载 → sha256 → data/assets/{article_id}/{hash}.{ext}
 │     ├─ 直连 4xx（热链图床）→ 用浏览器会话拦截响应补齐
 │     └─ 仍失败 → download_status='failed'，前端显示占位
 ├─ 8. 补图注（__NEXT_DATA__.textImageList[].desc，按图片 URL 匹配）
 ├─ 9. 写入 articles / blocks / images，建 FTS 索引
 └─ 10. 用户可选：立即调 LLM 生成全文概览
```

**关键实现细节**

| 问题 | 对策 |
|---|---|
| 懒加载图片 | 依次尝试 `srcset` / `data-src` / `data-original` / `data-lazy-src`；渲染路径滚动触发 |
| 图片防盗链 | 下载与前端渲染都补 `Referer: {站点域名}` |
| 相对路径 | `urljoin(article_url, src)` 统一转绝对 |
| 站点私有正文容器 | per-domain CSS selector 配置（`CONTENT_SELECTORS`），命中后直接拆 DOM |
| 客户端渲染站点 | `RENDER_DOMAINS` 配置 + `--render` 强制，走 Playwright |
| 热链图床 403 | 拦截浏览器自身的图片响应取 body |
| 抓取幂等 | `canonical_url` 唯一索引；重复提交返回已有文章 |
| 图片重复 | 跨文章按 `sha256` 复用同一文件 |
| 重抓残留 | 按当前 blocks 引用清理资产目录里的孤立文件 |

### 5.1 M0 实测结论（2026-09-14）

环境：Python 3.13 + httpx 0.28.1 + trafilatura 2.2.0 + selectolax 0.4.11 + playwright 1.62.0。
样本：Sixth Tone ×4、China Daily ×2。脚本 `backend/scripts/fetch_once.py`，产物在 `backend/data/m0/`。

#### 结论一：提取路径必须有优先级，trafilatura 只做兜底

实测下来，通用提取（trafilatura）在这两个站点都**不是最优解**，只能当最后一道兜底。
最终确定的优先级：

| 优先级 | 路径 | 适用 | 实测代价 |
|---|---|---|---|
| 1 | **Playwright 渲染** | 客户端渲染正文/图片的站点（Sixth Tone） | ~41 s / 篇 |
| 2 | **`__NEXT_DATA__`** | Next.js 站点，静态可取结构化数据 | < 1 s |
| 3 | **站点正文容器**（per-domain 选择器） | 新闻门户站（China Daily `#Content`） | < 1 s |
| 4 | trafilatura 通用提取 | 以上都不适用时的兜底 | < 1 s |

最终验收（第二轮，带正文图的文章）：

| 文章 | 提取路径 | 正文块 | 图片 | 图注 | 字数 |
|---|---|---|---|---|---|
| Sixth Tone 1018994 | Playwright | 45 段 + 15 图 | **15/15** | 14 | 2230 |
| China Daily WS6aa35187 | `#Content` | 16 段 + 3 图 | **3/3** | 3 | 821 |

#### 结论二：容器命中后**不能**再过 trafilatura

这条是踩出来的。China Daily 的 `#Content` 里明明有 3 个 `<figure class="image">`
（`<img>` + `<figcaption>` 俱全），但把容器 HTML 交给 trafilatura 后，
输出长度从 5954 掉到 4510，**3 张图 + 3 条图注全部消失**，
即使显式传了 `include_images=True`。

> 容器选择器一旦命中，容器内容就是正文，直接做 DOM 拆分。
> trafilatura 的启发式在这一步只会帮倒忙。

#### 结论三：Sixth Tone 的图文位置**只能**从渲染后的 DOM 得到

这是决定架构的一条。该站的正文数据分三块存放：

| 数据 | 位置 | 内容 |
|---|---|---|
| 正文 | `__NEXT_DATA__.props.pageProps.detailData.data.content` | 61 个 `<p>`，**0 个 `<img>`，无占位符** |
| 图集 | 同级的 `textImageList[]` | 14 张图，含 `url` / `width` / `height` / **`desc`（完整图注）** |
| 封面 | `headPic` / `bigPic` | 1 张 |

进正文 DOM 后只有 12 个 `<img>`（10 个社交图标 + 封面 + logo），**14 张正文图一张都没有**。

**曾假设「`content` 里的空 `<p>` 是图片占位符」—— 用 Playwright 渲染后证伪了：**
实测 14 张图的实际锚点落在第 4、4、9、14、20、20、22、26、30、38、42×4 个非空段之后，
与 SSR 里那 16 个空 `<p>`（第 5、8、12、18、25…）**完全不对应**，且图是成组出现的。

> 所以 `__NEXT_DATA__` 只能提供**内容**，提供不了**位置**。
> 图文位置是客户端渲染时算出来的，必须走 Playwright。

**图注的最佳来源是 `__NEXT_DATA__` 的 `desc`**，不是渲染后的 DOM
（DOM 里的 `alt` 只有 `"introduction"` 这种无语义值）。
做法：渲染路径拿位置，`__NEXT_DATA__` 按图片 URL 匹配补图注 —— 各取所长。

#### 结论四：直连 403 的热链图床，用「拦截浏览器自己的响应」破解

`img2.chinadaily.com.cn` 的三种尝试结果：

| 方式 | 结果 |
|---|---|
| Python 直连（5 种 Referer/UA 组合） | 全部 **403**（1674 字节拦截页） |
| 浏览器内 `fetch(img)` | **Failed to fetch** —— 跨域且无 CORS 头，JS 读不到像素 |
| 页面 `<img>` 加载 | **成功**（1198×799） |
| `context.request.get()` | **403** —— 不共享图片加载上下文 |

> 唯一可行解：`page.on("response")` 拦截浏览器自己发起的图片响应，取出 body。
> 已实现为 `fetch_images_via_browser()`，直连失败的图自动重试。
> 实测 China Daily 的 3 张图 100% 补齐。

#### 其它必须处理的经验

- **裸 `<img>` 要单独处理。** Sixth Tone 的正文首图是 `<body>` 直接子节点，
  只处理 `<p>` / `<figure>` 会**静默丢图**（不报错，`images=0/0`）。
- **装饰图黑名单必需。** 分享图标 / logo / 二维码 / 埋点像素要在入库前丢掉。
  China Daily 的装饰图集中在 `/image_e/` 路径，可直接按路径过滤。
- **per-domain 容器是必需项。** China Daily 通用提取会产出 31 块、21 张图，
  其中全是分享按钮图标和侧栏「相关阅读」缩略图（19th Chinese Bridge、Heilongjiang
  rice harvest…）。配 `#Content` 后收敛为干净正文。
- 正文页常重复一次标题（`<h3>` 与 `<title>` 相同），需去重。
- 元数据的站点名可能是脏值（Sixth Tone 返回 `#Sixthtone`），需清洗并兜底为域名。
- 重抓会让旧图片变成孤立文件，需按当前 blocks 引用做一次清理。
- `language` 六个样本全部未命中（无可用的 `<html lang>`），M1 需补 langdetect 或默认 `en`。
- Sixth Tone 内嵌视频（`video1.sixthtone.com/...`）已作为 `type='video'` 的 block 落库，
  M0 只记录 URL 不下载。

#### 后续待观察

- ~~渲染路径 41 s / 篇，M1 入库必须做成**异步任务 + 进度轮询**，不能同步阻塞请求。~~
  → M1 已实现：线程池 + `articles.status` 轮询（见 §5.2）。
- ~~渲染路径的正文小标题（如 `Life on the move`）未识别为 heading。~~
  → M1 已解决：实测 Sixth Tone 的小标题写作 `<p><strong>Life on the move</strong></p>`,
  **既不是 `<h*>`，class 也是空的**，唯一线索是「整段就是一个加粗片段」。
  已按此规则识别（`fetcher._is_subhead_para`，含 100 字符长度上限防止误判强调句），
  辅以 `class` 特征规则；Sixth Tone 那篇的 heading 数从 0 变为 4。

### 5.2 服务化实现（M1）

抓取逻辑从 M0 脚本原样提升为 `app/services/fetcher.py`，
**脚本与服务共用同一份实现**：`scripts/fetch_once.py` 退化为命令行包装，
只多了「落 Markdown/JSON 供人工验收」这一步，避免两份代码各自漂移。

```
POST /api/articles {url}
  └─ ingest.get_or_create_article()   按 canonical_url 查重
       ├─ 已存在且 done  → 直接返回（幂等，重复提交不重复抓）
       └─ 新建 / failed  → jobs.submit_fetch() → 立即返回 status=pending|fetching
            └─ [后台线程] fetcher.fetch_article()
                 ├─ 原始 HTML → data/raw/{article_id}.html
                 ├─ 图片       → data/assets/{article_id}/*.{ext}
                 └─ ingest.save_fetch()  一个事务内完成：
                      blocks/images upsert → stale 标记 → FTS 重建 → articles 更新
                         └─ status: done | failed（错误摘要写 error_msg）
```

| 决策 | 做法 | 原因 |
|---|---|---|
| 异步模型 | `ThreadPoolExecutor`（默认 2 线程），不是 asyncio | Playwright 的 sync API 不能在事件循环线程里跑 |
| 同文去重 | 进程内 `_inflight` 集合，重复提交返回 409 | 防同一篇被抓两遍、浪费 40 s |
| 前端感知进度 | 轮询 `GET /api/articles/{id}` 的 `status` | 比 SSE 简单，抓取只有三个状态 |
| 图片服务 | `GET /media/{article_id}/{filename}` | 图片已本地化，**不再需要 Referer**；文件名含内容哈希，可长期缓存 |
| 错误处理 | 抓取异常一律落 `status=failed` + `error_msg`，不抛给请求线程 | 后台线程的异常没人接，必须自己兜住 |
| SQLite 并发 | 每操作一个短连接 + WAL + `busy_timeout=8s` | 请求线程与抓取线程都要写库，不共享连接对象 |

**幂等与失败重试**：同一 URL 重复 `POST` 返回同一条记录；只有 `status` 为
`failed` 的才会因再次提交而重新入队，避免「手滑点两次多抓一遍」。
`POST /api/articles/{id}/refetch` 是显式重抓入口。

---

## 6. AI 编排

> **M8 的架构变更：选段只剩一个动作 —— 【解析】。**
> M3–M7 期间选段有三个并列任务（`translate` / `explain` / `grammar`），
> 现在合并成一个：一次产出「译文 + 主干/附加成分 + 可复用语法要点」。
> 理由与取舍见 §6.7。

### 6.1 分流策略

| 粒度 | 音标 | 释义 / 解析 |
|---|---|---|
| 单词 | **本地词典库**（ipa-dict 英美双轨，ECDICT 兜底） | **本地词典库**（ECDICT 中文释义 + 英文释义 + 词性） |
| 短语 / 句子 / 段落 | — | LLM（`deepseek-flash`）结构化输出：译文 + 结构拆解 + 语法要点 |

**M10 的实质变更：单词不再经过模型。** M3–M9 里划词是「词典给音标 + LLM 给释义」，
现在整条链路都是本地文件读取。三条理由：

1. **LLM 不适合做词典。** 查词要的是稳定、可复现、逐字可信的客观事实；
   模型的释义会随 prompt 与采样漂移，还得靠缓存去「钉住」它。这是拿长处在换短处。
2. **快得不讲道理。** 本地查询 0.0x 毫秒（见下），而原来是并行两个请求、
   释义要等 1~3 秒。划词从「等一下」变成「瞬间」。
3. **不花钱、不联网、不外泄。** 划词的词不再发给任何第三方，离线也能查。

代价必须写清楚：词卡**少了**「语境义、例句、常见搭配、易错提示」——那些只有模型能给。
所以这是一次**明确的降级**，换来的是速度、成本、离线与隐私。需要语境理解时，
选中整句点【解析】仍然走模型（§6.7）。

#### 6.1.1 本地词典的数据源与查询

| 文件 | 来源 / 许可 | 体积 | 提供什么 |
|---|---|---|---|
| `stardict.db` | ECDICT 官方预编译 SQLite（MIT, © 2025 Linwei） | 约 **811 MB** | 中文释义（99%）、英文释义（5%）、词性、考纲标签、词频 |
| `ipa_en_us.txt` | ipa-dict `en_US`（源自 cmudict，MIT） | 3.1 MB | 美音（GA） |
| `ipa_en_uk.txt` | ipa-dict `en_UK`（源自 ipacards，**GPL 3.0**） | 1.6 MB | 英音（RP） |

三个文件都不入版本库（`.gitignore` 已排除 `backend/data/*`）；仅本地个人使用，
不随仓库分发。

**安装由服务自己负责**：`lifespan` 里调一次 `dictdata.ensure_async()` —— 缺哪个下哪个，
已有的不重下。三条理由：

1. `data/` 是可再生的，但这三个文件是**外部数据**，不会自己回来。以前的唯一入口是
   `scripts/fetch_dicts.py`，纯手工、还要等 200 MB：换机器 / 重装 / 手滑清空之后，
   用户只会看到「本地词典库没有安装」和一句「去跑脚本」。
2. **下载必须在后台线程里**，不能堵启动：压缩包 207 MiB、解压后 811 MB，阻塞几分钟
   意味着 uvicorn 一直不监听 —— 比「暂时查不到词」糟糕得多。后台补还让文章、检索、
   生词本这些不依赖词典的功能立刻可用。
3. 落盘用「`.part` → 原子改名」+ 解压目录放在**目标同卷**（跨卷 rename 会失败）。
   原子改名不是洁癖：服务运行中也在下载，而查词是无锁读同一个路径的 ——
   读者要么看到「文件不存在」，要么看到完整成品，绝不会读到写了一半的 SQLite。

状态挂在 `/api/health` 的 `dict.bootstrap`（`state` / `missing` / 当前数据集与百分比），
并在词卡的 `setup` 字段里给一份精简版：**查词撞上「库没装」时显示「正在下载 42%」，
而不是让用户去跑脚本** —— 服务这边已经在下了。`scripts/fetch_dicts.py` 保留为手工入口
（多了 `--verify` 体检与 `--force` 重下），但下载实现只有一份，就在 `services/dictdata.py`。

**为什么音标不直接用 ECDICT**：它只有**一条**音标，且是上世纪末的英式写法
（用西里尔字母 `ә` 冒充 `ə`、用 `:` 当长音符号、用 `'` 当重音号），实测只覆盖
10% 的词条。ipa-dict 是现代宽式 IPA、英美双轨、**且收屈折形式**
（`ran` / `runs` / `running` 各自独立成条），实测对常见词覆盖
88%~96%（柯林斯星级词 英 88% / 美 92%；牛津核心词 英 90% / 美 96%）。
所以：**ipa-dict 为主，ECDICT 那条降为兜底**，它的老式写法只在前端显示层归一化
（见 §7.1.1）。

**查询必须走等值查询**（实测数据，`scripts/fetch_dicts.py --verify` 会打印索引构成）：

| 写法 | 耗时 | 计划 |
|---|---|---|
| `WHERE word = ?` | 0.0 ms | 走 `stardict_2` 索引 |
| `WHERE word = ? COLLATE NOCASE` | 0.0 ms | 同上（**列本身就是 NOCASE**） |
| `WHERE sw = ?` | 0.2 ms | 走 `stardict_3(sw, word)` |
| `WHERE lower(word) = ?` | **458.7 ms** | **全表扫描**，绝不能用 |

官方这份库的 `word` 列声明成 NOCASE，所以 `=` 天生大小写不敏感
（`beijing` 能查到 `Beijing`）——`lower()` / `LIKE` 一点好处都没有，
却会让每次划词从 0 毫秒变成半秒。这条是**踩过才写下来**的。

**为什么不做缓存表**：以前有 `dict_cache` 缓存 Wiktionary 的解析结果，
那是网络请求、值得缓存。现在是本地文件读取，再套一层缓存只是给自己加一个
失效维度（`parser_ver`、TTL），所以整张表连同读写代码一起去掉了。

**`worth`（值不值得记）改由本地规则推导**（原先由模型判）。按信号强度从上往下：

1. 命中 `zk`/`gk`/`cet4` → 0（中考、高考、四级：读到这个阶段的人早会了）
2. 命中 `cet6`/`ky`/`toefl`/`ielts`/`gre` → 1（大纲词就是要背的）
3. 牛津三千 或 柯林斯 ≥ 4 星 → 0（学习者词典的核心高频词）
4. 词频排名在 1~2000 之间 → 0
5. 其余 → 1（沿用「判不准就填 1」的立场）

两条实测教训：**第 1 条必须排在第 2 条前面**（`make` 的标签是 `zk gk ielts`，
先看考试标签会把它判成"值得记"）；**`frq` 可能是 0**，那是「没排上名次」的占位值
（`'hood(0)`、`-ability(0)`），不是最高频，所以判据写成 `1 <= frq <= 2000`。

另外必须知道的覆盖率现实：这份 1.0.28 库里带考纲标签的只有 **1.5 万词**
（340 万的 0.4%）、有词性列的只有 **1%**、有英文释义的只有 **5%**。
所以词性还加了第二条来源：中文释义常以 `n. / adj. / vt.` 开头，
缺 `pos` 列时从那里取；两条都没有就留空（前端本来就把整条释义显示出来，
标记仍然看得见，没必要编一个）。

**降级路径要分清三种**，提示文案完全不同（`WordEntryOut.source`）：

| 情况 | `source` | 用户看到 |
|---|---|---|
| 词典库文件不在 | `unavailable` | 「正在下载 ECDICT 词库 42%」（`setup` 字段；服务启动时已在后台补，见上）；下载失败/被关掉时才提示去跑 `fetch_dicts.py` |
| 库在、没这个词 | `missing` | 「词典未收录这个词」（数字、拼写错、极生僻） |
| 查到 | `ecdict` | 正常词卡 |
#### 6.1.2 发音音频（M3 起，M7 换源，M10 起英美双槽 + 点击才下）

发音一开始和音标同源：Wiktionary 词条 `===Pronunciation===` 下的 `{{audio|en|…}}`，
文件托管在 Wikimedia Commons。**这个功能 M3 就写了，但一直基本不生效** ——
M6 调研、M7 落地，期间查出两件事 —— 前一件在 M6 修掉，后一件在 M7 落地，都值得记下来。

**第一件：解析器把录音丢了。** `{{audio}}` 的文件名有两代命名：

| 代 | 例子 | 口音写在哪 |
|---|---|---|
| 老（Commons 自己录的） | `en-us-resilient.ogg` | **文件名前缀** |
| 新（Lingua Libre 批量录的） | `LL-Q1860 (eng)-Vealhurl-documentary.wav` | **具名参数** `a=Southern England` |

旧实现只看文件名里有没有 `en-us-` / `en-uk-`，判不出来就 `continue` 掉 ——
于是大量**本来有录音**的词（用户实际踩到的 `documentary`）在界面上完全没有喇叭。
更隐蔽的是：`_params()` 本来就故意丢掉具名参数（IPA 模板里的 `a=hyperforeign`
是噪声），所以 `a=Southern England` 连读都没读到。

- 单独读一份具名参数（`_named_params`），`a=` / `accent=` / `pron=` 都认；
- 口音标签**先查音系标签**（`RP` / `GA`），再按**地名词表**匹配
  （`Southern England` → 英、`Texas` / `New Jersey` → 美）；
- **分不出口音的进第三槽 `audio`**（澳音、爱音、印音…）。这是关键取舍：
  分不出口音 ≠ 没有录音，硬塞进 `audio_uk` / `audio_us` 是在数据层撒谎，
  直接丢掉又回到原 bug。
- 文件名里的空格与括号必须 `quote()` 转义 —— `Special:FilePath/LL-Q1860 (eng)-…`
  带裸括号会取不到文件。

**第二件：Wiktionary 的录音本身不够用。** 实测美音覆盖约 8/10、**英音仅 2/10**，
而且格式是 `.ogg`（Vorbis）—— **Safari / iOS 放不了**。所以 M7 把录音**换源到有道**
（`services/audio.py`）：

```
https://dict.youdao.com/dictvoice?audio=<word>&type=2      # type=2 = 美音
```

| 选择 | 理由 |
|---|---|
| **有道优先、词典回退** | 有道覆盖率明显更高（实测连 `flibbertigibbet` 都有），且直接给 mp3、无需 key；Wiktionary 那张录音降为回退，不做删除（见下） |
| **英美双槽**（M10） | 同一个端点，`type=1` 英音、`type=2` 美音。音标既然是英美并排，发音就不能只有一边 —— 词卡上每个音标后面跟着**它自己的**喇叭 |
| **回退候选按请求口音排序** | 要英音就先排英音（`uk → 分不出口音 → us`），要美音反之。分不出口音那档垫在中间：它是真实录音，只是判不出英美，排在最后那侧是明确不匹配的音 |
| **音标与录音分家** | 红线 3：音标是这个词的客观事实，录音是某个人读的。两者互不蕴含 —— 专名常有录音却没音标（`Thailand` 有美音无英音），冷僻词有音标却没人录过。所以**音标缺了哪一边，喇叭照样给** |
| **音频一律本地化** | 浏览器永远只连本机 `GET /api/dict/{word}/audio?accent=uk|us`。顺带解决了 ogg/Safari 与离线两个问题 |

**判定「有没有」要分清两件事**（这是最容易写错的地方）：

| 有道的回答 | 性质 | 处理 |
|---|---|---|
| `200` + 真 mp3 | 有 | 落盘 |
| `500` / `404` | **确定的否定** | 换回退源；全都否定才落 `missing`（负缓存，永不重试） |
| 超时 / 连接失败 / HTML 错误页 | 这次没成 | 落 `pending`，以后还能试 |

混为一谈的后果很严重：把一次网络抖动当成「这词没录音」写进负缓存，
**这个词就永远哑了**，而且日志上完全看不出来。

**下载要隐蔽。** 有道 `dictvoice` 是非官方接口，批量拉会触发风控。做法：

- **严格串行**：全局一把闸门（`audio._Gate`），同一时刻只有一个外向请求。
  并发是风控的第一特征。
- **间隔 + 抖动**：默认 3 秒起步、再叠 0～1.5 秒随机。固定节拍（每 2.000 秒一次）
  本身就是机器特征。
- **退避重试**：2s → 4s → 8s，单次下载最多 2 次尝试，不硬刚。
- **熔断**：连续失败 5 次就静默 10 分钟，期间一律走回退源。
  被风控盯上时最忌讳「继续重试证明自己是脚本」。
- **只下用户真正点过的词**：不做全量预热、不扫生词本批量补。M7 时是「划词即排后台
  下载任务」，M10 改成**点喇叭才下**（见下），流量形状更接近人。
- **查词路径零写入、零外网**：`audio.slots()` 只做两条主键查询，返回两个槽位的
  状态（没登记过就是 `pending`，喇叭照画）。下载只发生在播放端点里。
- 文件名按**内容 sha256** 命名（`data/audio/<16位>.mp3`），换源重下或两词共用
  同一份录音都不会产生重复文件。

**为什么砍掉预热**（M7 → M10 的改动，值得记一笔）：M7 的做法是划词时排后台任务，
好处是「点下去多半已经有文件」。但双口音一来，预热等于把闸门流量**直接翻倍**，
而有道是非官方接口、最怕流量形状不像人 —— 为了「一个可能没人点的喇叭」去多下一份，
是拿风控风险换几秒等待。改成点击式之后：后台一份都不下，代价只是首次点等几秒。

**台账与文件不一致时以文件为准**：`audio_cache` 说 `ok` 但文件不在（手工清了
`data/audio/`），接口把这一槽降级成 `pending`，下次点重新下 —— 宁可多下一次，
也不要吐一个必然 404 的地址出去。
### 6.2 缓存

`ai_logs.input_hash = sha1(prompt_type | prompt_version | model | 归一化输入 | 上下文)`

命中即秒回，本次 `source='cache'`、`tokens=0`。改 prompt 只需升 `PROMPT_VERSION`，
旧记录自然不命中。

两处 M3 修正：

- **`ai_logs` 去掉了 `UNIQUE(input_hash)`**。需求 5 要「AI 协助过的内容全部记录」，
  而同一段文字会被反复划过；带唯一约束的话第二次写不进流水，痕迹就断了。
  现在是「普通索引 + 查询取最新一条复用结果」：结果依然缓存（不重复烧 token），
  但每次调用都有独立记录。老库由 `db.py::_migrate_ai_logs` 自动重建表。
- **同一个划词重复点开不新增流水**（否则点十次刷十条），只把 `source` 报为 `cache`；
  换了划词但内容相同的，复用结果并补一条 `source='cache'` 的流水。
- **改了结果内容就必须升 `PROMPT_VERSION`，哪怕 prompt 一个字没改**。
  M3 就栽过一次：给词卡加「词形兜底」（音标改从 lemma 回查）时只动了清洗逻辑，
  没升版本号，于是新逻辑被旧缓存整个盖掉 —— 界面继续显示「词典未收录」，
  而代码其实已经写对了。这类 bug 特别难查，因为「代码正确 + 缓存陈旧」
  在日志里看不出任何异常。现在的版本号是 `m3.2`。

**`ai_logs` 写入要能扛住痕迹被删**：`_guard` 检查通过后、`_save_log` 写入前，
用户完全可能把那条痕迹删掉。`ai_logs.highlight_id` 的 `ON DELETE SET NULL`
只作用于已存在的行，拦不住 INSERT 去引用一个刚消失的 id，会直接抛
`IntegrityError` 把整次 AI 结果弄丢。所以写日志前先确认痕迹还在，不在就把
关联置空 —— 需求 5 要的是「有记录」，链接失效不该连记录一起丢。

**记录可以删（误操作清理）**。「划词即翻译」意味着误操作无法避免：手一抖划中
一个词就会调一次模型、落一条流水。`DELETE /api/ai/logs/{id}` 是**物理删除**，
不做软删除、不做回收站 —— 用户要的是「这条不该存在」，而不是「这条藏起来」。

三个连带效应，都是刻意的：

- **缓存一起消失**（`_find_cached` 读的就是这张表）：同一段文字再划会重新调模型、
  重新落一条流水。「删了就是删了」比「删了还留着缓存」好解释；那本来就是一次
  真实调用的产出，删除是用户的明确意愿。
- **`fts_ai` 成对删**：它没有触发器，索引由 `_save_log` 手写，只删主表会在 FTS 里
  留下搜不到也管不着的孤儿行（`services/ai.delete_log` 在同一事务里删两处）。
- **不牵连其他痕迹**：高亮、生词本条目都不动 —— 删的是「这一次 AI 调用」，
  不是那处标注。与 `DELETE /api/highlights/{id}`（删标注、留流水）是两条独立路径。

需求 5「AI 协助过的内容全部记录」约束的是**系统**不得悄悄丢记录，
不是禁止用户清理自己不要的记录，两者不冲突。

### 6.3 输出契约

两类任务，各一套 JSON（prompt 里的返回格式定义 + `services/ai.py` 的 `CLEANERS`
收敛成固定形状）：

| 任务 | 关键字段 |
|---|---|
| `dict` | lemma / pos / meaning_cn / meaning_in_ctx / example / collocations / register |
| `explain` | translation / summary_cn / **skeleton** / **parts[]**（text / role / **attach** / explain）/ key_phrases[] / grammar_points[]（pattern / category / explain_cn / evidence / similar）/ difficulty / hard_note |

**`skeleton` + `parts` 是「主干 → 挂靠」结构，不是语法类别分栏**（M8）。
`attach` 这个字段是硬要求：每条附加成分都必须说清挂在哪个成分上 ——
读者卡住的地方通常是「这坨东西挂在谁身上」，而不是「它叫什么从句」。

**`grammar_points` 与 `parts` 的去向不同**：前者是「换个句子也用得上」的结构条目，
自动进语法清单（§6.5）；后者是逐句拆解，只留在解析结果与 ai_logs 里。

> 历史形状（M3–M7）：`translate` 的 translation / notes[]、`explain` 的
> summary_cn / structure[] / clauses[] / grammar_notes[]、`grammar` 的 points[]。
> 任务已下线，但旧流水要能回看（需求 5），前端按字段形状识别并走只读渲染（§6.7）。

**dict 任务里模型根本拿不到音标字段**：`_clean_dict_result` 用词典的结果
**覆盖** phonetic 系列字段。即使模型自作主张在 JSON 里塞一个 `phonetic`，
也进不了最终结果 —— 红线 3 是在代码层保证的，不靠 prompt 的自觉。

**「能解析」不等于「有内容」**。`extract_json` 只保证拿到的是一段 JSON，
不保证里面有东西。M3 冷启动实测撞到过一次：`deepseek-flash` 回了一个
`{"lemma": "straight"}` —— 合法、能解析，于是被当成成功结果写进 `ai_logs`
并进了缓存，用户看到的是一张**只有词头、没有释义的空卡片**，而且因为命中
缓存，重划多少次都是同一张。

现在每个任务都要说清「什么才算有内容」（`ai.py::_SUBSTANCE`）：

| 任务 | 至少要有一个非空 |
|---|---|
| `dict` | `meaning_cn` / `meaning_en` |
| `explain` | `translation`（结构拆解允许为空：划到的可能只是一个短语，本来就没有主干可拆） |

校验器通过 `chat_json(..., validate=...)` 传进**重试循环内部** —— 放在调用方
判就只能「失败即报错」，白白浪费重试机会。另外缓存命中路径也要过同一关
（`_usable`）：历史里躺着的空结果不许算命中，否则这条记录永远自愈不了。

### 6.4 M3 实测：`max_tokens` 是这类模型最容易踩的坑

`deepseek-flash` 会**先输出 reasoning token，再输出正文**。第一版按「答案大概多长」
来设 `max_tokens`（词卡 900、解析 2200），后果是 JSON 在写到 `"example"` 中途
被截断、解析失败，接口直接 502。

修法是两层（`services/llm.py`）：

1. **检测 `finish_reason == "length"` 就把预算翻倍重试**（上限 8000）。
   原样重试会再次截断 —— 病根是预算，不是运气。
2. 同时收紧 prompt 的**硬性长度限制**（整个 JSON 900 字以内，M3 时是 700 ——
   合并后多了一节 grammar_points，上限相应放宽），因为输出越长越慢。

实测耗时（同一段 216 字选段，**M3 时测的，当时三者尚未合并**）：

| 任务 | 首次 | 备注 |
|---|---|---|
| `dict` | 1.1s | 命中缓存 0.3s |
| `translate` | 2.6s | M8 已下线（并入解析） |
| `grammar` | 7.9s | 输出 1570 tokens；M8 已下线（并入解析） |
| `explain` | 10~17s | 输出最多；M8 起它是**唯一**的选段任务 |

合并后的【解析】比旧 explain 多一节 `grammar_points`，`max_tokens` 从 4000
提到 4600；量级与旧 explain 相当（合并后未逐条重新计时）。等待在 UI 上不是
单纯转圈：`AiPanel` 按已耗时切换文案（生成译文 → 拆解主干与附加成分 → 提炼语法要点）
并显示秒数，让等待可解释。

### 6.5 汇总清单生成

单篇汇总 = **纯聚合**该文章的 `vocab_sources` + `grammar_points`，**不再补 LLM 调用**：

```
生词  ← vocab_sources(该篇) JOIN vocab
语法  ← grammar_points WHERE article_id = ?
另附  ← 划过线但 NOT EXISTS 对应 ai_logs 的痕迹（「划了没解析」）
```

原设计里那句「再补一次 LLM 调用做归类」在 M4 实现时被去掉了，理由：

- 清单里的每一行本来就是模型在前面的每一次划词/选段时产出的。再跑一次
  等于让模型复述一遍自己的旧结论，既要花钱又可能改口（同一句话两次解析
  不一致，用户会怀疑哪个是真的）。
- 语法归类可以在**入册时**做：`grammar.persist_points()` 走
  `normalize_category()` 把自由文本（「倒装 / 时态 / 从句」）归一到既定语域。
  （M8 起入册的触发点从独立的 `grammar` 任务改成解析结果里的 `grammar_points`
  小节，落的还是同一张表、同一条路径。）
- 「去重」也有了物理保证：`idx_grammar_dedupe ON (article_id, COALESCE(pattern,''))`。

输出 Markdown（`export.render_summary_markdown`）：

```markdown
# {title}
> {source} · {published_at} · 共 N 词 M 语法点

## 生词表
| 单词 | 音标 | 词性 | 释义 | 文中例句 |
|---|---|---|---|---|
| preserve | /pɹəˈzɜːv/ | v. | 保存；保留（此处：把…保存下来） | Chen preserved the memory… |

## 语法清单
| 结构 | 类别 | 解析 | 原文例句 |
|---|---|---|---|
```

释义一栏是 **本义 + 语境义**（`export._meaning`）：只放语境义会得到一句像译文的
说明（「她将莫高窟的记忆保存下来，传给后代。」），脱离文章就没法用。

导出格式：`Markdown` / `Anki CSV`（`front,back,tags` 列）。

**Anki CSV 的契约**（`export.ANKI_HEADER`）：
首行第一个字符必须是 `#separator:Comma` —— **不能写 BOM**，多一个 `\ufeff`
Anki 就认不出这条指令，整份文件会被当成普通笔记导进去。首行之后依次是
`#html:true`、`#columns:front,back,tags`，**不能再写表头行**。
`front` 只放词形：音标放正面等于直接把答案给出去。

### 6.6 SSE 流式输出（M5）

`explain` 实测 10~17 s。M3 用「阶段文案 + 秒数」扛着，M5 补上真流式：
`POST /api/ai/stream` 边生成边吐字，用户看到的是自己那句话在长出来，而不是
一个转圈的圈。**流式只是把同一份结果换个方式送出来**，不是第二套编排 ——
这一点决定了下面的每一个取舍。

**帧协议**（`api/ai.py::_frame`）：

```
event: open    data: {"kind":"explain","prompt_version":"m8.0",...}   -- 一次，先声明要干什么
event: delta   data: {"t":"片段"}                                     -- 增量文字
event: reset   data: {}                                              -- 丢弃已吐的（重试/纠正时）
event: done    data: {"result":{...},"source":"llm|cache","...":...}   -- 终值与一次性端点同构
event: error   data: {"detail":"..."}                                -- 失败原因
```

`data:` **恒为一行**。`_frame()` 用 `json.dumps(ensure_ascii=False)` 把片段里的
换行转义成 `\n`，所以任何字符串（含模型吐出的多行代码）都装得进一行 ——
SSE 协议按行切帧，换行漏出去就会把一帧劈成两帧。

**目录里没有 `services/stream.py`。** 流式没有自己的编排层，它是
`llm.chat_json()` 多了一个 `on_delta` 回调而已：

```
api/ai.py  ──► ai.run(on_delta, on_retry) ──► llm.chat_json(on_delta)
                     │                              │
                     └──── 清洗 / 校验 / 落库 / 缓存 / 入册 ──── 全部复用原路径
```

于是「流式端点的结果」与「一次性端点的结果」逐字相同（验收脚本对此有断言），
缓存也共用一份：先流式问一次，再点一次性，第二次命中缓存。

**线程 + 队列，不是 async。** `ai.run` 是同步的（httpx 同步客户端 + Playwright
sync 的限制，见 §11 并发模型），在 async generator 里直接 `await` 它会阻塞整个
事件循环，一个用户的流式请求会把所有人的请求都卡住。做法：

```
run_in_executor(同步 worker) ──push──► queue.Queue ──await/get──► async generator ──► 响应
```

worker 在 `ai.run` 里跑，每次 `on_delta` 把片段塞进队列；async generator 阻塞在
`queue.get()`（`to_thread` 包一层，不占事件循环），拿到就吐一帧。
另有 `_HEARTBEAT_S = 12.0` 的心跳注释帧 `: ping`：`explain` 首字可能等十几秒，
中间没有字节的话，反向代理会先把连接掐了。

**客户端断开不取消调用。** `asyncio.CancelledError` 时**不停** worker —— 需求 5
要的是「AI 协助过的内容全部记录」，用户关掉页面不能成为这条记录丢掉的借口。
worker 会把结果照常清洗、落库、入册，只是没人接那些 delta 了。

**`reasoning_content` 只报心跳，不进正文。** DeepSeek 的思考过程通过
`delta.reasoning_content` 流出来（不是 `content`）。它**不进** `on_delta` 的正文，
只作为「模型还在想」的信号报给前端（`on_delta("")` + 前端的 `[data-testid="ai-thinking"]`）。
理由：思考过程是散文，正文是 JSON —— 混着吐会让前端的增量框里一半是能读的、
一半是没法读的，JSON 解析也别指望了。

### 6.7 三操作合并为【解析】

M3 起选段有三个并列任务：`translate`（翻译）、`explain`（精读解析）、`grammar`（语法点）。
M8 合并成一个 —— 界面上的【解析】，内部仍是 `explain` 这一个 `prompt_type`。

**为什么合并**：

| 理由 | 说明 |
|---|---|
| 三者的边界是假的 | 译文与结构拆解是一件事的两面：「这句什么意思」和「这句为什么这么读」在长难句上分不开。分成三个按钮，用户得先猜「我这次想要哪种」，而答案通常是「都要」 |
| 旧拆解的组织方式不对 | 旧 explain 按语法类别分栏（`structure[]` / `clauses[]` / `key_phrases[]`），读起来像语法书条目。读者卡住的地方不是「不知道它叫定语从句」，而是「找不着主干、不知道这坨东西挂在谁身上」。新结构是「主干（skeleton）→ 附加成分（parts，每条带 attach）」 |
| 清单的边界要留住 | `grammar_points` 是「换个句子也用得上」的结构条目，继续自动进语法清单（§6.5）；`parts` 是逐句拆解，只留在解析结果里。合并的是**入口**，不是把两类产出混成一格 |
| 成本档位 | 合并后一次解析比原来的 translate 贵（10~20 s / 数千 token vs 1.5 s / 200 token）。补偿是流式 + 产出顺序：**译文先吐**，只想看意思的人等前几秒就够 —— 所以不必为「只想翻译」再留第二个按钮 |

**落地的取舍**（每条都有对应的验收锚点）：

- `prompt_type` 仍叫 `explain`（不是 `parse`）：`ai_logs` 里的历史记录用的就是它，
  改名会让新旧流水失去同一个检索键。用户看到的叫【解析】，内部叫什么都行。
- `/api/ai/translate`、`/api/ai/grammar` **删除**，不做兼容层：留着等于承认
  「解析」不是唯一入口，前端迟早有人点回旧路。`smoke_ai.py` 有断言钉住两者返回 404。
- 旧记录**原样保留**（需求 5）：前端按**字段形状**判断新旧（老形状有 `structure` /
  `clauses` / `grammar_notes`），旧的解析走只读渲染，旧的 translate / grammar
  流水也仍能从历史列表点开回看；对旧记录点「重试」等于用新版解析重跑一次。
- `PROMPT_VERSION` 升到 `m8.0`：产出整体变了，旧缓存必须失效（红线 4）。
- 长度上限 700 → 900 字（多了一节 grammar_points），`max_tokens` 4000 → 4600。

---

## 7. 前端设计

### 7.1 页面

| 页面 | 内容 |
|---|---|
| Home | URL 输入框、抓取任务状态、已收集文章列表（封面 + 字数 + 未读标注数） |
| Reader | 左正文 / 右 AI 侧栏；顶部工具栏（字号、行距、主题、显示/隐藏已有标注、进度） |
| Vocab | 生词本：搜词形/释义、按状态与「只看重点」筛、按文章筛、四种排序；改状态/改释义/切重点/移除/跳原文；导出 Anki CSV 与 Markdown |
| Summary | 单篇文章的生词表 + 语法清单；三个导出按钮 |
| Search | 三组结果（正文 / 生词本 / AI 记录）一次给全，每条都能跳回原文位置并闪一下 |
| Review | 间隔复习：卡片正面词形 + 音标 + 词性，翻面看释义与文中例句，四档评分；清空态显示下次到期 |
| AppNav | 顶部导航（文章库 / 生词本 / 复习 / 检索），生词本与复习各带徽标 |

### 7.1.1 M4 的页面与交互（已实现）

**生词本**（`pages/Vocab.tsx`）
- 收录不是点出来的：划词时后端自动入册（`services/ai._post_persist` → `vocab.enroll`）。
  所以这里**没有**「加入生词本」按钮 —— 需求 5 的立场是不漏，靠手动收藏必然漏。
- 筛选维度对应三种真实用法：按状态（复习时滤掉已掌握）、只看重点（词攒多了先啃
  `worth=1`）、按文章（刚精读完过一遍）。
- 「标为重点」只改排序与筛选，不会删词：模型判错时用户还能自己找回来。
- 从检索结果点词形带 `?q=` 过来，直接落到那个词上。
- **同一个词的不同词形可能各占一行**（`treasure` 与 `treasured`）。`lemma` 由模型
  判定，而 `treasured` 作形容词在词典里本来就是独立条目（`excited` / `invading`
  同理），所以**不强制词形还原** —— 强行归并会把「现在分词作定语」这类真正的
  独立词条揉进原形里。重复了让用户用「移除」合并即可。

**单篇清单**（`pages/Summary.tsx`）
- 只有生词表与语法清单两栏。**没有「划了线但还没解析」**：划一段句子不落痕
  （§7.2），点过【解析】才有痕迹与解析记录 —— 清单里的每一条都对应一次真实
  发生过的解析，「待解析」不是一种状态。

**检索**（`pages/Search.tsx`）
- 片段里只允许 `<mark>` 一种标签（`safeSnippet()`）：片段内容是抓来的网页正文和
  模型输出，直接 `dangerouslySetInnerHTML` 等于把第三方内容当 HTML 执行。
  做法是先整段转义、再把转义后的 `&lt;mark&gt;` 还原成真标签。

**跳回原文的深链**（`?block=&start=&end=`）
- 优先级**高于**「回到上次位置」：用户点的是「带我去看这里」，被 `last_block_id`
  劫走会很恼火。
- 定位痕迹优先复用**区间完全一致**的真实痕迹（可点、可编辑）；找不到才合成一条
  `id = FOCUS_ID (-1)` 的：不进库、不可点（`BlockRenderer` 见到负 id 不挂 onClick）、
  刷新即消失。两者都靠 `data-focus='1'` 触发 `@keyframes er-flash` 闪两下。
- `splitByHighlights` 的归属规则改过两次：旧实现是游标式扫描，嵌套区间会被
  更长的痕迹整段吃掉 —— 搜一个词、那个词正好在已划线的句子里时，定位标记一个字
  都渲染不出来（M4 实测踩到）。改成「按所有边界点切段 + 优先级归属」之后，
  优先级排序又反过一次：**从「起点早者优先」改成「区间短者优先」**。原因是
  单词与句/短语现在有两档底色（§7.4），嵌套时内层才是更具体的那条 ——
  旧规则会让整条长句把里面的词整段吃掉，用户就分不清「哪个词查过译」了。
  现在的顺序是「负数 id（深链定位标记）→ 区间短者 → 等长时起点早者」，
  无重叠场景的结果与从前逐字相同（§11 已定项）。

**音标显示层归一**（`lib/phonetic.ts`）
- ipa-dict 的齿龈近音写作 `/stɹeɪt/`，学习词典（牛津/剑桥/朗文/柯林斯）一律写
  `/streɪt/`。`displayPhonetic()` 只把 `ɹ` 替换成 `r`，**只改渲染**：
  词典库原文、接口返回、生词本存的仍是原样（红线 3 的比对锚点就是它）。
- ECDICT 那条兜底音标还要多三条：它是上世纪末的排版习惯 —— 用西里尔字母 `ә`
  冒充 `ə`、用 `:` 当长音符号、用 `'` 当重音号。同样只在显示层整理。
  这一层随时可以改成恒等映射退回去，不污染任何已落库的东西。

### 7.1.2 M5 的页面与交互（已实现）

**复习页**（`pages/Review.tsx`）

- **队列不回拉，是「本次会话快照」。** 装载时取一次 `/api/review/queue`，评完一张
  就从本地列表移除。若每评一张都回拉队列，服务端会按新的 `due` 重排 —— 用户刚
  评完的卡可能因为别的卡到期又跳回来，卡片顺序在手里跳来跳去。
- **正面不泄露答案。** 正面只有词形（含变形词原形）、音标、词性；释义、语境义、
  例句在翻面之后。理由和 Anki CSV 的 `front` 只放词形是同一条（§6.5）。
- **「提示」按钮挖词给例句**（`hintMode`）：显示文中例句，但用 `﹏﹏` 把目标词
  挖掉（`maskWord()`）。想不起来时看语境能想起来，看整句答案就背不出来了。
- **四档评分带下次间隔**。每个按钮上直接印出「评了之后什么时候再见」
  （`soonestDue` / `preview()` 算的），用户点之前就知道代价 —— 这是 `hard`/`easy`
  能被人认真用的前提。
- **清空态的「下次到期」不能只信服务端。** `queue.next_due` 是**装载时**算的，
  那时卡片还没被排到未来；队列快照又不回拉，于是评完最后一张时它显示「—」。
  修法：前端另存 `soonestDue`，从每张卡评分响应的 `review.due` 取最小值，与
  `next_due` 取较早者。
- **例句出处要单独查。** `vocab.article_id` 是「首次入册那篇」，而卡片上的例句是
  按「优先当前筛选的那篇、否则最早那篇」挑的 —— 两者会分叉。`vocab._sentence_sources()`
  用同一套排序批量查出真正的出处（一次查询，不逐行子查询）。
- **键盘操作**：`1/2/3/4` 对应 again/hard/good/easy，`空格` 翻面。复习是重度重复
  动作，鼠标移动距离就是效率。
- **导航徽标**：复习 tab 吃 `/api/review/stats.due_now`，为 0 时不显示 —— 一个
  永远亮着的 `0` 只会让人学会忽略徽标。

**流式增量**（`lib/stream.ts` + `components/AiPanel.tsx`）

- **不用 `EventSource`。** 它只能发 GET，带不了请求体，而 `/api/ai/stream` 的入参
  （任务类型、选区、上下文）是一段 JSON。改用 `fetch` + `ReadableStream` 手动切帧。
- **增量按 `requestAnimationFrame` 节流。** 一次 `explain` 能吐 150~400 个 chunk；
  逐个 `setState` 会触发几百次重渲染，页面在流式过程中反而变卡。做法是把 chunk
  攒进 ref，rAF 回调里一次性落地。
- **未收到 `done`/`error` 就报「连接中断」。** 流正常结束必须有终帧；只看到
  `delta` 然后流关了（网关超时、进程挂了），说明结果不完整，不能当成功。

### 7.1.3 M7 的发音交互（已实现）

**`components/AudioButton.tsx`（新）** —— 发音按钮抽成独立组件，因为现在有四个
消费方（划词浮层、AI 词卡、生词本行、复习卡正面），而「播放中 / 不可用」两个状态
在哪儿都要一致。

- **它不消费外链。** 只接受 `word`（自己拼 `/api/dict/{word}/audio`）或后端算好的
  `src`。音频一律由后端本地化，前端拿不到也不需要知道有道。
- **`status === 'missing'` 时不画按钮。** 那是后端明确说的终态，画出来只会骗用户
  点出一句「音频不可用」。`pending` 和未知照画 —— 后端会当场去取。
- `data-url` / `data-accent` / `data-origin` 三个属性是**给验收脚本用的锚点**：
  「界面有没有出现外链」这条断言只能靠 `data-url` 来判。

**四个位置都放喇叭**

| 位置 | 说明 |
|---|---|
| 划词浮层（`SelectionPopover`） | **每个音标后面跟着它自己的喇叭**（M10）；点哪个才下哪个，首次点等几秒 |
| AI 词卡（`AiResultView.PhoneticLine`） | 只用于回看历史记录；老记录里没有 `audio` 字段时按 `word` 重拼 |
| 生词本行（`Vocab.tsx`） | 英、美各一个，一眼扫过去就能听 |
| **复习卡正面**（`Review.tsx`） | 复习是最需要听音的场景，之前恰恰没有 |

复习卡把喇叭放在**正面**是有意的：卡正面本来就摆着音标，听一遍并不泄露释义，
而「想不起来怎么读」正是要练的东西之一。

**`data-accent` 用原值（`us`/`uk`），不是展示文案。** 展示层（`🔊 美` / `🔊 英`）
是另一回事，混在一起会让断言依赖界面措辞，改一次文案就挂一批测试。
两个喇叭比一个喇叭更依赖这个锚点：`[data-testid=play-audio]` 现在会命中两个元素。

### 7.2 交互流（M3 已实现）

```
mouseup（绑在 <article> 上）
  └─ readSelection() 算 {blockId, start, end, surface, kind}
      │   偏移先用 DOM 精确算，再用 blocks.text 切片校验；
      │   对不上就退化成「在正文里搜这段文字」（只接受唯一匹配）
      │   跨块的选区直接拒绝 —— 精读的划词/选句都在块内
      │   落在词内部的片段由 snapToWholeWord 吸附到整个词（划词只认整词）
      │
      ├─ kind == 'word'
      │     ├─ POST /api/articles/{id}/highlights     ← 单词在这一刻就落库（需求 5）
      │     │     └─ 高亮立刻由本地 state 渲染，不等列表接口回来
      │     └─ GET  /api/dict/{word}?article_id=…     ← 本地词典，毫秒级
      │           ├─ 英美音标 + 词性 + 中/英释义 + 两个发音槽状态，一次拿全
      │           └─ 后端顺带把词收进生词本（回执在 enrolled 里）
      │
      └─ 其它 kind → 浮层只给一个【解析】按钮：**不落库、不调模型**
            └─ 点【解析】才先补落痕迹（同上的 POST），再调模型
                  └─ POST /api/ai/explain
                        └─ 结果进右侧 AiPanel，并刷新本篇解析记录
```

> M10 之前这里是两个并行请求（词典约 1s + 模型 1~3s），音标先出、释义后到。
> 本地化之后没有理由再拆：两次往返、两个 loading、两处都可能失败，换不来任何东西。

**「词自动查、句不自动查」是刻意的。** 划一个词就是想查它；而拖拽选一段
往往是误触、或先看看有多长，自动解析纯属浪费 token。

**「没点【解析】就当没划过」**：划中一段句子不写 highlights、不写 ai_logs ——
正文里不出高亮，清单里也不出条目（把浮层关掉就等于没划过）。于是
「正文里有没有这条高亮」与「这件事有没有发生过」是同一个判断：解析记录挂靠的
`highlight_id`、清单里的语法点、正文的底色指向同一件事。代价是「划过但没解析」
这类痕迹在库里根本不存在 —— 汇总页曾经有一栏列它们，已随这条规则一起删掉。

**「词」必须是完整单词，片段会被吸附回去**（`selection.ts::snapToWholeWord`）。
鼠标拖选停不准词边界：想选 `Chang` 手一抖就成了 `hang`，再抖一下就剩 `a`。
以前这种片段会被当成一个独立单词去查词典、读音标、自动进生词本 —— 卡片上写着
一张给「a」的释义，生词本里多一条查不到的词。判定刻意保守，只处理**落在同一个
词里、且自己不含空格**的片段（左右任意一侧紧挨着字母数字，标点不算词内字符，
所以选 `China` 不会被吸成 `China's`）；跨词、带空格的选区原样保留 —— 那是用户
真的想选一段。吸附结果会落库（区间就是整个词），卡片上标一句「已按整词查」，
免得用户以为卡片显示了别的词。单 token 但**不含字母**的选区（`1929`、`——`）一律
按短语处理，不去查词典（口径与后端 `highlights.infer_kind` 一致）。

**落库先于调用。** 痕迹写失败也不阻塞释义（单词查词、选段解析都是）——
用户要的是解释，不是数据库状态。

### 7.3 渲染（M2 已实现）

`blocks.text` 是纯文本，渲染层**不做任何富文本解析** —— 富文本一旦解析进 DOM，
M3 的划词偏移就多了一套坐标系，两边迟早对不上。样式全由块类型决定：

| 块类型 | 渲染 |
|---|---|
| `title` | 阅读页顶部的 `<h1>`（M9，见 §7.9）。**不在 BlockRenderer 的遍历里** —— 它要排在「标题 + 元信息 + 操作按钮」那个 header 中，由阅读页自己画一次 |
| `heading` | `<h{level}>`，字号按 `level` 分档（Sixth Tone 的小标题是 h3） |
| `para` | `<p>` |
| `image` | `<figure><img>`，点击全屏放大（ESC / 点任意处关闭） |
| `caption` | 紧跟图片的 caption 块**并进同一个 `<figure>`** 作 `figcaption`；孤立的按小字段落渲染 |
| `quote` / `list` / `table` / `video` | 各自样式；`video` 只显示 URL（M0 起就不下载视频） |
| `placeholder` | 不渲染 |

**高亮是渲染期重建的**（M3）：每个文本块拿到自己的 `Highlight[]`，用
`splitByHighlights` 切成「未高亮 / 高亮 / 未高亮」片段，高亮段渲染成 `<mark>`。
数据层从头到尾只存 `{block_id, start_offset, end_offset}`，HTML 里没有任何
高亮痕迹 —— 这是红线 2 的落地方式。

一个容易错的细节：`list` 块会被拆成多个 `<li>`，每行的文本坐标都不再等于
块坐标。所以 `<TextWithHighlights>` 带一个 `base` 参数（该行在块内的起点），
高亮区间在渲染前先平移过去。不做这层平移的话，第二行之后的高亮全会错位。

图片用 `loading="lazy"` 懒加载；未本地化的图直接给占位块，不做二次抓取。

### 7.4 主题与排版（M2 已实现）

三套阅读主题 light / sepia / dark，全部通过 CSS 变量切换：

```
[data-theme='light'] { --paper: #fff;    --ink: #23272f; --hl: #fff2a8; --hl-word: #c6ecd2; … }
[data-theme='sepia'] { --paper: #f8f3e9; --ink: #3a3226; --hl: #f3e3a1; --hl-word: #d5e5bc; … }
[data-theme='dark']  { --paper: #16181c; --ink: #dfe3e8; --hl: #5c4a15; --hl-word: #1d4234; … }
```

- 正文用 serif 系统字体栈（Georgia / Times），界面用 sans。**不引外部字体** —— 阅读器要能离线打开。
- 栏宽、字号、行距由 `--er-width` / `--er-fs` / `--er-lh` 三个变量驱动，
  切换设置只改变量，不重建 DOM（不给 M3 的高亮区间制造麻烦）。
- 偏好存 `localStorage`（键 `er:prefs`）。它是「这台设备怎么显示」，不是学习数据，**不进后端**。
  含 M9 的右栏宽度 `panelWidth`（拖面板左边缘改、双击复位，见 §7.9）。
- `--hl` 自 M2 就定义好，M3 的 `<mark>` 直接复用，不必回头改主题。
- **底色分两档**：`--hl`（黄）给短语 / 句子 / 段落，`--hl-word`（绿）给单词，
  `<mark>` 上带 `data-kind` 分流。起因是真实场景：解析过一条长句之后，
  读者还要划句中几个词 —— 一类底色时根本看不出「这个我查过译没有」，
  而这两类痕迹的归宿本来就不同（词进生词本，句进解析清单）。
  嵌套时由 `splitByHighlights` 保证**内层的词赢**（§7.1.1），
  阅读页标题下用两个同色小方块作图例（`er-hl-swatch`）。

### 7.5 阅读进度（M2 已实现）

位置同时记两个量，**恢复时优先用 `last_block_id`**：

| 量 | 用途 | 为什么需要它 |
|---|---|---|
| `last_block_id` | 恢复的首选锚点 | 图片懒加载会把页面撑高，按百分比还原必然漂移；块 id 不受影响 |
| `scroll_pct` | 顶部进度条 + 列表页显示「停在 42%」 | 用户看的是比例，不是块号 |
| `read_seconds` | 累计阅读时长（**增量上报**） | 增量累加，重复提交、网络抖动都不会把总时长冲掉 |

采集与上报：

```
scroll (rAF 节流)
  └─ 找到视口顶部第一个块（偏移 TOOLBAR_OFFSET，避开工具栏遮挡）→ 记为当前位置
计时器 1s：document.visibilityState === 'visible' 时 pending += 1
上报时机：每 15s ｜ visibilitychange→hidden ｜ pagehide（keepalive）｜ 路由卸载
```

还原的两个坑（都已处理，`Reader.tsx`）：

1. **程序化滚动会污染进度。** 滚动还原本身就是一次 `scrollTo`，
   不屏蔽的话它会把「上次读到 42%」立刻改写成 0%。做法是 `useReadingProgress`
   接受 `suspended`，恢复期间只更新基线不上报。
2. **图片加载完会把内容往下推。** 只还原一次必然偏，所以在图片 `load` 事件
   与前 0.35/1.2/2.6 秒各校准一次；**一旦用户自己滚了（wheel / touchmove / keydown）
   就立刻放弃校准**，不把人拽回去。

后端 `PUT /api/articles/{id}/progress` 对 `last_block_id` 做归属校验，
不属于该文章的块 id 按 `null` 处理，避免脏数据把「继续阅读」指到别人文章上。

### 7.6 离线可读（M2 已实现）

「离线」的准确含义是**不依赖原站**，而不是不依赖本机服务：

| 资源 | 来源 | 断网后 |
|---|---|---|
| 正文 blocks | `GET /api/articles/{id}` | 走 `localStorage` 缓存（`er:cache:` 前缀），顶栏出现「离线阅读」标记 |
| 图片 | 后端 `/media/...`（抓取阶段已下载） | 正常显示 |
| 字体 | 系统字体栈 | 正常显示 |

GET 采取「网络优先 + 缓存兜底」：**HTTP 4xx/5xx 属于真实业务错误，不会被缓存掩盖**，
只有网络层失败才降级读缓存。写操作（进度、标注）不缓存。

M2 验收用 Playwright 真浏览器验证（`backend/scripts/smoke_ui.py`）：
拦住一切非 `127.0.0.1` 的请求后页面图文完整 → 证明无外部依赖；
切断 `/api` 后刷新正文仍在 → 证明缓存生效。

---

### 7.7 划词与 AI 面板（M3 已实现）

**选区 → 偏移**（`lib/selection.ts`）。渲染出来的 DOM 的 `textContent` 不一定等于
`blocks.text`（列表块就是例子），所以算法是「先算后验」：

1. 用 `TreeWalker` 算出 range 两端点相对块的字符偏移
2. 拿 `blocks.text.slice(start, end)` 与选区文字比对，一致就直接用
3. 不一致 → 退化成「在正文里搜这段文字」，且**只接受唯一匹配**
4. 两者都失败 → 返回 null，不产生任何痕迹（宁可让用户重选，也不写错位数据）
5. 区间再交给 `snapToWholeWord` 吸附一次：落在词内部的片段扩到整个词
   （划词只认整词，理由与判定边界见 §7.2）

**浮层**（`components/SelectionPopover.tsx`）按 `kind` 分两种形态：词卡（音标 +
释义 + 例句 + 搭配）或纯动作条（**解析**；只有已经落过痕的选区才多一个「取消标记」，
没点过【解析】的选段身上什么都没有）。
定位是 `position: fixed` + 视口边界收敛，下方空间不够就翻到选区上方。

**关浮层要把浏览器的原生选区一起收掉**（`Reader.closePopover` → `removeAllRanges()`）。
鼠标拖出来的那圈蓝底是**浏览器自己的**选区，不是我们的 state：只 `setSel(null)`
的话，点浮层上的 ✕ 之后那行字看起来仍然是「选中」，只有重新划一次才会消失。
实测过浏览器自己的行为：点空白处它会自己收（`collapsed=true`），**点按钮不会**
（`ranges=1`、选中的文本还在）—— 所以这一步不能省。

**「正在查看」的蓝框（`mark[data-active='1']`）只表达一件事**：右侧面板现在展示的
就是这条痕迹。它以前没有生命周期，只有「换一条解析」才会移动，于是关掉面板、
点空白处之后那圈蓝框都还亮着，被读成「关不掉、一直是选中状态」。现在三种情形
都收掉：① 关面板；② 点「别处」（不在痕迹上、不在面板与浮层里）；③ 划了新的一段。
点痕迹本身不清（点了马上会设回来，清了反而闪一下）；深链定位（`focusHighlight`）
那圈不受影响 —— 它是 URL 带来的「带我去看这里」，与面板无关。

**底色两档**：单词走 `--hl-word`，短语 / 句子 / 段落走 `--hl`（§7.4）。

**一次拿全**（M10）：划词后只发**一个**请求（`GET /api/dict/{word}`），
本地词典毫秒级返回 —— 音标、词性、释义、两个发音槽一起到，没有「先出什么后出什么」
这回事。M3–M9 是两个并行请求（词典约 1s + 模型 1~3s）、音标先出释义后到，
那套骨架屏与「先确认选中了哪个词」的取舍随本地化一起消失了。

**变形词的音标走两级兜底**（M10 重写）：现在主音标来自 ipa-dict，它**收屈折形式**
（`ran` / `runs` / `running` 各自独立成条），所以「划到 -ing 形式就没音标」这个
老问题基本不存在了。两级兜底是：

> ① ipa-dict 命中该词形 → 用它（英美两条都可能有）；
> ② 没命中 → 用**原形**的：先 ipa-dict、再 ECDICT 那条；
> ③ 都没有 → 用本词形自己的 ECDICT 音标；仍没有才留空。

`lemma` 来自 ECDICT 的 `exchange` 字段（`preserved` → `0:preserve`），**纯本地**。
记 `phonetic_of=lemma` 时界面提示「音标取自原形」，避免把变形词的读音挂在原形上。

历史背景值得留一句：M3–M9 走 Wiktionary 时这个问题很尖锐 —— `preserved` 在
Wiktionary **有**条目 `/pɹəˈzɜːvd/`，拿到的是过去式的读音，配原形词条就是错的，
所以当时是「只要 `lemma != surface` 就**无条件**回查原形」，还为此把
`PROMPT_VERSION` 从 m4.0 升到 m4.1 去作废旧缓存。现在这条口径由**词典自己的
lemma** 决定，不再依赖模型给的 lemma。

**「滚动就关浮层」的坑（M3 浏览器验收实测）**：一开始写成「只要 `window scroll`
就 `setSel(null)`」，结果浮层闪一下就消失、永远点不到。原因是划词后页面常会自己
动一下 —— 正文里十几张图片陆续加载把排版撑开，浏览器滚动锚定随之调整 `scrollY`。
改成两段式：前 600ms 是排版稳定期，其间滚动一律忽略；稳定期后再取页面位置作基准，
位移超过 48px 才算「用户真的滚走了」。这条只有真浏览器能测出来，jsdom 里复现不了。

**AI 面板**（`components/AiPanel.tsx`）除了展示结果，还承担需求 5 的检索入口：
底部列出本篇所有 `ai_logs`，点击即可回看当时的解析结果。

**「加载中」是可解释的**：explain 实测要 10~17 秒，单纯转圈会被当成卡死。
面板按已耗时切换阶段文案并显示秒数。

**主题联动**：结果面板与高亮都用 `--paper / --panel / --ink / --hl` 这套变量，
所以三套阅读主题切换时，正文、高亮、侧栏一起变，不需要各自再写一套配色。

### 7.8 构建与部署形态

前端产物**由后端同进程直发**，日常不必再单起一个前端服务：
`npm run build` 出 `frontend/dist/`，FastAPI 启动时把它挂上（`app/main.py::mount_frontend()`，
开关 `SERVE_FRONTEND`）。打开 <http://127.0.0.1:8010> 就是界面。

三条必须做对的细节：

| 细节 | 为什么 |
|---|---|
| SPA 回退到 `index.html` | 路由是 `BrowserRouter`（History API），`/a/3?block=12` 这类深链刷新时服务端并不存在这个路径。`StaticFiles(html=True)` 只会在目录里找文件，兜不住，必须自己写 catch-all |
| catch-all **必须最后注册** | Starlette 按注册顺序匹配。提前注册会把 `/api/*`、`/media/*`、`/docs` 一起吞掉 |
| `/api`、`/media` 前缀、以及带扩展名的路径不回退 | 前者若返回 HTML，前端 `res.json()` 抛 `Unexpected token '<'`，比 404 难查；后者（`favicon.ico`、`robots.txt`）塞 HTML 回去只会让浏览器解析失败，404 更诚实 |

缓存分两档：`index.html` 走 `no-cache`（换了版本不能还跑旧壳子），`/assets/*` 文件名带
内容哈希所以给 `immutable` 长缓存（`_HashedAssets`）。两者配套才是正确的组合。

**Vite 5173 不删**：改前端代码时仍走 `npm run dev`（有 HMR），它与 8010 并存、互不干扰。
8010 的形态解决的是「自己用一个服务就够」，不是开发形态。验收脚本用 `FRONT_URL`
环境变量切换目标，默认仍是 5173：

```bash
python scripts/smoke_ui.py --article-id 2                     # 打 5173（开发形态）
FRONT_URL=http://127.0.0.1:8010 python scripts/smoke_ui.py    # 打 8010（构建产物形态）
```

单服务形态下 M2 验收 20/20（含图片本地化、断 `/api` 离线可读、刷新回位）。

### 7.9 标题划词与右栏拖宽（M9 已实现）

**标题也是块。** 标题原本只活在 `articles.title` 这一列里，于是它有一条正文没有的
特权：划不了词。修法不是给渲染层开个后门，而是让它进坐标系 ——
`ingest.title_block()` 在入库时把标题写成 `blocks` 的一行（`type='title'`、
`order_index=0`、`text=articles.title`，正文块由 fetcher 重排成 1..n）。
之后的三条路径全部与正文同构，没有一处特判：

| 环节 | 走的还是老路 |
|---|---|
| 选区 → 偏移 | `readSelection` 按 `data-block-id` 找块，h1 上挂了它 |
| 落库 | `POST /api/articles/{id}/highlights`，`block_id` 就是标题块 |
| 重建高亮 | `splitByHighlights` 切区间，`<mark>` 由 `TextWithHighlights` 画出（标题与正文共用这个组件） |
| 重抓 | 标题的 `block_uid` 是 `sha1('title|<归一化文本>#0')`，命中即复用行 id → 标题上的痕迹不丢 |
| 检索 / 深链 | 标题块进了 `fts_blocks`，命中后 `?block=&start=&end=` 照常能跳回来（顺带把标题变成可检索的） |

三个刻意的取舍：

- **类型不复用 `heading`，单独用 `title`**：前端要把它画成页面标题而不是正文小标题，
  导出侧 `# 标题` 已经写过一行，不必再出一行（`render_markdown` 对未知类型天然忽略）。
  代价是 `buildRenderItems` 必须显式跳过它 —— 否则标题会渲染两次。
- **h1 上带 `prose-en`**：高亮底色、选区颜色这些规则都挂在 `.prose-en` 作用域下，
  标题要和正文一样能划能高亮，就得进同一个作用域；另起一套选择器只会越走越远。
  只把「标题不该被断词」这一条覆盖回去（`.blk-heading.prose-en { hyphens: none }`）。
- **老文章要补一行**（`ingest.backfill_title_blocks()`，由 `db.init_db()` 调的
  `_migrate_title_blocks()` 触发）：不补的话用户手上的文章标题依然划不动，
  得先重抓一遍才生效。补的行走同一个 uid 算法，所以下次重抓会被复用而不是另起一行。

**右栏宽度可拖。** 面板左边框即拖拽把手（命中区左右各 4px，跨在 border 上）：
只做 1px 的话得对准那一个像素才拖得动，等于没有；悬停/拖动时把那条边线点亮成
`--link`，不然没人知道这里能拉。松手（或双击）才写偏好：

| 决定 | 理由 |
|---|---|
| 拖动中只改 AiPanel 的局部宽度 | 偏好每动一像素写一次 localStorage 是浪费；更要紧的是偏好在 App 里，一改就让整个阅读页（几百个高亮节点）重渲染，手感发涩 |
| 用指针捕获（`setPointerCapture`）而不是窗口监听 | 拖到面板外、甚至窗口外，`pointermove/up` 也照样回来，不会「松手了还在拖」 |
| 宽度进 `ReadingPrefs.panelWidth` | 它和字号、栏宽同类：**这台设备怎么显示**，只落 localStorage，不进后端（§7.4） |
| 上限随窗口收缩 | 窗口只有 900px 宽时还允许拉 760，正文会只剩一条缝（`clampPanelWidth`，下限 300 / 上限 760） |

---

## 8. API 契约

已实现（M1 + M2 + **M3**）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/articles` | `{url, force_render?}` → 幂等创建，返回 article（`status=pending\|fetching\|done\|failed`） |
| `GET` | `/api/articles` | 列表，`?limit=&offset=&q=`，按 id 倒序；带 `cover_url`、`block_count`、`image_count` 与阅读进度 |
| `GET` | `/api/articles/{id}` | 详情：article + blocks（图片信息已平铺，含 `media_url`） |
| `POST` | `/api/articles/{id}/refetch` | 显式重抓；已在跑返回 409 |
| `DELETE` | `/api/articles/{id}` | 删除文章、级联痕迹与磁盘资产 |
| `GET` | `/api/articles/{id}/markdown` | 导出 Markdown（图片指向 `/media`） |
| `GET` | `/api/articles/{id}/progress` | 阅读进度；从未读过返回空对象而非 404（M2） |
| `PUT` | `/api/articles/{id}/progress` | 上报进度，`read_seconds` 为增量并累加；`last_block_id` 校验归属（M2） |
| `GET` | `/api/search?q=` | **三组**（正文 / 生词本 / AI 记录）一次返回，每条带 `<mark>` 片段与跳回原文的位置（M4 起换契约，M2 的 `{total, items}` 已废弃） |
| `GET` | `/media/{article_id}/{filename}` | 本地化图片静态服务 |
| `GET` | `/api/health` | 健康检查，附 `fetching`、`render_domains`、LLM 是否已配 key（不回声 key 本身）、`prompt_version`（M5 起另有 `vocab` / `review`；M7 起有 `audio` 本地化统计与熔断状态） |
| `GET` | `/api/dict/{word}` | **完整词条**（M3；M10 改成返回本地词典词条）。纯本地查询、毫秒级、**不碰 LLM 也不出网**。返回 `word`/`headword`/`lemma`/`pos`/`phonetic(+uk/us)`/`phonetic_source`/`phonetic_of`/`meaning_cn`/`meaning_en`/`worth`/`tags`/`audios`（按口音索引的两个发音槽）/`source`/`enrolled`。`?article_id=` 会顺手入册，`?enroll=0` 关掉副作用 |
| `GET` | `/api/dict/{word}/audio` | **音频字节流**（M7；M10 加 `?accent=`）。本地优先，没有才当场下载（受节流器约束）。返回 `audio/*` + `X-Audio-Origin` / `X-Audio-Accent`；该口音没有录音时 404。不写进 OpenAPI，前端由 `AudioButton` 拼 |
| `GET` | `/api/dict?word=` | 同 `/api/dict/{word}`，供含 `/` 的词（如 `and/or`）走 query 形式 |
| `POST` | `/api/articles/{id}/highlights` | 创建划词痕迹（M3）。同块同区间幂等；`surface` 与正文切片不符则以后者为准，偏差过大直接 422 |
| `GET` | `/api/articles/{id}/highlights` | 该篇痕迹列表（M3） |
| `GET` | `/api/highlights` | 全局痕迹列表，供 M4 生词本 / 汇总页用（M3） |
| `GET` | `/api/highlights/{id}` | 单条痕迹（M3） |
| `PATCH` | `/api/highlights/{id}` | 改批注 / 颜色（M3） |
| `DELETE` | `/api/highlights/{id}` | 删除痕迹；`ai_logs` 保留（`highlight_id` 置空），学习历史不消失（M3） |
| ~~`POST`~~ | ~~`/api/ai/dict`~~ | **已下线**（M10）：划词改查本地词典，不再经模型。历史 dict 记录仍躺在 `ai_logs` 里可回看，但不会再产生新的 |
| `POST` | `/api/ai/explain` | 选段【解析】（M3 起，M8 合并三操作）：译文 / 主干 / 附加成分（含挂靠）/ 短语 / 可复用语法要点 / 难度；语法要点自动入清单 |
| ~~`POST`~~ | ~~`/api/ai/translate`~~ | **M8 删除**（并入解析）。旧记录仍在 ai_logs 里可回看 |
| ~~`POST`~~ | ~~`/api/ai/grammar`~~ | **M8 删除**（并入解析）。`smoke_ai.py` 有断言钉住这两个路径返回 404 |
| `GET` | `/api/ai/logs` | AI 流水，可按 `article_id` / `highlight_id` 过滤（M3） |
| `DELETE` | `/api/ai/logs/{id}` | 删除一条解析记录（误操作清理）。连 `fts_ai` 索引一起删，缓存随之失效；高亮与生词本不动。不存在返回 404 |
| `GET` | `/api/ai/stats` | 调用次数 / 缓存命中 / token 消耗（M3） |
| `GET` | `/api/ai/health` | 真实探一次模型，确认 key 与模型名可用（M3） |

> 阅读进度没有单独的状态端点，直接挂在 article 上（`get_article` / `list_articles`
> 共用同一个 `LEFT JOIN article_progress`）：前端打开文章时一次请求同时拿到
> 正文、元数据和上次读到哪，省一轮往返。

M4（生词本 / 清单 / 导出，已实现）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/vocab` | 生词本列表。`article_id` 按**出现记录**筛（不是 `vocab.article_id`）、`status`、`worth`、`q`、`sort=recent\|alpha\|priority\|seen`、分页 |
| `GET` | `/api/vocab/stats` | 总数 / 重点数 / 今日新增 / 分状态计数 |
| `GET` | `/api/vocab/{id}` | 单条详情，带 `sources`（在哪几篇、哪句里划过） |
| `PATCH` | `/api/vocab/{id}` | 改 `status` / `meaning_cn` / `note` / `worth`；非法状态 422 |
| `DELETE` | `/api/vocab/{id}` | 只删生词本这一行 —— 原文高亮与 AI 流水都不动（红线 1） |
| `GET` | `/api/vocab/export?format=anki\|md` | 生词本整体导出，可按 `article_id` / `status` 限定 |
| `GET` | `/api/grammar?article_id=` | 语法清单 |
| `PATCH` | `/api/grammar/{id}` | 改类别 / 讲解；空讲解 422 |
| `DELETE` | `/api/grammar/{id}` | 删除语法点 |
| `GET` | `/api/articles/{id}/summary` | 汇总：`article` / `words` / `grammar` / `grammar_by_category` / `stats` |
| `GET` | `/api/articles/{id}/export?format=md\|anki\|text` | 单篇导出；未知格式 422 |
| `GET` | `/api/search?q=&limit=` | **三组一起返回**（`blocks` / `vocab` / `ai`）+ `total`，每条带跳回原文的位置 |
| `GET` | `/api/search/suggest?q=` | 生词本前缀补全，供检索框下拉 |

> **故意没有 `POST /api/vocab`。** 入册是划词时自动发生的；让用户手工「加入生词本」
> 等于承认自动入册漏了词，而需求 5 的立场是不漏。移除有接口，新增没有。

> `GET /api/search` 在 M2/M3 只搜正文并返回 `{total, items}`；M4 换成三组。
> 需求 5 要「AI 交互记录可全文检索」、需求 6 要「生词可检索」，只搜正文等于
> 三条要求只兑现了一条。三组一起返回而不是拆三个端点：用户输一次，期望一次看全。

> 检索的查询串要先清洗（`search.fts_query`）：抽出字母/数字/汉字片段，每个做成
> **带引号的前缀短语**（`"inva"*`）。裸写 `inva*` 会把用户输入里的大写 `OR` /
> `NOT` 当成 FTS5 运算符，搜「a OR b」直接 500（M4 实测踩到）。
> 注意前缀撞的是 **porter 词干**：`sitt` 匹配不到索引里的 `sit`，得搜完整的
> `sitting` 或 `sit`。中文另走 `LIKE` 兜底（unicode61 不切分汉字）。

M5（间隔复习 / 流式，已实现）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/ai/stream` | **SSE 流式**（§6.6）。`{task, ...}` 与一次性端点同构；帧协议 `open/delta/reset/done/error`，终帧的 `result` 与一次性端点**逐字相同** |
| `GET` | `/api/review/queue` | 本次要复习的卡。排序：到期早者优先，新词垫后；`?limit=&new_limit=&article_id=`。返回 `cards` + `next_due` + `stats` |
| `GET` | `/api/review/cards/{id}` | 单张卡（复习页按 id 深链） |
| `POST` | `/api/review/cards/{id}/grade` | 评分 `{grade: again\|hard\|good\|easy, elapsed_ms?}`；返回新排期 `{state, due, interval_days, reps, lapses, ease, preview}` |
| `POST` | `/api/review/cards/{id}/suspend` | 暂停出题（**不动生词本**，词还在列表里） |
| `POST` | `/api/review/cards/{id}/resume` | 恢复出题 |
| `GET` | `/api/review/stats` | `total` / `due_now` / `new` / `learning` / `review` / `suspended` / `due_today` / `reviews_today` |
| `GET` | `/api/review/logs` | 复习流水，可按 `vocab_id` / `since` 过滤 |

> **故意没有 `POST /api/review/cards`。** 卡片就是生词本条目，只能由划词自动产生
> （§7.1.1 的入册规则）。手工造卡等于承认划词漏了词，和「没有 `POST /api/vocab`」
> 是同一条立场。

规划中：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/articles/{id}/status` | 单独的状态端点。**不做**：`GET /api/articles/{id}` 已承载状态查询，抓取只有三个中间状态，前端每 3 秒轮询详情即可（见下） |

**增量轮询**：`GET /api/articles/{id}` 已承载状态查询，不再单独做 `/status` 端点 ——
抓取只有三个中间状态，前端每 3 秒轮询一次详情即可，省一套契约。

---

## 9. 实施里程碑

用户已选定：**直接做完整闭环**。里程碑仍按顺序推进，每个都可独立验收。

| 里程碑 | 交付 | 验收标准 |
|---|---|---|
| **M0 抓取验证** | `scripts/fetch_once.py` + 诊断脚本，输出 Markdown + 本地图片 | **已通过**（2026-09-14）：六个样本 6/6 成功。带图文章 Sixth Tone 15/15 图 + 14 图注、China Daily 3/3 图 + 3 图注，图文顺序正确。详见 §5.1 |
| **M1 后端骨架** | FastAPI + SQLite + 抓取入库接口 | **已通过**：`POST /api/articles` 后数据库有完整的 articles/blocks/images 记录。`smoke_api.py` 24 项断言全过 |
| **M2 阅读器** | React 阅读页 + 图片服务 + 进度记录 | **已通过**（2026-09-14）：`smoke_ui.py` 20 项断言全过 —— Sixth Tone 样本 41 段 / 4 小标题 / 15 图 / 14 图注与后端一一对应；非本机请求 0 个、无 JS 报错；切断 `/api` 后刷新正文仍在；滚到 50% 后刷新位置偏差 5.5%。详见 §7.3–7.6 |
| **M3 学习中台** | 划词卡片 + 选段 AI 面板 + 自动落库 | **已通过**（2026-09-14）：后端 `smoke_ai.py` **42/42**、浏览器 `smoke_ui_m3.py` **21/21**。关键项：划词即落库（需求 5）；刷新后高亮区间与库记录逐字一致（红线 2）；音标与词典端点逐字相等、且变形词能回查原形（红线 3）；重复选中命中缓存不烧 token；删痕迹不删 AI 历史 |
| **M4 汇总与检索** | 生词本 + 单篇清单 + 全文检索 + 导出 | **已通过**（2026-09-14）：后端 `smoke_m4.py` **130/130**（连跑两次 131/131，幂等）、浏览器 `smoke_ui_m4.py` **56/56**；M3 回归 `smoke_ai.py` **43/43**、`smoke_ui_m3.py` **21/21**，M2 回归 `smoke_ui.py` **20/20**、`smoke_api.py` **30/30**。关键项：划词自动入册且重复点开不重复计数（需求 5/6 的连接点）；生词本音标与词典端点逐字相等、变形词取自原形（红线 3）；移除生词/语法点不牵连原文痕迹与 AI 流水（红线 1）；导出的 Markdown 可直接阅读、Anki CSV 首行无 BOM 且三列可解析；三组检索都能命中并跳回原文闪一下 |
| **M5 复习与流式** | 间隔复习（SRS）+ SSE 流式输出 | **已通过**（2026-09-14）：后端 `smoke_m5.py` **130/130**、浏览器 `smoke_ui_m5.py` **51/51**。关键项：四档调度在 `learning`/`review` 两个阶段的推进与边界（ease 夹在 1.3~3.2、间隔上限 365 天）逐条对账；`again` 在 review 阶段**折半而非归零**、`reps` 只数成功复习；暂停后不进队列但生词本照旧；**流式端点的 `done.result` 与一次性端点逐字相同**，且流式过一次后一次性端点命中同一缓存；前端正面不泄露答案、数字键评分、真流式增量 |
| **M6 发音修复** | 修 `{{audio}}` 解析：英美分槽 + 通用槽兜底 | **已通过**（2026-09-15）：`smoke_ai.py` [B2] 新增 11 项（含 6 条纯函数口音归类）、`smoke_ui_m3.py` [B2] 新增 4 项，端到端复现用户报的 `documentary` 场景 —— **词卡上出现 `🔊 美`**。回归：`smoke_ai.py` **54/54**、`smoke_ui_m3.py` **25/25**、`smoke_ui_m4.py` 56/56、`smoke_ui_m5.py` 51/51、`smoke_m4.py` 130/130、`smoke_m5.py` 130/130、`smoke_api.py` 30/30 |
| **M7 发音换源与本地化** | 有道 `dictvoice` 优先 + 词典回退 + 音频本地化 + 隐蔽下载 | **已通过**（2026-09-15）：`smoke_m7.py` **54/54**（A 纯函数／B 冷启动 pending→落盘／C 负缓存／D 打桩验回退与「否定 vs 失败」分叉／E 闸门间隔·抖动·熔断／F 端点健壮性／G 契约与 health）。回归 `smoke_ai.py` **59/59**、`smoke_m4.py` **130+/130+**、`smoke_m5.py` 130/130、`smoke_ui_m3.py` **29/29**、`smoke_ui_m4.py` 59+/59+、`smoke_ui_m5.py` **53/53**、`smoke_api.py` 30/30、`smoke_ui.py` 20/20。关键项：真实网络下 `resilient`／`serendipity`／`documentary`／`preserve` 均拿到真 mp3（11–14 KB，ID3 头）；`zzzqqqxx*` 返回 500 → 落 `missing` 且不重试；**浏览器只连 `/api/dict/{word}/audio`，页面无任何外链音频**；`audio_cache` 与 `dict_cache` 各管一段，重抓文章不碰音频台账（红线 1） |

| **M8 解析合并** | 选段三操作（翻译 / 精读解析 / 语法点）合并为【解析】：主干 + 挂靠结构 + 语法要点自动入册；旧端点删除、旧记录只读回看 | **已实现，验收见脚本**：`smoke_ai.py`（[E] 解析结构断言 / [F] 旧端点返回 404）、`smoke_m4.py` [F]（语法要点自动入清单 + 幂等）、`smoke_ui_m3.py` [C]（浮层单按钮 → `parse-result` 渲染）、`smoke_m5.py` [F]（流式改走 explain，一次性端点结果逐字相同）。设计与取舍见 §6.7 |
| **M9 标题可划 + 右栏可拖** | 标题成为 `blocks` 的一行（`type='title'`），划词/高亮/深链与正文同构；老文章由启动迁移补行。AI 面板左边缘可拖动改宽（双击复位，宽度进阅读偏好） | **已通过**（2026-09-15）：`smoke_m9.py` **39/39** —— 标题块只渲染一次、划句弹【解析】、半截词吸附整词、库内区间与标题真实位置逐字一致、刷新后按区间重建（嵌套时段切分正确、词/句两档底色）、检索命中标题后深链回来能落在标题上闪一下；右栏：默认 380、命中区跨在边框上、悬停点亮边线、往左拖 140px → 520、下限夹 300、刷新保持、双击回 380。脚本刻意掐断词典/模型请求并删净自己造的痕迹（跑之前 7 条、跑完还是 7 条）。回归：`smoke_ui.py` **20/20**、`smoke_ui_m3.py` **49/49**、`smoke_ui_m4.py` **60/60**、`smoke_m4.py` 90/90（`--offline`，另有 2 项因跳过 LLM 而必然拿不到 AI 命中，非回归）。设计与取舍见 §7.9 |
| **M10 划词本地化 + 英美双口音** | 划词改查本地词典库（ECDICT 释义 + ipa-dict 英美双音标），删除 `/api/ai/dict` 与 dict prompt；`audio_cache` 改 `(word, accent)` 复合主键、音频不预热改为点喇叭才下；点单词高亮重弹词卡；清掉全部历史迁移与 `dict_cache` | **已通过**（2026-09-16）：`smoke_m10.py` 见脚本输出。关键项（真实浏览器 + 真实网络逐条验证）：`/api/health` 报出 `dict.ecdict`(811 MB 在位) 与 `dict.ipa`；`resilient` 拿到英美双音标、`pos=adj.`、`worth=1`（gre），`the`/`run` 为 `worth=0`（zk/gk），`preserved` 的 `lemma=preserve` 取自 `exchange` 且音标随之取原形；`Beijing`/`Thailand` **只有美音音标 → 只显示一条，但英音喇叭照样给**；未收录词与「库文件缺失」两条降级各有对应文案；入册首次 `created=true`、再次 `false`，**同一篇里重复查同一个词 `seen_count` 不再增长**；点喇叭才下载（英 0.8 s / 美 3.8 s，同一把闸门），槽位 `pending→ok` 且落盘 11.7 KB mp3；点正文里已划过的单词（绿色高亮）**重新弹出词卡**（不依赖任何历史记录）；选段【解析】链路未受影响（面板开启、流式增量正常）；汇总页「划过但没解析」不再把单词算进去（该栏随后整体删除，见下一行） |
| **选段不落痕** | 划中一段句子不再写 `highlights` / `ai_logs`：**点过【解析】才有痕迹与解析记录**，没点过就当没划过；汇总页「划了线但还没解析」整栏删除（接口字段同步删除）；解析后清单缓存立即作废，不必再手动刷新 | **已通过**（2026-09-17）：临时浏览器脚本 14/15 —— 唯一一条「失败」是脚本自己掐断的 `/api/ai/stream` 与 `/api/dict` 两条请求（预期报错）。关键项：选段后正文无 `<mark>`、库里没有新痕迹、浮层上没有「取消标记」；点【解析】后痕迹与高亮才出现；单词仍划中即落库；清单页与接口都不再有 `uncovered`；本次造的痕迹跑完删净。回归脚本 `smoke_m4.py` / `smoke_m10.py` / `smoke_m9.py` / `smoke_ui_m3.py` / `smoke_ui_m4.py` 已同步改到新口径（待重跑） |

M0 是风险闸门：若两个站点抓取质量不达标，先解决抓取再往下走。

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| Cloudflare / 反爬 | 静态请求 403，抓不到正文 | Playwright 降级；降低频率；缓存原始 HTML 避免重复抓 |
| 图床 IP 级拦截 | 图片全部 403（实测 `img2.chinadaily.com.cn`） | **已解决**：`page.on("response")` 拦截浏览器自身的图片响应取 body，3/3 补齐 |
| 新闻门户站正文污染 | 侧栏推荐位、分享图标混入正文（实测 China Daily） | **已解决**：per-domain 正文容器 + 装饰图黑名单，双保险 |
| 纯 CSR 站点丢图 | SSR HTML 无正文图，且 JSON 无位置信息（实测 Sixth Tone） | **已解决**：`RENDER_DOMAINS` 走 Playwright 渲染；代价 41 s/篇，M1 必须异步化 |
| 渲染路径慢 | 41 s/篇，同步请求会超时 | **已解决**：后台线程池 + `status` 轮询（§5.2） |
| 渲染路径小标题丢失 | 渲染后 DOM 里小标题不是 `<h*>`，被当作普通段落 | **已解决**：按「整段仅一个加粗片段」识别（`_is_subhead_para`），实测 heading 0 → 4 |
| 付费墙 / 动态渲染 | 正文缺失 | 检测正文长度阈值，低于阈值标记 `failed` 并提示用户 |
| 图片防盗链 | 图片 403 或坏图 | 下载带 `Referer`；失败落占位图并记录 |
| 词典 API 不可用 | 音标拿不到，划词功能残废 | **已解决**（M3）：原定源 `dictionaryapi.dev` 实测全部 522 / 超时 → 换成 Wiktionary `action=parse` 解析 `{{IPA}}` 模板，10 个词（含专名）10/10 命中 |
| LLM 输出被 `max_tokens` 截断 | JSON 解析失败，接口 502 | **已解决**（M3）：`finish_reason == "length"` 时把预算翻倍重试，同时用 prompt 压硬性字数上限（§6.4） |
| LLM 音标错误 | 学习到错误发音 | **已解决**（M3）：音标一律以词典为准，且 `_clean_dict_result` 会用词典结果**覆盖**模型输出 —— 代码层保证，不依赖 prompt |
| 变形词查到的是自己的读音 | 词条是原形、音标是变形词，读起来是错的（`preserve` 配 `/pɹəˈzɜːvd/`） | **已解决**（M4.1）：只要 `lemma != surface` 就**无条件**回查原形（M3 的「查不到才回查」不够，`preserved` 在 Wiktionary 有页）；升 `PROMPT_VERSION` 让旧缓存失效（§7.7） |
| LLM 回「合法但全空」的 JSON | 空卡片写进库并进缓存，重划也修不好 | **已解决**（M3）：按任务定义「什么才算有内容」，不达标走重试、重试用完报 502；缓存命中路径同样校验，历史空结果不算命中（§6.3） |
| 划词后页面自行滚动把浮层顶掉 | 浮层闪一下就消失，用户点不到 | **已解决**（M3）：图片加载会触发滚动锚定，改为按偏移跟随正文重新定位，而不是「一滚就关」（§7.7） |
| 痕迹被删导致 AI 流水写不进 | 整次 AI 结果连同痕迹一起丢（需求 5 断档） | **已解决**（M3）：写日志前确认 `highlight_id` 仍在，不在就置空（§6.2） |
| 划词偏移算错 | 高亮错位、痕迹脏数据 | **已解决**（M3）：DOM 偏移 + `blocks.text` 切片双重校验；后端再校验一次，偏差过大直接 422；跨块选区拒绝 |
| 长任务等待被当成卡死 | 用户以为程序挂了 | **已解决**（M3）：explain 按耗时切换阶段文案并显示秒数；结果落缓存，第二次即秒回 |
| 变形词挂着变形词自己的读音 | 生词本读起来是错的（`preserve` 配 `/pɹəˈzɜːvd/`） | **已解决**（M4）：只要 `lemma != surface` 就回查原形，查到就用原形的并记 `phonetic_of`；`PROMPT_VERSION` 升到 m4.1 让旧缓存失效（§7.7） |
| 冗余列 `COALESCE` 把坏值永久钉住 | 第一次查到的错音标再也修不回来 | **已解决**（M4）：两套口径 —— 音标系列（用户改不了）**新值优先**；释义/批注（用户能改）**旧值优先**只补空 |
| 嵌套高亮被更长的那条整段吃掉 | 「跳回原文闪一下」在最有用的场景下失效；长句还会把里面的词吃掉，看不出哪个词查过译 | **已解决**（M4 + 本轮）：`splitByHighlights` 改成按边界切段 + 优先级归属，优先级从「起点早者优先」改为**「区间短者优先」**（负数 id 的定位标记仍最优先）—— 嵌套时内层才是更具体的那条（§7.1.1） |
| 只划中词里几个字母被当成独立单词 | 卡片给的是给「a」的释义，还会自动进生词本、落一条错痕迹 | **已解决**（本轮）：`selection.ts::snapToWholeWord` 把词内片段吸附到整个词，卡片标注「已按整词查」。判据保守（只处理同一个词内、自己不含空格的片段；标点不算词内字符，所以选 `China` 不会被吸成 `China's`），跨词选区原样保留（§7.2） |
| 关掉浮层 / 面板后那行字还是「选中」状态 | 用户以为关不掉，只有重新划一次才消失 | **已解决**（本轮）：① 关浮层时连浏览器原生选区一起收 —— 实测点空白处浏览器会自己收、**点按钮不会**；② `mark[data-active]` 那圈蓝框改成「关面板 / 点别处 / 新划一段」都收，不再只有换一条解析才移动（§7.7） |
| 单词与短语/句子底色一样 | 解析过一条长句之后再划句里的词，看不出来哪个词查过译 | **已解决**（本轮）：`--hl-word` 与 `--hl` 两档底色 + `mark[data-kind]` 分流，阅读页标题下给图例；嵌套时内层（词）赢（§7.4 / §7.1.1） |
| FTS5 关键词把查询语法搞崩 | 搜「a OR b」直接 500 | **已解决**（M4）：每个片段加引号变成前缀短语 `"or"*`，运算符退化回普通词 |
| 检索片段里的抓取内容当 HTML 执行 | XSS | **已解决**（M4）：`safeSnippet()` 先整段转义，再把 `&lt;mark&gt;` 还原成真标签 —— 白名单只有一个标签 |
| Anki 认不出导出文件 | 整份 CSV 被当成普通笔记导进去 | **已解决**（M4）：首行严格以 `#separator:Comma` 开头，**不写 BOM**；首行之后不再写表头行 |
| 迁移索引在脏库上崩掉 | 老库缺列 / 有重复行时 `init_db` 整体失败，服务起不来 | **已解决**（M4）：唯一索引不由 `schema.sql` 建，改由 `_migrate_m4()`「先加列、先去重，再建索引」 |
| M5 迁移在旧库上建索引过早 | `idx_vocab_due` 依赖 `srs_suspended`/`srs_due` 两列，列还没补上就 `CREATE INDEX` 会直接报错 | **已解决**（M5）：`_migrate_m5()` 先 `ALTER TABLE` 补 9 个 `srs_*` 列，**再**建索引；「给存量词补排期」只在**首次** M5 迁移时跑（判据：`srs_interval` 列此前是否存在），否则每次启动都把用户排好的 `due` 冲掉 |
| 流式把事件循环堵死 | `ai.run` 是同步的（httpx + Playwright sync），在 async generator 里 `await` 会卡住所有并发请求 | **已解决**（M5）：`run_in_executor` 跑 worker + `queue.Queue` 传片段，async generator 只做搬运（§6.6） |
| 长首字等待被网关掐断 | `explain` 首字可能等十几秒，中间无字节时反向代理先断连 | **已解决**（M5）：每 12 s 发一个心跳注释帧 `: ping`；前端「模型还在想」的提示由 `reasoning_content` 回调驱动，不靠数据帧 |
| SSE 帧被换行劈开 | 模型吐多行文本时，`data:` 只发了一行，下一行被当成新帧 → 前端解析失败 | **已解决**（M5）：`_frame()` 用 `json.dumps` 把换行转义成 `\n`，`data:` 恒为一行 |
| 用户关页面丢掉 AI 结果 | 需求 5 要「AI 协助过的内容全部记录」，断连即丢等于漏洞 | **已解决**（M5）：`CancelledError` 时不停 worker，结果照常清洗/落库/入册，只是没人接 delta |
| 流式与一次性结果不一致 | 同一个问题两个端点给两份答案，用户不知道该信哪个 | **已解决**（M5）：流式不做第二套编排，复用 `ai.run` + `llm.chat_json(on_delta)`；验收脚本断言 `done.result` 与一次性端点**逐字相同**，且共用同一份 `input_hash` 缓存 |
| 一次 `explain` 触发几百次重渲染 | 页面在流式过程中反而变卡，流式白做 | **已解决**（M5）：增量按 `requestAnimationFrame` 节流（`pushDelta`），chunk 攒进 ref 一次落地 |
| `again` 把间隔清零导致用户不敢点 | 一次想不起来就从头来，用户宁愿空着也不点「忘了」，复习数据全失真 | **已解决**（M5）：review 阶段 `again` → `interval = max(1, interval * 0.5)` **折半而非归零**；`lapses` 单独记，`ease` 也只在 review 阶段调 |
| 队列快照让清空态显示「下次到期：—」 | 最后一张卡评完时提示「没有下次」 | **已解决**（M5）：`queue.next_due` 是装载时算的，前端另存 `soonestDue` 从评分响应取 `min(due)`，与 `next_due` 取较早者 |
| 卡片例句出处与 `vocab.article_id` 分叉 | 卡上写的出处是错的 | **已解决**（M5）：`vocab._sentence_sources()` 按「优先当前筛选篇、否则最早篇」的同一套排序批量查出真出处 |
| 验收脚本污染真实学习数据 | 跑一次验收就把用户排好的 SRS 进度冲乱 | **已解决**（M5）：`smoke_ui_m5.py` 先 `quarantine()`（把真实卡暂停并快照），造一次性卡验收，`finally` 里 `unquarantine()` 还原 |
| 明明有发音却「没有喇叭」 | 词典端点给了录音，界面却不显示播放按钮 —— 用户直接判定功能没做 | **已解决**（M6）：`{{audio}}` 的口音在新式 Lingua Libre 文件里写在具名参数 `a=Southern England` 里（不在文件名里），旧实现判不出来就整条丢弃。现在单独读具名参数 + 地名标签归类 + 第三槽兜底（§6.1.1） |
| 分不出口音的录音被丢掉 | 澳音、爱音、印音全都不显示喇叭，覆盖率凭空掉一大截 | **已解决**（M6）：加 `audio` 通用槽，前端按 `us → uk → audio` 取用。**分不出口音不等于没有录音**，硬塞英美是数据层撒谎，丢掉是回到原 bug |
| 旧的词典缓存把空发音钉住 | dict_cache 是永久缓存，修了解析器也修不好已查过的词 | **已解决**（M6）：加 `parser_ver` 列，版本不符即视为未命中、重取；查哪个词顺手修哪个词，不做全表回填 |
| 音频文件名带空格括号 | `Special:FilePath/LL-Q1860 (eng)-….wav` 裸括号取不到文件 | **已解决**（M6）：`quote(fname, safe="")` 转义后再拼 URL（`%20` / `%28` / `%29`） |
| ogg 在 Safari/iOS 放不了 | 苹果设备上点了没反应 | **已解决**（M7）：录音换源到有道 `dictvoice`（直接给 mp3），且**一律本地化到 `data/audio/`**，浏览器只连本机端点 —— 格式与跨域两个问题一起消失。回退源万一给的是 ogg，`AudioButton` 仍有失败态（按钮变「音频不可用」）（§6.1.1） |
| 有道 `dictvoice` 是非官方接口，批量拉会触发风控 | 整个发音功能被掐，且可能牵连 IP | **已解决**（M7）：`audio._Gate` 全局串行 + 3 s 间隔 + 0～1.5 s 抖动 + 退避重试（最多 2 次）+ 连续失败 5 次熔断 10 分钟（期间走回退源）+ 只下用户真正查过的词（不预热、不扫生词本）（§6.1.1） |
| 把「网络抖动」当成「这词没录音」写进负缓存 | **那个词永远哑掉**，且日志里看不出任何异常 | **已解决**（M7）：三态严格分开 —— 有道 `500`/`404` 才算**确定的否定**（`none`，可永久负缓存、不重试）；超时／连接失败／HTML 错误页只是**这次没成**（`fail`，落 `pending` 以后还能试）；只有全部候选都 `none` 才写 `missing`（§6.1.1） |
| 查词被下载阻塞 | 划词浮层转圈十几秒，体验比没有发音更差 | **已解决**（M7）：`audio.plan()` 只登记候选 + 排后台线程，**不阻塞**查询；首次查词 `audio_ready=false` 但 `audio` 地址已给，播放端点会阻塞到文件落地为止（§6.1.1） |
| 一次性生词的音频文件没有台账 | 清完探针行后 `data/audio/` 残留孤儿文件 | **已解决**（M7）：验收脚本 `purge_probes()` 在清表后按「没有任何 DB 行指向它」删除文件；正常路径靠内容 sha256 命名（`<16位>.mp3`），换源重下或两词共用同一份录音都不产生重复文件 |
| 音标展示口径分叉（生词本取英音、词卡取美音） | `help` 这类英美音标不同的词，两处显示不一致，用户不知道该信哪个 | **已解决**（M7）：全库只留 `dictionary.primary_phonetic(uk, us)` 一处口径（**美音优先**），`vocab`/`search`/`summarize` 共 5 处全部改调它。`smoke_m4.py` 曾报「生词本存的音标与词典一致」失败，就是这个 bug |
| LLM 成本与延迟 | 反复点击烧钱 | 内容哈希缓存 + 结构化输出 + 词典优先 |
| 版权 | 法律风险 | 仅本地个人学习使用，不做分发；保留原文 URL 与来源标注 |
| 偏移量漂移 | 高亮错位 | 偏移锚在不可变的 `blocks.text` 上；块内容变更时重算或失效标记 |
| 站点结构变更 | 提取质量下降 | per-domain 选择器配置化，不改代码 |

---

## 11. 已定项与待定项

### 已定

| 项 | 结论 |
|---|---|
| LLM 供应商 | DeepSeek，模型 `deepseek-flash`，走 OpenAI 兼容端点（httpx 直连，不引 `openai` 包） |
| 词典 API | **Wiktionary**（`action=parse` 解析 `{{IPA}}` 模板）。原定 dictionaryapi.dev 已失效，见 §6.1 |
| 技术栈 | Python 3.13 + FastAPI + React 19（详见 §2） |
| MVP 范围 | 直接做完整闭环 M0 → M7 |
| 并发模型 | 抓取走线程池（非 asyncio），因 Playwright sync API 限制；SQLite 短连接 + WAL |
| 图片资产命名 | `data/assets/{article_id}/{sha1(origin_url)[:16]}.{ext}`，扩展名由字节魔数判断 |
| 语言字段 | 页面无 `<html lang>` 时兜底 `en`（六个样本实测全部命中不到） |
| 痕迹保护 | `block_uid` 内容指纹 + `is_stale` 标记，重抓不删块（§4.3） |
| 前端请求层 | TanStack Query（缓存 + 轮询）；GET 再加一层 `localStorage` 兜底做离线（§7.6） |
| 阅读主题 | light/sepia/dark 三套 CSS 变量，偏好只存 `localStorage`，不进后端（§7.4） |
| 前端开发代理 | Vite 把 `/api`、`/media` 反代到 `127.0.0.1:8010`，前端因此同源，免 CORS |
| 前端产物托管 | 构建后由后端同进程直发（catch-all SPA 回退 + `index.html` `no-cache` + `/assets` `immutable`），日常只需起 8010；Vite 5173 保留给「正在改前端」时用（§7.8） |
| 前端包管理 | **npm**。pnpm 11 在本机会把 `@types/react`、`@types/react-dom` 装成空目录（`.ignored_*`），tsc 因此报 TS2688，换 npm 解决 |
| 进度锚点优先级 | 恢复时 `last_block_id` 优先于 `scroll_pct`（懒加载图片会改变页面总高，§7.5） |
| 划词自动落库 | 划词即写 `highlights`，不等用户确认（需求 5）。同区间幂等，删痕迹不删 AI 历史（§7.2） |
| 词自动查、句不自动查 | 划中单个词立刻查词典 + 调模型；选段只给按钮，点了才调（省 token，防误触，§7.2） |
| 划词只认整词 | 落在词内部的片段由 `snapToWholeWord` 吸附到整个词（区间、落库、卡片三处一致，卡片标「已按整词查」）。只处理同词内、无空格的片段，标点不算词内字符 —— 半截字母查词典只会得到胡说，还会混进生词本（§7.2） |
| 「单词」的门槛 | 单 token **且含字母**才算 word：`1929`、`——`、`…` 划中也没有词可查，按短语处理（前端 `inferKind` 与后端 `infer_kind` 口径一致，§7.2） |
| 痕迹两档底色 | 单词 `--hl-word`（绿）、短语/句子/段落 `--hl`（黄），`mark[data-kind]` 分流；嵌套时内层赢（§7.4 / §7.1.1）。一类底色时，长句里那些查过译的词根本看不出来 |
| 「选中态」的两条清理 | ① 关浮层 → 连浏览器原生选区一起清（`removeAllRanges`；点按钮浏览器自己不会清）；② `mark[data-active]` 蓝框 = 「面板正在看的这条」，关面板 / 点别处 / 新划一段都收（§7.7） |
| 音标来源强制 | `_clean_dict_result` 用词典结果覆盖模型的 phonetic 字段 —— 代码层兜底，不靠 prompt 自觉（§6.3） |
| `ai_logs` 唯一约束 | **去掉** `UNIQUE(input_hash)`，改普通索引 + 取最新一条复用结果（需求 5 要完整流水，§6.2） |
| AI 结果不进 localStorage | 后端已有 `input_hash` 缓存，本地再存一份会造成「后端换了 prompt、前端还在用旧结果」的错位 |
| 跨块选区 | 直接拒绝。精读的划词/选句都在块内，跨块只会得到被截断的半句话，还多一套偏移 |
| 变形词音标口径 | 只要 `lemma != surface` 就**无条件**回查原形，用原形的音标并记 `phonetic_of=lemma`（仍走词典 API，不碰 LLM）。M4.1 收紧：M3 的「查不到才回查」会让 `preserved` 配着自己的读音进库（§7.7） |
| 浮层跟随滚动 | 不再「一滚就关」，改为按 `blockId + 偏移` 重新量位置（`selection.ts::rectOfOffsets`）。图片加载会触发滚动锚定，一滚就关会让浮层闪没（§7.7） |
| `ai_logs` 写入容错 | 写日志前确认 `highlight_id` 仍存在，不在就置空。否则痕迹被删会让 INSERT 撞外键、整次 AI 结果连同流水一起丢（§6.2） |
| prompt 版本 | 当前 `m8.0`。**改清洗/兜底逻辑也要升**，否则新逻辑被旧缓存盖掉（§6.2）。m4.0 → m4.1 是「音标一律取原形」逼出来的，m4.1 → m8.0 是三操作合并逼出来的 |
| M4 入册规则 | **划过的词全收**。需求 5 的立场本就是「AI 协助过的内容全部记录」；漏收让用户回头找不到，多收一条只是列表长一点 —— 代价不对等。模型的 `worth` 判定只用来排序与筛选（`?worth=1`），**不当作过滤器**（§7.1.1） |
| 生词本拆表 | `vocab` 按 `lemma` 唯一（音标/释义跨文章复用），出现记录在 `vocab_sources`（一篇一词一条）。**「按文章筛生词」只能靠 `vocab_sources`**，`vocab.article_id` 是「首次入册那篇」，拿它筛会漏 |
| 入册是自动的 | 划词时后端自动入册（`_post_persist`），**不提供 `POST /api/vocab`**。手工新增等于承认自动入册漏了词 |
| 复用路径也要入册 | `source=cache` 的复用路径同样走 `_post_persist`：要记的是「在这篇文章里我确实查过这个词」，与模型有没有被调用无关。入册失败只 warning，不丢已到手的结果 |
| 变形词音标口径 | **一律取原形**（`preserved` → `/pɹəˈzɜːv/` of=`preserve`），不是变形词自己的读音。只要 `lemma != surface` 就回查原形；词典两边都没有才留空。锚点是 `phonetic_of or lemma` |
| 音标显示层 | 界面把 `ɹ` 显示成 `r`（学习词典惯例），**数据层不动** —— `dict_cache`、接口、生词本存的仍是词典原文，红线 3 的比对锚点就是它（§7.1.1） |
| 冗余列更新口径 | 分两类：音标系列（用户改不了）**新值优先**；释义/批注（用户能改）**旧值优先**只补空。否则第一次查到的坏音标会被 `COALESCE` 永久钉住 |
| 清单是纯聚合 | 汇总**不再补 LLM 调用**。语法归类在入册时做（`normalize_category`），去重靠 `idx_grammar_dedupe`（§6.5） |
| 语法清单只收解析的 `grammar_points` 小节 | **不混入 `parts`** —— 那是「这一句怎么读」的逐句拆解（主干挂靠、从句修饰谁），换个句子就没用了；`grammar_points` 才是可复用的结构条目，入册走 `grammar.persist_points()`（M8 前这条写的是「只收 `grammar` 任务」，入口换了、边界没换） |
| 选段只有一个动作：解析 | M8 把 `translate` / `explain` / `grammar` 合成一个。译文与结构拆解本来是一件事的两面；分成三个按钮只会让用户先猜「我这次想要哪种」，而答案通常是「都要」（§6.7） |
| 旧任务端点删除而非保留 | `/api/ai/translate`、`/api/ai/grammar` 直接删掉，不做 301/兼容层：留着等于承认「解析」不是唯一入口。旧流水还在 ai_logs 里，前端按字段形状只读回看（§6.7） |
| 解析内部仍叫 `explain` | 不改成 `parse`：旧流水的 `prompt_type` 就是 `explain`，改名会让新旧记录失去同一个检索键。界面叫【解析】，内部名字无所谓 |
| 解析的输出顺序即成本补偿 | 合并后一次解析比旧的「翻译」贵得多，靠**流式 + 产出顺序**（译文 → 主干 → 附加成分 → 语法要点）找回来：只想看意思的人等前几秒就够，不必再留一个「只翻译」按钮 |
| 检索三组一起返回 | 不拆三个端点：用户输一次，期望一次看全。查询串先清洗（抽字母/数字/汉字片段 → 带引号的前缀短语），中文走 `LIKE` 兜底（§8） |
| 检索片段转义 | `safeSnippet()` 白名单只有一个 `<mark>`：片段来自抓来的正文与模型输出，先整段转义再还原标记 |
| 导出走浏览器原生下载 | `<a download>`，不用 fetch + Blob —— 保留 `Content-Disposition` 的重命名行为，也便于验收脚本观察 download 事件 |
| 深链优先于位置还原 | `?block=&start=&end=` 比 `last_block_id` 优先。定位痕迹优先复用区间一致的真实痕迹，没有才合成 `id=-1` 的（不进库、不可点、刷新即消失），两者都闪两下（§7.1.1） |
| 唯一索引不在 `schema.sql` 建 | `idx_vocab_status` / `idx_grammar_dedupe` 由 `db._migrate_m4()` 建：老库缺列或有重复行时，直接 `CREATE UNIQUE INDEX` 会让 `init_db` 整体崩掉，服务起不来 |
| SRS 档位 | **四档** `again/hard/good/easy`（Anki 语义），不做教科书 SM-2 的六档：手机上是点按，六个按钮的区分度用户自己也说不清，统计也更脏（§7.1.2） |
| SRS 两阶段 | `srs_state ∈ {new, learning, review}`。learning 走分钟级两步 `(1, 10)` 后毕业起手 1 天；`easy` 可跳步直接毕业起手 4 天；review 阶段按 `interval × ease` 推进，ease 夹在 `1.3 ~ 3.2`，间隔上限 365 天 |
| `srs_state` 与 `vocab.status` 并行 | **故意不合并**。前者是算法进度（服务端独写），后者是用户主观标记（M4 可改）；合并成一套会让「用户标了 known」和「算法排到明天」互相覆盖 |
| `again` 折半而非归零 | review 阶段 `interval = max(1, interval * 0.5)`。一次清零会让用户宁愿卡着也不点「忘了」，反而让复习数据失真（`lapses` 另记） |
| `reps` 的计数口径 | **只数成功复习**：new→learning 不加，毕业 +1，review 每次 `good/hard/easy` +1，`again` 不加。否则「忘了」也会让 `reps` 涨，数字失去意义 |
| 新词限量 | 队列对 `srs_state='new'` 的词单独限量（默认 20），**到期旧词不限量** —— 旧词是「欠的债」（不还就一直欠着），新词是「可选的」（每天加一点就够） |
| 复习流水独立成表 | `review_logs` 只增不改，是「复习效果」的唯一凭据；`vocab.srs_*` 视为从流水算出来的缓存。删卡片走 `ON DELETE CASCADE`，但两张表的职责边界不模糊 |
| SSE 不改编排 | 流式只是 `llm.chat_json(on_delta)` + `ai.run(on_delta)`，清洗/校验/落库/缓存/入册全部复用原路径。**目录里没有 `services/stream.py`**，也不新增第二套结果处理（§6.6） |
| SSE 用线程 + 队列 | `ai.run` 是同步的，async generator 里直接 `await` 会阻塞事件循环。`run_in_executor` 跑 worker + `queue.Queue` 传帧；`CancelledError` 时**不停** worker（需求 5 要留痕） |
| 前端手动切 SSE 帧 | 不用 `EventSource`（只能 GET、带不了 body）。`fetch` + `ReadableStream` 手动解析（`lib/stream.ts`）；未收到 `done`/`error` 即报「连接中断」 |
| `PROMPT_VERSION` 现在是 `m8.0` | m4.1 → m8.0 是「三操作合并」逼出来的：解析的产出整体换了形状（`translation/skeleton/parts/grammar_points`），不升版本号，旧 explain 缓存会冒充新结构返回。**M5–M7 期间它一直停在 `m4.1`**，因为那三个里程碑没改 prompt，也没改结果内容 —— 乐观升版本会让全站缓存凭空失效、白烧一遍 token |
| 暂停 ≠ 删词 | 暂停只改 `srs_suspended`，词仍在生词本里、仍在检索里。用户「暂时不想复习」和「这个词我不记了」是两件事，后者走生词本的移除 |
| 发音三槽制 | `audio_uk` / `audio_us` 装**分得出英美**的录音，`audio` 装分不出的（澳音、爱音…）。前端 `us → uk → audio` 取用。**分不出口音不等于没有录音**，不能因为归不了类就丢掉（§6.1.1） |
| 口音标签的判据顺序 | 先 `a=` 具名参数里的音系标签（`RP`/`GA`），再地名词表（`Southern England`/`Texas`），最后才是文件名前缀。反过来会漏掉所有 Lingua Libre 的新式文件 |
| `dict_cache` 有 `parser_ver` | 原本「永久缓存」的前提是「音标是客观事实不会变」，但**解析口径会变**。版本不符即当未命中重取，与 `PROMPT_VERSION` 同一思路。缓存会不会变取决于产出逻辑会不会变，不是取决于事实会不会变 |
| 发音源：有道优先、词典回退 | `dict.youdao.com/dictvoice?audio=<w>&type=2` 实测直接给 mp3、无需 key、覆盖率明显高于 Wiktionary（连 `flibbertigibbet` 都有）。Wiktionary 那张录音降为**回退**，不做删除（§6.1.1） |
| 发音一律美式 | ~~需求明确。回退候选排序也照此办理：**美音 → 分不出口音（`other`）→ 英音**。英音**垫底而非丢弃** —— 有录音总比没声音强，但 `accent` 如实标注、界面照实显示 `🔊 英`~~ **已被 M10 推翻**：改成英美双槽、回退候选按**请求的口音**排序（要英音就先排英音）。「分不出口音垫在中间、明确不匹配的那侧排最后」这条仍然有效 |
| 音标与录音彻底分家 | 音标仍只认 Wiktionary（红线 3，绝不让 LLM 编），录音走有道。两者互不蕴含：专名常有录音却没 IPA，冷僻词常有 IPA 却没人录过。捆在一起只会两败俱伤（§6.1.1） |
| 音频一律本地化 | 与 M3 的「直接播 Commons 原始 URL」相反，**推翻**。浏览器只连 `GET /api/dict/{word}/audio`，页面零外链。顺带解决 ogg/Safari、离线、防盗链三个问题。落盘 `data/audio/<sha256(bytes)[:16]>.<ext>`，内容寻址 ⇒ 换源重下或两词共用同一份录音都不产生重复文件（§6.1.1） |
| `audio_cache` 与 `dict_cache` 分开 | 前者是**下载事务的状态机**（有失败、重试、熔断、负缓存），后者是**解析结果**（可永久缓存 + `parser_ver`）。合并会让「解析对了但下载失败」和「解析失败」混成一格，两人的失效条件也完全不同（§4.1） |
| 音频「有没有」分三态 | `ok`（拿到）／`none`（**明确的否定**，可永久负缓存、不重试）／`fail`（超时、连接失败、HTML 错误页，落 `pending` 还能再试）。只有**所有**候选都 `none` 才写 `missing`。混为一谈会让一次网络抖动把词**永远弄哑**，而且日志上完全看不出来（§6.1.1） |
| 下载要隐蔽 | `dictvoice` 是非官方接口，批量拉会触发风控。六条：全局串行闸门（并发是风控第一特征）／3 s 间隔 + 0～1.5 s 抖动（固定节拍本身就是机器特征）／退避重试 2s→4s→8s 最多 2 次／连续失败 5 次熔断 600 s 期间走回退源／负缓存不重试／**只下用户真正看过的词**（不预热、不扫生词本批量补，流量形状跟人看文章一致） |
| 查词不等下载 | `audio.plan()` 只登记候选 + 排后台 daemon 线程，**不阻塞**划词浮层。首次查某词 `audio_ready=false` 但 `audio` 地址照给，播放端点会阻塞到文件落地。反过来「查词即下载」会让浮层转十几秒，比没有发音更差 |
| 音标展示口径：美音优先 | 全库只留 `dictionary.primary_phonetic(uk, us)` 一处（返回 `us or uk`），`vocab`／`search`／`summarize` 共 5 处全部改调它。之前生词本取英音、词卡取美音，`help` 这类词两处显示不一致 —— M7 回归时被 `smoke_m4.py` 抓出，是真 bug |
| `audio_cache` 不带 `parser_ver` | `dict_cache` 需要版本号是因为**解析口径会变**；下载没有「口径」，一个 mp3 要么下到了要么没有。它的失效条件是风控/换源，走 `?refresh=1` 或熔断恢复，不是版本号（§4.1） |
| 音频端点的 URL 转义 | 词先 `normalize_word` 再 `quote`（`quote(word, safe="")`）后拼进 `dictvoice`。带空格的词组不转义会直接 400 |
| 播放端点不写进 OpenAPI | `include_in_schema=False`。它返回的是**字节流**而非 JSON，放进契约表只会干扰；前端由 `AudioButton.tsx::audioUrl()` 一处拼装 |
| 标题也是块，不是渲染技巧 | 标题原来只在 `articles.title` 里，所以划不了词。要让它能划，就必须进「偏移只有 `blocks.text` 一套」这套坐标系（红线 2）—— 在 `h1` 上挂个假 id、渲染层再补一层，等于给标题开第二套坐标，迟早对不上（§7.9） |
| 标题块的类型单独用 `title` | 不复用 `heading`：前端要把它画成页面标题（不是正文小标题的样式与间距），导出侧 `# 标题` 已经写过一行。代价是 `buildRenderItems` 要显式跳过它，否则标题渲染两次 |
| 老文章由启动迁移补标题块 | 不补的话，用户手上已有的文章标题依然划不动，得先重抓一遍才生效。`_migrate_title_blocks()` 走**同一个 uid 算法**，所以补的行在下次重抓时会被复用，现在划的痕迹不会断（§7.9）。**M10 已删除该迁移**（连 `_migrate_*` 整套一起）：`data/` 可再生，不必再为历史库保留分支 —— 迁移要服务的只是「已经存在的老库」 |
| 右栏拖宽：松手才写偏好 | 拖动中只改 AiPanel 的局部宽度。偏好每动一像素写一次 localStorage 是浪费；更要紧的是偏好在 App 里，一改就让整个阅读页（含几百个高亮节点）重渲染，手感发涩（§7.9） |
| 右栏拖拽用指针捕获 | `setPointerCapture` 而非窗口监听：拖到面板外甚至窗口外，`pointermove/up` 也照样回来，不会出现「松手了还在拖」。命中区左右各 4px 跨在边框上 —— 只做 1px 等于没有 |
| 解析记录可删（物理删除） | 划词是「选中即翻译」，误操作无法避免，所以每条记录都能删。**不做软删除**：要的是「这条不该存在」，不是「这条藏起来」。删的是这一次 AI 调用 —— `fts_ai` 索引成对删、缓存随之失效（同一段文字再划会重新调模型），但高亮与生词本条目都不动（§6.2 / §8） |
| 删除入口悬停才显示 | 列表里常驻一排 ✕ 会让界面看起来像「一堆待清理的垃圾」，也跟行内的「缓存 · #123」「时间戳」抢位置。悬停整行/整卡时按钮才浮在右上角（`.er-del-host` + `.er-log-del`），两处列表（右栏、检索结果）共用 |
| **划词不再经过模型**（M10） | 词典释义是客观事实、要可复现，而模型输出会漂移、还得靠缓存去钉住它 —— 拿长处在换短处。换来的是：毫秒级、零外网、零成本、不外泄。**这是一次明确的降级**（词卡的语境义/例句/搭配没有了），需要语境理解时选段【解析】仍在（§6.1） |
| 词典数据用「只读 SQLite + 内存 TSV」 | ECDICT 是 811 MB 的预编译 SQLite，直接 `mode=ro` 查，不导入 `english.db`（330 万行搬一次毫无收益，还让备份变重）；ipa-dict 只有 4.7 MB、只有「词 → 音标」一个键，懒加载进内存即可，**为它建表只是多一个需要同步的产物**（§6.1.1） |
| 音标：ipa-dict 主、ECDICT 兜底 | ECDICT 只有一条音标、覆盖 10%、且是上世纪末的英式写法；ipa-dict 是现代宽式 IPA、英美双轨、**收屈折形式**，实测对常见词覆盖 88%~96%。用主源的质量，保留兜底的覆盖（§6.1.1） |
| `worth` 改本地规则推导 | 原先由模型判「值不值得记」，现在按「考纲标签 → 牛津三千/柯林斯星级 → 词频排名」逐级判定。实测这份库里带标签的只有 1.5 万词，所以必须有后两级兜底；`make` 这种 `zk gk ielts` 的词证明**基础标签必须优先于考试标签**（§6.1.1） |
| `audio_cache` 改复合主键 `(word, accent)` | 一个词 × 一个口音 = 一个可下载对象。靠加列做双口音会变成十来个 `*_uk` / `*_us` 字段。（因为不保历史数据，直接建成复合主键，不写迁移） |
| 音频不预热，点喇叭才下 | 双口音之后预热等于把闸门流量翻倍，而有道最怕流量形状不像人 —— 为「一个可能没人点的喇叭」多下一份是拿风控风险换几秒等待。后台一份都不下，代价只是首次点等几秒（§6.1.2） |
| 删掉 `dict_cache` | 它是为「联网解析 Wiktionary 结果」而生的缓存（连 `parser_ver` 作废机制都是为它加的）。换成本地文件读取后，缓存只剩给自己加失效维度这一个作用 —— 表与读写代码一并删掉（§6.1.1 / §4.1） |
| **M10 不升 `PROMPT_VERSION`** | 红线 4 的口径是「prompt 或产出变了才升」。本次只删掉 dict 任务，`explain` 的提示词与产出逐字未变；升一次版本会让全部历史解析缓存作废、白烧一遍 token，换不来任何东西 |
| 点单词高亮 → 重弹词卡 | 旧实现依赖 `ai_logs`：记录被删、或这个词压根没跑过 AI（M10 之后全部如此），点了就什么都没有。改成当场重查本地词典 —— 反正是毫秒级，且不依赖任何历史 |
| 词典端点契约改成 `WordEntryOut` | 一次返回完整词条 + 两个发音槽 + 入册回执，前端不再需要「GET 音标 + POST 释义」两处拼装。`cached` / `error` 这类字段随之消失：本地读取没有缓存可命中，失败只有「没这个词」与「库没装」两种（§9） |

### 待定

- [ ] 是否需要从浏览器直接提交 URL（浏览器插件 / 油猴脚本）
- [ ] 难度评分算法（词频表 + 句长 + 从句密度）。`explain` 现在让模型直接给 CEFR 等级，够用但不精确
- [ ] M2 阅读器的离线策略：图片已在本地，是否额外缓存一份 blocks 快照到 IndexedDB
- [ ] 渲染站点（41 s/篇）是否值得缓存渲染结果，避免重抓时重复付这个代价
- [ ] 是否给词卡补回「语境义 / 例句」—— 那两栏只有模型能给，M10 拿掉之后是明确的功能退步。可能的形态：默认纯词典，需要一个额外的按钮才调模型（§6.1）
- [ ] ipa-dict 的覆盖盲区（生僻词、部分专名只有单边音标）是否值得再接一个更大的音标源（kaikki 的抽取数据有 20+ GB，性价比要先算清楚）
- [ ] 生词本的「导出为 Anki deck（.apkg）」而不只是 CSV —— CSV 够用，先不做
- [ ] 复习页的「本次会话统计」（评了几张 / 正确率 / 用时）—— `review_logs` 已有全部原料，是纯前端展示，暂时没做
- [ ] 手写卡（不靠划词，自己造一张卡）—— 与「入册必须自动」的立场冲突，待确认需求是否真的存在
- [ ] 词组发音（`in spite of` / `as long as`）—— 有道对含空格的词组时好时坏，
      目前只保证单词。要做得先定策略：整串送 `dictvoice` 还是拆成单词连播
- [ ] 音频的自清理（`data/audio/` 只增不减）—— 内容哈希命名 ⇒ 换源重下会留下旧版本，
      但正常使用一个词只会下一份。要做也应做成显式的手动清理，不做后台定时删（会踩到正在播的文件）

### 已完成（曾列入待定）

- [x] 间隔复习（SRS）—— M5 落地，四档 SM-2 变体 + 暂停/恢复 + 流水 + 统计
- [x] explain 的 SSE 流式输出 —— M5 落地，与一次性端点共用同一份结果与缓存
- [x] 发音音频换源（M6 调研结论 → M7 落地）：定案**有道 `dictvoice` 优先 + Wiktionary 回退**，
      不再考虑「服务端 ogg 转 mp3」这条路 —— 换源既解决 Safari 也解决覆盖率，转码只是打补丁（§6.1.1）
- [x] 音频本地化（M7 落地）：推翻 M3 的「直接播 Commons 原链」，改为 `data/audio/` 内容哈希存盘 + 本机端点。
      落盘前用魔数嗅探真实格式（`ID3`／`OggS`／`RIFF`／`fLaC`／`webm`，裸 mp3 靠帧同步字 `0xFF 0xE*`），
      不信 URL 后缀 —— 回退源给的文件名与真实格式经常对不上
- [x] 生词本页 / 复习页的喇叭按钮（M7 落地）：抽出 `components/AudioButton.tsx` 供四处消费方共用
      （划词浮层／AI 词卡／生词本行／复习卡正面）。复习卡把喇叭放在**正面** ——
      正面本来就摆音标，听一遍不泄露释义
- [x] 引入 ECDICT 本地词库（M10 落地）：不再只是「音标离线兜底」，而是**释义的唯一来源**；
      音标另用 ipa-dict 补齐英美双轨。划词全程零外网、毫秒级、不烧 token（§6.1）
- [x] 英美双口音（M10 落地）：音标两栏 + 两个喇叭各下各的。数据侧 `audio_cache` 改
      `(word, accent)` 复合主键；交互侧砍掉预热、改成点击才下（§6.1.2）
