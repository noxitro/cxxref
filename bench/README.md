# bench — Copilot CLI での検索手段の比較

## 目的

同じ問題を GitHub Copilot CLI に解かせ、検索手段ごとに正答率・往復回数・トークン・credits を比べます。条件は次のとおりです。

- A: rg のみ
- B: clangd の LSP
- C: ltidx の TSV 索引を rg で引く(使い方の指示付き)
- D: LSP と、LSP を使えという指示
- E: `../query` の CLI `ltq`(使い方の指示付き)。シェルを渡しますが、承認するのは `ltq` だけです
- F: `../mcp` の MCP サーバー(指示なし)。作業フォルダの `.mcp.json` に登録します。シェルは渡しません
- G: F に使い方の指示(`mcp-instructions.md`)を足したもの
- H: G の索引の自動更新を止めたもの(`--no-auto-update`)。コードを変えた後の問題(U1・U2)で、自動更新の効き目を G と比べます
- P: このリポジトリをプラグインとして読ませたもの(`--plugin-dir`)。MCP サーバーと使い方の rule はプラグインから渡り、作業フォルダには指示のファイルも `.mcp.json` も置きません。G と比べて、チームで配る形でも同じ効き目かを見ます(v9。`../docs/design.md` の「プラグインとしての配布」)
- S: Serena(clangd)の MCP だけ(指示なし)。Y: S に、Serena を先に使う指示(`serena-instructions.md`)を足したもの。`-Serena`(または `CRI_BENCH_SERENA`)に Serena を入れたフォルダ(`bin\serena.exe` と設定の `home\`)を渡します。clangd が読むルートの `compile_commands.json` は実行の間だけ置き、Serena と clangd の索引(`.serena`・`.cache`)は消さずに使い回すので、先に `serena project index <写し> --ls cpp` で作っておきます(v10。`../docs/design.md` の「Serena との比較」)

結果と考察は `../docs/design.md` にあります。問題は 2 つのコードベースで用意しています。

- `tasks.json`: C++ のゲームエンジン [Nox](https://github.com/noxitro/Nox) の問題。回答の書式は `answer-format.md`
  - T1〜T4: C++ の記号の参照を探す問題
  - R1〜R3: rg のほうが向く問題(プリプロセッサのマクロ、文字列リテラル、`.vcxproj` の設定)。索引を渡したときに、かえって悪くならないかを見ます
  - U1・U2: 索引を作った後にコードを変えてから聞く問題。`edit` に書いた変更を `run.ps1` が実行の前に当て、後で中身と更新日時を元に戻します。U1 は .cpp を 2 つ変え(呼び出しを 1 つ足し、別のファイルで 1 つ消す)、U2 は多くの TU が読むヘッダーの先頭に 2 行足します
- コードベース B(公開されている OSS の C++ ライブラリ)の 4 問(E1〜E4)と、コードベース C(公開されている大規模な OSS のコンパイラ基盤、2,350 TU)の 3 問(L1〜L3)。特定の製品を評価したものと読まれないよう、問題と正解のファイルは公開していません。手元では `local\`(git に入れない)に `tasks-<名前>.json` と回答の書式を置きます
  - C には、自動更新だけを測る問題(CU1・CU2。`autoupdate_bench.py` 用)もあります

## 前提

- Copilot CLI(`copilot`)にサインイン済みであること。既定のモデルは gpt-6-luna です
- 解析対象の Nox の写し(作業フォルダ)と、その `.cdb\compile_commands.json`。`run.ps1` は作業フォルダの `.github\` と `.index\` を書き換えるので、元のリポジトリではなく写しを使います(下の「計測に使うソースの写し」)
- 条件 C・E は、ltidx で作った索引(`<名前>.tsv` と `.tu.tsv`・`.skip.tsv`)
- 条件 B・D は clangd(`-Clangd` でパスを指定。既定は PATH 上の `clangd`。`batch.ps1` にも同じ `-Clangd` があります)
  - clangd の索引(`<作業フォルダ>\.cdb\.cache\clangd\index`)を、計測の前にそろえておきます。`python -I clangd_warm.py <clangd.exe> <写し>` が `re-indexed TUs: 0 (ready)` と出るまで流します(初回は数分。索引がそろっていないと、Copilot の clangd が計測の途中で索引を作り直し、参照が欠けます)

## 使い方

```powershell
$env:CRI_BENCH_WORKSPACE = 'D:\work\nox-copy'   # 作業フォルダ
$env:CRI_BENCH_INDEXDIR  = 'D:\work\index'      # 索引の置き場所
pwsh -File run.ps1 -Cond A -TaskId T3 -Rep 1
pwsh -File run.ps1 -Cond E -TaskId T3 -Rep 1 -Round v3 -Index nox-v3.tsv
python -I collect.py .
# 別のコードベースの問題は、問題ファイルと書式を指定する(local\ に置いたもの)
pwsh -File run.ps1 -Cond G -TaskId E2 -Rep 1 -Round v5 -Index b-v5.tsv -Tasks local\tasks-b.json -Format local\answer-format-b.md
# 問題 × 条件 × 回数をまとめて流す(終わった回は飛ばすので、止まっても同じコマンドで続きから流せる)
pwsh -File batch.ps1 -Round v6 -TaskIds T1,T2,T3,T4 -Conds A,F,G -Reps 5 -Index nox-v6.tsv -Digest <指紋>
# 自動更新だけを、Copilot を使わずに測る(MCP サーバーを直接起動して、edit を当ててから refs を呼ぶ)
python -I autoupdate_bench.py --workspace <写し> --index <索引.tsv> --reps 5 U1=nox::JobSystem::Initialize U2=nox::detail::IDelegateBase::Reset
```

- `run.ps1` は、条件ごとの `.github\`(と `.index\`)を作業フォルダに置き直してから、1 問を実行します
  - Copilot に渡すのは読み取り専用のツール(`view` `rg` `grep` `glob` `lsp`)だけで、書き込みは渡しません
  - 条件 B・D の `.github\lsp.json` は公式の形(`lspServers`)のままで、`command` だけを記録用の中継 `lsp_log_proxy.py` にします。中継はメッセージをそのまま通し、やり取りを `runs\<名前>.lsp.log` に書きます。`python -I lsp_summary.py <回>` で、サーバーが起動したか・0 件の応答の数・最初にファイルを開いた時刻・索引し直しの有無を 1 回 1 行で出します
  - シェルは条件 E だけで渡し、`--allow-tool 'shell(ltq)'` で `ltq` 以外のコマンドは承認しません(非対話モードなので拒否されます)
  - セッションは `runs\index.jsonl` に記録し、やり取りの Markdown は `runs\` に出します(`runs\` はリポジトリに入れません)
- `-Round` は計測の回です。v1 は最初の計測、v2 は `dep` 行と指示の改善後、v3 は ltq、v4 は MCP、v5 はコードベース B での MCP の再計測、v6 は写しを固定して各問 5 回で測り直したもの、v7 は MCP サーバーに出力の大きさの上限を入れた後の G、v8 はコードベース C の問題文の範囲を言い直した後の A・G、v11 は clangd の準備を確かめてから測り直した B・D です。集計は回ごとに出ます
- `-Index` は、条件 C・E・F・G・H で使う索引のファイル名です(`-IndexDir` 配下)。条件 F・G・H では `.deps.tsv` も写し、作業フォルダ向けの作り方の記録(`.recipe.json`)を作るので、自動更新が働きます(`compile_commands.json` は `-Cdb`、既定は `<作業フォルダ>\.cdb`)
- 自動更新の作り直しが Copilot の終了後も続いているときは、終わるのを待ってから次に進みます
- 条件 F・G の `.mcp.json` は、Copilot CLI が信頼済みのフォルダでしか読みません。`run.ps1` は `--trust-folder` を付けて実行します
- `collect.py` は `~/.copilot/session-store.db` と、各セッションの `events.jsonl` を読み取り専用で開き、`runs\summary.tsv` と条件ごとの平均を出します。`rep=0` は試運転として集計から外します
- 問題と正解は `tasks.json`(と `tasks-<名前>.json`。`collect.py` はすべて読むので、id はファイルをまたいで重ねません)、回答の書式は `answer-format.md` です。指示は、索引用が `index-instructions.md`(v1 の計測で使ったものは `index-instructions-v1.md`)、LSP 用が `lsp-instructions.md`(v1 の計測で使ったものは `lsp-instructions-v1.md`)、ltq 用が `cli-instructions.md` です

## 計測に使うソースの写し

正解の行番号は、ソースの写しに対するものです。写しは計測の外で書き換えないでください。`snapshot.py digest <写し>` の指紋で、写しが変わっていないかを確かめます(`batch.ps1 -Digest` は始める前と各回の後に確かめます)。指紋は、計測の道具が書き換える `.` で始まるフォルダ・ファイル(`.index` `.cdb` `.github` `.mcp.json`)を除いて作ります。

| 写し | 場所(手元) | 指紋(SHA-256) |
|---|---|---|
| Nox(2026-11-03 の作業ツリー。未コミットの変更を含む) | `E:\dev\bench-data\cxxref\nox-20261103` | `d15d190d64f53034e5312f6d669708b9a3b6a44b5650cd43687759b8d6598e6a`(1,764 ファイル) |
| コードベース B | `E:\dev\bench-data\cxxref\b` | `58c865fb54c9a4c1d3a01cd500bba354c32c0b2b8371f7966becddcc02f3d805`(1,667 ファイル) |
| コードベース C(X86 だけを、アサーションありの Release で構成) | `E:\dev\bench-data\cxxref\` の下(フォルダ名は伏せます) | `bbacd634c6ad35de17a2f16becc3f5822bfbf4c206ad9a04359a035636338bb1`(10,338 ファイル) |
| O3DE 2605.0(`3db6943`。Ninja・profile・unity build なし・テストなしで構成。`build/` は `.snapshot-exclude` で指紋から除く) | `E:\dev\bench-data\cxxref\o3de-src` | `d26eea97d767cecd8a24c030e1cc8833f1ef269b8e03ef0d210cb134ac50254a`(29,950 ファイル) |

O3DE の問題(O1〜O3)は公開しています(`tasks-o3de.json`・`answer-format-o3de.md`)。O3DE は誰でも同じ版を取ってこられるので、写しの作り方を `../docs/design.md` の「大規模な公開コード」に書きました。正解は索引を使わずに rg と読解で作り、判定の理由を各問の `note` に残しています。

写しを別の場所に移したときは、`snapshot.py rebase <写し>\.cdb <元の場所> <新しい場所>` で `compile_commands.json` の中のパスを書き換えます。

## 注意

- 実行のたびに Copilot の credits を消費します(gpt-6-luna で 1 問 0.1〜0.7 credits)
- 正解の行番号は、上の写しに対するものです。今の Nox では行がずれています
