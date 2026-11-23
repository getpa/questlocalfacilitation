# Quest Hub (GUI版) 技術検討・設計書

- ステータス: Draft v0.4（2026-11-23）— Phase 0a 実装済み、Phase 0b（PoC）実施済み（§7.1）
  - v0.2: 要件の確定事項（§1.1）を反映
  - v0.3: モジュールの責任分離を見直し（§3）、接続先SSIDの収集（R7, §4.9）を追加
  - v0.4: Mambaforge を前提にしない環境構築インストーラー（§9, Phase 0a）を追加
- 対象: 既存CLI（`scripts/quest_multi_scrcpy.sh` ほか）の機能をGUIで完結させる新コンポーネント
- 前提: macOS ホスト / Meta Quest 3 最大30台 / 同一LAN / scrcpy 5.0.1

---

## 1. 要求

| # | 要求 | 補足 |
|---|------|------|
| R1 | デバイスのTCPデバッグ有効化 | USB接続端末を検出し `adb tcpip` をGUIから実行 |
| R2 | IPアドレスの取得・保存 | 端末ごとに Alias / IP / ポート / 配置 を永続化 |
| R3 | 画面の表示（音声つき） | 最大30台をグリッド表示、1台拡大表示、選択した端末の音声を再生 |
| R4 | 録画の開始・停止（音声つき） | 全台/選択台、表示と独立に開始・停止 |
| R5 | HTTPSで接続できる簡易ビューワー | iPad等から閲覧。低フレームレートで負荷を抑える |
| R6 | 入力操作（可能であれば） | ホストGUIからタップ・キー送信。優先度は低 |
| R7 | **接続先SSIDの収集** | 端末ごとの SSID（+ BSSID・帯域・電波強度）を取得・表示・保存し、想定外のSSIDを警告 |

### 1.1 確定事項（2026-11-23）

| 項目 | 決定 | 設計への影響 |
|------|------|-------------|
| ホストのブラウザ | **Chrome 前提** | WebCodecs を標準で使う。デスクトップの殻は Chrome の `--app` ウィンドウ（§3.1） |
| 音声 | **必要** | scrcpy の音声ソケットを使う。録画は映像+音声、ライブ再生は選択した1台のみ（§4.4） |
| ビューワー | **iPad等、低fps、自己署名証明書OK** | HTTPS + WebCodecs に一本化。グリッドはキーフレームのみ配信（§4.6） |
| 入力操作 | **可能であれば** | 制御ソケットで実装。Phase 5（任意）とし、ホストGUIのみ（§4.5） |
| SSID | **収集する** | 接続状態の監視に Wi-Fi 情報を含める（§4.9） |

非機能要求（推定。要確認）
- N1: 30台 × 4Mbps（既存標準）で破綻しない。ホスト表示の遅延は scrcpy ネイティブ並み（〜100ms台）。
- N2: 端末の切断・再接続から自動で復帰（既存ワーカーと同等）。
- N3: 生徒の画面と音声を扱うため、ビューワーは LAN 内限定かつ認証付き。
- N4: 既存CLIと端末リスト（`quest_devices.tsv`）を互換に保つ。

---

## 2. 中心となる技術判断：映像・音声をどう扱うか

| 観点 | A. scrcpyプロセス管理型 | B. ストリーム自前受信型（推奨） |
|------|------------------------|-------------------------------|
| 仕組み | 1台=1 scrcpyウィンドウ。GUIは起動・停止・配置のみ | `scrcpy-server.jar` を直接起動し、H.264/音声を受けてアプリ内で配信 |
| R3 画面表示 | ◎ ネイティブ・低遅延（ただし別ウィンドウ30枚） | ○ GUIのタイルとして表示（WebCodecs・HWデコード） |
| R4 録画 | △ 停止/開始のたびに scrcpy 再起動 | ◎ 表示中でも録画だけ開始・停止（再エンコードなし） |
| R5 ビューワー | ✕ 映像を取り出せない | ◎ 同じストリームを配信 |
| R6 入力 | ◎ scrcpyのウィンドウで操作可 | ○ 制御ソケットを自前実装 |
| 端末負荷 | 1本 | 1本（視聴者が何人でも増えない） |
| 実装量 / リスク | 小 / 低 | 中〜大 / プロトコル互換（§7） |

**結論: B を採用します。** A は「ネイティブで開く」ボタン（1台だけ scrcpy ウィンドウで表示・操作）として残し、R6 が完成するまでの代わりにもします。B のキャプチャと同時に開くと端末側のキャプチャが2本になるため、開いている間は B の該当端末を一時停止します。

```
Quest 3 ──(adb forward)──▶ StreamSupervisor ──▶ FrameHub ─┬─▶ media_ws ─▶ ホストGUI (WebCodecs 映像+音声)
  scrcpy-server.jar          (1台1つ, 再起動担当)           │              └▶ iPadビューワー (キーフレームのみ/全フレーム)
                             video/audio/control           └─▶ Recorder (PyAV → MKV 映像+音声)
                             ◀── control (タップ/キー) ──── services/input ◀── ホストGUI
```

---

## 3. アーキテクチャ

### 3.1 全体方針：「ローカルWebアプリ + Chrome アプリウィンドウ」

| 候補 | 評価 |
|------|------|
| **Python (FastAPI + asyncio) + ブラウザUI** ✅ | 既存 conda 環境・`discover_quest3.py` の資産を活かせる。ホストGUIとiPadビューワーで画面の実装を共有できる |
| PySide6 (Qt) ネイティブ | ビューワーを別に作る必要があり二重実装 |
| Electron / Tauri | Node/Rust のツールチェーンが増える |

