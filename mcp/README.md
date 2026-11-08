# cxxref の MCP サーバー

## 目的

ltq の問い合わせを、MCP(Model Context Protocol)のツールとして GitHub Copilot に渡します。
CLI 方式(`ltq`)と違って Copilot にシェルを渡す必要が無く、ツールはサーバー名(`code-ref-index`)で承認できます。

| ツール | 内容 |
|---|---|
| `refs` | 記号の呼び出し・参照・定義、仮想関数の上書きの関係、テンプレート内の未解決の同名呼び出し、索引に無いテキスト一致とその理由を 1 回で返す(`ltq refs` と同じ) |
| `check` | `path:line` ごとに、索引の行と、その行を含むコメント・無効な `#if` を返す(`ltq check` と同じ) |

どちらも読み取り専用です。

## 前提

- Python 3.10 以降(標準ライブラリだけを使います。追加のインストールは要りません)
- 対象プロジェクトの索引。`<ルート>\.index\index.tsv` を `..\scripts\index-project.bat` で作ります

## 起動

Copilot が起動するので、手で起動する必要はありません。手で動かすときは次のとおりです(標準入出力で JSON-RPC を話します)。

```bat
code-ref-index-mcp.cmd [--root <ソースのルート>] [--index <索引.tsv>] [--auto-update-wait 秒 | --no-auto-update] [--max-output-bytes N]
```

- `--root` を省くと、起動されたときのカレントフォルダから親へさかのぼり、`.index` のある最初のフォルダをルートにします(`.git` と同じ探し方)。Copilot CLI は作業フォルダで起動するので、ソースのルートより下で起動しても、ルートの索引を引きます。そのときは出力の先頭に `root: <ルート> (the index is in this parent folder ...)` と出し、パスはルートからの相対で返します。探すのは呼び出しのたびなので、サーバーの起動後に作った索引も拾います
  - ツールの引数 `root`(作業中のフォルダの絶対パス)を渡されたときは、そこからさかのぼって探します
  - プラグインとして起動されると、作業フォルダはプラグインの場所になり、MCP の roots も空です(Copilot CLI 1.0.105 で確かめました)。そのとき(作業フォルダが環境変数 `PLUGIN_ROOT` と同じとき)は、`root` が渡されなければ、Copilot CLI のセッションの記録(`<COPILOT_HOME か ~\.copilot>\session-state\<COPILOT_AGENT_SESSION_ID>\workspace.yaml` の `cwd`)から作業フォルダを取ります。この記録は公開の仕様ではないので、プラグインの rule は `root` を渡すようモデルに指示しています
  - 索引が見つからなければ、索引の作り方(`scripts\index-project.bat` と、ltidx が無ければ `scripts\get-ltidx.bat`)を、このサーバーの隣の絶対パスで添えます
- `--index` を省くと、`<ルート>\.index` の中の索引本体(構成ごとの `<名前>.tsv`)をすべてまとめて引きます
- 環境変数 `CRI_ROOT` と `CRI_INDEX` でも指定できます
- `--auto-update-wait`(既定 10 秒)と `--no-auto-update` は、下の「索引の自動更新」を参照してください。環境変数 `CRI_AUTO_UPDATE_WAIT`・`CRI_AUTO_UPDATE=0` でも指定できます
- `--max-output-bytes`(既定 18,000)は、結果の大きさの上限です。超えると、`refs` は短い形(`ltq refs --compact`)で出し直し、それでも超えれば末尾を切って、そう書き添えます。環境変数 `CRI_MAX_OUTPUT_BYTES` でも指定でき、0 で切りません
  - Copilot CLI は、ツールの結果が上限(既定は 20,480 バイト、環境変数 `COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES`)を超えると、中身をモデルに渡さず、ファイルに退かせて先頭だけを見せます。大きなコードベースでよくある名前を引くと 75〜110 KB になり、答えの要の部分が届かなかったので、上限を設けました(`../docs/design.md` の「測り直し」)

## 索引の自動更新

コードを変えた後に `index-project.bat -Update` を手で実行する必要はありません。`refs` か `check` が呼ばれるたびに、索引の後に変わったソースがあるかを確かめ、あれば次のように作り直します(`autoupdate.py`)。

1. 裏で作り直しを始めます。ふだんは変わったファイルを読む TU だけを `ltidx -update` で解析し直します。新しいソースや消えた TU があるときは、`compile_commands.json` もソリューションや CMake から作り直します。最低優先度で、同時に 1 つだけ走ります
2. 最大 10 秒、終わるのを待ちます。終われば新しい索引で答え、出力の先頭に `auto-update: rebuilt the index ...` と出します
3. 終わらなければ、今の索引(変わったファイルの行は付け直したもの)で答え、`auto-update: rebuilding the index in the background ...` と出します。次の呼び出しから新しい索引を使います

実測では、ふつうの .cpp を変えたときは 0.5 秒、clang のヘッダーを読む重い .cpp を変えたときは 3.9 秒で、作り直してから答えました。多くの TU が読むヘッダーを変えると全体の作り直しに近い時間(Nox で 90〜120 秒)がかかるので、そのときは裏で続けます。

- **前提**: 索引を `..\scripts\index-project.bat` で作ったこと。作り方を `<索引>.tsv.recipe.json` に残し、それを使って作り直します。この機能より前に作った索引では、一度だけ `index-project.bat` を実行し直してください(それまでは `auto-update: off for this index` と出ます)
- **失敗したとき**: `auto-update: rebuild failed` と出し、今の索引で答えます。ログは `.index\update.log` にあります。次にソースが変わるまでは、同じ作り直しを繰り返しません
- **止めるとき**: 裏で CPU を使われたくない大きなコードベースでは、登録に `--no-auto-update` を足します。待たずにすぐ答えさせるなら `--auto-update-wait 0` です

  ```bat
  copilot mcp add code-ref-index -- python -I <このリポジトリ>\mcp\server.py --no-auto-update
  ```

