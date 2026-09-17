"""抓取服务：URL → 正文 blocks + 本地化图片。

由 M0 验证脚本（scripts/fetch_once.py）提升而来，差异只在于：
  - 不写 Markdown / JSON，改为返回结构化结果，落库交给 ingest.py
  - 原始 HTML 与图片按 asset_key 落到 data/ 下由配置决定的目录
  - 配置项全部来自 app.config.settings，不再有模块级常量
  - 日志通过回调输出，便于同时写进 article.error_msg 或服务日志

提取路径优先级（docs/DESIGN.md §5.1 的四条实测结论）：
  1. Playwright 渲染 —— 唯一能同时拿到「图片 + 图注 + 正确位置」的方式，
     客户端渲染站点（Sixth Tone）必须走这条
  2. __NEXT_DATA__ —— Next.js 站点的结构化正文与图集，但图无位置信息
  3. per-domain 正文容器 —— 命中后容器内容即正文，不能再过 trafilatura
     （实测它会把 <figure> 连图注一起丢掉）
  4. trafilatura 通用提取 —— 精度优先，不足再降级召回优先
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import trafilatura
from selectolax.parser import HTMLParser

from ..config import settings

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
CONTAINER_TAGS = {"div", "section", "article", "main", "span"}
IMG_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-echo", "data-url")
DROP_TAGS = {"script", "style", "noscript", "iframe", "form", "nav", "aside", "footer", "header"}

# 站点私有正文容器：命中后先裁剪 DOM，再交给 trafilatura，
# 避免把侧栏 / 推荐位 / 分享按钮吃进正文。值为候选选择器，按顺序尝试。
CONTENT_SELECTORS: dict[str, list[str]] = {
    "chinadaily.com.cn": ["#Content", "#lft-art", "div.article"],
    "sixthtone.com": [],          # 走渲染路径，静态容器规则留空
}

# 装饰性图片黑名单：分享图标、logo、二维码、占位图、埋点像素
IMAGE_SKIP_PATTERNS = re.compile(
    r"(facebook|twitter|linkedin|instagram|weibo|weixin|whatsapp|telegram|reddit"
    r"|/share|sharing|qrcode|qr_code|/logo|logo\.|/icon|icon\.|sprite|placeholder"
    r"|blank\.|spacer|pixel|1x1\.|/image_e/|\.svg$)",
    re.I,
)

# 用 class 特征把小标题从正文段落里认出来。
# 静态与渲染 DOM 里的小标题常是 <p class="subtitle"> 而非 <h2>。
SUBHEAD_CLASS_RE = re.compile(
    r"(sub-?title|sub-?head|section-?title|article-?title|title-[0-9]|heading)", re.I
)

NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# 渲染后从 DOM 里挑正文容器：优先以封面图为锚，退化为「直接子 <p> 最多」的容器
PICK_CONTAINER_JS = r"""
() => {
  const cover = document.querySelector('figure[class*="coverImgBox"], figure[class*="cover"]');
  let box = cover ? cover.parentElement : null;
  if (!box) {
    const cands = [...document.querySelectorAll('article, main, section, div')]
      .map(el => ({el, n: el.querySelectorAll(':scope > p').length}))
      .filter(x => x.n >= 3)
      .sort((a, b) => b.n - a.n);
    box = cands.length ? cands[0].el : document.body;
  }
  return box ? box.innerHTML : '';
}
"""

_noop: Callable[[str], None] = lambda _msg: None
_host_lock = threading.Lock()
_last_hit: dict[str, float] = {}


# ---------------------------------------------------------------- 基础工具

def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\xa0", " ").replace("\u200b", "").replace("\u200e", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_tags(html: str) -> str:
    """不依赖 DOM 的粗粒度去标签，仅用于字数统计等阈值判断。"""
    return clean_text(re.sub(r"<[^>]{0,400}>", " ", html or ""))


def canonical_url(url: str) -> str:
    """去掉 fragment 与跟踪参数，作为查重键。"""
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(q), ""))


def slug_of(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.replace("www.", "")
    path = re.sub(r"[^A-Za-z0-9]+", "-", p.path).strip("-")
    return f"{host}-{path}"[:110]


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))


def polite_wait(url: str) -> None:
    """同域名最小请求间隔，礼貌抓取。"""
    host = urlparse(url).netloc
    gap = settings.fetch_rate_limit
    with _host_lock:
        prev = _last_hit.get(host)
        now = time.monotonic()
        if prev is not None and now - prev < gap:
            time.sleep(gap - (now - prev))
        _last_hit[host] = time.monotonic()


def make_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(settings.fetch_timeout),
        headers={"User-Agent": settings.fetch_ua, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
    )


def should_skip_image(url: str, alt: str = "") -> str | None:
    """装饰图 / 分享图标 / 占位图 → 返回原因，正常图返回 None。"""
    u = (url or "").lower()
    if not u or u.startswith("data:"):
        return "empty"
    if IMAGE_SKIP_PATTERNS.search(u):
        return "decorative"
    if re.search(r"(icon|logo|二维码|分享|share)", alt or "", re.I):
        return "decorative"
    return None


def need_render(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host.endswith(d) for d in settings.render_domain_set)


# ---------------------------------------------------------------- 渲染 / Next.js

def render_html(url: str, timeout_s: int | None = None) -> str | None:
    """用无头浏览器渲染页面，返回正文容器的 HTML（图片、图注、顺序都在里面）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    timeout_s = timeout_s or settings.render_timeout_s
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=settings.fetch_ua, viewport={"width": 1400, "height": 1000})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            page.wait_for_timeout(settings.render_wait_ms)
            for _ in range(10):          # 滚动触发懒加载图片
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(400)
            page.wait_for_timeout(1200)
            html = page.evaluate(PICK_CONTAINER_JS)
            browser.close()
        return html or None
    except Exception:  # noqa: BLE001
        return None


