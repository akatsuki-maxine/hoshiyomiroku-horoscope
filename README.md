# 星読録・ホロスコープ生成機 / Horoscope Generator

出生図、トランジット、シナストリー、ソーラーリターンを生成する FastAPI アプリケーションです。

## ライセンスと重要事項

このリポジトリの生成機プログラムは GNU Affero General Public License version 3.0 (AGPL-3.0) に従って公開します。既存の公開リポジトリにある正式な `LICENSE`（AGPL-3.0）を維持してください。

サードパーティーのライブラリは `THIRD_PARTY_NOTICES.md` を参照してください。ライブラリのライセンスをこのリポジトリのライセンスで上書きすることはできません。

このリポジトリの対象はホロスコープ生成機のプログラムです。生成機と独立した小説、記事、分析データ、商標、ロゴ等を自動的にこのライセンスで公開するという意味ではありません。ただしプログラムに組み込まれた素材・一体化したファイルは別途権利を確認してください。

## 構成

- `main.py` — FastAPI、HTML、チャート生成、アクセス制限
- `geonames_lookup.py` — ローカル地名検索（リクエスト時の外部API通信なし）
- `build_geonames_db.py` — GeoNamesから地名データを取得・SQLite索引を生成
- `requirements.txt` — Python ライブラリ
- `Dockerfile` — Cloud Run 用の実行コンテナ
- `.python-version` / `Procfile` — 開発・起動設定
- `.gcloudignore` — Cloud デプロイ時の除外設定
- `THIRD_PARTY_NOTICES.md` — 第三者ソフトウェア・OSMに関する告知

## ローカル起動例

Python 3.11 環境で以下を実行します。

```sh
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

アクセス先: http://127.0.0.1:8000/

## 環境変数

- `HOROSCOPE_SOURCE_URL=https://github.com/OWNER/REPO` — 画面上の対応ソース案内（公開リポジトリの実際のURLに変更）
- `HOROSCOPE_GEONAMES_DB` — SQLite地名辞書へのパス（省略時: `data/geonames.sqlite3`）
- `PORT` — Cloud Run が指定する待受ポート

## 出生地検索：オフライン GeoNames

- `build_geonames_db.py` はビルド時に GeoNames の `cities500.zip`、`JP.zip`、`admin1CodesASCII.txt`、`countryInfo.txt` を取得し、SQLite の `data/geonames.sqlite3` を生成します。**ユーザーの地名検索時には外部の地名検索APIを呼びません。**
- Python のローカル実行前に `python build_geonames_db.py` を一度実行してください（インターネット接続が必要）。Dockerfile ではコンテナのビルド時に自動生成します。
- GeoNames データは CC BY 4.0。出典・ライセンス表記を維持してください。詳細は `THIRD_PARTY_NOTICES.md`。
- 収録データにない地名は検索できません。市区町村を省略するか、代替として `緯度,経度`（例 `34.65,134.99`）を入力してください。
- 入力例：`東京都千代田区`、`兵庫県明石市`、`Paris France`、`New York, USA`。**データ収録状況によるため、デプロイ前に実データでテストしてください。**
- 同名都市には曖昧さがあるため、地名の選択・表示機能を将来的に追加するのが望ましいです。
- アプリの位置検索は都市中心座標を使います。出生施設の正確な位置とは限りません。
- 定期的なデータ更新は、新版のDockerイメージを再ビルドして行ってください。

地名データ配布元：https://download.geonames.org/export/dump/ （CC BY 4.0）

## セキュリティ

`.env`、APIキー、認証トークン、出生情報の実データ、画像出力、Cloudの鍵ファイルをコミットしないでください。GitHubにアップロードする前に変更履歴・ファイル内容を確認してください。

## 関連リンク

- Kerykeion: https://github.com/g-battaglia/kerykeion
- pyswisseph: https://github.com/astrorigin/pyswisseph
- Swiss Ephemeris: https://www.astro.com/swisseph/swephinfo_e.htm
- AGPL-3.0: https://www.gnu.org/licenses/agpl-3.0.html
