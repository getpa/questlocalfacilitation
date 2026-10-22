# agents.md — ローカルWi-Fi上の **複数台 Meta Quest 3** を **scrcpy** で一括操作・同時モニタリングする運用ガイド

> 目的：研究室・授業などで **最大30台の Quest 3** を **ワイヤレス ADB + scrcpy** で同時モニタリングし、必要に応じて入力補助や録画を行うための実践手順とスクリプト集。  
> 想定OS：**macOS（Apple Silicon / Intel）**。Windows / Linux 向けの記述は省略。  
> 注意：scrcpy は **1インスタンス=1台** 表示が基本です。**完全な入力同期ブロードキャスト機能はありません**（`adb shell input` で最低限の一括操作を補助）。

---

## 0. 構成の全体像

- **各 Quest 3**：Developer Mode ON → USB デバッグ常時許可 → 同一LAN（推奨：Wi-Fi 6 / 6E、5GHz以上）
- **Mac**：Android Platform Tools (`adb`) と `scrcpy` を Homebrew で導入済み
- **初回接続**：USB → `adb tcpip 5555` → 端末IP固定 or DHCP予約 → 以後は Wi-Fi (`adb connect <IP>:5555`)
- **同時表示**：本稿のエントリースクリプトで 1台=1ウィンドウを自動起動・整列。30台まで想定。
- **録画**：同一スクリプトを `--record` モードで起動すると、台数分の MP4 を並列記録
- **一括操作（任意）**：`adb shell input` をループ実行する補助スクリプトで最低限のナビゲーションを送信

---

## 1. 前提セットアップ（初回）
### 1.1 事前の到達性チェック (任意)
### 1.2 端末のスリープ抑止
授業中に画面が暗転しないよう、`./scripts/keep_awake.sh --enable` で `stay_on_while_plugged_in` を一括設定できます。終了後は `--disable` で元に戻してください。

セッション前に `./scripts/check_reachability.sh --ping --adb` を実行すると、`quest_devices.tsv` に登録された端末のネットワーク疎通 (ICMP) と ADB 応答状態を表で確認できます。


1. **Developer Mode ON**  
   スマホの Meta Quest アプリ → デバイス → 開発者モードを有効化。ヘッドセット側で「USB デバッグを常に許可」。
2. **プロジェクト環境の構築（mamba）**  
   ```bash
   mamba env create -p ./.mamba-env -f environment.yml
   ./scripts/bootstrap_binaries.sh ./.mamba-env
   conda activate "${PWD}/.mamba-env"
   ```
   `bootstrap_binaries.sh` が scrcpy / adb を環境内に設置します。
3. **USB接続で Wi-Fi ADB を有効化**  
   ```bash
   adb devices                     # USB検出を確認
   adb tcpip 5555                  # Wi-Fiモードへ切替
   adb connect 192.168.10.21:5555  # 以後は Wi-Fi で利用
   ```
   複数台をまとめて Wi-Fi モードに切り替える場合は `./scripts/usb_to_tcp.sh --port 5555` を使用すると `adb tcpip` を一括実行できます。
4. **IP固定**  
   ルーターの DHCP 予約で「端末名 ↔ IP」を固定（例：Quest-01 → 192.168.10.21）。30台運用時は表計算で管理。
5. **ディレクトリ準備**  
   本レポジトリ直下に `scripts/` と録画保存用 `recordings/` を作成しておく。

### 1.3 Quest OS の近接センサー対策
Meta Horizon OS v76 以降は「ヘッドセットを装着している」状態でないとスクリーンキャストが黒画面（中央にポインタのみ）になる場合があります。`./scripts/quest_os_tweaks.sh engage` を実行すると、Guardian を一時停止し、`prox_close` ブロードキャストで近接センサーを強制的に閉じた状態にし、必要であれば `debug.oculus.capture.*` の setprop も一括投入します。授業後は `restore` サブコマンドで Guardian/近接センサー設定を元に戻してください。

```bash
./scripts/quest_os_tweaks.sh engage --capture-width 1920 --capture-height 1920 --capture-full-rate 1 --capture-eye 2
./scripts/quest_os_tweaks.sh restore
```

`quest_multi_scrcpy.sh start` はKEYCODE_WAKEUPを送信します。setpropとprox_closeの追加対策は `--quest-tweaks` で明示的に有効にします。個別に Guardian を残したい、prox ブロードキャストを抑止したい場合は後述の `--no-quest-tweaks` / `--quest-no-guardian` / `--quest-no-prox` を付けてください。