def nextjs_article(html: str) -> dict | None:
    """Next.js 站点：正文与图集分离在 __NEXT_DATA__ 里，直接取结构化数据。

    实测 Sixth Tone 的 SSR HTML 中正文图完全不出现（客户端 JS 渲染），
    但 __NEXT_DATA__.props.pageProps.detailData.data 里有：
      - content       正文 HTML（只有文字，0 个 <img>）
      - textImageList 图集，含 url / width / height / desc（完整图注）
    """
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None

    dd = ((blob.get("props") or {}).get("pageProps") or {}).get("detailData")
    if not isinstance(dd, dict):
        return None
    d = dd.get("data")
    if not isinstance(d, dict):
        return None
    content = d.get("content")
    if not isinstance(content, str) or len(content) < 200:
        return None

    images = []
    for it in d.get("textImageList") or []:
        if isinstance(it, dict) and it.get("url"):
            images.append({
                "url": it["url"],
                "alt": clean_text(it.get("desc") or ""),
                "width": it.get("width"),
                "height": it.get("height"),
            })

    authors = [a for a in (d.get("authorList") or []) if isinstance(a, dict)]
    return {
        "content_html": content,
        "images": images,
        "title": d.get("name"),
        "date": d.get("pubTime"),
        "author": authors[0].get("name") if authors else None,
        "cover": d.get("headPic") or d.get("bigPic"),
    }


def fill_image_slots(blocks: list[Block], images: list[dict]) -> tuple[list[Block], list[str]]:
    """把与正文分离的图集按占位段落顺序插回正文。

    占位不足时，余下的图追加到文末并计入 leftover（调用方据此告警）。
    """
    slots = [i for i, b in enumerate(blocks) if b.type == "placeholder"]
    if not images and not slots:
        return blocks, []

    used_slot = set(slots[: len(images)])
    out: list[Block] = []
    it = iter(images)

    for i, b in enumerate(blocks):
        if b.type == "placeholder":
            if i in used_slot:
                im = next(it)
                out.append(Block(order=0, type="image", origin_url=im["url"], alt=im.get("alt") or ""))
            continue
        out.append(b)

    leftover = [im["url"] for im in it]
    for u in leftover:
        out.append(Block(order=0, type="image", origin_url=u, alt=""))

    for j, b in enumerate(out):
        b.order = j + 1
    return out, leftover


def attach_captions(blocks: list[Block], captions: dict[str, str]) -> list[Block]:
    """给图片补图注。

    Next.js 站点的图注（textImageList[].desc）不在正文 DOM 里，
    但它是完整的英文描述，对精读很有价值，所以按图片 URL 匹配补回来。
    """
    if not captions:
        return blocks
    out: list[Block] = []
    used: set[str] = set()
    for b in blocks:
        out.append(b)
        if b.type == "image" and b.origin_url:
            key = urlparse(b.origin_url).path
            cap = captions.get(key)
            if cap and key not in used:
                used.add(key)
                out.append(Block(order=0, type="caption", text=cap))
    for j, x in enumerate(out):
        x.order = j + 1
    return out


