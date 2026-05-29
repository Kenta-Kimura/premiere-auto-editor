# Premiere Auto Editor MVP

GoPro由来のMP4/MOV素材をPremiereで編集する前に解析し、Premiereへ読み込むためのCSV / SRT / FCP 7 XMLを生成するローカルWebアプリです。

## MVP方針

- `.prproj` は直接生成・編集しません。
- 無音区間は削除しません。`cut_candidates.csv` に「カット候補」として記録し、Premiere読み込み用XMLでは候補境界でクリップを分割します。
- 旅行Vlogでは、風景、建物、食事、移動、屋内展示などの無音素材も残す前提です。
- B-roll / 見どころ検出はMVP対象外です。
- Premiere用XMLは、Premiereが読み込めるFCP 7 XML互換形式の最小実装です。

## Macアプリ形式 vs ローカルWebアプリ形式

MVPではローカルWebアプリ形式を推奨します。

| 形式 | 長所 | 短所 | MVP判断 |
| --- | --- | --- | --- |
| Macアプリ | Finder連携や配布体験が良い。将来的にドラッグ&ドロップや通知を作りやすい。 | SwiftUI / Electron / Tauriなどのアプリ化作業が先に発生し、動画解析本体の検証が遅くなる。署名や配布も考慮が必要。 | v0.2以降で検討 |
| ローカルWebアプリ | PythonだけでGUIを出せる。FFmpeg / WhisperなどCLI処理とつなぎやすい。MVP検証が速い。 | フォルダ選択はパス入力が中心。ネイティブアプリらしさは弱い。 | MVPに適する |

## 推奨技術構成

- UI: ローカルWebアプリ
- 実装言語: Python 3 標準ライブラリ
- 動画メタ情報: FFprobe
- 無音検出: FFmpeg `silencedetect`（初期値: `-38dB`、`2.0秒`）
- 書き起こし: ローカルのOpenAI Whisper CLI、または将来的に `whisper.cpp`
- Premiere連携: `subtitles.srt`、CSV、FCP 7 XML

書き起こしはMacローカル運用なら、MVPでは導入が簡単なOpenAI Whisper CLIを優先し、速度やApple Silicon最適化が必要になった段階で `whisper.cpp` に差し替えるのが現実的です。

## ディレクトリ構成

```text
.
├── app.py
├── premiere_auto_editor/
│   ├── __init__.py
│   └── analyzer.py
├── static/
│   ├── app.js
│   ├── index.html
│   └── styles.css
└── README.md
```

## セットアップ

Python 3はmacOSに入っていることが多いですが、FFmpeg / FFprobeは別途必要です。

```bash
brew install ffmpeg
```

