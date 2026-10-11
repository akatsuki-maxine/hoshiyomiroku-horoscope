# 星読録 — 出生地検索をGeoNamesローカル辞書へ移行する手順

## 重要
- 既存のCloud Runは**非公開のまま**維持してください。
- 検索時に Nominatim を呼び出す処理はこの版でなくなります。都市の座標は GeoNames を元にした SQLite辞書から探します。
- `cities500.zip` + `JP.zip` は Docker **ビルド時** に公式配布サイトから取得するため、Cloud Runアプリの実行中は外部地名検索APIに通信しません。
- 初回Dockerビルドおよびデータ更新ではGeoNamesへの接続が必要です。
- このパッケージは小規模な**合成テスト**と構文確認を行っています。実際のGeoNames配布データを使ったテストはCloud Shell側で行ってください。
- 当該zipに`LICENSE`は入っていません。既存GitHubリポジトリにある正式なAGPL-3.0 `LICENSE`を維持してください。

## 1. 現状のバックアップ（Cloud Shell）

```sh
cp ~/horoscope-app/main.py ~/horoscope-main-before-geonames.py
cp ~/horoscope-app/Dockerfile ~/horoscope-Dockerfile-before-geonames
```

## 2. ZIPを Cloud Shellへアップロードして展開

MacにダウンロードしたZIPを Cloud Shell「︙」→「アップロード」で `hoshiyomiroku_geonames_offline_v1.zip` としてアップロード。

```sh
cd ~/horoscope-app
unzip -o ~/hoshiyomiroku_geonames_offline_v1.zip
python3 -m py_compile main.py geonames_lookup.py build_geonames_db.py
python3 -m unittest -v test_offline_geonames.py
```

## 3. 本物の地名辞書を作って、検索結果を確認

```sh
python3 build_geonames_db.py
python3 - <<'PY'
from geonames_lookup import lookup_place
for name in ['東京都千代田区', '兵庫県明石市', '京都府京都市', 'Paris France', 'New York, USA']:
    try:
        print(name, '->', lookup_place(name))
    except ValueError as e:
        print(name, '-> 検索不可:', str(e))
PY
```

**注意:** 入力地点が正しく検索できるかを確認し、表示座標と地図を照合してください。異なる都市が選ばれる場合はまだ公開しないでください。

## 4. Cloud Runに非公開でデプロイ（実データの確認後）

```sh
gcloud run deploy horoscope-web --source . --project=horoscope-generator-511308 --region=asia-northeast1 --cpu=1 --memory=1Gi --min=0 --max=1 --concurrency=1 --timeout=120 --cpu-throttling --no-allow-unauthenticated
```

Cloud ShellのDBは `.gcloudignore` で除外され、Cloud Buildで辞書を改めて生成します（公式データのダウンロードが必要）。

## 5. 非公開プレビュー確認

```sh
gcloud run services proxy horoscope-web --region=asia-northeast1 --project=horoscope-generator-511308 --port=8080
```

Webプレビューで出生図・トランジット・シナストリー・ソーラーリターンを確認し、Cloud ShellでCtrl+Cでプロキシを終了してください。

## 6. GitHubへ反映

GitHub `https://github.com/akatsuki-maxine/hoshiyomiroku-horoscope` に、ZIP内の追加・変更ファイルをアップロードし、Commitしてください。既存の `LICENSE` は削除しない。`data/geonames.sqlite3`、GeoNamesダウンロードZIP、個人情報や認証ファイルはアップロードしない。実稼働しているコードとGitHubソースを一致させる。

## トラブル時

GeoNamesデータのダウンロードエラーでCloud Runの新リビジョンのビルドが失敗しても、通常は以前の稼働中リビジョンが残ります。GitHubとCloud Runの整合性に注意してください。