def narrow_html(html: str, url: str) -> tuple[str, str]:
    """按域名配置裁剪到真实正文容器。返回 (HTML, 命中的选择器)。"""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    sels = CONTENT_SELECTORS.get(host, [])
    if not sels:
        for k, v in CONTENT_SELECTORS.items():
            if host.endswith(k):
                sels = v
                break
    if not sels:
        return html, ""
    tree = HTMLParser(html)
    for sel in sels:
        try:
            node = tree.css_first(sel)
        except Exception:  # noqa: BLE001
            continue
        if node is not None and len(clean_text(node.text())) >= 200:
            return node.html or html, sel
    return html, ""


def clean_sitename(name: str, url: str) -> str:
    """元数据里的站点名常见脏值（如 '#Sixthtone'），清洗并兜底为域名。"""
    s = re.sub(r"^[#@\s|·\-]+", "", (name or "")).strip()
    s = re.sub(r"\s*[|·]\s*$", "", s).strip()
    if len(s) < 3:
        return urlparse(url).netloc.replace("www.", "")
    return s


def meta_content(html: str, *keys: str) -> str:
    """按顺序取第一个命中的 <meta property|name=... content=...>，兼容属性顺序颠倒。"""
    for k in keys:
        for pat in (
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(k)}["\'][^>]*?content=["\']([^"\']*)',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']{re.escape(k)}["\']',
        ):
            m = re.search(pat, html, re.I)
            if m and m.group(1).strip():
                return clean_text(m.group(1))
    return ""


def _norm_key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def drop_duplicate_title(blocks: list[Block], title: str) -> list[Block]:
    """正文首块若与网页标题重复，丢弃，避免 Markdown 里标题出现两次。"""
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())  # noqa: E731
    t = norm(title)
    if not t:
        return blocks
    out = list(blocks)
    for i, b in enumerate(out[:3]):
        if b.type == "heading":
            bt = norm(b.text)
            if bt and (bt == t or bt in t or t in bt):
                out.pop(i)
                break
    for j, b in enumerate(out):
        b.order = j + 1
    return out


def article_headers(url: str) -> dict[str, str]:
    p = urlparse(url)
    return {
        "User-Agent": settings.fetch_ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "Referer": f"{p.scheme}://{p.netloc}/",
    }


# ---------------------------------------------------------------- 数据结构

@dataclass
class Block:
    order: int
    type: str                 # heading|para|quote|list|image|caption|table|video|placeholder
    text: str = ""
    level: int | None = None
    origin_url: str | None = None
    local_path: str | None = None
    alt: str = ""
    status: str = ""          # 图片下载结果：ok|cached|ok(browser)|failed:xxx
    width: int | None = None
    height: int | None = None

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("order", None)
        d.pop("status", None)
        return d


@dataclass
class Report:
    url: str
    canonical_url: str
    slug: str
    status: str = "pending"       # done|partial|failed
    http_status: int | None = None
    content_type: str = ""
    html_bytes: int = 0
    extractor_pass: str = ""
    content_selector: str = ""
    image_placement: str = ""
    title: str = ""
    author: str = ""
    date: str = ""
    sitename: str = ""
    language: str = ""
    blocks_total: int = 0
    blocks_by_type: dict = field(default_factory=dict)
    images_ok: int = 0
    images_failed: int = 0
    images_skipped: int = 0
    images_via_browser: int = 0
    words: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    report: Report
    blocks: list[Block]
    raw_html_path: str = ""      # 相对 data/ 的路径

    @property
    def ok(self) -> bool:
        return self.report.status in ("done", "partial") and bool(self.blocks)


# ---------------------------------------------------------------- DOM → blocks

def _children(node) -> list:
    out, c = [], node.child
    while c is not None:
        out.append(c)
        c = c.next
    return out


