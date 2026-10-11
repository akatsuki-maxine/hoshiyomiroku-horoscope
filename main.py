# 星読録ホロスコープ生成機・公開準備版（AGPLによる公開を選ぶ場合の実装例）
# 本ファイルを一般公開する場合は、依存先のAGPL-3.0ライセンスに対応し、
# LICENSE、第三者著作権表示および対応ソースへのアクセスを提供すること。
# 現在のGoogle Cloud Runは非公開。公開URLの権限をまだ変更しないこと。

import base64
from collections import deque
from datetime import datetime
from functools import lru_cache
from threading import Lock
import os
import time
import re
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from kerykeion import AstrologicalSubjectFactory
from kerykeion.chart_data_factory import ChartDataFactory
from kerykeion.charts.chart_drawer import ChartDrawer
from kerykeion.planetary_return_factory import PlanetaryReturnFactory
from timezonefinder import TimezoneFinder


app = FastAPI(title="神格反転ホロスコープ生成機")

# 公開初期の簡易的な過負荷対策。Cloud Run側でも最大インスタンス数を制限すること。
# プロセス内の制限なので、複数インスタンス・複数リビジョン間では共有されない。
_GENERATION_WINDOW_SECONDS = 60
_GENERATION_MAX_PER_WINDOW = 24
_GENERATION_MAX_BODY_BYTES = 7 * 1024 * 1024
_generation_requests = deque()
_generation_lock = Lock()