- 起動: `python -m questhub` → サーバー起動 → `open -na "Google Chrome" --args --app=http://127.0.0.1:8765` で**アドレスバーのない専用ウィンドウ**を開く。
- `.app` 化は任意です（起動スクリプトを Platypus 等で包む程度で十分）。

### 3.2 レイヤーと依存の規則

```
 UI（ブラウザ）      static/host  static/viewer  static/common
        │ HTTP / WebSocket
 Web / API          web/app  web/media_ws  web/auth  web/tls
        │
 アプリケーション     services/{provisioning, streaming, recording, input, telemetry}   core/events
        │
 ドメイン            devices/*            stream/*                 record/*
        │
 インフラ            adb/client  adb/tracker  PyAV  SQLite
```

**規則**
1. **依存は上から下への一方向だけ**。ドメインは services を、services は web を知らない。
2. **手順（オーケストレーション）は services にだけ書く**。web のハンドラは「入力の検証 → 認可の確認 → service を呼ぶ」だけ。
3. **状態変化は `core/events` に発行する**。上位層への通知はイベントで行い、上位を直接呼ばない。`/ws/events` はイベントを購読して転送するだけ。
4. **台帳への書き込みは `devices/registry` の公開APIだけ**。他のモジュールは観測結果を返すか、イベントとして出す。
5. **ドメインのモジュールは失敗しても再試行しない**。再試行・バックオフは生存管理を担うモジュール（`devices/manager`, `stream/supervisor`）だけが持つ。
6. **プロトコルの処理は純粋関数**（`stream/protocol`, `stream/control`, `devices/wifi_parse`）。I/O を持たず、ゴールデンテストの対象にする。

`core/events` はどの層からも使える横断モジュールです（依存先を持たない）。

### 3.3 モジュールと責任

```
questhub/
  __main__.py                  # 起動（サーバー + Chrome --app）、依存の組み立て（DI）
  config.py                    # 設定値（ポート、映像/音声、保存先、想定SSID）
  core/
    events.py                  # 型付きイベントバス（asyncio）
  adb/
    client.py                  # adb CLI の非同期ラッパー（.mamba-env 優先、タイムアウト付き）
    tracker.py                 # `adb track-devices -l` の常駐監視 → 生のイベント
  devices/
    registry.py                # 台帳の唯一の書き込み窓口（SQLite）、TSV 入出力
    manager.py                 # 接続状態の状態機械と再接続（映像の状態は持たない）
    provisioning.py            # 1台分の手順: IP取得 / tcpip / connect（台帳には書かない）
    discovery.py               # LANスキャン → (serial, ip) の一覧を返す
    quest.py                   # WAKEUP / 画面点灯待ち / Quest向け調整と復元
    telemetry.py               # 1台分の計測: バッテリー + Wi-Fi情報（R7）を返す
    wifi_parse.py              # dumpsys / cmd wifi 出力の解析（純粋関数）
  stream/
    supervisor.py              # 1台のメディアの生存管理（起動・再起動・停止、STREAMING状態）
    scrcpy_server.py           # 1回分の接続（push → 起動 → forward → 3ソケット受信）。再試行しない
    protocol.py                # ヘッダ・パケット解析（純粋関数）
    control.py                 # 制御メッセージのシリアライズ（純粋関数）
    hub.py                     # config/最新キーフレームのキャッシュ + 購読者への配信
  record/
    recorder.py                # hub を購読し、PyAV で MKV にストリームコピー
  services/
    provisioning.py            # ユースケース: USB端末をWi-Fi化して台帳に登録（一括・進捗）
    streaming.py               # ユースケース: 映像の開始・停止、CLIとのロック、ネイティブで開く
    recording.py               # ユースケース: 録画セッション、ディスク監視、ファイル名規則
    input.py                   # ユースケース: 単体・一括入力（%座標 → px）
    telemetry.py               # ユースケース: 定期計測、SSID不一致などの警告判定
  web/
    app.py                     # REST / WS のルーティングのみ
    media_ws.py                # 映像WSの購読アダプタ（間引き・バックプレッシャー）
    auth.py                    # admin / viewer の権限、アクセスコード、CSRF
    tls.py                     # ローカルCA・サーバー証明書・.mobileconfig
    static/{host,viewer,common}/
```

| モジュール | 責任 | 持たない責任 |
|-----------|------|-------------|
| `web/app` | ルーティング、入力検証、認可 | 手順の実行、ドメインの直接呼び出し |
| `web/media_ws` | 視聴者ごとの間引き（keyframes / every=N）、遅い購読者は次のキーフレームまで捨てる | キャッシュ、デコード |
| `services/*` | ユースケースの手順、複数モジュールの調整、イベント発行 | プロトコルの詳細、再試行ループ |
| `devices/registry` | 台帳の永続化、`observe()` によるIP/SSID変更の判定、衝突の解決 | adb 呼び出し |
| `devices/manager` | 接続状態（USB / TCPIP / CONNECTED / OFFLINE）、再接続のバックオフ | 映像・録画の状態、台帳への書き込み |
| `devices/telemetry` | 1回分のバッテリー・Wi-Fi情報の取得 | 定期実行（services/telemetry が担当） |
| `stream/supervisor` | 1台のメディアの生存管理、再起動、STREAMING の発行 | 接続の確立（manager の状態を見るだけ） |
| `stream/scrcpy_server` | 1回分の接続と受信 | 再試行 |
| `stream/hub` | config と最新キーフレームのキャッシュ、映像/音声の配信 | 視聴者ごとの方針 |
| `record/recorder` | 1ファイル分の多重化 | 対象の選択、ファイル名規則（services/recording） |