def _is_subhead_para(node, text: str) -> bool:
    """整段就是一个加粗片段 → 判为小标题。

    实测 Sixth Tone（渲染后）的 section 小标题写作 `<p><strong>Life on the move</strong></p>`，
    既不是 `<h*>`，class 也是空的 —— 靠标签与类名都认不出来，加粗是唯一线索。
    加长度上限，避免把「整段加粗的重点句」误判成标题。
    """
    if len(text) > 100:
        return False
    # 注意：selectolax 的文本节点 tag 是 "-text"，不能用「tag 非空」来筛元素节点
    strongs = [c for c in _children(node) if (c.tag or "").lower() in ("strong", "b")]
    if len(strongs) != 1:
        return False
    inner = clean_text(strongs[0].text(deep=True))
    return bool(inner) and inner == text


def _img_url(img, base: str) -> str | None:
    attrs = img.attributes or {}
    for a in IMG_ATTRS:
        v = attrs.get(a)
        if v and not v.startswith("data:"):
            return urljoin(base, v.strip())
    srcset = attrs.get("srcset") or attrs.get("data-srcset")
    if srcset:
        best, best_w = None, -1
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = 0
            if len(bits) > 1 and bits[1].lower().endswith("w"):
                try:
                    w = int(bits[1][:-1])
                except ValueError:
                    w = 0
            if w > best_w:
                best, best_w = bits[0], w
        if best:
            return urljoin(base, best)
    src = attrs.get("src")
    if src and not src.startswith("data:"):
        return urljoin(base, src)
    return None


def split_blocks(content_html: str, keep_empty_para: bool = False) -> tuple[list[Block], int]:
    """把提取后的正文 HTML 按文档顺序拆成 blocks。

    keep_empty_para=True 时保留空段落，作为「图片占位点」的锚。
    返回 (blocks, 被丢弃的装饰图数量)。
    """
    tree = HTMLParser(content_html)
    root = tree.body or tree.root
    blocks: list[Block] = []
    skipped = 0

    def add(**kw) -> None:
        nonlocal skipped
        if kw.get("type") == "image":
            if should_skip_image(kw.get("origin_url") or "", kw.get("alt") or ""):
                skipped += 1
                return
        blocks.append(Block(order=len(blocks) + 1, **kw))

    def handle(node) -> None:
        tag = (node.tag or "").lower()
        if tag in DROP_TAGS:
            return

        if tag in HEADING_TAGS:
            t = clean_text(node.text(deep=True))
            if t:
                add(type="heading", text=t, level=int(tag[1]))
            return

        if tag in ("img", "graphic"):
            u = _img_url(node, "")
            if u:
                add(type="image", origin_url=u, alt=clean_text((node.attributes or {}).get("alt")))
            return

        if tag == "video":
            attrs = node.attributes or {}
            u = attrs.get("src")
            if not u:
                s = node.css_first("source")
                u = (s.attributes or {}).get("src") if s is not None else None
            if u:
                add(type="video", origin_url=urljoin("", u), alt=clean_text(attrs.get("poster")))
            return

        if tag == "p":
            cls = (node.attributes or {}).get("class") or ""
            imgs = node.css("img, graphic")
            t = clean_text(node.text(deep=True))
            if t:
                # 小标题有两种伪装：<p class="subtitle">（class 特征）与
                # <p><strong>x</strong></p>（加粗特征）。都别降级成正文段落。
                if not imgs and (SUBHEAD_CLASS_RE.search(cls) or _is_subhead_para(node, t)):
                    add(type="heading", text=t, level=3)
                else:
                    add(type="para", text=t)
            elif keep_empty_para:
                add(type="placeholder", text="")
            for img in imgs:
                u = _img_url(img, "")
                if u:
                    add(type="image", origin_url=u, alt=clean_text((img.attributes or {}).get("alt")))
            return

        if tag in ("figure", "graphic"):
            img = node.css_first("img") or (node if tag == "graphic" else None)
            cap = node.css_first("figcaption")
            u = _img_url(img, "") if img is not None else None
            if u:
                add(type="image", origin_url=u, alt=clean_text((img.attributes or {}).get("alt")))
            elif tag == "figure":
                t = clean_text(node.text(deep=True))
                if t:
                    add(type="para", text=t)
            if cap is not None:
                ct = clean_text(cap.text(deep=True))
                if ct:
                    add(type="caption", text=ct)
            return

        if tag == "blockquote":
            t = clean_text(node.text(deep=True))
            if t:
                add(type="quote", text=t)
            return

        if tag in ("ul", "ol"):
            items = [clean_text(li.text(deep=True)) for li in node.css("li")]
            items = [i for i in items if i]
            if items:
                add(type="list", text="\n".join(f"- {i}" for i in items))
            return

        if tag == "table":
            rows = []
            for tr in node.css("tr"):
                cells = [clean_text(td.text(deep=True)) for td in tr.css("th, td")]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                add(type="table", text="\n".join(rows))
            return

        if tag in CONTAINER_TAGS:
            for c in _children(node):
                handle(c)

    for c in _children(root):
        handle(c)
    return blocks, skipped