---

## 2. 端末リスト（最大30台）の管理方法

`scripts/quest_devices.tsv` に、端末ごとの Alias とエンドポイントをタブ区切りで記述します。3列目以降は任意（後述）。

### 2.1.1 LAN内の端末自動検出 (任意)
`./scripts/discover_quest3.py` を使うと、Wi-Fi ADB (既定ポート5555) で待ち受けている Quest をスキャンできます。

```bash
conda activate "${PWD}/.mamba-env"
./scripts/discover_quest3.py --connect           # en0等から自動検出
./scripts/discover_quest3.py --cidr 192.168.10.0/24
```

`--connect` を付けると `adb connect` でモデル/シリアルを取得し、`scripts/quest_devices.tsv` に貼り付け可能な行も出力されます。


```tsv
# alias	endpoint(=IP[:PORT])	[col]	[row]
Quest-01	192.168.10.21:5555
Quest-02	192.168.10.22:5555
Quest-03	192.168.10.23:5555	2	0   # 0始まりの列・行で手動配置
...
Quest-30	192.168.10.50:5555
```

- **列・行を省略**した端末は、スクリプトが自動レイアウト（デフォルト 5列×6行）します。
- 列・行を指定した場合は、0始まりのグリッド座標としてその位置に固定。
- LIST の順序はウィンドウタイトルにも反映されるため、席番号や用途順に並べると識別が容易です。

---

## 3. mac 用エントリースクリプト

実装は `scripts/quest_multi_scrcpy.sh` を正として参照してください。起動設定は `./scripts/quest_multi_scrcpy.sh start --dry-run` と `README.md` で確認できます。

- 公式 scrcpy 5.0.1 と同梱サーバーを使用（旧独自サーバーの上書きを廃止）。
- 標準は長辺1280px・最大30fps・4Mbps。`MAX_SIZE` / `MAX_FPS` / `BIT_RATE` で変更可能。
- `SCRCPY_HWDEC=auto` でMacのハードウェアデコードを利用可能な場合に使用。問題の切り分けは `disabled`。
- `VIDEO_BUFFER=50` はWi-Fiの揺らぎを吸収する代わりに表示遅延が50ms増えます（標準0）。
- 端末ごとのワーカーが接続待ち・再試行を担当。ステータス監視はワーカーを重複起動しません。
- 以下の運用例には過去の設定も含まれます。最新のフラグ・標準値は実装とREADMEを優先してください。


### スクリプトのポイント

- **録画**：`--record` で各端末のMP4を保存。異常終了後の再試行には `_retryNN` が付きます。
- **レイアウト**：`GRID_COLUMNS` / `GRID_ROWS` と `DISPLAY_WIDTH` / `DISPLAY_HEIGHT` で調整。
- **監視と復旧**：バッテリー監視は標準60秒。接続待ちと異常終了からの復帰は端末ワーカーが独立して担当し、`--no-status` でも動作します。通常のウィンドウ終了は再起動しません。
- **ポート**：端末ごとに `SCRCPY_BASE_PORT + index * 100` から100ポートを探索します。
- **音声**：標準 `dup`。大規模表示で音声が不要なら `--no-audio`。自動クリップボード同期は標準で無効。
- **Quest OS対策**：KEYCODE_WAKEUPは毎回送信。setpropとprox_closeは `--quest-tweaks` 指定時のみ。
- **終了**：Ctrl+Cでワーカーと実際のscrcpy子プロセスを停止します。

---

### 4.0 バッテリー状況の一括確認
`start` コマンドの実行中はステータスが背景監視され、接続切断でscrcpyが異常終了した端末は、端末ワーカーが `adb connect` と起動をやり直します。専用ウォッチのみ使いたい場合は `status` コマンドを利用すると、端末リストをクリア画面で数十秒ごとに更新します。既に接続済みなら `--skip-connect` を付けて再接続を抑止。ミラー中も毎回接続を確認したい場合は `start` 側で `--status-connect` を指定してください。

```bash
conda activate "${PWD}/.mamba-env"
./scripts/quest_multi_scrcpy.sh status --interval 15       # 15秒刻みで更新
./scripts/quest_multi_scrcpy.sh status --skip-connect      # 既に接続済みの場合
```

## 4. スクリプトの使い方