**台帳API（`registry.observe`）**

```python
registry.observe(serial, source, ip=None, port=None, wifi=None)
# source: "usb" | "scan" | "manual" | "telemetry"
# - IP・SSID の変更を判定し、DeviceObserved / IpChanged / SsidChanged を発行
# - 同じ IP が別の serial で観測されたら、古い方の IP を「未確認」に落とす
# - manual で入力した値は scan / telemetry で上書きしない（ロックフラグ）
```

### 3.4 状態は2つに分ける

v0.2 では1つの状態機械に接続・映像・録画を入れていましたが、持ち主を分けます。

**接続状態（`devices/manager` が持つ）**

```
            USB挿入                      tcpip成功+IP取得
 UNKNOWN ─────────▶ USB_READY ─────────────────────▶ TCPIP_ENABLED
   ▲                 │  ▲ (unauthorized → 「ヘッドセット内で許可を」表示)   │ adb connect
   │                 ▼  │                                              ▼
 OFFLINE ◀────────── 切断 ──────────────────────────────────── CONNECTED
   │  (3秒バックオフで再接続, 指数上限30秒)
   └─ 再起動等で tcpip 解除 → NEEDS_USB（「USBで再有効化が必要」バッジ）
```

**メディア状態（`stream/supervisor` が持つ）**

```
 IDLE ──start──▶ STARTING ──受信開始──▶ STREAMING ──異常終了──▶ BACKOFF ──▶ STARTING
   ▲                                       │                            
   └────────────────stop / 接続が OFFLINE──┘     録画中かどうかは services/recording が持つ
```

- supervisor は manager の `ConnectionChanged` イベントを購読し、CONNECTED になったら起動、OFFLINE になったら停止します。
- 画面上の「端末の状態」は、2つの状態と録画の有無をUIで合成して表示します。

### 3.5 代表的な処理の流れ

**映像開始**
1. `web/app` `POST /api/streams/{serial}/start` → `services/streaming.start(serial)`
2. service: CLI ロックを確認 → `manager` が CONNECTED か確認 → `quest.wake()` → `supervisor.start()`
3. supervisor: `scrcpy_server.run()` を呼び、受け取ったパケットを `hub` に流す → `MediaStateChanged(STREAMING)` を発行
4. `/ws/events` 経由で GUI のタイルが「表示中」になり、`/ws/media/{serial}` を購読する

**Wi-Fi化（R1/R2/R7）**
1. `services/provisioning.enable_wifi(serials)`
2. 1台ごとに: `devices/provisioning.read_identity()` → `telemetry.read_wifi()` → `provisioning.tcpip()` → `provisioning.connect()`
3. 結果を `registry.observe(serial, "usb", ip, port, wifi)` → `ProvisioningProgress` を発行

---

## 4. 機能別設計

### 4.1 R1: TCPデバッグ有効化

1. `adb/tracker` が `adb track-devices -l` を常駐監視し、`manager` が USB端末（シリアルに `:` を含まない）を USB_READY にする。
2. 「Wi-Fi化」ボタン（個別/一括）→ `services/provisioning`（§3.5）。
   - IP取得: `ip -o -4 addr show scope global`、だめなら `getprop dhcp.wlan0.ipaddress`（既存 `usb_to_tcp.sh` の `detect_ip` を移植）
   - `adb -s SERIAL tcpip 5555` → `adb connect IP:5555` で疎通を確認
3. USBを抜いてよいタイミングをGUIで明示する。`unauthorized` はヘッドセット内の許可待ちとして案内する。

### 4.2 R2: IPアドレスの取得・保存

- 台帳 `~/.questhub/devices.sqlite`

  | 列 | 例 |
  |----|----|
  | serial (PK) | 2G0YC5ZG8L0123 |
  | alias | Quest-01 |
  | ip / port / ip_locked | 192.168.10.21 / 5555 / false |
  | grid_col / grid_row | 0 / 0（空なら自動配置） |
  | ssid / bssid / band / rssi / wifi_seen_at | §4.9 |
  | model, last_seen, notes | |

- 観測履歴テーブル `observations(serial, source, ip, ssid, bssid, at)` に変更の履歴を残す（トラブルシュート用。直近N件に制限）。
- IP更新の経路: (a) Wi-Fi化時、(b) LANスキャン、(c) 手入力 — いずれも `registry.observe()` を通る。
- `scripts/quest_devices.tsv` 形式で読み込み・書き出しでき、CLIとの互換を保つ。
- 表の直接編集と、グリッド上のドラッグ＆ドロップによる座席配置。

### 4.3 R3: 画面表示

**サーバー側**
1. `scrcpy_server`: `scrcpy-server` を `/data/local/tmp/` に push（ハッシュが同じならスキップ）。
2. `adb forward tcp:<空きポート> localabstract:scrcpy_<SCID>`（SCIDは31bit乱数）。
3. `app_process / com.genymobile.scrcpy.Server 5.0.1 scid=<SCID> tunnel_forward=true audio=true audio_source=playback audio_dup=true control=true cleanup=true max_size=1280 max_fps=30 video_bit_rate=4000000 video_codec_options=<GOP設定>`（キー名は `Options.java` で要確認）
4. ソケットは **映像 → 音声 → 制御** の順に接続する。最初のソケットでダミーバイトと端末名、各ストリームの先頭でコーデックIDを受け取る。以降、映像はセッションパケット（解像度。回転時に再送）とメディアパケット（12バイトヘッダ: config/key フラグ + PTS 61bit + サイズ）を受け取る。
5. `hub` は**直近の config（SPS/PPS）と最新のキーフレーム**をキャッシュし、途中から来た購読者にすぐ渡す。