# ---------------------------------------------------------------- 图片本地化

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg",
    "image/avif": ".avif", "image/bmp": ".bmp",
}

MAGIC_EXT = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG", ".png"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
)


def sniff_ext(data: bytes, fallback_url: str = "") -> str:
    """从字节魔数判断图片类型，失败再看 URL 后缀。"""
    for magic, ext in MAGIC_EXT:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    hint = Path(urlparse(fallback_url).path).suffix.lower()
    if hint in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp"):
        return ".jpg" if hint == ".jpeg" else hint
    return ".jpg"


def save_image_bytes(data: bytes, url: str, assets_dir: Path) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = assets_dir / f"{h}{sniff_ext(data, url)}"
    path.write_bytes(data)
    return f"assets/{assets_dir.name}/{path.name}"


def fetch_images_via_browser(article_url: str, targets: list[str]) -> dict[str, bytes]:
    """用无头浏览器打开文章页，拦截它自己发起的图片响应。

    应对「直连 403 但浏览器能显示」的热链保护图床（实测 img2.chinadaily.com.cn）：
    这类图片 JS 端 fetch 会被 CORS 拦、Python 直连 403，只有截浏览器响应这一条路。
    返回 {URL 的 path: 图片字节}。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}

    want = {urlparse(u).path for u in targets}
    got: dict[str, bytes] = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=settings.fetch_ua, viewport={"width": 1400, "height": 1000})

            def on_response(resp) -> None:
                try:
                    key = urlparse(resp.url).path
                    if key in want and key not in got:
                        ct = (resp.headers.get("content-type") or "").lower()
                        if ct.startswith("image/"):
                            got[key] = resp.body()
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", on_response)
            try:
                page.goto(article_url, wait_until="domcontentloaded",
                          timeout=settings.render_timeout_s * 1000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2500)
            for _ in range(10):            # 滚动触发懒加载
                if len(got) >= len(want):
                    break
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(500)
            page.wait_for_timeout(1000)
            browser.close()
    except Exception:  # noqa: BLE001
        return got
    return got


def download_image(
    client: httpx.Client,
    url: str,
    assets_dir: Path,
    referer: str,
) -> tuple[str | None, str]:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    for ext in (".jpg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp"):
        p = assets_dir / f"{h}{ext}"
        if p.exists():
            return f"assets/{assets_dir.name}/{p.name}", "cached"

    try:
        r = client.get(url, headers={"User-Agent": settings.fetch_ua, "Referer": referer,
                                     "Accept": "image/*,*/*;q=0.8"})
    except Exception as exc:  # noqa: BLE001
        return None, f"failed:{type(exc).__name__}"

    if r.status_code != 200:
        return None, f"failed:http{r.status_code}"
    if not r.content:
        return None, "failed:empty"

    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime and not mime.startswith("image/"):
        return None, f"failed:not-image({mime})"

    return save_image_bytes(r.content, url, assets_dir), "ok"


# ---------------------------------------------------------------- 主流程

def fetch_article(
    url: str,
    *,
    asset_key: str,
    force_render: bool = False,
    log: Callable[[str], None] | None = None,
    data_root: Path | None = None,
) -> FetchResult:
    """抓取单篇文章。

    asset_key 决定 raw/ 与 assets/ 下的目录名。data_root 默认取配置里的数据目录；
    M0 命令行脚本会指到 data/m0/，与服务端产物分开存放。
    """
    emit = log or _noop
    t0 = time.monotonic()
    settings.ensure_dirs()
    root = Path(data_root) if data_root else settings.data_dir
    raw_dir = root / "raw"
    assets_dir = root / "assets" / asset_key
    for d in (raw_dir, assets_dir):
        d.mkdir(parents=True, exist_ok=True)
    rep = Report(url=url, canonical_url=canonical_url(url), slug=slug_of(url))

    emit(f"[FETCH] {url}")

    # 1. 抓取
    with make_client() as client:
        try:
            resp = client.get(url, headers=article_headers(url))
        except Exception as exc:  # noqa: BLE001
            rep.status = "failed"
            rep.errors.append(f"request failed: {type(exc).__name__}: {exc}")
            rep.elapsed_s = round(time.monotonic() - t0, 2)
            return FetchResult(report=rep, blocks=[])

        rep.http_status = resp.status_code
        rep.content_type = (resp.headers.get("content-type") or "").split(";")[0]
        rep.html_bytes = len(resp.content)
        emit(f"  HTTP {resp.status_code}  {rep.content_type}  {rep.html_bytes / 1024:.1f} KB")

        if resp.status_code != 200 or "html" not in rep.content_type:
            rep.status = "failed"
            rep.errors.append(f"bad response: {resp.status_code} {rep.content_type}")
            rep.elapsed_s = round(time.monotonic() - t0, 2)
            return FetchResult(report=rep, blocks=[])

        html = resp.text
        raw_path = raw_dir / f"{asset_key}.html"
        if "just a moment" in html.lower() or "cf-browser-verification" in html.lower():
            rep.errors.append("疑似 Cloudflare 挑战页，需要 Playwright 兜底")

        # 1.5 按域名裁剪到真实正文容器（若有配置）
        source_html, sel = narrow_html(html, url)
        rep.content_selector = sel
        if sel:
            emit(f"  正文容器命中：{sel}  ({len(source_html) / 1024:.1f} KB)")

        # 2. 元数据（用未裁剪的原始 HTML，og:/meta 都在 head 里）
        try:
            md = trafilatura.extract_metadata(html, default_url=url, extensive=False)
            if md is not None:
                for attr in ("title", "author", "date", "sitename", "language"):
                    v = getattr(md, attr, None)
                    if v:
                        setattr(rep, attr, clean_text(str(v)))
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"metadata: {type(exc).__name__}: {exc}")

        # og:site_name 比 trafilatura 猜的准（实测它把 <title> 的 "#Sixthtone" 当站点名）
        og_site = meta_content(html, "og:site_name")
        rep.sitename = clean_sitename(og_site or rep.sitename, url)

        # 作者常被填成站点名（Sixth Tone 那篇的 author 就是 "Sixth Tone"），同则清空
        if rep.author and _norm_key(rep.author) == _norm_key(rep.sitename):
            rep.author = ""

        if not rep.language:
            m = re.search(r"<html[^>]*\blang=[\"']([A-Za-z\-]{2,12})", html, re.I)
            if m:
                rep.language = m.group(1).lower()
        rep.language = (rep.language or "en").split("-")[0].lower()[:8]

        # 3. 正文提取：按可靠性从高到低依次尝试
        content_html = None
        nd_images: list[dict] = []
        keep_placeholder = False

        # 无论走哪条路径都解析一次：__NEXT_DATA__ 还提供图注（desc）
        nd = nextjs_article(html)
        nd_captions = {
            urlparse(im["url"]).path: im["alt"]
            for im in (nd or {}).get("images", []) if im.get("alt")
        }

        # 3a. 无头浏览器渲染：唯一能把「图片 + 图注 + 正确位置」一起拿到的方式
        if force_render or need_render(url):
            emit("  启动无头浏览器渲染…")
            rendered = render_html(url)
            if rendered and word_count(strip_tags(rendered)) >= 200:
                content_html = rendered
                rep.extractor_pass = "playwright"
                emit(f"  渲染成功：正文容器 {len(rendered) / 1024:.1f} KB")
            else:
                rep.errors.append("Playwright 渲染失败或内容过短，回退静态提取")

        # 3b. Next.js：__NEXT_DATA__ 里有完整正文与图集（但图无位置信息）
        if content_html is None and nd:
            content_html = nd["content_html"]
            nd_images = nd["images"]
            keep_placeholder = True
            rep.extractor_pass = "next_data"
            rep.title = clean_text(nd.get("title")) or rep.title
            rep.date = clean_text(nd.get("date")) or rep.date
            rep.author = clean_text(nd.get("author")) or rep.author
            emit(f"  __NEXT_DATA__ 命中：正文 {len(content_html)} 字符，图集 {len(nd_images)} 张")

        # 3c. 站点正文容器已命中时，容器内容即正文。
        #     不能再过 trafilatura —— 实测它会把 <figure> 连图注一起丢掉（China Daily）
        if content_html is None and sel:
            content_html = source_html
            rep.extractor_pass = f"selector:{sel}"

        # 3d. 通用提取（精度优先，失败降级为召回优先）
        if content_html is None:
            for label, kwargs in (
                ("precision", dict(favor_precision=True, favor_recall=False)),
                ("recall", dict(favor_precision=False, favor_recall=True)),
            ):
                try:
                    got = trafilatura.extract(
                        source_html, url=url, output_format="html",
                        include_images=True, include_formatting=True,
                        include_tables=True, include_comments=False,
                        **kwargs,
                    )
                except Exception as exc:  # noqa: BLE001
                    rep.errors.append(f"extract({label}): {type(exc).__name__}: {exc}")
                    continue
                if got and word_count(strip_tags(got)) >= 200:
                    content_html, rep.extractor_pass = got, label
                    break
                if got and content_html is None:
                    content_html, rep.extractor_pass = got, f"{label}(short)"

        if content_html is None:
            rep.status = "failed"
            rep.errors.append("未能提取正文（渲染 / Next.js / 容器选择器 / trafilatura 全部失败）")
            rep.elapsed_s = round(time.monotonic() - t0, 2)
            return FetchResult(report=rep, blocks=[])

        # 原始 HTML 只在确定抓取成功后落盘，避免留下失败样本
        raw_path.write_text(html, encoding="utf-8")

        # 4. 拆 blocks
        blocks, skipped = split_blocks(content_html, keep_empty_para=keep_placeholder)
        blocks = drop_duplicate_title(blocks, rep.title)
        rep.images_skipped = skipped

        # 4b. 把与正文分离的图集插回正文
        if nd_images:
            blocks, leftover = fill_image_slots(blocks, nd_images)
            rep.image_placement = f"{len(nd_images) - len(leftover)}/{len(nd_images)} 张插回正文"
            if leftover:
                rep.errors.append(f"{len(leftover)} 张图未找到正文占位，已追加到文末")

        # 4c. 补图注（来自 __NEXT_DATA__，DOM 里拿不到）
        blocks = attach_captions(blocks, nd_captions)

        # 5. 图片本地化
        pending = [b for b in blocks if b.type == "image" and b.origin_url]
        emit(f"  blocks={len(blocks)}  images={len(pending)}  skipped={skipped}  pass={rep.extractor_pass}")
        for b in pending:
            b.origin_url = urljoin(url, b.origin_url)
            polite_wait(b.origin_url)
            rel, status = download_image(client, b.origin_url, assets_dir, referer=url)
            b.local_path, b.status = rel, status
            if rel:
                rep.images_ok += 1
            else:
                rep.images_failed += 1
                rep.errors.append(f"image {status}: {b.origin_url}")

        # 5b. 直连失败的图片（热链保护图床）改用浏览器拦截下载
        retry = [b for b in pending if b.status.startswith("failed:http4")]
        if retry:
            emit(f"  {len(retry)} 张直连被拒，改用浏览器会话重试…")
            bodies = fetch_images_via_browser(url, [b.origin_url for b in retry])
            for b in retry:
                data = bodies.get(urlparse(b.origin_url).path)
                if not data:
                    continue
                b.local_path = save_image_bytes(data, b.origin_url, assets_dir)
                b.status = "ok(browser)"
                rep.images_ok += 1
                rep.images_failed -= 1
                rep.images_via_browser += 1
                rep.errors = [e for e in rep.errors if b.origin_url not in e]
            emit(f"  浏览器会话补齐 {rep.images_via_browser} 张")

        # 清理不再被当前 blocks 引用的历史图片，避免重抓/改版后留下孤立文件
        keep = {Path(b.local_path).name for b in blocks if b.local_path}
        orphans = 0
        for f in assets_dir.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink(missing_ok=True)
                orphans += 1
        if orphans:
            emit(f"  清理孤立图片 {orphans} 个")

    # 6. 汇总
    plain = "\n".join(b.text for b in blocks if b.text)
    rep.blocks_total = len(blocks)
    rep.words = word_count(plain)
    for b in blocks:
        rep.blocks_by_type[b.type] = rep.blocks_by_type.get(b.type, 0) + 1

    rep.status = "done" if rep.words >= 200 else "partial"
    rep.elapsed_s = round(time.monotonic() - t0, 2)
    return FetchResult(
        report=rep,
        blocks=blocks,
        raw_html_path=str(raw_path.relative_to(root)),
    )