```bash
# 標準モード（ミラーのみ）
./scripts/quest_multi_scrcpy.sh start

# 録画モード（recordings/ 以下に全端末分を保存）
./scripts/quest_multi_scrcpy.sh start --record

# レイアウト検証のみ
GRID_COLUMNS=6 GRID_ROWS=5 ./scripts/quest_multi_scrcpy.sh start --dry-run

# 録画モード + カスタム保存先
RECORD_DIR="$PWD/recordings/2024-lesson01" ./scripts/quest_multi_scrcpy.sh start --record

# Quest OS ワークアラウンドのカスタマイズ
./scripts/quest_multi_scrcpy.sh start --quest-tweaks --quest-no-guardian
QUEST_CAPTURE_FULL_RATE=1 QUEST_CAPTURE_EYE=2 ./scripts/quest_multi_scrcpy.sh start --quest-tweaks --quest-capture-width 1920 --quest-capture-height 1920
./scripts/quest_multi_scrcpy.sh start --quest-skip-awake-check   # 旧挙動に合わせる
```

- 録画モード時は **30台 × 高ビットレート** となるため、Mac のストレージ帯域と余裕を事前に確認してください。
- 端末名（Alias）はウィンドウタイトルと録画ファイル名に反映されるので、座席番号や受講者 ID を推奨。
- 停止時はターミナルで `Ctrl+C`。バックグラウンドジョブも trap でまとめて終了します。

---

## 5. 大規模表示のチューニング

- **複数モニター**：モニターごとに別スクリプトを起動するか、`DISPLAY_WIDTH`/`HEIGHT` を各モニターの解像度に合わせて環境変数で切替。
- **描画負荷**：`BIT_RATE=6M MAX_SIZE=1280` など帯域優先設定に変更すると CPU/GPU 負荷を軽減。
- **優先表示**：特定端末のみ 1080p 以上で見たい場合、別スクリプトを録画モードで個別起動（他は `--max-size=1280`）。
- **Wi-Fi 設計**：
  - Quest 3 は 30台同時通信で帯域を圧迫するため、AP を複数台用意しチャネル分散。
  - `adb connect` が不安定な台は電波強度を計測し、AP 至近に配置。
  - DHCP 予約の一覧表を現場に掲示しておくとトラブルシュートが迅速。

---

## 6. 一括操作（ブロードキャスト入力）の実用例

### 6.1 入力対象の共有

`scripts/devices.env` に端末エンドポイントを配列で記述。

```bash
DEVICES=(
  "192.168.10.21:5555"
  "192.168.10.22:5555"
  # ... Quest-30 まで
)
```

### 6.2 代表的な操作スクリプト（macOS bash）

- **ホーム→戻る→OK**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  for d in "${DEVICES[@]}"; do
    adb -s "$d" shell input keyevent KEYCODE_HOME
    adb -s "$d" shell input keyevent KEYCODE_BACK
    adb -s "$d" shell input keyevent KEYCODE_DPAD_CENTER
  done
  ```
- **座標タップ（%指定）**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  XPCT=${1:-50}
  YPCT=${2:-50}
  for d in "${DEVICES[@]}"; do
    size=$(adb -s "$d" shell wm size | tr -d '\r')
    wh=$(echo "$size" | grep -oE '[0-9]+x[0-9]+' | head -n1)
    W=${wh%x*}
    H=${wh#*x}
    X=$(( W * XPCT / 100 ))
    Y=$(( H * YPCT / 100 ))
    adb -s "$d" shell input tap "$X" "$Y"
  done
  ```
- **スワイプ（上方向 300ms）**
  ```bash
  #!/usr/bin/env bash
  source "$(dirname "$0")/devices.env"
  for d in "${DEVICES[@]}"; do
    wh=$(adb -s "$d" shell wm size | grep -oE '[0-9]+x[0-9]+' | head -n1)
    W=${wh%x*}
    H=${wh#*x}
    X=$(( W / 2 ))
    Y1=$(( H * 3 / 4 ))
    Y2=$(( H / 4 ))
    adb -s "$d" shell input swipe "$X" "$Y1" "$X" "$Y2" 300
  done
  ```

> **同期性の注意**：Wi-Fi 伝送と VR UI のリフレッシュで時間差が生じます。厳密な同時操作が必要な授業では、手順を番号付きで伝える・代表端末のみ入力を共有するといった運用で吸収してください。

---

## 7. 録画・スクリーンショット運用