**クライアント側（ホストGUI / Chrome）**
- `/ws/media/{serial}` でバイナリフレームを受信 → **WebCodecs `VideoDecoder`**（Annex B、codec文字列は SPS から `avc1.PPCCLL` を生成）→ `<canvas>` に描画。
- 画面外のタイルや非表示のタブではデコードを止める（`IntersectionObserver` / `visibilitychange`）。
- **キーフレーム間隔は 1秒** に短縮する（`video_codec_options`）。`RESET_VIDEO (type=17)` で即時にキーフレームを要求できるかも検証する（§7 P3）。

### 4.4 音声（R3/R4）

| 項目 | 設計 |
|------|------|
| 取得 | `audio_source=playback` + `audio_dup=true`（ヘッドセットのスピーカーは鳴ったまま）。SDK 33未満または失敗時は `audio=false` で映像のみにして警告する |
| コーデック | **Opus**（scrcpy標準、128kbps）。30台で約4Mbps |
| 録画 | 映像と同じ MKV に Opus トラックとしてストリームコピー。A/V の同期は双方の PTS（µs）で取る |
| ライブ再生（ホスト） | **選択した1台のみ**。WebCodecs `AudioDecoder`(opus) → AudioWorklet のリングバッファ（約80ms） |
| ライブ再生（iPad） | 拡大表示中の1台のみ、任意でON。Safari の Opus 対応は要検証（§7 P7） |

### 4.5 R6: 入力操作（可能であれば）

- 制御ソケット（`control=true`）は常に張る（`RESET_VIDEO` にも使う）。
- **ホストGUIの拡大ビューでのみ**操作可能。iPadビューワーは閲覧専用。
  - マウス → `INJECT_TOUCH_EVENT`（canvas座標から端末解像度への変換は `static/common` で行い、端末座標で送る）
  - BACK / HOME / WAKEUP（`INJECT_KEYCODE`）、テキスト（`INJECT_TEXT`）
  - **一括送信**: `services/input` が選択端末に同じキー、または % 座標のタップを送る
- 経路: `/ws/control/{serial}` → `services/input` → `supervisor` が持つ制御チャネル → `stream/control` でシリアライズ。
- VR の 3D UI ではタッチの注入が効かない場面がある。その場合は「ネイティブで開く」で代用する。

### 4.6 R5: iPadビューワー（HTTPS・低fps）

**方式: HTTPS + WebCodecs に一本化**（iPadOS 16.4 以降の Safari は H.264 の `VideoDecoder` に対応）。

**証明書**
- Safari では証明書の警告を許可しても `wss://` には例外が効かないことがあるため、警告を許可するだけの運用には頼らない。
- `web/tls` が初回起動時に**ローカルCA**を生成し、サーバー証明書（SANにホストのIP・ホスト名）を発行する。
- iPad への導入（1台につき1回）: `http://<host>:8766/ca` の QR → 構成プロファイルをインストール → 「一般 > 情報 > 証明書信頼設定」で信頼をON。
- ホストのIPが変わったらサーバー証明書だけを自動で再発行する。

**低fps配信（`web/media_ws` の責任）**
| モード | 中身 | fps（GOP 1秒時） | 帯域/台 |
|--------|------|-----------------|--------|
| グリッド（標準） | キーフレームのみ。N個に1個に間引き可 | 1 / 0.5 / 0.2 fps | 数百kbps程度（要実測） |
| 拡大 | 全フレーム | 端末の設定どおり（標準30） | 4Mbps |

- 拡大は視聴者1人につき1台まで、全体の上限（例: 4本）を超えたらグリッドモードに戻す。

### 4.7 R4: 録画

- `services/recording` が対象を選び、ファイル名を決め、端末ごとに `recorder` を生成する。`recorder` は hub を購読して1ファイルを書くだけ。
- 開始時は hub のキャッシュ（config + キーフレーム）から書き始めるため、開始の遅れは最大でもGOP1つ（1秒）。
- **PyAV でストリームコピー**（H.264 + Opus → **MKV**）。停止後に任意で MP4 に remux。
- ファイル名: `recordings/<セッション名>/<Alias>_<YYYYmmdd-HHMMSS>[_partNN].mkv`。supervisor の再起動で途切れた場合は `_partNN` で分割。
- 録画セッションのメタデータ（端末、IP、**SSID**、開始・終了時刻、途切れの記録）を `session.json` として保存する。
- ディスク残量の監視（閾値を下回ったら警告し、自動停止）。

### 4.8 セキュリティ

- 管理GUIは `http://127.0.0.1:8765`（localhost なので WebCodecs が使える）。ビューワーは `https://0.0.0.0:8443`（有効化したときだけ）。CA配布は `http://0.0.0.0:8766/ca`（CA証明書のダウンロードのみ）。
- ビューワー用のアクセスコード（セッションごとにランダム、QRに含める）。管理APIは localhost のみ + CSRFトークン。制御（R6）は管理側だけ。
- ビューワーに返す端末情報から SSID・BSSID・IP は除く（ネットワーク構成を閲覧者に見せない）。
- CA秘密鍵は `~/.questhub/tls/`（権限600）。授業終了時に「ビューワー停止」と「全端末の `adb usb`」を1クリックで実行できる。

