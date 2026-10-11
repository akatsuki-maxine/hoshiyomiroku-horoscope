#!/usr/bin/env python3
"""Audit offline GeoNames municipality lookup against an independent Japan municipality list.

Run in ~/horoscope-app after building data/geonames.sqlite3:
    python3 audit_jp_municipalities.py

The official GeoNames lookup remains entirely offline. This one-time audit downloads
Geolonia's Japanese municipality name list and saves it locally for reproducibility.
Geolonia's address data is licensed CC BY 4.0:
https://github.com/geolonia/japanese-addresses
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

from geonames_lookup import DB_PATH, JP_PREFECTURES, lookup_place

REFERENCE_URL = "https://geolonia.github.io/japanese-addresses/api/ja.json"
REFERENCE_LICENSE = "https://github.com/geolonia/japanese-addresses"


def get_reference(path: Path, refresh: bool = False) -> dict[str, list[str]]:
    if refresh or not path.is_file():
        print("自治体名の照合用一覧を取得中（Geolonia・監査時のみ通信）...", flush=True)
        request = urllib.request.Request(
            REFERENCE_URL,
            headers={"User-Agent": "Hoshiyomiroku-Horoscope-Municipality-Audit/1.0"},
        )
        with urllib.request.urlopen(request, timeout=50) as response:
            raw = response.read(2_500_001)
        if len(raw) > 2_500_000:
            raise RuntimeError("参照データが想定外の大きさです。")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or len(data) != 47:
            raise RuntimeError("参照データが想定した47都道府県の構造ではありません。")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != set(JP_PREFECTURES):
        raise RuntimeError("参照データの都道府県名が現在の検索プログラムと一致しません。")
    for pref, cities in data.items():
        if not isinstance(cities, list) or not all(isinstance(c, str) for c in cities):
            raise RuntimeError(f"市区町村名の形式が不正です: {pref}")
    return data


def classify_city(name: str) -> str:
    if name.endswith("区"):
        return "区（政令市を含む）"
    if name.endswith("市"):
        return "市"
    if name.endswith("町"):
        return "町"
    if name.endswith("村"):
        return "村"
    return "その他"


def _place_index(db_file: Path) -> tuple[dict[tuple[float, float], set[str]], set[str]]:
    positions: dict[tuple[float, float], set[str]] = collections.defaultdict(set)
    registered: set[str] = set()
    with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True) as conn:
        for lat, lon, admin in conn.execute(
            "SELECT latitude, longitude, admin1_name FROM places WHERE country='JP'"
        ):
            positions[(round(lat, 6), round(lon, 6))].add(admin)
            if admin:
                registered.add(admin)
    return positions, registered


def audit(db_file: Path, reference: dict[str, list[str]], csv_file: Path) -> tuple[collections.Counter, list[dict]]:
    positions, registered = _place_index(db_file)
    unmatched_admin = [pref for pref, romaji in JP_PREFECTURES.items() if romaji not in registered]
    print("都道府県コードの照合:", "OK" if not unmatched_admin else ", ".join(unmatched_admin))
    print("自治体別の検索検証中...", flush=True)

    rows: list[dict] = []
    totals: collections.Counter = collections.Counter()
    for pref, cities in reference.items():
        for city in sorted(set(cities)):
            full_name = pref + city
            kind = classify_city(city)
            row = {"prefecture": pref, "municipality": city, "kind": kind,
                   "query": full_name, "status": "", "result": "",
                   "latitude": "", "longitude": "", "notes": ""}
            try:
                found = lookup_place(full_name, db_file)
                row["result"] = str(found["display_name"])
                row["latitude"] = str(found["latitude"])
                row["longitude"] = str(found["longitude"])
                if found["country"] != "JP":
                    row["status"] = "WRONG_COUNTRY"
                    row["notes"] = "日本以外の地点が選択された"
                else:
                    admins = positions.get((round(found["latitude"], 6), round(found["longitude"], 6)), set())
                    if JP_PREFECTURES[pref] in admins:
                        row["status"] = "OK"
                    elif admins:
                        row["status"] = "WRONG_PREFECTURE"
                        row["notes"] = "一致地点の登録都道府県=" + "/".join(sorted(admins))
                    else:
                        row["status"] = "NO_POSITION_MATCH"
                        row["notes"] = "返却座標が日本の元データと一致しない"
            except ValueError as exc:
                row["status"] = "NOT_FOUND"
                row["notes"] = str(exc)
            except (sqlite3.DatabaseError, OSError) as exc:
                row["status"] = "SYSTEM_ERROR"
                row["notes"] = type(exc).__name__ + ": " + str(exc)
            rows.append(row)
            totals[row["status"]] += 1
            totals["total"] += 1
            totals[f"kind_{kind}_total"] += 1
            totals[f"kind_{kind}_{row['status']}"] += 1

    csv_file.parent.mkdir(parents=True, exist_ok=True)
    fields = ["prefecture", "municipality", "kind", "query", "status", "result", "latitude", "longitude", "notes"]
    with csv_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return totals, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="日本の市区町村・区のオフライン地名検索を網羅的に検査します")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="GeoNames SQLiteファイル")
    parser.add_argument("--reference", type=Path, default=Path.home() / "horoscope-audit" / "geolonia_ja.json", help="独立した自治体名一覧")
    parser.add_argument("--output", type=Path, default=Path.home() / "horoscope-audit" / "municipality_audit_results.csv", help="詳細CSV")
    parser.add_argument("--refresh", action="store_true", help="自治体名一覧を再取得")
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"ERROR: 地名辞書がありません: {args.db}", file=sys.stderr)
        return 2
    try:
        reference = get_reference(args.reference, args.refresh)
        totals, results = audit(args.db, reference, args.output)
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    total = totals["total"]
    ok = totals["OK"]
    print("\n===== 全国市区町村検索の検査結果 =====")
    print(f"照合した住所名：{total:,} 件")
    print(f"正常（同じ都道府県）：{ok:,} 件 ({ok / total:.1%})")
    for status in ["NOT_FOUND", "WRONG_PREFECTURE", "WRONG_COUNTRY", "NO_POSITION_MATCH", "SYSTEM_ERROR"]:
        if totals[status]:
            print(f"{status}：{totals[status]:,} 件")
    print("\n種類別の正常率：")
    for kind in ("市", "区（政令市を含む）", "町", "村", "その他"):
        amount = totals[f"kind_{kind}_total"]
        if amount:
            good = totals[f"kind_{kind}_OK"]
            print(f"  {kind}: {good:,} / {amount:,} ({good / amount:.1%})")
    errors = [r for r in results if r["status"] != "OK"]
    print("\n失敗例（最大30件）：")
    for row in errors[:30]:
        print(f"  {row['query']} [{row['status']}] {row['result']}")
    if not errors:
        print("  なし")
    print(f"\n全結果CSV：{args.output.resolve()}")
    print(f"照合元：Geolonia Japanese Addresses ({REFERENCE_LICENSE}, CC BY 4.0)")
    print("注：OKは同じ都道府県のGeoNames座標が返されたことを意味し、出生地点の精密な正しさは保証しません。")
    if errors:
        print("未対応・誤選択があるため、Cloud Runへの本番更新はまだ行わないでください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