- **フル録画**：`--record` モードを使うと、端末ごとにマルチトラック MP4 が生成され、後追い分析が容易。
- **代表録画のみ**：台数が多い場合は `scripts/quest_multi_scrcpy.sh` を二度起動し、①代表端末だけ録画ビットレート高め、②その他はミラーのみ、など役割分担も可能。
- **スクリーンショット**：
  ```bash
  adb -s 192.168.10.21:5555 shell screencap -p /sdcard/quest01.png
  adb -s 192.168.10.21:5555 pull /sdcard/quest01.png ./screenshots/
  ```
- **録画ファイル整理**：授業や実験単位で `RECORD_DIR` を切り替え、`find recordings -name '*.mp4' -mtime +7 -delete` 等でローテーション。

---

## 8. 運用ベストプラクティス（30台規模を想定）

- **事前チェック**：
  - 端末充電 80%以上、バッテリー or 給電ケーブル確認
  - `adb connect` 成否リストを15分前に確認（`--dry-run` → `start`）
  - ウィンドウタイトルと座席表を照合し、講師/TAが即座に特定できるよう整理
- **ネットワーク設計**：
  - AP 2〜3台で 10台/セグメント 程度に分散し、チャネル干渉を回避
  - VLAN やゲスト隔離が有効な Wi-Fi では `adb connect` が塞がる場合があるので事前許可
- **操作ポリシー**：
  - 完全同期クリックを期待せず、ガイド資料やプロジェクタで順序を共有
  - 必要に応じて `adb shell input` で HOME / CONFIRM 等を送る
- **終了時手順**：
  - `Ctrl+C` で scrcpy を停止 → `adb disconnect`
  - 研究室外へ持ち出す端末は `adb usb` で Wi-Fi ADB を無効化し、セキュリティを確保

---

## 9. トラブルシューティング

- **`adb connect` 失敗**：IP変更 / DHCP未固定 / ファイアウォールの確認。Quest の Wi-Fi 接続を一旦切断→再接続で復旧するケースも多い。
- **scrcpy ウィンドウが溢れる**：`GRID_COLUMNS`/`GRID_ROWS` を増やす、または複数モニターに分けて複数回起動。
- **録画ファイルが欠損**：ストレージ空き容量と書き込み速度を確認。`--record` は高負荷のため、必要端末のみに限定するか、USB-C 外付けSSDを活用。
- **入力が効かない UI**：VR 特有のジェスチャーは `adb shell input` で完全再現できない場合あり。Quest 側ガーディアン設定などは手動で対応。
- **scrcpy が黒画面で中央にポインタだけ表示される**：近接センサーが開いたままの可能性大。`./scripts/quest_os_tweaks.sh engage` もしくは `quest_multi_scrcpy.sh start` の Quest ワークアラウンド（デフォルト有効）で `prox_close` を送ってから再起動。`--quest-awake-timeout` を延ばすと装着してからの起動待ちに余裕を持たせられます。
- **Meta メニューをレーザーポインタでなぞると映像が激しく点滅**：Horizon OS v76+ の既知問題。`--crop`/`--client-crop`/`--angle` などの描画フィルターは外し、メニュー操作後に flicker が始まったら該当ウィンドウのみ停止→再実行でリフレッシュ。録画継続が必要な場合は OBS 側でトリミングするなど後段で処理してください。

---

## 10. 付録：補助ユーティリティ（macOS bash）

### 10.1 一括接続 / 切断

```bash
#!/usr/bin/env bash
source "$(dirname "$0")/devices.env"
case "$1" in
  connect)
    for d in "${DEVICES[@]}"; do adb connect "$d"; done ;;
  disconnect)
    adb disconnect ;;
  *)
    echo "Usage: $0 {connect|disconnect}" ;;
esac
```

### 10.2 解像度一覧ダンプ

```bash
#!/usr/bin/env bash
source "$(dirname "$0")/devices.env"
for d in "${DEVICES[@]}"; do
  echo "== $d =="
  adb -s "$d" shell wm size
  adb -s "$d" shell wm density
  echo

done
```

---

参考メモ
- scrcpy は複数インスタンス同時起動が前提。Mac の GPU 負荷を監視し、必要なら端末グループを複数 Mac に分散。
- 完全な「ミラー + 入力同期」を求める場合は、Quest 向け MDM や有償配信ソリューションも検討。コスト・遅延・管理性を比較し、ケースバイケースで選定してください。