### 4.9 R7: 接続先SSIDの収集

**目的**: 端末が想定外のネットワーク（ゲストWi-Fi、別のAP、テザリング等）に接続していることを早期に発見し、AP の分散状況（agents.md §5, §8）を把握する。

**取得方法（`devices/telemetry` + `devices/wifi_parse`）**

| 優先 | コマンド | 取れる項目 | 備考 |
|------|---------|-----------|------|
| 1 | `adb shell cmd wifi status` | SSID, BSSID, RSSI, 周波数, リンク速度 | Android 11以降。出力形式は版で揺れるため解析は寛容に |
| 2 | `adb shell dumpsys wifi` の `mWifiInfo` 行 | SSID, BSSID, RSSI, Link speed, Frequency | 出力が大きい（数百KB）ので `grep` 相当の絞り込みを端末側で行う |
| 3 | `adb shell dumpsys connectivity` | SSID（NetworkCapabilities） | 予備 |

- 解析結果は `WifiInfo(ssid, bssid, rssi_dbm, freq_mhz, band, link_mbps)`。周波数から帯域（2.4 / 5 / 6 GHz）を求める。
- `<unknown ssid>` や空の場合は「取得不可」として扱い、警告はしない（Quest のプライバシー制限で隠される可能性。§7 P9）。

**取得のタイミング（`services/telemetry`）**
- Wi-Fi化の直前（USB接続中。SSIDとIPを同時に記録）
- 接続直後（CONNECTED への遷移時）
- 定期計測: バッテリーと同じ周期（標準60秒）。RSSI は変動が大きいので UI では直近の値と5分間の最小値を表示
- 映像の異常終了・切断の直後（原因の切り分け用）

**判定と表示**
- 設定の「想定SSID」（複数可）と比べて、一致しなければ **黄色の警告バッジ**。端末一覧に SSID / 帯域 / RSSI / BSSID の列を出す。
- 2.4GHz 接続や RSSI が -70dBm 未満の端末には注意を表示する（帯域不足・切断の予兆）。
- BSSID ごとの台数を集計して表示し、1台の AP に端末が偏っていないか確認できるようにする。
- ホスト Mac の SSID との自動比較は行わない（macOS 14 以降は位置情報の権限なしに SSID を取得できない場合があるため）。想定SSIDは設定画面で入力する。

**保存**
- 最新値は台帳（`ssid`, `bssid`, `band`, `rssi`, `wifi_seen_at`）、変更は観測履歴に残す（`registry.observe(..., wifi=...)` 経由。SSID が変わったら `SsidChanged` を発行）。
- TSV エクスポートには含めない（既存CLIの形式を変えないため）。別途 CSV で一覧を書き出せる。

---

## 5. API（案）

| メソッド | パス | 用途 | 権限 |
|---------|------|------|------|
| GET | `/api/devices` | 台帳 + 現在状態（viewer には SSID/IP を除いて返す） | viewer |
| PATCH | `/api/devices/{serial}` | alias / 配置 / IP 編集 | admin |
| POST | `/api/devices/{serial}/tcpip` | R1 Wi-Fi化 | admin |
| POST | `/api/devices/{serial}/connect` / `disconnect` | 接続管理 | admin |
| POST | `/api/scan` | LANスキャン | admin |
| GET/POST | `/api/registry/export.tsv` / `import` | TSV互換 | admin |
| GET | `/api/network` | SSID・BSSID ごとの集計、警告一覧（R7） | admin |
| GET | `/api/network/export.csv` | 端末ごとの Wi-Fi 情報の書き出し | admin |
| PUT | `/api/settings/expected-ssids` | 想定SSIDの設定 | admin |
| POST | `/api/streams/{serial}/start` / `stop` | 映像の開始・停止 | admin |
| POST / DELETE | `/api/recordings` `{serials:[...]\|"all", session}` | 録画の開始 / 停止 | admin |
| POST | `/api/input/broadcast` `{serials, keycode\|tap%}` | 一括入力（R6） | admin |
| WS | `/ws/events` | 状態・バッテリー・Wi-Fi・録画進捗の push | viewer（項目を制限） |
| WS | `/ws/media/{serial}?video=full\|keyframes&every=N&audio=0\|1` | 映像・音声 | viewer |
| WS | `/ws/control/{serial}` | タップ・キー（R6） | admin |
| GET | `/`, `/view`, `/view/{serial}`, `/ca` | 管理GUI / ビューワー / CA配布 | – |

**主なイベント（`core/events`）**: `UsbDeviceAttached`, `ConnectionChanged`, `MediaStateChanged`, `DeviceObserved`, `IpChanged`, `SsidChanged`, `TelemetryUpdated`, `NetworkWarning`, `ProvisioningProgress`, `RecordingProgress`, `RecordingStopped`

---

## 6. 性能見積もり

| 項目 | 見積もり | 根拠・懸念 |
|------|---------|-----------|
| Wi-Fi 受信 | 30 × (4Mbps + 0.13Mbps) ≒ 124Mbps | 既存CLI + 音声 |
| Python 側の処理 | 約900映像 + 約1500音声パケット/秒の転送 | asyncio で十分。デコードはしない |
| 計測（バッテリー + Wi-Fi） | 30台 × 2コマンド / 60秒 | `adb` の同時実行数を8程度に制限する |
| 録画 | ストリームコピー × 30 | ディスク書き込みは約15MB/s |
| ホスト Chrome のデコード | H.264 720p級 × 30 | **要検証（最大リスク）** |
| iPad | キーフレームのみ × 30 + 拡大1 | iPad 側のデコーダ上限は要実測 |

