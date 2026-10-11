# 星読録・ホロスコープ生成機 / Horoscope Generator

出生図、トランジット、シナストリー、ソーラーリターンを生成する FastAPI アプリケーションです。

## ライセンスと重要事項

このリポジトリの生成機プログラムには GNU Affero General Public License version 3.0 (AGPL-3.0) を適用する予定です。**公開リポジトリを作成するとき、GitHub のライセンステンプレートで「GNU Affero General Public License v3.0」を選んで `LICENSE` を追加してください。** `LICENSE` が存在しない状態のまま一般公開しないでください。

サードパーティーのライブラリは `THIRD_PARTY_NOTICES.md` を参照してください。ライブラリのライセンスをこのリポジトリのライセンスで上書きすることはできません。

このリポジトリの対象はホロスコープ生成機のプログラムです。生成機と独立した小説、記事、分析データ、商標、ロゴ等を自動的にこのライセンスで公開するという意味ではありません。ただしプログラムに組み込まれた素材・一体化したファイルは別途権利を確認してください。

## 構成

- `main.py` — FastAPI、HTML、チャート生成、アクセス制限
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

- `HOROSCOPE_GEOCODING_ENABLED=0` — 外部地名検索の緊急停止。`1` で有効
- `HOROSCOPE_SOURCE_URL=https://github.com/OWNER/REPO` — 画面上の対応ソース案内（公開リポジトリの実際のURLに変更）
- `HOROSCOPE_NOMINATIM_DOMAIN` — 利用するNominatimホスト。既定はOSMF公共サーバー
- `PORT` — Cloud Run が指定する待受ポート

## 公共Nominatim利用上の制限

現在の検索制限・キャッシュは **プロセス内だけ** に存在し、Cloud Runの再起動・複数リビジョン・一時的な並列インスタンス間で共有されません。これだけでOSMF公共Nominatimの「アプリ全体で最大1リクエスト/秒」を保証できません。一般公開には全体制御、セルフホスト、または条件に適合する代替サービスを検討してください。

公共サーバーの利用規約: https://operations.osmfoundation.org/policies/nominatim/

## セキュリティ

`.env`、APIキー、認証トークン、出生情報の実データ、画像出力、Cloudの鍵ファイルをコミットしないでください。GitHubにアップロードする前に変更履歴・ファイル内容を確認してください。

## 関連リンク

- Kerykeion: https://github.com/g-battaglia/kerykeion
- pyswisseph: https://github.com/astrorigin/pyswisseph
- Swiss Ephemeris: https://www.astro.com/swisseph/swephinfo_e.htm
- AGPL-3.0: https://www.gnu.org/licenses/agpl-3.0.html
