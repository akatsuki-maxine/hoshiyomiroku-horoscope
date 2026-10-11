"""Reproducibly build an offline SQLite gazetteer from GeoNames CC BY 4.0 data.

Requires Internet access only during build; no API key is needed.
Sources: cities500.zip, JP.zip, admin1CodesASCII.txt, countryInfo.txt
https://download.geonames.org/export/dump/readme.txt
"""
from __future__ import annotations

import io
import re
import os
import sqlite3
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from geonames_lookup import normalized_key, JP_PREFECTURES, EXTRA_COUNTRIES

BASE = "https://download.geonames.org/export/dump/"
OUTPUT = Path(__file__).resolve().parent / "data" / "geonames.sqlite3"
SOURCES = ("admin1CodesASCII.txt", "countryInfo.txt", "cities500.zip", "JP.zip")


def fetch_to_disk(name: str, directory: Path) -> Path:
    destination = directory / name
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "Hoshiyomiroku-Horoscope-DataBuild/1.0 (https://github.com/akatsuki-maxine/hoshiyomiroku-horoscope)"})
    for retry in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as response, destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if destination.stat().st_size < 100:
                raise ValueError(f"ダウンロードファイルが小さすぎます: {name}")
            return destination
        except (OSError, ValueError) as e:
            print(f"download retry {retry+1}/4: {name}: {e}", flush=True)
            if retry == 3:
                raise
            time.sleep(min(3 * (retry + 1), 9))
    raise AssertionError("unreachable")


def read_admin1(path: Path):
    aliases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            key, name, ascii_name = fields[:3]
            raw_admin_name = ascii_name or name
            raw_admin_name = re.sub(r"(?i)[\s_-]+(?:prefecture|province|ken|fu|to)\s*$", "", raw_admin_name)
            aliases[key] = normalized_key(raw_admin_name)
    return aliases


def read_country_aliases(path: Path):
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) > 4 and fields[0] and fields[4]:
            entries.add((normalized_key(fields[0]), fields[0]))
            entries.add((normalized_key(fields[4]), fields[0]))
    entries.update((normalized_key(name), cc) for name, cc in EXTRA_COUNTRIES.items())
    return sorted((name, code) for name, code in entries if name)


def record_stream(path: Path, inside: str):
    with zipfile.ZipFile(path) as bundle:
        with bundle.open(inside) as binary:
            with io.TextIOWrapper(binary, encoding="utf-8", errors="replace") as reader:
                yield from reader


def build(cities_path: Path, japan_path: Path, admin_path: Path, country_path: Path, destination: Path, minimum_places: int=40000, minimum_jp: int=1000):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".building")
    temp.unlink(missing_ok=True)
    admin_names = read_admin1(admin_path)
    conn = sqlite3.connect(temp)
    try:
        conn.executescript("""
        PRAGMA synchronous=OFF;
        PRAGMA journal_mode=OFF;
        CREATE TABLE places (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, latitude REAL NOT NULL,
            longitude REAL NOT NULL, country TEXT NOT NULL, admin1 TEXT,
            admin1_name TEXT, population INTEGER NOT NULL,
            feature_class TEXT NOT NULL, feature_code TEXT NOT NULL
        );
        CREATE TABLE aliases(alias TEXT NOT NULL, place_id INTEGER NOT NULL,
                             PRIMARY KEY(alias, place_id)) WITHOUT ROWID;
        CREATE TABLE country_aliases(alias TEXT PRIMARY KEY, country TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.executemany("INSERT OR IGNORE INTO country_aliases VALUES (?, ?)", read_country_aliases(country_path))
        place_rows, alias_rows = [], []
        seen = set()
        total = 0
        total_jp = 0
        def flush():
            if place_rows:
                conn.executemany("INSERT OR IGNORE INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", place_rows)
                place_rows.clear()
            if alias_rows:
                conn.executemany("INSERT OR IGNORE INTO aliases VALUES (?, ?)", alias_rows)
                alias_rows.clear()
        for src, inner, japan_only in ((cities_path, "cities500.txt", False), (japan_path, "JP.txt", True)):
            for line in record_stream(src, inner):
                fields = line.rstrip("\r\n").split("\t")
                if len(fields) < 19:
                    continue
                feature_class, feature_code = fields[6], fields[7]
                country = fields[8]
                if feature_class != "P" and not (japan_only and feature_class == "A" and feature_code in ("ADM1", "ADM2", "ADM3", "ADM4")):
                    continue
                if japan_only and country != "JP":
                    continue
                try:
                    gid = int(fields[0]); lat = float(fields[4]); lon = float(fields[5])
                    population = int(fields[14] or 0)
                except ValueError:
                    continue
                if gid in seen:
                    continue
                seen.add(gid)
                a1 = fields[10]
                a1_name = admin_names.get(f"{country}.{a1}", "")
                place_rows.append((gid, fields[1], lat, lon, country, a1, a1_name,
                                   population, feature_class, feature_code))
                aliases = {normalized_key(fields[1]), normalized_key(fields[2])}
                aliases.update(normalized_key(alias) for alias in fields[3].split(",") if alias)
                # GeoNames内部の日本語表記があればそれを優先して登録する
                aliases.discard("")
                alias_rows.extend((alias, gid) for alias in aliases if len(alias) <= 120)
                total += 1
                total_jp += int(country == "JP")
                if len(place_rows) >= 1000:
                    flush()
            flush()
            print(f"Indexed {inner}, total places={total:,}, JP={total_jp:,}", flush=True)
        if total < minimum_places or total_jp < minimum_jp:
            raise RuntimeError(f"GeoNamesデータが不完全です: total={total}, JP={total_jp}")
        conn.execute("INSERT INTO metadata VALUES (?, ?)", ("created_utc", datetime.now(timezone.utc).isoformat()))
        conn.execute("INSERT INTO metadata VALUES (?, ?)", ("source", BASE))
        conn.commit()
        print(f"Created {total:,} places, {conn.execute('SELECT count(*) FROM aliases').fetchone()[0]:,} aliases", flush=True)
    except BaseException:
        conn.close()
        temp.unlink(missing_ok=True)
        raise
    else:
        conn.close()
        temp.replace(destination)
    print(f"Database ready: {destination} ({destination.stat().st_size / (1024*1024):.1f} MiB)", flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="geonames-build-") as tmp:
        folder = Path(tmp)
        downloads = {name: fetch_to_disk(name, folder) for name in SOURCES}
        build(downloads["cities500.zip"], downloads["JP.zip"],
              downloads["admin1CodesASCII.txt"], downloads["countryInfo.txt"], OUTPUT)


if __name__ == "__main__":
    main()