---

## 7. リスクと事前検証（PoC）項目

| # | 検証内容 | 失敗した場合の代替策 |
|---|---------|------------|
| P1 | scrcpy-server 5.0.1 を直接起動し、Quest 3 から映像・音声・制御の3ソケットを確立できるか（公式 develop.md は「4.0 時点のプロトコル」と記載。**5.0.1 の実機とソースで確認**） | server のバージョンを固定し、`Options.java` に合わせる |
| P2 | Chrome で WebCodecs のデコーダ30本が同時に動くか | グリッドはキーフレームのみ / 低fps描画、または表示を2画面に分ける |
| P3 | `video_codec_options` で i-frame 間隔を 1秒にできるか。`RESET_VIDEO` でキーフレームが出るか | 録画開始と iPad の低fps配信の間隔が標準のGOPに依存する |
| P4 | iPad Safari で自作CA信頼後に `wss://` + WebCodecs が動くか | MSE（jmuxer）でフォールバック |
| P5 | Horizon OS の近接センサーによる黒画面が直接起動でも起きるか | 既存の WAKEUP / prox_close を `quest.py` に移植済みのものを使う |
| P6 | Quest 3 で `audio_source=playback` + `audio_dup` が動くか | `audio_dup` なし（ヘッドセットが無音）を選択式にする |
| P7 | iPad Safari の `AudioDecoder` が Opus に対応しているか | iPad では音声なし |
| P8 | タッチ注入が Quest の 2D パネルで効くか | 入力はキー送信だけにし、細かい操作は「ネイティブで開く」 |
| P9 | Quest 3 で `cmd wifi status` / `dumpsys wifi` から SSID が取得できるか（`<unknown ssid>` に伏せられないか） | `dumpsys connectivity`、それも不可なら BSSID / IPサブネットだけで AP を識別 |

### 7.1 PoC 結果（2026-11-23, Quest 3 / Horizon OS Android 14, Wi-Fi 6E）

実装: `questhub/`（`stream/protocol.py`, `stream/scrcpy_server.py`, `stream/hub.py`, `record/recorder.py`, `devices/wifi_parse.py`, `poc/`）。起動: `./.mamba-env/bin/python -m questhub.poc`。

| # | 結果 | 実測・備考 |
|---|------|-----------|
| P1 | ✅ | 公式 5.0.1 サーバーを直接起動し、映像・音声・制御の3ソケットを確立（接続まで約1秒）。プロトコルは v5.0.1 のソース（`Streamer.java` / `DesktopConnection.java`）に一致: コーデックID(4B) → セッションパケット(12B, MSB=1, 幅・高さ) → メディアパケット(12Bヘッダ, bit62=config, bit61=key, ビッグエンディアン)。ダミーバイトは最初のソケットの accept 直後、端末名(64B)は全ソケット接続後 |
| P2 | 一部 | 1台は Chromium（アプリ内ブラウザ）で WebCodecs 30fps・エラー0。30台の同時デコードは未検証（実機が1台のため） |
| P3 | ✅（注記あり） | `video_codec_options=i-frame-interval=1` を指定しても Quest のエンコーダは約2秒間隔。`RESET_VIDEO`(17) はキャプチャを再起動し、約0.1秒でキーフレーム（セッション・config も再送）。録画開始時に使用 |
| P4 | 未 | iPad 実機なし |
| P5 | 未確認 | 今回の取得では黒画面にならなかった。ただし装着状態は記録していないため、未装着時の挙動は別途確認が必要 |
| P6 | ✅ | `audio_source=playback` + `audio_dup=true` で Opus 48kHz/2ch を取得。ヘッドセット側の音も残る |
| P7 | 未 | iPad 実機なし |
| P8 | 未 | PoC では入力未実装 |
| P9 | ✅ | `cmd wifi status` で SSID・BSSID・RSSI・周波数・リンク速度が取得できた（伏せ字なし） |

その他の知見
- 映像は 1280×684（両眼並列）、静止に近い画面で約0.7Mbps。
- PyAV の `add_mux_stream` は extradata を設定できないため、録画は「テンプレートストリーム」（H.264 は config+キーフレームを h264 demuxer で開いたもの、Opus は libopus で生成した1パケットの Ogg）から出力ストリームを作り、codec_context.extradata に端末の SPS/PPS・OpusHead を設定する方式にした。MKV は H.264(avcC) + Opus で再生・デコードできることを確認。
- 映像と音声の PTS は同じ単調時計で、録画の先頭キーフレームを基準にすると音声の開始ずれは約15ms。
- 録画開始時に最初のキーフレームより音声 config が遅れることがあるため、最大1秒待ってから書き始める。
- サーバーを端末側で強制終了しても、セッションは3秒（テストでは1秒）後に自動で再接続した。

**PoC（1台）: 映像+音声を受信 → Chrome に表示・再生 → MKV 録画 + SSID 取得**。P1・P3・P6・P9 を最初に確認し、P2 は台数を増やして負荷試験します。失敗した場合は方式A に切り替えます。

---

## 8. 開発フェーズ

