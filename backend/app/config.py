"""集中配置：全部来自环境变量 / backend/.env。

设计约定（见 docs/DESIGN.md §2.1）：密钥只落在 .env（不进版本库），
代码里不出现任何硬编码 token。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 存储 ----
    data_dir: Path = Field(default=BACKEND_DIR / "data")
    db_path: Path | None = None

    # ---- 抓取 ----
    fetch_ua: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    fetch_rate_limit: float = 2.0        # 每域名最小请求间隔（秒）
    fetch_timeout: float = 25.0
    render_timeout_s: int = 60           # 无头浏览器渲染超时
    render_domains: str = "sixthtone.com"  # 必须渲染的域名，逗号分隔
    render_wait_ms: int = 3500           # 渲染后等待 JS 出图的静默时间

    # ---- 后台任务 ----
    job_workers: int = 2

    # ---- 前端托管 ----
    # 构建产物由后端一起发（单进程即可使用），省掉 Vite 那个常驻服务。
    # 开发时改前端仍走 `npm run dev` 5173，两条路可同时存在，互不干扰。
    serve_frontend: bool = True
    frontend_dist: Path | None = None   # 缺省 = <repo>/frontend/dist

    # ---- LLM（M3 使用，M1 只落配置）----
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""

    # ---- 词典（M3 起本地化）----
    # 音标与释义全部来自本地离线词典（红线 3），划词路径不再打任何外网。
    #   ECDICT   释义 / 词性 / 考纲标签 / 词频（官方预编译 SQLite）
    #   ipa-dict 英美双音标（美 GA / 英 RP）
    # 两个文件都有几百 MB / 几 MB，不入版本库；服务启动时会自检并自动补齐
    # （也可用 scripts/fetch_dicts.py 手工装，见下一个开关）。
    # 留 override 位是为了让数据能放到别的盘（本机 data/ 之外的路径）。
    ecdict_db: Path | None = None        # 缺省 = <data>/stardict.db
    ipa_dict_dir: Path | None = None     # 缺省 = <data>，里面放 ipa_en_us.txt / ipa_en_uk.txt

    # 启动时自检并补齐缺失的词典数据（缺哪个下哪个，已有的不重下）。
    # 为什么要它：data/ 是可再生的，但这两个库**不进版本库**，换机器 / 重装 /
    # 清空 data/ 之后，划词会一直提示「本地词典库没有安装」——那一步纯手工、
    # 还要等 200 MB 下载，很容易被忘掉。打开它就没有「忘了装」这回事了。
    # 下载在**后台线程**里跑，不堵启动（见 services/dictdata.py）。
    # 关掉：DICT_AUTODOWNLOAD=false（离线开发、或想自己控制下载时机）。
    dict_autodownload: bool = True
    # 词典录音（Wikimedia Commons）的回退候选**只在有道拿不到时才去抓**，
    # 所以这里仍留着 Wiktionary 的接口地址。见 services/dictionary.py 的 audio_fallback。
    dict_api_base: str = "https://en.wiktionary.org/w/api.php"
    dict_audio_base: str = "https://dict.youdao.com/dictvoice"

    # ---- 发音音频（M7 使用）----
    # 有道 dictvoice 是**非官方接口**，批量拉容易被风控，所以这里每个参数都是
    # 「往慢里调」的。调小 audio_min_interval / 加大 job 并发前先想清楚。
    #
    # 音频**不做预热**：点喇叭才下载（见 services/audio.py）。所以这里的间隔
    # 只影响「连着点好几个词」时的节奏，不再影响划词。
    audio_enabled: bool = True
    # 默认口音。音标是英美双轨，发音两边都能下；这个值决定「主打哪个」——
    # 词卡上两个喇叭并列，生词本/复习卡的主音标也按它优先（primary_phonetic 口径）。
    audio_accent: str = "uk"
    audio_min_interval: float = 3.0      # 相邻两次外向音频请求的最小间隔（秒）
    audio_jitter: float = 1.5            # 叠加的随机抖动上限（秒）：固定节拍更像脚本
    audio_timeout: float = 20.0
    audio_max_attempts: int = 2          # 单次下载的尝试次数（含首次）
    audio_backoff_base: float = 2.0      # 重试退避基数：2s → 4s → 8s…
    audio_breaker_after: int = 5         # 连续失败几次就熔断
    audio_breaker_cooldown: float = 600.0  # 熔断后静默多久（秒）
    audio_min_bytes: int = 1024          # 小于这个字节数一律当失败（错误页）
    audio_ua: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    @field_validator("data_dir", mode="before")
    @classmethod
    def _abs_data_dir(cls, v: object) -> Path:
        p = Path(str(v))
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def db_file(self) -> Path:
        return self.db_path or (self.data_dir / "english.db")

    @property
    def ecdict_file(self) -> Path:
        """ECDICT 库文件（释义 / 词性 / 考纲标签 / 词频）。

        只读连接，**不参与 init_db 的建表**：它是外来的第三方库，
        和 english.db 不是一回事，删了这个文件只意味着「查不到词」。
        """
        return self.ecdict_db or (self.data_dir / "stardict.db")

    @property
    def ipa_dir(self) -> Path:
        """ipa-dict 的两个 TSV 所在目录（ipa_en_us.txt / ipa_en_uk.txt）。

        缺省直接放 data/ 下，不另起子目录：这两个文件加起来 4.6 MiB，
        也没有伴随的索引或元数据文件，单独一个目录只是多一层嵌套。
        """
        return self.ipa_dict_dir or self.data_dir

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def audio_dir(self) -> Path:
        """本地化的发音文件。文件名是内容 sha256 前 16 位，天然去重。"""
        return self.data_dir / "audio"

    @property
    def render_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.render_domains.split(",") if d.strip()}

    @property
    def dist_dir(self) -> Path:
        """前端构建产物目录（`npm run build` 的输出）。"""
        raw = self.frontend_dist or (BACKEND_DIR.parent / "frontend" / "dist")
        return Path(raw).resolve()

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.assets_dir, self.audio_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
