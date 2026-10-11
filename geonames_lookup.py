"""Offline GeoNames place lookup for public horoscope generation.

Only local SQLite queries are performed at request time; no public geocoding API.
See THIRD_PARTY_NOTICES.md for data license and attribution.
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

DB_PATH = Path(os.environ.get("HOROSCOPE_GEONAMES_DB", Path(__file__).resolve().parent / "data" / "geonames.sqlite3"))

# 日本の都道府県名の読み替え。GeoNames側の admin1CodesASCII.txt と照合する。
JP_PREFECTURES = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}

EXTRA_COUNTRIES = {
    "日本": "JP", "日本国": "JP", "japan": "JP", "jp": "JP",
    "アメリカ": "US", "アメリカ合衆国": "US", "usa": "US", "united states": "US", "us": "US",
    "英国": "GB", "イギリス": "GB", "united kingdom": "GB", "uk": "GB",
    "フランス": "FR", "france": "FR", "ドイツ": "DE", "germany": "DE",
    "中国": "CN", "china": "CN", "韓国": "KR", "south korea": "KR",
    "カナダ": "CA", "canada": "CA", "オーストラリア": "AU", "australia": "AU",
}


def normalized_key(value: str) -> str:
    # ラテン文字のアクセントだけを除去。日本語の濁点・半濁点は保持する。
    value = unicodedata.normalize("NFKC", value).casefold()
    chars = []
    for char in value:
        if "LATIN" in unicodedata.name(char, ""):
            chars.extend(piece for piece in unicodedata.normalize("NFKD", char)
                         if unicodedata.category(piece) != "Mn")
        else:
            chars.append(char)
    return "".join(ch for ch in chars if unicodedata.category(ch)[0] in ("L", "N"))


def _parse_coordinates(value: str):
    # 緯度,経度 の入力を許可。負値も可。ジオコーダー外部通信は発生しない。
    match = re.fullmatch(r"\s*([+-]?\d{1,2}(?:\.\d{1,8})?)\s*[,，]\s*([+-]?\d{1,3}(?:\.\d{1,8})?)\s*", value)
    if not match:
        return None
    lat, lon = map(float, match.groups())
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("緯度は -90〜90、経度は -180〜180 で指定してください。")
    return {"display_name": f"緯度 {lat:g}°, 経度 {lon:g}°", "latitude": lat, "longitude": lon, "country": None}


def _get_country_code(conn, text: str):
    key = normalized_key(text)
    if not key:
        return None
    if key in {normalized_key(k) for k in EXTRA_COUNTRIES}:
        for alias, code in EXTRA_COUNTRIES.items():
            if normalized_key(alias) == key:
                return code
    result = conn.execute("SELECT country FROM country_aliases WHERE alias=?", (key,)).fetchone()
    return result[0] if result else None


def _extract_location(conn, raw: str):
    """Return name, country ISO, and Japan prefecture code (optional)."""
    value = raw.strip()
    country = None
    pref = None
    parts = [p.strip() for p in re.split(r"[,，、]+", value) if p.strip()]
    if len(parts) >= 2:
        possible_country = _get_country_code(conn, parts[-1])
        if possible_country:
            country = possible_country
            parts.pop()
        if len(parts) >= 2:
            possible_pref = JP_PREFECTURES.get(parts[-1])
            if possible_pref:
                pref = possible_pref
                country = "JP"
                parts.pop()
        value = " ".join(parts)

    if not country:
        # 'Paris France' / 'New York USA' の国名接尾辞に対応
        segments = value.split()
        for size in range(min(3, len(segments) - 1), 0, -1):
            suffix = " ".join(segments[-size:])
            cc = _get_country_code(conn, suffix)
            if cc:
                country = cc
                value = " ".join(segments[:-size])
                break

    if not country:
        for alias, cc in sorted(EXTRA_COUNTRIES.items(), key=lambda kv: len(kv[0]), reverse=True):
            # 日本語の接尾辞は空白がなくても解析できる
            if (any(ord(ch) > 127 for ch in alias) and value.endswith(alias)
                    and len(value) > len(alias)):
                country = cc
                value = value[:-len(alias)].strip()
                break

    if not pref:
        for pref_name, pref_roman in sorted(JP_PREFECTURES.items(), key=lambda kv: len(kv[0]), reverse=True):
            if value.startswith(pref_name) and len(value) > len(pref_name):
                pref = pref_roman
                country = "JP"
                value = value[len(pref_name):].strip()
                break
            if value.endswith(pref_name) and len(value) > len(pref_name):
                pref = pref_roman
                country = "JP"
                value = value[:-len(pref_name)].strip()
                break

    return value, country, pref


# 全国自治体照合（Geoloniaの自治体名一覧、2026-10）で検出した、
# 漢字表記揺れと政令指定都市の区名重複の例外。GeoNames公式IDを使用し、
# 都道府県コードを照合する。
# 重要: 区名だけで検索しない。例: 大阪市西区(1855256)と堺市西区(11497694)は別地点。
# 参照: https://www.geonames.org/11497694/nishi-ku.html
_JP_EXACT_MUNICIPALITY_GEOID = {
    "青森県東津軽郡外ケ浜町": (7439942, "aomori"),
    "茨城県龍ヶ崎市": (2111256, "ibaraki"),
    "埼玉県さいたま市桜区": (7464119, "saitama"),
    "埼玉県さいたま市浦和区": (1849188, "saitama"),
    "千葉県袖ヶ浦市": (2110982, "chiba"),
    "千葉県鎌ヶ谷市": (2112445, "chiba"),
    "神奈川県川崎市川崎区": (7470872, "kanagawa"),
    "神奈川県川崎市麻生区": (7470887, "kanagawa"),
    "神奈川県横浜市保土ケ谷区": (7470864, "kanagawa"),
    "大阪府堺市美原区": (1857331, "osaka"),
    "大阪府堺市西区": (11497694, "osaka"),
}


def _exact_municipality(conn: sqlite3.Connection, name: str):
    """Use a verified GeoNames ID, never an ambiguous ward name."""
    match = _JP_EXACT_MUNICIPALITY_GEOID.get(name.strip())
    if match is None:
        return None
    gid, expected_pref = match
    row = conn.execute(
        """SELECT id, name, latitude, longitude, country, admin1_name, feature_class, feature_code
           FROM places WHERE id=?""", (gid,)
    ).fetchone()
    if (row is None or row["country"] != "JP"
            or row["admin1_name"] != expected_pref
            or row["feature_class"] != "A"
            or row["feature_code"] not in ("ADM2", "ADM3")):
        # Do NOT fall back to a different ward that happens to share a name.
        raise ValueError(
            f"自治体の参照地点（GeoNames ID {gid}）を検証できません。管理者に連絡してください。"
        )
    return {
        "display_name": f"{row['name']} (JP)",
        "latitude": float(row["latitude"]),
        "longitude": float(row["longitude"]),
        "country": "JP",
    }


def lookup_place(raw: str, db_path: str | Path | None = None):
    """Resolve a place using a local GeoNames DB. Never contacts an external host."""
    if not 1 <= len(raw.strip()) <= 120:
        raise ValueError("出生地・観測地点は120文字以内で入力してください。")
    coords = _parse_coordinates(raw)
    if coords:
        return coords
    path = Path(db_path or DB_PATH)
    if not path.is_file():
        raise ValueError("地名辞書が見つかりません。管理者に連絡してください。")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exact = _exact_municipality(conn, raw)
        if exact is not None:
            return exact
        city, country, pref = _extract_location(conn, raw)
        # 地方自治体の正式表記「○○郡△△町／村」では、GeoNames側の別名が
        # 「△△町／村」のみであることが多い。正確な表記を最優先し、
        # 見つからなければ郡名を外した候補を順に照合する。
        candidates = [city]
        if "郡" in city and city.endswith(("町", "村")):
            municipality = city.split("郡", 1)[1].strip()
            if municipality and municipality != city:
                candidates.append(municipality)

        names = []
        for candidate in candidates:
            names.append(normalized_key(candidate))
            # 「市・区・町・村」を省いた GeoNames の別名にも対応する。
            if candidate.endswith(("市", "区", "町", "村")) and len(candidate) > 2:
                names.append(normalized_key(candidate[:-1]))
        names = list(dict.fromkeys(filter(None, names)))
        if not names:
            raise ValueError("都市名を入力してください。")
        rows = []
        for name in names:
            sql = """SELECT p.id, p.name, p.latitude, p.longitude, p.country, p.admin1,
                            p.admin1_name, p.population, p.feature_class, p.feature_code
                       FROM aliases a JOIN places p ON p.id=a.place_id
                       WHERE a.alias=?"""
            params = [name]
            if country:
                sql += " AND p.country=?"
                params.append(country)
            if pref:
                sql += " AND p.admin1_name=?"
                params.append(pref)
            sql += " ORDER BY CASE WHEN p.feature_class='P' THEN 0 ELSE 1 END, p.population DESC LIMIT 30"
            rows = conn.execute(sql, params).fetchall()
            if rows:
                break
        if not rows:
            raise ValueError("地名辞書で出生地を特定できませんでした。市区町村名・都道府県・国名を確認するか、緯度,経度を入力してください。")
        item = rows[0]
        return {
            "display_name": f"{item['name']} ({item['country']})",
            "latitude": float(item["latitude"]),
            "longitude": float(item["longitude"]),
            "country": item["country"],
        }
    finally:
        conn.close()