| Phase | 内容 | 完了条件 |
|-------|------|---------|
| 0a. インストーラー | `install.sh`（micromamba）、`environment.yml` の分割、`bootstrap_binaries.sh` の修正、lock、`--doctor`、CI | conda のない macOS で `./install.sh` → `--doctor` が全項目 OK（§9） |
| 0b. PoC | P1/P3/P6/P9 の検証（1台）、P2 の負荷試験 | 映像と音声が Chrome で再生でき、MKV が再生でき、SSID が取れる |
| 1. 基盤と端末管理 | `core/events`、`adb/*`、`devices/*`、`services/provisioning`・`telemetry`、台帳UI | GUIだけで USB端末 → Wi-Fi接続 → 台帳保存（IP・SSID）まで完了する |
| 2. 表示 | `stream/*`、`services/streaming`、`web/media_ws`、グリッドUI、1台の音声再生、ネイティブで開く | 用意できる最大台数を安定表示し、切断から復帰できる |
| 3. 録画 | `record/recorder`、`services/recording`、ディスク監視、`session.json` | 表示を止めずに録画の開始・停止ができる |
| 4. iPadビューワー | `web/tls`、アクセスコード、キーフレームのみのグリッド、拡大 | iPad から低fpsのグリッドと拡大1台を閲覧できる |
| 5. 入力（任意） | `stream/control`、`services/input`、タップ/キー、一括送信 | 拡大ビューから BACK/HOME とタップが効く |
| 6. 仕上げ | Chrome `--app` 起動、Quest向け調整のUI、ネットワーク集計画面、終了時の一括 `adb usb` | TAが手順書なしで操作できる |

**テスト方針**
- 純粋関数（`protocol`, `control`, `wifi_parse`）: 実機の出力サンプルを fixtures に保存し、ゴールデンテストする。`wifi_parse` は Android の版ごとの出力差を fixtures で網羅する。
- ドメイン: 偽の adb（既存 tests と同じ方式）と**偽の scrcpy-server**（録画済みの H.264/Opus をプロトコル形式で流すTCPサーバー）で検証する。
- services: ドメインを差し替えて、手順とイベントの発行順を検証する（web を通さずにテストできることが §3.2 の規則の狙い）。
- CI（macOS / Ubuntu）は実機なしで回す。

## 9. 環境構築インストーラー（bootstrap）

### 9.1 目的と前提

- **Mambaforge / conda / Homebrew / Xcode Command Line Tools がない素の macOS** で、1コマンドで GUI が起動できる状態にする。
- 管理者権限（sudo）を使わない。ユーザーのシェル設定（`~/.zshrc` 等）を書き換えない。入れるものはすべて**リポジトリ内**に置き、フォルダを消せば元に戻る。
- 既存の CLI（`scripts/*.sh`）が参照する `./.mamba-env` をそのまま作るので、CLI もこのインストーラーで使えるようになる。

### 9.2 素の macOS での落とし穴（現状の調査結果）

| 落とし穴 | 現状 | 対策 |
|---------|------|------|
| `python3` / `git` は CLT のシム。実行すると**CLTのインストールダイアログ**が出て止まる | `bootstrap_binaries.sh` は環境の PATH を通す**前**に `python3` を呼び、`git` の存在を無条件に要求している | インストーラーは `curl` / `tar` / `shasum` / `uname` など OS 標準のコマンドだけを使う。`bootstrap_binaries.sh` の `python3` 呼び出しを `cd && pwd` に置き換え、`git` と `java` の確認をソースビルド時だけにする |
| macOS 標準の bash は 3.2 | — | インストーラーは bash 3.2 互換で書く（連想配列や `${v,,}` を使わない）。CI で `/bin/bash` を使ってテストする |
| `environment.yml` にソースビルド用の重い依存（meson, ninja, sdl3, ffmpeg, openjdk）が入っている | 公式リリースを使う通常の手順では不要 | **実行用** `environment.yml` と**ビルド用** `environment-build.yml` に分ける。通常は実行用だけ入れる（ダウンロード量とインストール時間を大幅に削減） |
| platform-tools は `latest` を検証なしでダウンロード | 再現性・改ざん検知がない | バージョンと SHA-256 を固定するオプションを追加（既定は従来どおり latest、`--strict` で固定値を必須に） |
| macOS 15 以降の「ローカルネットワーク」権限 | 未拒否のターミナル/Python が LAN の端末へ接続すると許可ダイアログが出る。拒否すると `adb connect` が `No route to host` になる | 診断（`--doctor`）で検出し、システム設定のどこで許可するかを表示する |
| Rosetta 上のターミナル | `uname -m` が `x86_64` を返し、Intel 版を入れてしまう | `sysctl -n sysctl.proc_translated` で判定し、Apple Silicon なら arm64 版を入れる |

### 9.3 構成

```
install.sh                     # エントリーポイント（bash 3.2 互換）
environment.yml                # 実行用: python, av, fastapi, uvicorn, cryptography, qrcode, requests, jq ほか
environment-build.yml          # ソースビルド用の追加分: meson, ninja, sdl3, ffmpeg, libusb, openjdk, git, pkg-config
locks/
  conda-osx-arm64.txt          # explicit lock（URL + md5）。.gitignore の *.lock を避けて .txt
  conda-osx-64.txt
questhub.command               # 起動用ラッパー（.mamba-env/bin/python -m questhub）。activate 不要、Finder からダブルクリック可
                               #（`questhub/` パッケージと名前が衝突しないよう .command にした）
.tools/                        # （生成物・gitignore）micromamba 本体とパッケージキャッシュ
.mamba-env/                    # （生成物・gitignore）実行環境
```

### 9.4 install.sh の処理