- **判定**: ファイルの更新日時が索引より新しいかで決めます。`.vcxproj` のコンパイルの設定だけを変えた(ソースは変えていない)ときは気づかないので、`index-project.bat -Update` を手で実行します
- **待つ時間の上限**: Copilot CLI はサーバーごとにツール呼び出しの待ち時間を設定できます(`copilot mcp add --timeout <ms>`)。既定値は公式の文書で見つけられませんでした。既定の 10 秒は、他の Copilot クライアントで報告されている上限(Eclipse 版の 60 秒)より十分短くしてあります【既報】

## Copilot CLI で使う

チームで使うときは、このリポジトリをプラグインとして入れると、登録と使い方の指示が要りません(`../README.md` の「チームで使う」)。以下は手で登録するときの方法です。

すべてのプロジェクトで使う(ユーザー設定 `~/.copilot/mcp-config.json` に登録。`../scripts/register-copilot.bat` が同じことをします):

```bat
copilot mcp add code-ref-index -- python -I <このリポジトリ>\mcp\server.py
```

`.index` の無いプロジェクトでは、ツールは「索引が無い」と返すだけです。

プロジェクトごとに使う場合は、そのプロジェクトの `.mcp.json`(または `.github/mcp.json`)に書きます。ただし、Copilot CLI がこのファイルを読むのは**信頼済みのフォルダだけ**です。非対話モード(`copilot -p`)では `--trust-folder` を付けるか、事前に信頼しておきます【公式: docs.github.com の Copilot CLI「Add MCP servers」】。

```json
{
  "mcpServers": {
    "code-ref-index": {
      "type": "local",
      "command": "python",
      "args": ["-I", "<このリポジトリ>\\mcp\\server.py"],
      "tools": ["*"]
    }
  }
}
```

- ツールの名前は `code-ref-index-refs` と `code-ref-index-check` になります
- 非対話モードで承認するには `--allow-tool code-ref-index` を付けます。`--available-tools` で絞るときも `code-ref-index` を入れます
- サーバーのパスは各自の環境のものなので、リポジトリにコミットする `.mcp.json` には向きません。ユーザー設定に登録するほうが手軽です

### 使い方の指示

Copilot CLI は、許可リストに無い MCP サーバーの初期化時の説明(instructions)をシステムプロンプトに入れません。それでも計測では、指示なしで 12 回とも `refs` が最初に使われました(正答 11/12)。指示を足すと 12/12 になり、トークンも減りました(`../docs/design.md`)。指示を入れるなら、`../bench/mcp-instructions.md` の内容をプロジェクトの `.github/copilot-instructions.md` に足します。

## VS Code の Copilot で使う

`.vscode/mcp.json` に書きます(Copilot CLI はこのファイルを読みません)。VS Code での動作は未確認です。

```json
{
  "servers": {
    "code-ref-index": {
      "type": "stdio",
      "command": "python",
      "args": ["-I", "<このリポジトリ>\\mcp\\server.py", "--root", "${workspaceFolder}"]
    }
  }
}
```

## テスト

`test\run_test.bat` をダブルクリックします。`server.py` を子プロセスで起動し、合成した小さな索引に対して `initialize`・`tools/list`・`tools/call`(`refs`・`check`)と誤った要求を JSON-RPC で送り、返事を確かめます。結果の大きさの上限(短い形で収まればそれを返し、収まらなければ切ること、環境変数 `CRI_MAX_OUTPUT_BYTES`、0 なら切らないこと)も確かめます。Copilot は要りません。

`ltidx.exe` をビルド済みなら、自動更新も確かめます(`test_autoupdate.py`)。小さなプロジェクトを索引にしてからソースを変え、次の呼び出しで作り直されること、TU を足すと拾うこと、待たない設定でも次の呼び出しで新しい索引を使うこと、記録の無い索引には触らないこと、失敗を繰り返さないことです。GitHub Actions では、ltidx をビルドするジョブでこれを流します(`CRI_REQUIRE_LTIDX=1` を付け、`ltidx.exe` が無ければ飛ばさずに失敗させます)。

## 制約

- 最後に引いた索引を開いたまま保ち、索引のファイル(本体と `.tu.tsv`・`.skip.tsv`・`.deps.tsv`)の大きさと更新日時が変わらない間は使い回します。ソースの走査と鮮度の確認は、呼ばれるたびにやり直します。常駐中に索引を作り直しても、次の呼び出しから新しい索引を使います
- 結果が大きくて短い形(`--compact`)で出し直すときは、同じ呼び出しの中の索引の検索とテキスト検索の結果を使い回します
- TU が読んだファイルの一覧(`.deps.tsv` の 2 列目)を、`.index\<索引>.tsv.seen.cache` に書き残します。`.deps.tsv` の大きさと更新日時が同じ間は、`.deps.tsv`(UE5 で 6.7 GB)を読まずにこれを使います。書けないフォルダでは書かずに続けます
- Unreal Engine 5(索引 10 GB、ソース 20 万ファイル)では、1 回の `refs` が 43〜91 秒から 19〜33 秒になりました(サーバーを立ち上げ直した後の 1 回目から。結果は前と同じ)。残りの大半は、20 万ファイルを読むテキスト検索と、10 GB の索引の検索です(`../docs/design.md` の「Unreal Engine 5 での計測」)
- 自動更新は `.index` の中に `update.lock`(作り直し中だけ)・`update.json`(前回の結果)・`update.log` を置きます
- MCP の stdio トランスポートの、ツールに要る範囲(`initialize`・`tools/list`・`tools/call`・`ping`)だけを実装しています
