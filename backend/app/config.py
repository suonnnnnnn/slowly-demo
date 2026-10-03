from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# 仓库根目录，前后端共用同一个 .env
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    app_name: str = "CookClip"
    database_url: str = "sqlite:///./data/cookclip.db"
    local_storage_root: Path = Path("./data/storage")
    local_workers: int = 2
    anonymous_session_days: int = 180
    # 整个检索子进程的总预算（秒）。要容得下 Python 启动 + import yt-dlp + 联网，
    # 8 秒在健康网络下都很紧（光 import 就要 1~2 秒）。
    search_timeout_seconds: int = 25
    # 单次 socket 操作超时（秒）。比总预算小，这样连不上时能早点失败、拿到真实报错。
    search_socket_timeout_seconds: int = 10
    search_fetch_limit: int = 20
    search_result_limit: int = 12
    search_cache_ttl_seconds: int = 6 * 60 * 60
    search_empty_cache_ttl_seconds: int = 60 * 60
    search_max_concurrent: int = 1

    # ---------- 陪做 / 步骤检查用的对话模型（任意 OpenAI 兼容接口） ----------
    # 与前端的取值规则保持一致：MODEL_API_KEY 优先于 OPENROUTER_API_KEY
    model_api_base: str = "https://openrouter.ai/api/v1"
    model_api_key: str | None = None
    openrouter_api_key: str | None = None
    model_name: str = "inclusionai/ling-3.0-flash-sante:free"
    # 看图用的模型。留空表示就用 MODEL_NAME 本身。
    # 当前接入的 DeepSeek deepseek-chat 已支持图片输入（2026-10 实测能正确描述画面），
    # 所以默认按「能看图」处理。
    model_vision_name: str | None = None
    # 换成看不了图的模型时（发图会 400），设成 false 就不再发图，
    # 页面也会如实告诉用户照片只留给他自己对照。
    model_vision_enabled: bool = True
    model_timeout_seconds: float = 45.0

    @property
    def model_key(self) -> str | None:
        return self.model_api_key or self.openrouter_api_key

    @property
    def vision_model(self) -> str:
        return self.model_vision_name or self.model_name

    @property
    def model_endpoint(self) -> str:
        return self.model_api_base.rstrip("/")

    max_video_duration_seconds: int = 3600
    max_file_size_bytes: int = 2 * 1024 * 1024 * 1024
    temp_root: Path = Path("/tmp/cookclip")

    # ---------- 速度相关（都可以用 .env 覆盖） ----------
    # 下载的清晰度上限。步骤卡片只用到 640px 宽的截图，1080p 的像素基本都被丢掉，
    # 但视频页要给人看，所以默认 720p 作为「看得清又不太大」的折中。
    # 现场演示想更快可以调到 480；想更清楚调到 1080。
    max_video_height: int = 720
    # yt-dlp 的分片并发数。YouTube 是整段 https 下载，收益不如 HLS 明显，
    # 但在长视频上能看到可见提速；设 1 就是原来的单连接行为。
    download_concurrency: int = 4
    # 场景检测只解码 I 帧（实测快 ~6.6 倍：146s 的 1080p 从 9.5s 降到 1.4s）。
    # 代价是切点会与全解码略有出入，但仍然来自真实的画面切换点。
    # 想要「和全解码一模一样」的切点，设成 false。
    fast_scene_detect: bool = True
    # 逐段问模型时的并发数。免费模型单次不快，串行会等到天荒地老；
    # 但调太高容易被限流，超时后那一段会退回「只有截图」。
    caption_concurrency: int = 6

    # ffmpeg 的位置（目录或可执行文件都行）。留空则自动在 PATH 里找。
    # yt-dlp 合并音视频要用它，找不到就会报 "ffmpeg is not installed"。
    ffmpeg_location: str | None = None
    yt_dlp_cookie_file: str | None = None
    yt_dlp_cookie_browser: str | None = None
    allowed_video_domains: str = (
        "youtube.com,youtu.be,bilibili.com,b23.tv,tiktok.com,instagram.com"
    )

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower()
            for item in self.allowed_video_domains.split(",")
            if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