1. **前提の確認**: macOS 13 以降、アーキテクチャ（Rosetta を考慮）、空き容量（2GB以上）、`curl` で GitHub / conda-forge / dl.google.com に届くか（プロキシは `HTTPS_PROXY` を尊重）。
2. **micromamba の導入**: `mamba-org/micromamba-releases` から**固定バージョン**の単一バイナリを取得し、同じリリースの `.sha256` で検証して `.tools/bin/micromamba` に置く。`MAMBA_ROOT_PREFIX=.tools/mamba-root` でキャッシュもリポジトリ内に閉じる。
3. **実行環境の作成**: lock ファイルがあれば `micromamba create -p ./.mamba-env --file locks/conda-<plat>.txt`（全パッケージのハッシュを検証）、なければ `environment.yml` から作成。lock / yml の内容のハッシュを `.mamba-env/.questhub-spec.sha256` に記録し、変化がなければ何もしない。変化があれば `vendor/`（adb・scrcpy）を退避して作り直す（不要になったパッケージを残さないため）。
4. **バイナリの導入**: `scripts/bootstrap_binaries.sh ./.mamba-env` を呼ぶ（adb と scrcpy 5.0.1。scrcpy は SHA-256 で検証済みの既存処理を使う）。
5. **アプリの準備**: `./questhub.command` に実行権限を付け、`~/.questhub/`（権限700）を作成する（証明書と台帳の置き場）。
6. **Chrome の確認**: `/Applications/Google Chrome.app` がなければ、ダウンロードページの案内を表示する（自動では入れない）。
7. **診断**: 最後に `--doctor` と同じ検査を実行し、結果を表で表示する。

**オプション**

| オプション | 動作 |
|-----------|------|
| （なし） | 上記 1〜7。2回目以降は差分のみで冪等 |
| `--doctor` | 検査だけ行う: micromamba / Python / PyAV の import / adb / `scrcpy --version` が 5.0.1 / Chrome / ポート 8765・8443 の空き / ローカルネットワーク権限 / `adb devices` |
| `--build-from-source` | `environment-build.yml` も入れ、`SCRCPY_BUILD_FROM_GIT=1` で bootstrap する（このときだけ CLT が必要なので確認する） |
| `--strict` | lock ファイルと platform-tools の固定ハッシュを必須にする（授業用PCの再現性重視） |
| `--uninstall` | `.tools/` と `.mamba-env/` を削除する（`~/.questhub/` の台帳と証明書は `--purge-data` のときだけ削除。実行前に確認を出す） |

**配布方法**
- 開発者: `git clone` 後に `./install.sh`。
- 非開発者（git なし）: GitHub のリリース tarball を取得して展開し、`install.sh` を実行するワンライナーを README に載せる（リポジトリが公開されている場合のみ。§10 の未決事項）。
- 任意: `install.sh` の最後に、`./questhub` を呼ぶだけの `Quest Hub.app`（AppleScript を `osacompile`）を生成し、Dock から起動できるようにする。

### 9.5 lock ファイルの運用

- `environment.yml` を変えたら、`./install.sh --relock osx-arm64` と `--relock osx-64` で作り直してコミットする。どちらの Mac でも両方を生成できる（`micromamba create --dry-run --json --platform <plat>` の解を `osascript -l JavaScript` で explicit 形式に変換。`env export` は他アーキテクチャの環境を読めないため使わない）。
- 授業用PCは `--strict` で lock からのみ構築し、全台で同じパッケージにそろえる。

### 9.6 テスト

- CI（`macos-latest` = Apple Silicon、`macos-15-intel` = Intel）で、PATH から conda を外した状態で `/bin/bash ./install.sh --strict` → 再実行（no-op）→ `--doctor` → `--uninstall` を実行する。
- オフラインテスト（`tests/test_installer.py`）: 偽の micromamba で、lock の使用、`--no-rc` とキャッシュの隔離、冪等性、spec 変更時の再作成と `vendor/` の保持、`--strict` の lock 必須、アンインストールの安全装置を検証する。
- 2回目の実行が差分なしで数秒で終わること（冪等性）、`--uninstall` で生成物がすべて消えることもテストする。
- `shellcheck --shell=bash` に加えて、bash 3.2 で構文チェックする。

### 9.7 依存一覧

- 実行用（`environment.yml`）: `python=3.11`, `av`（PyAV）, `fastapi`, `uvicorn`, `cryptography`（CA/証明書）, `qrcode`, `requests`, `jq`, `coreutils`, `curl`, `unzip`
- ビルド用（`environment-build.yml`）: `git`, `meson`, `ninja`, `pkg-config`, `sdl3`, `ffmpeg`, `libusb`, `openjdk=17`
- フロントエンドはビルド不要の ES Modules で、Node は不要です。

## 10. 未決事項

1. iPad のグリッドの既定fps（1 / 0.5 / 0.2）と、拡大の同時視聴の上限 — PoC の実測後に決める。
2. 録画の納品形式（MKV のままか、停止後に MP4 に変換するか、音声を AAC にするか）。
3. iPadOS の最低バージョン（16.4以上を前提にしてよいか）。
4. 想定SSIDの運用（教室ごとに切り替えるか、全教室共通の一覧か）。
5. リポジトリを公開するか（非開発者向けのワンライナー配布ができるかが決まる）。非公開なら、配布用の tarball を別途用意する方法を検討する。
6. 対象 macOS の最低バージョン（13 Ventura 以上を想定）と、Intel Mac をサポートし続けるか。