@app.middleware("http")
async def protect_generation(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/generate":
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _GENERATION_MAX_BODY_BYTES:
                    return PlainTextResponse("送信データが大きすぎます。", status_code=413)
            except ValueError:
                return PlainTextResponse("無効なContent-Lengthです。", status_code=400)
        now = time.monotonic()
        with _generation_lock:
            while _generation_requests and now - _generation_requests[0] >= _GENERATION_WINDOW_SECONDS:
                _generation_requests.popleft()
            if len(_generation_requests) >= _GENERATION_MAX_PER_WINDOW:
                return PlainTextResponse("混み合っています。少し時間をおいて再試行してください。", status_code=429)
            _generation_requests.append(now)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path == "/generate":
        response.headers["Cache-Control"] = "no-store, private"
    return response


# 公共Nominatimの利用制限を守るため、1インスタンス内で地名検索を1.2秒間隔にする。
# 利用者が増えたら公共APIに依存せず、規約上適切な提供元へ切り替える。
geolocator = Nominatim(
    user_agent="shinkaku-hanten-horoscope-web/1.0 (+https://divine-inversion.com/)",
    domain=os.environ.get("HOROSCOPE_NOMINATIM_DOMAIN", "nominatim.openstreetmap.org"),
    timeout=10,
)
# 公共Nominatimの利用は「低トラフィック向けの暫定運用」です。
# Webサービス全体で毎秒1回以下が条件。プロセス単位の制限だけでは、
# Cloud Runの一時的な増設や複数リビジョンをまたぐ絶対保証にはなりません。
# 規約・利用量に不安があれば環境変数 HOROSCOPE_GEOCODING_ENABLED=0 で即時停止可能。
_limited_geocode = RateLimiter(
    geolocator.geocode,
    min_delay_seconds=1.7,
    max_retries=0,
    swallow_exceptions=False,
)
_GEO_LOOKUP_MAX_PER_MINUTE = 12
_GEO_LOOKUP_MAX_PER_HOUR = 80
_geo_lookups = deque()
_geo_lock = Lock()


def _remote_geocode(place_name: str):
    if os.environ.get("HOROSCOPE_GEOCODING_ENABLED", "1") != "1":
        raise ValueError("現在、出生地の新規検索を一時停止しています。")
    now = time.monotonic()
    with _geo_lock:
        while _geo_lookups and now - _geo_lookups[0] >= 3600:
            _geo_lookups.popleft()
        calls_in_last_minute = sum(now - tick < 60 for tick in _geo_lookups)
        if (calls_in_last_minute >= _GEO_LOOKUP_MAX_PER_MINUTE
                or len(_geo_lookups) >= _GEO_LOOKUP_MAX_PER_HOUR):
            raise ValueError("出生地検索が混雑しています。時間をおいて再試行してください。")
        _geo_lookups.append(now)
    return _limited_geocode(place_name, language="ja", exactly_one=True)


@lru_cache(maxsize=1024)
def _cached_geocode(place_name: str):
    return _remote_geocode(place_name)


timezone_finder = TimezoneFinder()


JAPANESE_TRANSLATIONS = {
    "Solar Return Chart": "ソーラーリターン図",
    "Lunar Return Chart": "ルナーリターン図",
    "Synastry Chart": "シナストリー図",
    "Composite Chart": "コンポジット図",
    "Transit Chart": "トランジット図",
    "Natal Chart": "出生図",
    "Solar Return": "ソーラーリターン",
    "Lunar Return": "ルナーリターン",
    "Synastry": "シナストリー",
    "Composite": "コンポジット",
    "Transit": "トランジット",
    "Natal": "出生図",
    "True South Lunar Node": "真ドラゴンテイル",
    "True North Lunar Node": "真ドラゴンヘッド",
    "True South Node": "真サウスノード",
    "True North Node": "真ノースノード",
    "Mean South Node": "平均サウスノード",
    "Mean North Node": "平均ノースノード",
    "North Node": "ドラゴンヘッド",
    "South Node": "ドラゴンテイル",
    "Imum Coeli": "イムム・コエリ",
    "Midheaven": "ミッドヘブン",
    "Ascendant": "アセンダント",
    "Descendant": "ディセンダント",
    "Mercury": "水星",
    "Jupiter": "木星",
    "Saturn": "土星",
    "Uranus": "天王星",
    "Neptune": "海王星",
    "Pluto": "冥王星",
    "Chiron": "キロン",
    "Lilith": "リリス",
    "Venus": "金星",
    "Mars": "火星",
    "Moon": "月",
    "Sun": "太陽",
    "Sagittarius": "射手座",
    "Capricorn": "山羊座",
    "Aquarius": "水瓶座",
    "Scorpio": "蠍座",
    "Aries": "牡羊座",
    "Taurus": "牡牛座",
    "Gemini": "双子座",
    "Cancer": "蟹座",
    "Leo": "獅子座",
    "Virgo": "乙女座",
    "Libra": "天秤座",
    "Pisces": "魚座",
    "Sesquiquadrate": "セスキコードレート",
    "Semi-Sextile": "セミセクスタイル",
    "Semi-Square": "セミスクエア",
    "Conjunction": "コンジャンクション",
    "Opposition": "オポジション",
    "Biquintile": "バイクインタイル",
    "Quincunx": "クインカンクス",
    "Quintile": "クインタイル",
    "Sextile": "セクスタイル",
    "Square": "スクエア",
    "Trine": "トライン",
    "Fire": "火",
    "Earth": "地",
    "Air": "風",
    "Water": "水",
    "Cardinal": "活動宮",
    "Fixed": "不動宮",
    "Mutable": "柔軟宮",
    "Applying": "接近",
    "Separating": "分離",
    "Static": "静止",
    "Retrograde": "逆行",
    "Direct": "順行",
    "House System": "ハウスシステム",
    "Whole Sign": "ホールサイン",
    "Regiomontanus": "レジオモンタナス",
    "Placidus": "プラシーダス",
    "Equal": "イコールハウス",
    "Koch": "コッホ",
    "12th House": "第12ハウス",
    "11th House": "第11ハウス",
    "10th House": "第10ハウス",
    "9th House": "第9ハウス",
    "8th House": "第8ハウス",
    "7th House": "第7ハウス",
    "6th House": "第6ハウス",
    "5th House": "第5ハウス",
    "4th House": "第4ハウス",
    "3rd House": "第3ハウス",
    "2nd House": "第2ハウス",
    "1st House": "第1ハウス",
    "Houses": "ハウス",
    "House": "ハウス",
    "Aspects": "アスペクト",
    "Aspect": "アスペクト",
    "Elements": "四元素",
    "Qualities": "三区分",
    "Quality": "区分",
    "Latitude": "緯度",
    "Longitude": "経度",
    "Location": "場所",
    "Birth Place": "出生地",
    "Local Time": "現地時刻",
    "Date": "日付",
    "Time": "時刻",
    "Orb": "オーブ",
    "Name": "名称",
    # SVG内のアスペクト表では小文字表記になる場合がある
    "sesquiquadrate": "セスキコードレート",
    "semi-sextile": "セミセクスタイル",
    "semi-square": "セミスクエア",
    "conjunction": "コンジャンクション",
    "opposition": "オポジション",
    "biquintile": "バイクインタイル",
    "quincunx": "クインカンクス",
    "quintile": "クインタイル",
    "sextile": "セクスタイル",
    "square": "スクエア",
    "trine": "トライン",
}


POINT_TRANSLATIONS = {
    "Sun": "太陽",
    "Moon": "月",
    "Mercury": "水星",
    "Venus": "金星",
    "Mars": "火星",
    "Jupiter": "木星",
    "Saturn": "土星",
    "Uranus": "天王星",
    "Neptune": "海王星",
    "Pluto": "冥王星",
    "Chiron": "キロン",
    "Ceres": "ケレス",
    "Pallas": "パラス",
    "Juno": "ジュノー",
    "Vesta": "ベスタ",
    "Mean_Node": "平均ドラゴンヘッド",
    "True_Node": "真ドラゴンヘッド",
    "Mean_North_Node": "平均ドラゴンヘッド",
    "True_North_Node": "真ドラゴンヘッド",
    "True_North_Lunar_Node": "真ドラゴンヘッド",
    "Mean_South_Node": "平均ドラゴンテイル",
    "True_South_Node": "真ドラゴンテイル",
    "True_South_Lunar_Node": "真ドラゴンテイル",
    "North_Node": "ドラゴンヘッド",
    "South_Node": "ドラゴンテイル",
    "Mean_Lilith": "平均リリス",
    "True_Lilith": "真リリス",
    "Lilith": "リリス",
    "Ascendant": "アセンダント",
    "Medium_Coeli": "ミッドヘブン",
    "Midheaven": "ミッドヘブン",
    "Descendant": "ディセンダント",
    "Imum_Coeli": "イムム・コエリ",
    "Vertex": "バーテックス",
    "Anti_Vertex": "アンチバーテックス",
    "Pars_Fortunae": "パート・オブ・フォーチュン",
    "Pars_Spiritus": "パート・オブ・スピリット",
    "Earth": "地球",
}

ASPECT_TRANSLATIONS = {
    "conjunction": "コンジャンクション",
    "semi-sextile": "セミセクスタイル",
    "sextile": "セクスタイル",
    "quintile": "クインタイル",
    "square": "スクエア",
    "trine": "トライン",
    "sesquiquadrate": "セスキコードレート",
    "biquintile": "バイクインタイル",
    "quincunx": "クインカンクス",
    "opposition": "オポジション",
    "semi-square": "セミスクエア",
    "parallel": "パラレル",
    "contra-parallel": "コントラパラレル",
}

ASPECT_SYMBOLS = {
    "conjunction": "☌",
    "semi-sextile": "⚺",
    "sextile": "⚹",
    "quintile": "Q",
    "square": "□",
    "trine": "△",
    "sesquiquadrate": "⚼",
    "biquintile": "bQ",
    "quincunx": "⚻",
    "opposition": "☍",
    "semi-square": "∠",
    "parallel": "∥",
    "contra-parallel": "∦",
}

MOVEMENT_TRANSLATIONS = {
    "Applying": "接近",
    "Separating": "分離",
    "Static": "静止",
}

HARMONIOUS_ASPECTS = {"semi-sextile", "sextile", "trine"}
TENSE_ASPECTS = {"semi-square", "square", "sesquiquadrate", "opposition"}
CREATIVE_ASPECTS = {"quintile", "biquintile"}


def search_birthplace(place_name: str):
    normalized_place = " ".join(place_name.strip().split())
    if not 1 <= len(normalized_place) <= 120:
        raise ValueError("出生地・観測地点は120文字以内で入力してください。")
    location = _cached_geocode(normalized_place)

    if location is None:
        raise ValueError(
            "出生地を特定できませんでした。都道府県名や国名を加えてください。"
        )

    latitude = float(location.latitude)
    longitude = float(location.longitude)
    timezone = timezone_finder.timezone_at(
        lng=longitude,
        lat=latitude,
    )

    if timezone is None:
        raise ValueError(
            "この地点のタイムゾーンを特定できませんでした。"
        )

    return {
        "display_name": location.address,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
    }


def localize_svg_to_japanese(svg_content: str) -> str:
    translations = sorted(
        JAPANESE_TRANSLATIONS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    def translate_text_node(match):
        text = match.group(1)
        for english, japanese in translations:
            text = text.replace(english, japanese)
        return f">{text}<"

    svg_content = re.sub(
        r">([^<>]+)<",
        translate_text_node,
        svg_content,
    )

    japanese_font_style = """
    <style>
        text, tspan {
            font-family: -apple-system, BlinkMacSystemFont,
                "Hiragino Sans", "Yu Gothic", "Noto Sans JP",
                sans-serif !important;
        }
    </style>
    """

    svg_content = re.sub(
        r"(<svg\b[^>]*>)",
        lambda match: match.group(1) + japanese_font_style,
        svg_content,
        count=1,
    )
    return svg_content


def apply_shinkaku_hanten_theme(svg_content: str) -> str:
    """神格反転独自の黒・青・赤・金テーマをSVGへ適用する。"""

    theme_style = """
    <style id="shinkaku-hanten-theme">
        :root {
            --sh-black: #020308;
            --sh-black-soft: #070b14;
            --sh-blue-dark: #071b3f;
            --sh-blue: #247cff;
            --sh-red-dark: #4b0815;
            --sh-red: #e23a52;
            --sh-gold: #d7b65d;
            --sh-gold-light: #f2dc92;

            --kerykeion-color-black: var(--sh-black);
            --kerykeion-color-white: #fffaf0;
            --kerykeion-color-neutral-content: var(--sh-gold-light);
            --kerykeion-color-base-content: var(--sh-gold);
            --kerykeion-color-primary: var(--sh-blue);
            --kerykeion-color-secondary: var(--sh-gold);
            --kerykeion-color-accent: var(--sh-red);
            --kerykeion-color-neutral: #171525;
            --kerykeion-color-base-100: var(--sh-black);
            --kerykeion-color-base-200: #030713;
            --kerykeion-color-base-300: #0a1730;
            --kerykeion-color-info: var(--sh-blue);
            --kerykeion-color-success: var(--sh-blue);
            --kerykeion-color-warning: var(--sh-gold);
            --kerykeion-color-error: var(--sh-red);

            --kerykeion-modern-planet-ring: #080e1e;
            --kerykeion-modern-planet-ring-outer: var(--sh-black-soft);
            --kerykeion-modern-house-ring: #0a1832;
            --kerykeion-modern-stroke: var(--sh-gold);
            --kerykeion-modern-retrograde: var(--sh-red);
            --kerykeion-modern-indicator: var(--sh-gold);
            --kerykeion-modern-zodiac-bg-0: #030713;
            --kerykeion-modern-zodiac-bg-1: #091735;
            --kerykeion-modern-zodiac-bg-2: #160711;
            --kerykeion-modern-zodiac-bg-3: #0b1730;
            --kerykeion-modern-zodiac-bg-4: #160711;
            --kerykeion-modern-zodiac-bg-5: #07152d;
            --kerykeion-modern-zodiac-bg-6: #030713;
            --kerykeion-modern-zodiac-bg-7: #160711;
            --kerykeion-modern-zodiac-bg-8: #07152d;
            --kerykeion-modern-zodiac-bg-9: #160711;
            --kerykeion-modern-zodiac-bg-10: #07152d;
            --kerykeion-modern-zodiac-bg-11: #160711;

            --kerykeion-chart-color-paper-0: var(--sh-gold-light);
            --kerykeion-chart-color-paper-1: var(--sh-black);
            --kerykeion-chart-color-houses-radix-line: var(--sh-gold);
            --kerykeion-chart-color-houses-transit-line: var(--sh-gold);
            --kerykeion-chart-color-zodiac-radix-ring-0: var(--sh-gold);
            --kerykeion-chart-color-zodiac-radix-ring-1: #806a35;
            --kerykeion-chart-color-zodiac-radix-ring-2: var(--sh-gold);

            /* 調和系は青、緊張系は赤、中立・創造系は金 */
            --kerykeion-chart-color-conjunction: var(--sh-gold);
            --kerykeion-chart-color-semi-sextile: var(--sh-blue);
            --kerykeion-chart-color-sextile: var(--sh-blue);
            --kerykeion-chart-color-trine: var(--sh-blue);
            --kerykeion-chart-color-semi-square: var(--sh-red);
            --kerykeion-chart-color-square: var(--sh-red);
            --kerykeion-chart-color-sesquiquadrate: var(--sh-red);
            --kerykeion-chart-color-opposition: var(--sh-red);
            --kerykeion-chart-color-quintile: var(--sh-gold);
            --kerykeion-chart-color-biquintile: var(--sh-gold);
            --kerykeion-chart-color-quincunx: var(--sh-gold);
        }

        svg { background: var(--sh-black) !important; }
        text, tspan { fill: var(--sh-gold-light); }
    </style>
    """

    return re.sub(
        r"(<svg\b[^>]*>)",
        lambda match: match.group(1) + theme_style,
        svg_content,
        count=1,
    )


def make_svg_background_transparent(svg_content: str) -> str:
    """チャート内部を保ったまま、SVGの土台だけを透明にする。"""

    transparent_style = """
    <style id="shinkaku-transparent-background">
        :root {
            --kerykeion-chart-color-paper-1: transparent;
            --kerykeion-color-base-100: transparent;
        }
        svg {
            background: transparent !important;
        }
    </style>
    """

    return svg_content.replace(
        "</svg>",
        transparent_style + "</svg>",
        1,
    )


MAX_LOGO_FILE_SIZE = 5 * 1024 * 1024
LOGO_POSITIONS = {
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "bottom-center",
}
LOGO_SIZES = {8, 12, 16, 20, 25}
LOGO_OPACITIES = {40, 60, 80, 100}


def detect_logo_mime_type(file_content: bytes) -> Optional[str]:
    """ファイルの実データから安全に画像形式を判定する。"""

    if file_content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if file_content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if (
        len(file_content) >= 12
        and file_content.startswith(b"RIFF")
        and file_content[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


def prepare_logo_data_url(logo_file: Optional[UploadFile]) -> Optional[str]:
    """アップロードされたロゴを検査し、SVG埋め込み用URLへ変換する。"""

    if logo_file is None or not getattr(logo_file, "filename", ""):
        return None

    file_content = logo_file.file.read(MAX_LOGO_FILE_SIZE + 1)
    if not file_content:
        return None
    if len(file_content) > MAX_LOGO_FILE_SIZE:
        raise ValueError("ロゴ画像は5MB以下にしてください。")

    mime_type = detect_logo_mime_type(file_content)
    if mime_type is None:
        raise ValueError(
            "ロゴはPNG、JPEG、WebPのいずれかを使用してください。"
        )

    encoded_logo = base64.b64encode(file_content).decode("ascii")
    return f"data:{mime_type};base64,{encoded_logo}"


def embed_logo_in_svg(
    svg_content: str,
    logo_data_url: Optional[str],
    position: str,
    size_percent: int,
    opacity_percent: int,
) -> str:
    """ロゴをSVG本体へ埋め込み、プレビューとPNGの両方へ反映する。"""

    if not logo_data_url:
        return svg_content

    if position not in LOGO_POSITIONS:
        position = "bottom-right"
    if size_percent not in LOGO_SIZES:
        size_percent = 12
    if opacity_percent not in LOGO_OPACITIES:
        opacity_percent = 80

    margin = 2
    horizontal_center = (100 - size_percent) / 2
    right = 100 - size_percent - margin
    bottom = 100 - size_percent - margin
    coordinates = {
        "top-left": (margin, margin),
        "top-right": (right, margin),
        "bottom-left": (margin, bottom),
        "bottom-right": (right, bottom),
        "bottom-center": (horizontal_center, bottom),
    }
    x_position, y_position = coordinates[position]
    opacity = opacity_percent / 100

    logo_markup = f"""
    <g id="shinkaku-hanten-logo" opacity="{opacity:g}"
       style="pointer-events:none">
        <image
            href="{logo_data_url}"
            x="{x_position:g}%"
            y="{y_position:g}%"
            width="{size_percent:g}%"
            height="{size_percent:g}%"
            preserveAspectRatio="xMidYMid meet"
        />
    </g>
    """

    return svg_content.replace("</svg>", logo_markup + "</svg>", 1)


def get_model_value(model, field_name: str, default=None):
    """Pydanticモデルと辞書の両方から安全に値を取得する。"""

    if isinstance(model, dict):
        return model.get(field_name, default)
    return getattr(model, field_name, default)


def normalize_aspect_name(aspect_name: str) -> str:
    """Kerykeionの表記揺れを翻訳辞書のキーへ統一する。"""

    return re.sub(
        r"-+",
        "-",
        str(aspect_name).strip().lower().replace("_", " ").replace(" ", "-"),
    )


def translate_point_name(point_name: str) -> str:
    """天体・感受点名を日本語へ変換する。"""

    original_name = str(point_name or "—").strip()
    lookup_name = original_name.replace(" ", "_").replace("-", "_")
    translated = POINT_TRANSLATIONS.get(lookup_name)
    if translated:
        return translated

    # 未登録の感受点でもアンダースコアが露出しないよう整形する
    return original_name.replace("_", " ")


def format_orb(orbit) -> str:
    """オーブを「度・分」表示へ整える。"""

    try:
        total_minutes = round(abs(float(orbit)) * 60)
    except (TypeError, ValueError):
        return "—"

    degrees, minutes = divmod(total_minutes, 60)
    return f"{degrees}°{minutes:02d}′"


def build_aspect_table_html(
    chart_data,
    chart_kind: str = "natal",
    first_subject_name: str = "",
    second_subject_name: str = "",
) -> str:
    """チャートデータから完全日本語のアスペクト一覧を作る。"""

    is_transit = chart_kind == "transit"
    is_synastry = chart_kind == "synastry"
    is_solar_return = chart_kind == "solar_return"
    if is_transit:
        heading = "出生図とのトランジット・アスペクト一覧"
        first_column = "出生天体・感受点"
        second_column = "トランジット天体・感受点"
    elif is_synastry:
        heading = "二人のシナストリー・アスペクト一覧"
        first_name = escape(first_subject_name or "人物1")
        second_name = escape(second_subject_name or "人物2")
        first_column = f"{first_name}の天体・感受点"
        second_column = f"{second_name}の天体・感受点"
    elif is_solar_return:
        heading = "出生図とのソーラーリターン・アスペクト一覧"
        first_column = "出生天体・感受点"
        second_column = "ソーラーリターン天体・感受点"
    else:
        heading = "アスペクト一覧"
        first_column = "天体・感受点1"
        second_column = "天体・感受点2"

    aspects = get_model_value(chart_data, "aspects", []) or []
    if not isinstance(aspects, (list, tuple)):
        aspects = get_model_value(aspects, "aspects", []) or []

    if not aspects:
        return f"""
        <section class="aspects-panel" aria-labelledby="aspects-title">
            <h2 id="aspects-title">{heading}</h2>
            <p class="aspects-empty">表示対象のアスペクトはありません。</p>
        </section>
        """

    rows = []
    for aspect in aspects:
        point_one = translate_point_name(get_model_value(aspect, "p1_name", "—"))
        point_two = translate_point_name(get_model_value(aspect, "p2_name", "—"))
        aspect_key = normalize_aspect_name(
            get_model_value(aspect, "aspect", "")
        )
        aspect_name = ASPECT_TRANSLATIONS.get(
            aspect_key,
            str(get_model_value(aspect, "aspect", "—")),
        )
        symbol = ASPECT_SYMBOLS.get(aspect_key, "•")

        aspect_degrees = get_model_value(aspect, "aspect_degrees", None)
        try:
            exact_angle = f"{float(aspect_degrees):g}°"
        except (TypeError, ValueError):
            exact_angle = "—"

        movement_value = str(
            get_model_value(aspect, "aspect_movement", "") or ""
        )
        movement = MOVEMENT_TRANSLATIONS.get(movement_value, movement_value or "—")

        if aspect_key in HARMONIOUS_ASPECTS:
            aspect_class = "aspect-harmonious"
        elif aspect_key in TENSE_ASPECTS:
            aspect_class = "aspect-tense"
        elif aspect_key in CREATIVE_ASPECTS:
            aspect_class = "aspect-creative"
        else:
            aspect_class = "aspect-neutral"

        rows.append(
            f"""
            <tr>
                <td class="point-name">{escape(point_one)}</td>
                <td class="aspect-name {aspect_class}">
                    <span class="aspect-symbol" aria-hidden="true">{escape(symbol)}</span>
                    {escape(aspect_name)}
                </td>
                <td class="point-name">{escape(point_two)}</td>
                <td>{escape(exact_angle)}</td>
                <td>{escape(format_orb(get_model_value(aspect, "orbit", None)))}</td>
                <td>{escape(movement)}</td>
            </tr>
            """
        )

    row_html = "".join(rows)
    return f"""
    <section class="aspects-panel" aria-labelledby="aspects-title">
        <div class="aspects-heading">
            <h2 id="aspects-title">{heading}</h2>
            <span class="aspect-count">全{len(rows)}件</span>
        </div>
        <p class="aspects-note">
            オーブは正確な角度からの差です。「接近」は形成中、「分離」は形成後を示します。
        </p>
        <div class="aspects-table-scroll">
            <table class="aspects-table">
                <thead>
                    <tr>
                        <th scope="col">{first_column}</th>
                        <th scope="col">アスペクト</th>
                        <th scope="col">{second_column}</th>
                        <th scope="col">角度</th>
                        <th scope="col">オーブ</th>
                        <th scope="col">状態</th>
                    </tr>
                </thead>
                <tbody>{row_html}</tbody>
            </table>
        </div>
    </section>
    """


def build_chart_download_page(
    svg_content: str,
    aspect_table_html: str,
    chart_title: str = "出生図",
    file_stem: str = "natal",
) -> str:
    """生成したSVGを表示し、ブラウザ上でPNG保存できるページを作る。"""

    encoded_svg = base64.b64encode(
        svg_content.encode("utf-8")
    ).decode("ascii")
    transparent_svg = make_svg_background_transparent(svg_content)
    encoded_transparent_svg = base64.b64encode(
        transparent_svg.encode("utf-8")
    ).decode("ascii")

    safe_chart_title = escape(chart_title)
    safe_file_stem = re.sub(r"[^a-z0-9-]", "-", file_stem.lower()).strip("-")
    if not safe_file_stem:
        safe_file_stem = "chart"

    return f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{safe_chart_title}・PNGダウンロード</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0;
                min-height: 100vh;
                padding: 24px;
                background:
                    radial-gradient(circle at 15% 0%, #0a2a62 0%, transparent 32%),
                    radial-gradient(circle at 85% 8%, #4b0815 0%, transparent 30%),
                    #020308;
                color: #f2dc92;
                font-family: -apple-system, BlinkMacSystemFont,
                    "Hiragino Sans", "Yu Gothic", sans-serif;
            }}
            .page {{
                width: min(1200px, 100%);
                margin: auto;
            }}
            .toolbar {{
                position: sticky;
                top: 12px;
                z-index: 10;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 16px;
                margin-bottom: 18px;
                padding: 14px 16px;
                border: 1px solid #d7b65d;
                border-radius: 12px;
                background: rgba(3, 6, 13, 0.94);
                box-shadow: 0 10px 35px rgba(0, 0, 0, 0.55);
                backdrop-filter: blur(10px);
            }}
            .title {{
                margin: 0;
                color: #f2dc92;
                font-size: 18px;
            }}
            .download-controls {{
                display: flex;
                align-items: center;
                gap: 10px;
            }}
            .option-select {{
                padding: 11px 12px;
                border: 1px solid #806a35;
                border-radius: 8px;
                background: #050a15;
                color: #f2dc92;
                font-size: 15px;
            }}
            .download-button {{
                flex: 0 0 auto;
                padding: 12px 22px;
                border: 1px solid #f2dc92;
                border-radius: 8px;
                background: linear-gradient(
                    110deg, #071b3f 0%, #247cff 34%,
                    #d7b65d 58%, #e23a52 100%
                );
                color: #fffaf0;
                font-size: 16px;
                font-weight: 700;
                cursor: pointer;
            }}
            .download-button:hover {{ filter: brightness(1.12); }}
            .download-button:disabled {{
                cursor: wait;
                filter: grayscale(0.6);
                opacity: 0.65;
            }}
            .chart-frame {{
                overflow: auto;
                border: 1px solid #806a35;
                border-radius: 12px;
                background: #020308;
                box-shadow: 0 20px 70px rgba(0, 0, 0, 0.65);
            }}
            .chart-frame.transparent-preview {{
                background-color: #f4f4f4;
                background-image:
                    linear-gradient(45deg, #c9c9c9 25%, transparent 25%),
                    linear-gradient(-45deg, #c9c9c9 25%, transparent 25%),
                    linear-gradient(45deg, transparent 75%, #c9c9c9 75%),
                    linear-gradient(-45deg, transparent 75%, #c9c9c9 75%);
                background-position: 0 0, 0 16px, 16px -16px, -16px 0;
                background-size: 32px 32px;
            }}
            #chart-image {{
                display: block;
                width: 100%;
                height: auto;
                background: #020308;
            }}
            .status {{
                min-height: 1.5em;
                margin: 12px 2px 0;
                color: #9db4d9;
                font-size: 13px;
            }}
            .aspects-panel {{
                margin-top: 26px;
                padding: 22px;
                border: 1px solid #806a35;
                border-radius: 12px;
                background: rgba(3, 6, 13, 0.96);
                box-shadow: 0 20px 70px rgba(0, 0, 0, 0.48);
            }}
            .aspects-heading {{
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 16px;
            }}
            .aspects-panel h2 {{
                margin: 0;
                color: #f2dc92;
                font-family: serif;
                font-size: 22px;
                letter-spacing: 0.06em;
            }}
            .aspect-count {{
                color: #9db4d9;
                font-size: 13px;
            }}
            .aspects-note {{
                margin: 8px 0 16px;
                color: #9db4d9;
                font-size: 13px;
                line-height: 1.7;
            }}
            .aspects-empty {{
                margin: 14px 0 0;
                color: #9db4d9;
            }}
            .aspects-table-scroll {{
                overflow-x: auto;
                border: 1px solid #263a5d;
                border-radius: 9px;
            }}
            .aspects-table {{
                width: 100%;
                min-width: 790px;
                border-collapse: collapse;
                color: #fffaf0;
                font-size: 14px;
            }}
            .aspects-table th {{
                padding: 12px 14px;
                border-bottom: 1px solid #806a35;
                background: #091326;
                color: #d7b65d;
                text-align: left;
                white-space: nowrap;
            }}
            .aspects-table td {{
                padding: 11px 14px;
                border-bottom: 1px solid #17243c;
                white-space: nowrap;
            }}
            .aspects-table tbody tr:nth-child(even) {{
                background: rgba(9, 23, 53, 0.34);
            }}
            .aspects-table tbody tr:last-child td {{
                border-bottom: 0;
            }}
            .point-name {{ color: #f2dc92; }}
            .aspect-name {{ font-weight: 700; }}
            .aspect-symbol {{
                display: inline-block;
                min-width: 1.6em;
                margin-right: 4px;
                text-align: center;
                font-size: 18px;
            }}
            .aspect-harmonious {{ color: #65a3ff; }}
            .aspect-tense {{ color: #ff647a; }}
            .aspect-creative,
            .aspect-neutral {{ color: #e5c66e; }}
            @media (max-width: 640px) {{
                body {{ padding: 12px; }}
                .toolbar {{ align-items: stretch; flex-direction: column; }}
                .download-controls {{ align-items: stretch; flex-direction: column; }}
                .download-button {{ width: 100%; }}
                .aspects-panel {{ padding: 16px 12px; }}
            }}
        </style>
    </head>
    <body>
        <main class="page">
            <div class="toolbar">
                <h1 class="title">神格反転ホロスコープ｜{safe_chart_title}</h1>
                <div class="download-controls">
                    <select id="background-select" class="option-select"
                            aria-label="PNG背景">
                        <option value="black">黒背景</option>
                        <option value="transparent" selected>透過背景</option>
                    </select>
                    <select id="resolution-select" class="option-select"
                            aria-label="PNG解像度">
                        <option value="4000">標準 4000px</option>
                        <option value="8000" selected>高精細 8000px</option>
                    </select>
                    <button id="download-button" class="download-button" disabled>
                        PNGをダウンロード
                    </button>
                </div>
            </div>

            <div id="chart-frame" class="chart-frame transparent-preview">
                <img
                    id="chart-image"
                    src="data:image/svg+xml;base64,{encoded_transparent_svg}"
                    alt="生成した{safe_chart_title}"
                >
            </div>
            <p id="status" class="status">画像を準備しています…</p>

            {aspect_table_html}
        </main>

        <script>
            const chartImage = document.getElementById("chart-image");
            const chartFrame = document.getElementById("chart-frame");
            const downloadButton = document.getElementById("download-button");
            const backgroundSelect = document.getElementById("background-select");
            const resolutionSelect = document.getElementById("resolution-select");
            const statusText = document.getElementById("status");
            const blackSvgUrl = "data:image/svg+xml;base64,{encoded_svg}";
            const transparentSvgUrl =
                "data:image/svg+xml;base64,{encoded_transparent_svg}";

            chartImage.addEventListener("load", () => {{
                downloadButton.disabled = false;
                statusText.textContent =
                    "準備完了です。細かな文字には「高精細 8000px」を推奨します。";
            }});

            chartImage.addEventListener("error", () => {{
                statusText.textContent = "ホロスコープ画像を読み込めませんでした。";
            }});

            backgroundSelect.addEventListener("change", () => {{
                const isTransparent = backgroundSelect.value === "transparent";
                downloadButton.disabled = true;
                chartFrame.classList.toggle("transparent-preview", isTransparent);
                chartImage.src = isTransparent ? transparentSvgUrl : blackSvgUrl;
                statusText.textContent = "背景を切り替えています…";
            }});

            downloadButton.addEventListener("click", () => {{
                downloadButton.disabled = true;
                backgroundSelect.disabled = true;
                resolutionSelect.disabled = true;
                statusText.textContent = "PNGを作成しています…";

                const sourceWidth = chartImage.naturalWidth || 1200;
                const sourceHeight = chartImage.naturalHeight || 1200;
                const sourceLongestSide = Math.max(sourceWidth, sourceHeight);
                const targetLongestSide = Number(resolutionSelect.value);
                const scale = targetLongestSide / sourceLongestSide;

                const canvas = document.createElement("canvas");
                canvas.width = Math.round(sourceWidth * scale);
                canvas.height = Math.round(sourceHeight * scale);

                const context = canvas.getContext("2d");
                const isTransparent = backgroundSelect.value === "transparent";
                context.clearRect(0, 0, canvas.width, canvas.height);
                if (!isTransparent) {{
                    context.fillStyle = "#020308";
                    context.fillRect(0, 0, canvas.width, canvas.height);
                }}
                context.drawImage(chartImage, 0, 0, canvas.width, canvas.height);

                canvas.toBlob((blob) => {{
                    if (!blob) {{
                        statusText.textContent = "PNGを作成できませんでした。";
                        downloadButton.disabled = false;
                        backgroundSelect.disabled = false;
                        resolutionSelect.disabled = false;
                        return;
                    }}

                    const downloadUrl = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = downloadUrl;
                    const backgroundName = isTransparent ? "transparent" : "black";
                    link.download =
                        `shinkaku-hanten-{safe_file_stem}-${{backgroundName}}-` +
                        `${{targetLongestSide}}px.png`;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    URL.revokeObjectURL(downloadUrl);

                    statusText.textContent = "PNGを保存しました。";
                    downloadButton.disabled = false;
                    backgroundSelect.disabled = false;
                    resolutionSelect.disabled = false;
                }}, "image/png");
            }});
        </script>
    </body>
    </html>
    """


FORM_HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>神格反転ホロスコープ生成機</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 40px 20px;
            background:
                radial-gradient(circle at 18% 0%, #0a2a62 0%, transparent 34%),
                radial-gradient(circle at 82% 12%, #4b0815 0%, transparent 31%),
                #020308;
            color: #f2dc92;
            font-family: -apple-system, BlinkMacSystemFont,
                "Hiragino Sans", "Yu Gothic", sans-serif;
            min-height: 100vh;
        }
        .container {
            width: min(760px, 100%);
            margin: auto;
            padding: 34px;
            border: 1px solid #d7b65d;
            border-radius: 18px;
            background: linear-gradient(145deg, rgba(4, 8, 17, 0.97), rgba(16, 5, 13, 0.97));
            box-shadow: 0 0 0 1px rgba(36, 124, 255, 0.18),
                        0 20px 70px rgba(0, 0, 0, 0.72);
        }
        h1 {
            margin: 0 0 8px;
            color: #f2dc92;
            text-align: center;
            font-family: serif;
            letter-spacing: 0.08em;
        }
        .description {
            margin: 0 0 30px;
            color: #a9bbdc;
            text-align: center;
            line-height: 1.8;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 18px;
        }
        .full { grid-column: 1 / -1; }
        fieldset {
            margin: 22px 0 0;
            padding: 22px;
            border: 1px solid #263f68;
            border-radius: 12px;
            background: rgba(3, 8, 18, 0.58);
        }
        fieldset[hidden] { display: none; }
        legend {
            padding: 0 10px;
            color: #f2dc92;
            font-family: serif;
            font-size: 18px;
            letter-spacing: 0.04em;
        }
        .mode-box {
            padding: 18px;
            border: 1px solid #806a35;
            border-radius: 12px;
            background: linear-gradient(120deg, rgba(7, 27, 63, 0.58), rgba(75, 8, 21, 0.35));
        }
        .section-note {
            grid-column: 1 / -1;
            margin: -4px 0 0;
            color: #8498bd;
            font-size: 12px;
            line-height: 1.7;
        }
        label {
            display: block;
            margin-bottom: 7px;
            color: #d7b65d;
            font-size: 14px;
        }
        input, select {
            width: 100%;
            padding: 12px 13px;
            border: 1px solid #294b80;
            border-radius: 8px;
            background: #050a15;
            color: #fffaf0;
            font-size: 16px;
        }
        input:focus, select:focus {
            border-color: #d7b65d;
            outline: none;
            box-shadow: 0 0 0 3px rgba(36, 124, 255, 0.22);
        }
        input[type="file"] {
            padding: 8px;
            color: #a9bbdc;
        }
        input[type="file"]::file-selector-button {
            margin-right: 12px;
            padding: 8px 12px;
            border: 1px solid #806a35;
            border-radius: 6px;
            background: #0a1832;
            color: #f2dc92;
            cursor: pointer;
        }
        button {
            width: 100%;
            margin-top: 28px;
            padding: 15px;
            border: 1px solid #f2dc92;
            border-radius: 9px;
            background: linear-gradient(110deg, #071b3f 0%, #247cff 28%,
                        #d7b65d 50%, #e23a52 72%, #4b0815 100%);
            color: #fffaf0;
            font-size: 17px;
            font-weight: bold;
            cursor: pointer;
        }
        button:hover { filter: brightness(1.12); }
        .notice {
            margin-top: 20px;
            color: #8498bd;
            font-size: 13px;
            line-height: 1.8;
        }
        @media (max-width: 600px) {
            .container { padding: 24px 18px; }
            .grid { grid-template-columns: 1fr; }
            .full { grid-column: auto; }
            fieldset { padding: 18px 14px; }
            .section-note { grid-column: auto; }
        }
    </style>
</head>
<body>
    <main class="container">
        <h1>ホロスコープ生成機</h1>
        <p class="description">
            出生図・トランジット・シナストリー・ソーラーリターンを生成します。<br>
            生成後は高解像度PNGとしてダウンロードできます。
        </p>
        <form action="/generate" method="post" target="_blank"
              enctype="multipart/form-data">
            <div class="mode-box">
                <label for="chart_type">作成するホロスコープ</label>
                <select id="chart_type" name="chart_type">
                    <option value="natal" selected>出生図</option>
                    <option value="transit">トランジット二重円</option>
                    <option value="synastry">シナストリー</option>
                    <option value="solar_return">ソーラーリターン</option>
                </select>
            </div>

            <fieldset>
                <legend>基準となる出生情報（内円）</legend>
                <div class="grid">
                    <div class="full">
                        <label for="name">人物名・チャート名</label>
                        <input id="name" name="name" value="サンプル" required>
                    </div>
                    <div>
                        <label for="year">出生年</label>
                        <input id="year" name="year" type="number" value="2000" required>
                    </div>
                    <div>
                        <label for="month">出生月</label>
                        <input id="month" name="month" type="number"
                               min="1" max="12" value="1" required>
                    </div>
                    <div>
                        <label for="day">出生日</label>
                        <input id="day" name="day" type="number"
                               min="1" max="31" value="1" required>
                    </div>
                    <div>
                        <label for="hour">出生時</label>
                        <input id="hour" name="hour" type="number"
                               min="0" max="23" value="12" required>
                    </div>
                    <div>
                        <label for="minute">出生分</label>
                        <input id="minute" name="minute" type="number"
                               min="0" max="59" value="0" required>
                    </div>
                    <div class="full">
                        <label for="birthplace">出生地</label>
                        <input id="birthplace" name="birthplace"
                               value="東京都千代田区"
                               placeholder="例：兵庫県明石市、Paris France" required>
                    </div>
                    <div>
                        <label for="house_system">ハウスシステム</label>
                        <select id="house_system" name="house_system">
                            <option value="P">プラシーダス</option>
                            <option value="W">ホールサイン</option>
                            <option value="A">イコールハウス</option>
                            <option value="K">コッホ</option>
                            <option value="R">レジオモンタナス</option>
                        </select>
                    </div>
                </div>
            </fieldset>

            <fieldset id="transit-fields">
                <legend>トランジット情報（外円）</legend>
                <div class="grid">
                    <div>
                        <label for="transit_year">対象年</label>
                        <input id="transit_year" name="transit_year" type="number"
                               value="2026" required>
                    </div>
                    <div>
                        <label for="transit_month">対象月</label>
                        <input id="transit_month" name="transit_month" type="number"
                               min="1" max="12" value="8" required>
                    </div>
                    <div>
                        <label for="transit_day">対象日</label>
                        <input id="transit_day" name="transit_day" type="number"
                               min="1" max="31" value="30" required>
                    </div>
                    <div>
                        <label for="transit_hour">対象時</label>
                        <input id="transit_hour" name="transit_hour" type="number"
                               min="0" max="23" value="12" required>
                    </div>
                    <div>
                        <label for="transit_minute">対象分</label>
                        <input id="transit_minute" name="transit_minute" type="number"
                               min="0" max="59" value="0" required>
                    </div>
                    <div class="full">
                        <label for="transit_place">対象日時にいる場所・観測地点</label>
                        <input id="transit_place" name="transit_place"
                               placeholder="空欄なら出生地と同じ場所を使用">
                    </div>
                    <p class="section-note">
                        内円に出生図、外円に指定日時の天体を配置します。
                        場所はトランジット側のハウスとアングルの計算に使います。
                    </p>
                </div>
            </fieldset>

            <fieldset id="synastry-fields">
                <legend>比較する人物の出生情報（外円）</legend>
                <div class="grid">
                    <div class="full">
                        <label for="partner_name">人物名</label>
                        <input id="partner_name" name="partner_name"
                               value="相手" required>
                    </div>
                    <div>
                        <label for="partner_year">出生年</label>
                        <input id="partner_year" name="partner_year" type="number"
                               value="1990" required>
                    </div>
                    <div>
                        <label for="partner_month">出生月</label>
                        <input id="partner_month" name="partner_month" type="number"
                               min="1" max="12" value="1" required>
                    </div>
                    <div>
                        <label for="partner_day">出生日</label>
                        <input id="partner_day" name="partner_day" type="number"
                               min="1" max="31" value="1" required>
                    </div>
                    <div>
                        <label for="partner_hour">出生時</label>
                        <input id="partner_hour" name="partner_hour" type="number"
                               min="0" max="23" value="12" required>
                    </div>
                    <div>
                        <label for="partner_minute">出生分</label>
                        <input id="partner_minute" name="partner_minute" type="number"
                               min="0" max="59" value="0" required>
                    </div>
                    <div class="full">
                        <label for="partner_birthplace">出生地</label>
                        <input id="partner_birthplace" name="partner_birthplace"
                               value="東京都新宿区"
                               placeholder="例：東京都新宿区、London UK" required>
                    </div>
                    <p class="section-note">
                        1人目を内円、2人目を外円へ配置し、二人の天体間のアスペクトを表示します。
                    </p>
                </div>
            </fieldset>

            <fieldset id="solar-return-fields">
                <legend>ソーラーリターン情報（外円）</legend>
                <div class="grid">
                    <div>
                        <label for="solar_return_year">対象年</label>
                        <input id="solar_return_year" name="solar_return_year"
                               type="number" value="2026" required>
                    </div>
                    <div class="full">
                        <label for="solar_return_place">ソーラーリターン時にいる場所・滞在予定地</label>
                        <input id="solar_return_place" name="solar_return_place"
                               placeholder="空欄なら出生地と同じ場所を使用">
                    </div>
                    <p class="section-note">
                        指定年に太陽が出生時の位置へ正確に戻る瞬間を計算します。
                        内円は出生図、外円はソーラーリターン図です。回帰時刻は誕生日の前後にずれる場合があります。
                    </p>
                </div>
            </fieldset>

            <fieldset id="logo-fields">
                <legend>ホロスコープ画像のロゴ</legend>
                <div class="grid">
                    <div class="full">
                        <label for="logo_file">ロゴ画像</label>
                        <input id="logo_file" name="logo_file" type="file"
                               accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp">
                    </div>
                    <div>
                        <label for="logo_position">配置</label>
                        <select id="logo_position" name="logo_position">
                            <option value="bottom-right" selected>右下</option>
                            <option value="bottom-left">左下</option>
                            <option value="top-right">右上</option>
                            <option value="top-left">左上</option>
                            <option value="bottom-center">下中央</option>
                        </select>
                    </div>
                    <div>
                        <label for="logo_size">大きさ</label>
                        <select id="logo_size" name="logo_size">
                            <option value="8">小・8%</option>
                            <option value="12" selected>標準・12%</option>
                            <option value="16">中・16%</option>
                            <option value="20">大・20%</option>
                            <option value="25">特大・25%</option>
                        </select>
                    </div>
                    <div>
                        <label for="logo_opacity">透明度</label>
                        <select id="logo_opacity" name="logo_opacity">
                            <option value="100">不透明・100%</option>
                            <option value="80" selected>やや透過・80%</option>
                            <option value="60">透過・60%</option>
                            <option value="40">薄い透かし・40%</option>
                        </select>
                    </div>
                    <p class="section-note">
                        PNG、JPEG、WebPに対応します。透過ロゴにはPNGを推奨します。
                        ロゴを選択しなければ、従来どおりロゴなしで生成します。
                    </p>
                </div>
            </fieldset>

            <button id="generate-button" type="submit">出生図を生成する</button>
        </form>
        <p class="notice">
            入力した場所から緯度・経度・タイムゾーンを自動取得します。<br>
            出生地の文字列はOpenStreetMap Nominatimの検索サービスに送信されます。番地や個人の住所は入力しないでください。<br>
            検索データ：<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a> / <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener noreferrer">Nominatimの利用規約</a>
        </p>
    </main>

    <script>
        const chartType = document.getElementById("chart_type");
        const transitFields = document.getElementById("transit-fields");
        const synastryFields = document.getElementById("synastry-fields");
        const solarReturnFields = document.getElementById("solar-return-fields");
        const generateButton = document.getElementById("generate-button");

        function updateChartType() {
            const isTransit = chartType.value === "transit";
            const isSynastry = chartType.value === "synastry";
            const isSolarReturn = chartType.value === "solar_return";
            transitFields.hidden = !isTransit;
            synastryFields.hidden = !isSynastry;
            solarReturnFields.hidden = !isSolarReturn;
            if (isTransit) {
                generateButton.textContent = "トランジット二重円を生成する";
            } else if (isSynastry) {
                generateButton.textContent = "シナストリーを生成する";
            } else if (isSolarReturn) {
                generateButton.textContent = "ソーラーリターンを生成する";
            } else {
                generateButton.textContent = "出生図を生成する";
            }
        }

        const now = new Date();
        document.getElementById("transit_year").value = now.getFullYear();
        document.getElementById("transit_month").value = now.getMonth() + 1;
        document.getElementById("transit_day").value = now.getDate();
        document.getElementById("transit_hour").value = now.getHours();
        document.getElementById("transit_minute").value = now.getMinutes();
        document.getElementById("solar_return_year").value = now.getFullYear();

        chartType.addEventListener("change", updateChartType);
        updateChartType();
    </script>
</body>
</html>
"""


def validate_date(year: int, month: int, day: int, hour: int, minute: int, kind: str):
    if not 1 <= year <= 3000:
        raise ValueError(f"{kind}の年は1〜3000の範囲で指定してください。")
    try:
        datetime(year, month, day, hour, minute)
    except ValueError:
        raise ValueError(f"{kind}の日時を確認してください。") from None


def validate_label(value: str, kind: str, max_length: int):
    if not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"{kind}は1〜{max_length}文字で入力してください。")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{kind}に使用できない文字が含まれています。")


def invalid_form_response(message: str):
    return HTMLResponse(
        content=(
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="robots" content="noindex">'
            '<title>入力エラー</title></head><body>'
            '<main><h1>入力内容を確認してください</h1><p>'
            + escape(message) + '</p><p><a href="/">入力画面へ戻る</a></p>'
            '</main></body></html>'
        ),
        status_code=400,
    )


def include_source_information(page: str) -> str:
    """AGPLで公開する場合のソースコード案内。リンク設定前は表示しない。"""
    source_url = os.environ.get("HOROSCOPE_SOURCE_URL", "").strip()
    if not source_url.startswith("https://"):
        return page
    safe_url = escape(source_url, quote=True)
    notice = (
        '<p class="notice" style="margin:16px auto;max-width:960px;line-height:1.9">'
        'このホロスコープ生成機はAGPL-3.0の条件で公開されています。'
        f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
        '対応ソースコード・ライセンスを確認する</a>。'
        '法的表示および無保証に関する事項はリンク先を参照してください。'
        '</p>'
    )
    return page.replace('</main>', notice + '</main>', 1)


@app.get("/", response_class=HTMLResponse)
def index():
    return include_source_information(FORM_HTML)


@app.post("/generate")
def generate_chart(
    name: Annotated[str, Form()],
    chart_type: Annotated[str, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    day: Annotated[int, Form()],
    hour: Annotated[int, Form()],
    minute: Annotated[int, Form()],
    birthplace: Annotated[str, Form()],
    house_system: Annotated[str, Form()],
    transit_year: Annotated[int, Form()],
    transit_month: Annotated[int, Form()],
    transit_day: Annotated[int, Form()],
    transit_hour: Annotated[int, Form()],
    transit_minute: Annotated[int, Form()],
    partner_name: Annotated[str, Form()],
    partner_year: Annotated[int, Form()],
    partner_month: Annotated[int, Form()],
    partner_day: Annotated[int, Form()],
    partner_hour: Annotated[int, Form()],
    partner_minute: Annotated[int, Form()],
    partner_birthplace: Annotated[str, Form()],
    solar_return_year: Annotated[int, Form()],
    logo_position: Annotated[str, Form()],
    logo_size: Annotated[int, Form()],
    logo_opacity: Annotated[int, Form()],
    transit_place: Annotated[str, Form()] = "",
    solar_return_place: Annotated[str, Form()] = "",
    logo_file: Annotated[Optional[UploadFile], File()] = None,
):
    chart_kind = (
        chart_type
        if chart_type in {"natal", "transit", "synastry", "solar_return"}
        else "natal"
    )

    try:
        validate_label(name, "人物名・チャート名", 80)
        validate_label(birthplace, "出生地", 120)
        validate_date(year, month, day, hour, minute, "出生")
        if chart_kind == "transit":
            validate_date(transit_year, transit_month, transit_day, transit_hour, transit_minute, "対象")
            if transit_place.strip():
                validate_label(transit_place, "観測地点", 120)
        elif chart_kind == "synastry":
            validate_label(partner_name, "比較する人物名", 80)
            validate_label(partner_birthplace, "比較する人物の出生地", 120)
            validate_date(partner_year, partner_month, partner_day, partner_hour, partner_minute, "比較する人物の出生")
        elif chart_kind == "solar_return":
            if not 1 <= solar_return_year <= 3000:
                raise ValueError("ソーラーリターンの対象年は1〜3000の範囲で指定してください。")
            if solar_return_place.strip():
                validate_label(solar_return_place, "滞在予定地", 120)
    except ValueError as error:
        return invalid_form_response(str(error))

    try:
        logo_data_url = prepare_logo_data_url(logo_file)
    except ValueError as error:
        return HTMLResponse(
            content=f"""
            <!DOCTYPE html>
            <html lang="ja">
            <head><meta charset="UTF-8"><title>ロゴ画像エラー</title></head>
            <body style="background:#080b12;color:#f3ead0;padding:50px;font-family:sans-serif">
                <main style="max-width:700px;margin:auto;padding:30px;border:1px solid #a93232">
                    <h1 style="color:#e26d6d">ロゴ画像を読み込めませんでした</h1>
                    <p>{escape(str(error))}</p>
                    <p><a href="/" style="color:#e2c16d">入力画面へ戻る</a></p>
                </main>
            </body>
            </html>
            """,
            status_code=400,
        )

    try:
        location_data = search_birthplace(birthplace)
        latitude = location_data["latitude"]
        longitude = location_data["longitude"]
        timezone = location_data["timezone"]

        if chart_kind == "transit":
            transit_place_name = transit_place.strip() or birthplace
            if transit_place_name == birthplace:
                transit_location_data = location_data
            else:
                transit_location_data = search_birthplace(transit_place_name)

            transit_latitude = transit_location_data["latitude"]
            transit_longitude = transit_location_data["longitude"]
            transit_timezone = transit_location_data["timezone"]

        if chart_kind == "synastry":
            if partner_birthplace.strip() == birthplace.strip():
                partner_location_data = location_data
            else:
                partner_location_data = search_birthplace(partner_birthplace)

            partner_latitude = partner_location_data["latitude"]
            partner_longitude = partner_location_data["longitude"]
            partner_timezone = partner_location_data["timezone"]

        if chart_kind == "solar_return":
            solar_return_place_name = solar_return_place.strip() or birthplace
            if solar_return_place_name == birthplace:
                solar_return_location_data = location_data
            else:
                solar_return_location_data = search_birthplace(
                    solar_return_place_name
                )

            solar_return_latitude = solar_return_location_data["latitude"]
            solar_return_longitude = solar_return_location_data["longitude"]
            solar_return_timezone = solar_return_location_data["timezone"]
    except (ValueError, GeocoderTimedOut, GeocoderServiceError) as error:
        return HTMLResponse(
            content=f"""
            <!DOCTYPE html>
            <html lang="ja">
            <head><meta charset="UTF-8"><title>場所検索エラー</title></head>
            <body style="background:#080b12;color:#f3ead0;padding:50px;font-family:sans-serif">
                <main style="max-width:700px;margin:auto;padding:30px;border:1px solid #a93232">
                    <h1 style="color:#e26d6d">入力した場所を検索できませんでした</h1>
                    <p>{escape(str(error))}</p>
                    <p>都道府県名や国名を含めて入力してください。</p>
                    <p><a href="/" style="color:#e2c16d">入力画面へ戻る</a></p>
                </main>
            </body>
            </html>
            """,
            status_code=400,
        )

    allowed_houses = {"P", "W", "A", "K", "R"}
    if house_system not in allowed_houses:
        house_system = "P"

    safe_name = (
        name.replace("<", "")
        .replace(">", "")
        .replace("&", "")
        .strip()
    )
    if not safe_name:
        safe_name = "出生図"

    safe_partner_name = (
        partner_name.replace("<", "")
        .replace(">", "")
        .replace("&", "")
        .strip()
    )
    if not safe_partner_name:
        safe_partner_name = "相手"

    subject = AstrologicalSubjectFactory.from_birth_data(
        safe_name,
        year,
        month,
        day,
        hour,
        minute,
        lng=longitude,
        lat=latitude,
        tz_str=timezone,
        online=False,
        houses_system_identifier=house_system,
    )

    if chart_kind == "transit":
        transit_subject = AstrologicalSubjectFactory.from_birth_data(
            (
                f"Transit {transit_year}/{transit_month:02d}/{transit_day:02d} "
                f"{transit_hour:02d}:{transit_minute:02d}"
            ),
            transit_year,
            transit_month,
            transit_day,
            transit_hour,
            transit_minute,
            lng=transit_longitude,
            lat=transit_latitude,
            tz_str=transit_timezone,
            online=False,
            houses_system_identifier=house_system,
        )
        chart_data = ChartDataFactory.create_transit_chart_data(
            subject,
            transit_subject,
        )
        output_filename = "transit-chart"
        chart_title = f"{safe_name}のトランジット二重円"
        file_stem = "transit"
    elif chart_kind == "synastry":
        partner_subject = AstrologicalSubjectFactory.from_birth_data(
            safe_partner_name,
            partner_year,
            partner_month,
            partner_day,
            partner_hour,
            partner_minute,
            lng=partner_longitude,
            lat=partner_latitude,
            tz_str=partner_timezone,
            online=False,
            houses_system_identifier=house_system,
        )
        chart_data = ChartDataFactory.create_synastry_chart_data(
            subject,
            partner_subject,
        )
        output_filename = "synastry-chart"
        chart_title = f"{safe_name} × {safe_partner_name}のシナストリー"
        file_stem = "synastry"
    elif chart_kind == "solar_return":
        return_factory = PlanetaryReturnFactory(
            subject,
            lng=solar_return_longitude,
            lat=solar_return_latitude,
            tz_str=solar_return_timezone,
            online=False,
        )
        solar_return_subject = return_factory.next_return_from_date(
            solar_return_year,
            1,
            1,
            return_type="Solar",
        )
        chart_data = ChartDataFactory.create_return_chart_data(
            subject,
            solar_return_subject,
        )
        output_filename = "solar-return-chart"
        chart_title = f"{safe_name}の{solar_return_year}年ソーラーリターン"
        file_stem = "solar-return"
    else:
        chart_data = ChartDataFactory.create_natal_chart_data(subject)
        output_filename = "natal-chart"
        chart_title = f"{safe_name}の出生図"
        file_stem = "natal"

    drawer = ChartDrawer(
        chart_data=chart_data,
        theme="dark",
        chart_language="EN",
    )

    with TemporaryDirectory() as temporary_directory:
        output_directory = Path(temporary_directory)
        drawer.save_svg(
            output_path=output_directory,
            filename=output_filename,
            style="modern",
        )

        svg_files = list(output_directory.glob("*.svg"))
        if not svg_files:
            return HTMLResponse(
                "<h1>SVGを生成できませんでした。</h1>",
                status_code=500,
            )

        svg_content = svg_files[0].read_text(encoding="utf-8")
        svg_content = localize_svg_to_japanese(svg_content)
        svg_content = apply_shinkaku_hanten_theme(svg_content)
        svg_content = embed_logo_in_svg(
            svg_content,
            logo_data_url,
            logo_position,
            logo_size,
            logo_opacity,
        )

    return HTMLResponse(
        content=include_source_information(build_chart_download_page(
            svg_content,
            build_aspect_table_html(
                chart_data,
                chart_kind,
                first_subject_name=safe_name,
                second_subject_name=safe_partner_name,
            ),
            chart_title=chart_title,
            file_stem=file_stem,
        )),
    )