書き起こしも使う場合:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv312
.venv312/bin/python -m pip install -U openai-whisper
```

Whisperは初回実行時にモデルを取得します。MVPでは `base` モデルを使います。

ChatGPTで補完する場合もAPIキーは使いません。解析後に画面へ表示される `ChatGPT補完用プロンプト` をChatGPTに貼り付け、返ってきたCSVをこの画面へ貼り戻します。

## 起動

```bash
python3 app.py
```

Codexの実行セッションに依存せず起動する場合:

```bash
./start_server.sh
```

停止する場合:

```bash
./stop_server.sh
```

Finderから起動したい場合は `run_app.command` を開きます。

ブラウザで次を開きます。

```text
http://127.0.0.1:8765
```

## GitHub管理

このプロジェクトをGitHubで管理する場合:

```bash
cd "/Users/kenta/Documents/Codex/Premiere Auto Editor"
git init -b main
git remote add origin https://github.com/Kenta-Kimura/premiere-auto-editor.git
git add .gitignore README.md app.py start_server.sh stop_server.sh run_app.command static premiere_auto_editor
git commit -m "Initial MVP"
git push -u origin main
```

`downloads/`、`tools/bin/`、`.venv/`、`.venv312/`、`.pip-cache/` はGit管理しません。FFmpeg / FFprobe / WhisperはREADMEのセットアップ手順で各Macに導入します。

## 使い方

1. Premiereで空のプロジェクトを作成します。
2. このツールを起動します。
3. 素材ファイルまたは素材フォルダを指定します。MP4/MOV単体とフォルダのどちらも使えます。
4. 必要なら出力フォルダと `terms.csv` を指定します。
5. 解析開始を押します。
6. 解析完了後、画面上のCSVプレビューを確認します。
7. 文字起こしをChatGPTで補完したい場合は、画面の `ChatGPT補完用プロンプト` をコピーしてChatGPTに貼り付けます。
8. ChatGPTが返したCSVを `補完済みCSVを貼り付け` に貼り、`補完を反映` を押します。
9. 問題なければ `確認して出力` を押します。
10. 出力フォルダ内に作られる実行ごとのサブフォルダから、CSV / SRT / XMLをPremiereへ読み込みます。

解析直後は指定フォルダへは出力しません。一時フォルダに解析結果を作り、CSVプレビューで内容を確認してから、指定出力先へコピーします。出力先を指定した場合も、その直下に `premiere_auto_editor_<素材名>_<日時>` というサブフォルダを作り、生成ファイルをまとめます。

素材、出力先、`terms.csv` はOSダイアログから選択できます。入力欄へのドラッグ＆ドロップにも対応していますが、通常のブラウザではFinderから絶対パスを渡せない場合があります。その場合は選択ボタンを使うか、Finderでパスをコピーして貼り付けてください。

解析中は次の停止操作ができます。

- `ここまで出力して停止`: 処理済みクリップ分のCSV / SRT / XMLを書き出して止めます。
- `中止`: 実行中のFFmpeg / Whisper処理を止めます。すでに書き出された途中ファイルは残る場合があります。

`cut_candidates.csv` が空の場合、素材に常時環境音、風切り音、カメラ操作音が入っていて、FFmpegが無音と判定していない可能性があります。まずはUIの `無音しきい値 dB` を `-30` から `-25`、必要なら `-20` に上げて試してください。`最小無音秒数` を `0.7` から `0.4` に下げると、短い静かな区間も候補に入ります。

地名や観光スポット名は、内蔵の旅行スポット辞書で自動補正します。`terms.csv` は内蔵辞書では足りない固有名詞を追加したい場合だけ使います。

`terms.csv` の形式:

```csv
before,after
誤変換された地名,正しい地名
```

## ChatGPTでの文字起こし補完

このMVPではOpenAI APIキーを使いません。ChatGPT Plusなどの画面に、解析後に表示されるプロンプトを貼り付けて補完します。

ChatGPTには `transcript.csv` の内容がプロンプト内に含まれます。ChatGPTの返答はCSVだけになるよう指示しています。返ってきたCSVを画面に貼り戻して `補完を反映` を押すと、`transcript.csv` と `subtitles.srt` が更新されます。

ChatGPTがMarkdownコードブロック付きで返した場合も、貼り戻し時に外側の ``` は自動的に取り除きます。

## 生成ファイル

- `clips.csv`: ファイル名、パス、尺、fps、解像度、音声トラック有無、音声状態
- `transcript.csv`: 書き起こしセグメント
- `cut_candidates.csv`: 無音区間・カット候補
- `summary.csv`: 素材ごとの総合判定
- `subtitles.srt`: Premiereに読み込む字幕
- `premiere_auto_editor.xml`: カット候補境界で分割したFCP 7 XML
- `manifest.json`: 実行条件と依存関係の記録

## XMLについて

Premiereの `.prproj` は直接編集せず、Premiereの読み込み機能で扱えるXMLを出力します。内部形式はPremiereが読み込めるXML交換形式ですが、互換性の対象はAdobe Premiereだけです。MVPでは動画トラックと音声トラックを候補境界で分割することを優先しています。Premiere側の読み込み結果は環境差が出る可能性があるため、次の段階で実素材を使ってXMLのリンク、チャンネル、マーカー表現を調整する想定です。
