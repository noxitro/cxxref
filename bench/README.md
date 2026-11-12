# bench — Copilot CLI での検索手段の比較

## 目的

同じ問題を GitHub Copilot CLI に解かせ、検索手段ごとに正答率・往復回数・トークン・credits を比べます。条件は次のとおりです。

- A: rg のみ
- B: clangd の LSP
- C: ltidx の TSV 索引を rg で引く(使い方の指示付き)
- D: LSP と、LSP を使えという指示
- E: `../query` の CLI `ltq`(使い方の指示付き)。シェルを渡しますが、承認するのは `ltq` だけです
- F: `../mcp` の MCP サーバー(指示なし)。`runs\<名前>.mcp.json` に書き、`--additional-mcp-config` で渡します。シェルは渡しません
- G: F に使い方の指示(`mcp-instructions.md`)を足したもの
- H: G の索引の自動更新を止めたもの(`--no-auto-update`)。コードを変えた後の問題(U1・U2)で、自動更新の効き目を G と比べます
- P: このリポジトリをプラグインとして読ませたもの(`--plugin-dir`)。MCP サーバーと使い方の rule はプラグインから渡り、作業フォルダには指示のファイルも `.mcp.json` も置きません。G と比べて、チームで配る形でも同じ効き目かを見ます(v9。`../docs/design.md` の「プラグインとしての配布」)
- S: Serena(clangd)の MCP だけ(指示なし)。Y: S に、Serena を先に使う指示(`serena-instructions.md`)を足したもの。`-Serena`(または `CRI_BENCH_SERENA`)に Serena を入れたフォルダ(`bin\serena.exe` と設定の `home\`)を渡します。clangd が読むルートの `compile_commands.json` は実行の間だけ置き、Serena と clangd の索引(`.serena`・`.cache`)は消さずに使い回すので、先に `serena project index <写し> --ls cpp` で作っておきます(v10。`../docs/design.md` の「Serena との比較」)

- I・K・L・M・N: ほかの MCP サーバー + 使い方の指示(v12)。起動方法・渡すツール・指示は `ext\<名前>.json` に書きます。パスは `${TOOLS}`(`-ToolsDir` か `CRI_BENCH_TOOLS`。ツールを入れたフォルダ)・`${LLVM}`(`-Llvm` か `CRI_BENCH_LLVM`)・`${WS}`(作業フォルダ)・`${CB}`(`-Codebase`。索引の置き場の名前)・`${JOBS}`(論理コアの半分)と、`vars.<コードベース>` の値で置き換えます。指示は、Serena(Y)と同じ「grep の前にまずこれを使う」一文に、作者の推奨の指示があればそれをそのまま足したものです(`ext\*-instructions.md`)
  - I: [clang_index_mcp](https://github.com/andreymedv/clang_index_mcp)(libclang。Windows で動かすための手直しを `ext\clang-index-mcp.patch` に、起動の bat を `ext\clang-index-mcp.run.bat` に置きました)
  - K: [clast](https://github.com/daveelton/clast)(libclang と SQLite。MSVC 形式の compile_commands.json は `ext\clast-make-cdb.py` で変換して渡します。索引を作り直す `ast_index` は渡しません)
  - L: [mcp-language-server](https://github.com/isaacphi/mcp-language-server)(LSP の仲立ち。Go 1.27.3 で `go build`。Windows の手直しと、起動の直後に 1 ファイルを開く `MLS_SEED_FILE` を `ext\mcp-language-server.patch` に置きました。`edit_file`・`rename_symbol` は渡しません)
  - M: [clangd-mcp](https://github.com/schuay/clangd-mcp)(clangd。`--seed-file` が要ります)
  - N: [clangd-mcp-server](https://github.com/felipeerias/clangd-mcp-server)(clangd。Windows の手直しを `ext\clangd-mcp-server.patch` に置きました。背景索引を `CLANGD_ARGS` で入れます)
  - L・M・N の clangd の索引は、`${TOOLS}\clangd-cdb-<名前>\.cache` に前もって作って共用します(`<名前>` は `-Codebase`。コードベース A は `a`)。どのツールも索引ができあがるのを待たずに答えるので、索引を作り終えてから流します

モデルは `-Model` で選びます(既定 gpt-6-luna。Copilot の契約の中のモデルは `claude-haiku-5.5` のように点で区切ります)。`-Provider anthropic` を付けると、Copilot の経路ではなく Anthropic API を直接使います(BYOK。従量課金)。このとき `-Model` は API のモデル名(`claude-haiku-5-5`)で、キーは `COPILOT_PROVIDER_API_KEY_COMMAND`(キーを出すコマンド)か `COPILOT_PROVIDER_API_KEY` を、このプロセスかユーザーの環境変数に置きます。BYOK の環境変数は 1 回の実行の間だけ置き、終わったら元に戻します。`-DryRun` は、条件の設定を作業フォルダに置いて中身を出し、片付けるだけです(Copilot は呼びません)。

結果と考察は `../docs/design.md` にあります。問題は 2 つのコードベースで用意しています。

- `local\tasks-a.json`(公開しない): C++ のゲームエンジン、コードベース A の問題。回答の書式は `answer-format.md`
  - T1〜T4: C++ の記号の参照を探す問題
  - R1〜R3: rg のほうが向く問題(プリプロセッサのマクロ、文字列リテラル、`.vcxproj` の設定)。索引を渡したときに、かえって悪くならないかを見ます
  - U1・U2: 索引を作った後にコードを変えてから聞く問題。`edit` に書いた変更を `run.ps1` が実行の前に当て、後で中身と更新日時を元に戻します。U1 は .cpp を 2 つ変え(呼び出しを 1 つ足し、別のファイルで 1 つ消す)、U2 は多くの TU が読むヘッダーの先頭に 2 行足します
- コードベース B(公開されている OSS の C++ ライブラリ)の 4 問(E1〜E4)と、コードベース C(公開されている大規模な OSS のコンパイラ基盤、2,350 TU)の 3 問(L1〜L3)。特定の製品を評価したものと読まれないよう、問題と正解のファイルは公開していません。手元では `local\`(git に入れない)に `tasks-<名前>.json` と回答の書式を置きます
  - C には、自動更新だけを測る問題(CU1・CU2。`autoupdate_bench.py` 用)もあります

## 前提

- Copilot CLI(`copilot`)にサインイン済みであること。既定のモデルは gpt-6-luna です
- 解析対象のコードベース A の写し(作業フォルダ)と、その `.cdb\compile_commands.json`。`run.ps1` は作業フォルダの `.github\` と `.index\` を書き換えるので、元のリポジトリではなく写しを使います(下の「計測に使うソースの写し」)
- 条件 C・E は、ltidx で作った索引(`<名前>.tsv` と `.tu.tsv`・`.skip.tsv`)
- 条件 B・D は clangd(`-Clangd` でパスを指定。既定は PATH 上の `clangd`。`batch.ps1` にも同じ `-Clangd` があります)
  - clangd の索引(`<作業フォルダ>\.cdb\.cache\clangd\index`)を、計測の前にそろえておきます。`python -I clangd_warm.py <clangd.exe> <写し>` が `re-indexed TUs: 0 (ready)` と出るまで流します(初回は数分。索引がそろっていないと、Copilot の clangd が計測の途中で索引を作り直し、参照が欠けます)

## 使い方

```powershell
$env:CRI_BENCH_WORKSPACE = 'D:\work\a-copy'   # 作業フォルダ
$env:CRI_BENCH_INDEXDIR  = 'D:\work\index'      # 索引の置き場所
pwsh -File run.ps1 -Cond A -TaskId T3 -Rep 1
pwsh -File run.ps1 -Cond E -TaskId T3 -Rep 1 -Round v3 -Index a-v3.tsv
python -I collect.py .
# 別のコードベースの問題は、問題ファイルと書式を指定する(local\ に置いたもの)
pwsh -File run.ps1 -Cond G -TaskId E2 -Rep 1 -Round v5 -Index b-v5.tsv -Tasks local\tasks-b.json -Format local\answer-format-b.md
# 問題 × 条件 × 回数をまとめて流す(終わった回は飛ばすので、止まっても同じコマンドで続きから流せる)
pwsh -File batch.ps1 -Round v6 -TaskIds T1,T2,T3,T4 -Conds A,F,G -Reps 5 -Index a-v6.tsv -Digest <指紋>
# 自動更新だけを、Copilot を使わずに測る(MCP サーバーを直接起動して、edit を当ててから refs を呼ぶ)
python -I autoupdate_bench.py --workspace <写し> --index <索引.tsv> --reps 5 U1=a::JobSystem::Initialize U2=a::detail::IDelegateBase::Reset
```

- `run.ps1` は、条件ごとの `.github\`(と `.index\`)を作業フォルダに置き直してから、1 問を実行します
  - Copilot に渡すのは読み取り専用のツール(`view` `rg` `grep` `glob` `lsp`)だけで、書き込みは渡しません
  - 条件 B・D の `.github\lsp.json` は公式の形(`lspServers`)のままで、`command` だけを記録用の中継 `lsp_log_proxy.py` にします。中継はメッセージをそのまま通し、やり取りを `runs\<名前>.lsp.log` に書きます。`python -I lsp_summary.py <回>` で、サーバーが起動したか・0 件の応答の数・最初にファイルを開いた時刻・索引し直しの有無を 1 回 1 行で出します
  - シェルは条件 E だけで渡し、`--allow-tool 'shell(ltq)'` で `ltq` 以外のコマンドは承認しません(非対話モードなので拒否されます)
  - セッションは `runs\index.jsonl` に記録し、やり取りの Markdown は `runs\` に出します(`runs\` はリポジトリに入れません)
- `-Round` は計測の回です。v1 は最初の計測、v2 は `dep` 行と指示の改善後、v3 は ltq、v4 は MCP、v5 はコードベース B での MCP の再計測、v6 は写しを固定して各問 5 回で測り直したもの、v7 は MCP サーバーに出力の大きさの上限を入れた後の G、v8 はコードベース C の問題文の範囲を言い直した後の A・G、v11 は clangd の準備を確かめてから測り直した B・D、v12 は Claude Haiku 5.5(BYOK)で A・G・Y とほかの MCP サーバー(I・K・L・M・N)を並べたものです。集計は回ごとに出ます
- `-Index` は、条件 C・E・F・G・H で使う索引のファイル名です(`-IndexDir` 配下)。条件 F・G・H では `.deps.tsv` も写し、作業フォルダ向けの作り方の記録(`.recipe.json`)を作るので、自動更新が働きます(`compile_commands.json` は `-Cdb`、既定は `<作業フォルダ>\.cdb`)
- 自動更新の作り直しが Copilot の終了後も続いているときは、終わるのを待ってから次に進みます
- 条件 F・G・H の MCP サーバーは、作業フォルダの `.mcp.json` ではなく `--additional-mcp-config` で渡します。ユーザー設定(`~/.copilot/mcp-config.json`)に同じ名前のサーバー(ふだん使う別のチェックアウト)が登録されていると、作業フォルダの `.mcp.json` があるときはユーザー設定のほうが起動され、このリポジトリのサーバーが使われませんでした(`--additional-mcp-config` を足しても同じ)。作業フォルダの `.mcp.json` を置かずに `--additional-mcp-config` だけで渡すと、このリポジトリのサーバーが使われます【実測: Copilot CLI 1.0.108、2026-11-11】。2026-11-11 より前の計測は、計測に使ったチェックアウトとユーザー設定のサーバーが同じだったので、影響は無かったと見ています【推測】
- `run.ps1` は、実行の間だけ `COPILOT_PLUGIN_DIR_ONLY=true` を設定し、インストール済みのプラグインを読ませません(`--plugin-dir` で渡したものだけ)。cxxref をプラグインとして入れてあると、その中のサーバーが同じ名前(`code-ref-index`)で起動され、`--additional-mcp-config` で渡したこのリポジトリのサーバーは使われませんでした【実測: Copilot CLI 1.0.109、2026-11-11。`copilot mcp list` は Plugin servers の `code-ref-index` だけを出し、`COPILOT_PLUGIN_DIR_ONLY=true` を付けると User servers のほうを出す】。入っていたのは 0.5.1 で、後の版で作った索引(範囲の種類 `local` がある)を引くと `KeyError: 'local'` を返します(0.5.1 のサーバーを同じ引数で直接呼んで再現)。今の ltq は、知らない種類の範囲を「その他」として並べます。上の項の「ユーザー設定のサーバーが起動された」回も同じエラーだったので、起動されていたのはこのプラグインのサーバーだったとみています【推測】
- `run.ps1` は `--trust-folder` を付けて実行します(作業フォルダの指示のファイルを読ませるため)
- 条件 F・G・H は、索引の差分(`.alt.tsv`・`.alt.json`)があれば一緒に写します
- `collect.py` は `~/.copilot/session-store.db` と、各セッションの `events.jsonl` を読み取り専用で開き、`runs\summary.tsv` と条件ごとの平均を出します。実行ごとの行の `failed` は、失敗したツール呼び出しの数です(答えが合っていても、MCP サーバーがエラーを返していた回を見分けます)。`rep=0` は試運転として集計から外します
- 問題と正解は `tasks-<名前>.json`(公開しないものは `local\` に置く。`collect.py` はすべて読むので、id はファイルをまたいで重ねません)、回答の書式は `answer-format.md` です。指示は、索引用が `index-instructions.md`(v1 の計測で使ったものは `index-instructions-v1.md`)、LSP 用が `lsp-instructions.md`(v1 の計測で使ったものは `lsp-instructions-v1.md`)、ltq 用が `cli-instructions.md` です

## v13: ふだんの作業に近い問題での計測

v13 は、`run.ps1` とは別の台本(`run13.py`・`batch13.py`・`score13.py`)で流します。計画と、流す前に固定した問題は `../docs/bench-plan-v13.md`、結果は `../docs/design.md` の「公平に近い条件での測り直し」にあります。

- 条件は A(なし)・P(マーケットプレイスから入れた v0.5.1)・M(MCP だけを `--additional-mcp-config` で渡し、rule は無し)・X(P に `v13\adverse-instructions.md` を足したもの)です。どれも空の `COPILOT_HOME` から始め、道具は既定の一式(編集と shell を含む)を渡します。パッケージの導入・ビルド・ネットワークのコマンドだけを `--deny-tool` で止めます
- 作業フォルダは git で管理した写しで、各回の前に `git checkout`・`git clean` で元に戻し、変更は差分(`runs13\<名前>.diff`)に残します
- 問題は `v13\tasks-stage1.json`(Python・C#・TypeScript、各 3 問)と、公開していない `local\v13-tasks-stage1-a.json`(索引の無いコードベース A の 3 問)・`local\v13-tasks-stage2-a.json`(コードベース A の 8 問)です。対象は `v13\sample.py`・`v13\sample2.py` が乱数で選びます。段階 2 の正解は、宣言と定義に引数を足した中身を VFS の overlay で差し替えて `clang-cl /Zs` にかけ、新しく出たエラーの位置から作ります(`v13\cxxcheck.py`)
- 条件 Q は、P の rule と ltidx だけを直した版に差し替えたものです(`run13.py` の冒頭)。段階 2 の測り直し(`--round v13q`)で使いました
- 採点は `python -I score13.py --tasks local\v13-tasks-stage2-a.json --compile` です。`--compile` は、差分を当てた中身で同じ構文チェックをかけ、元には無いエラーが出ないかを見ます(最低の優先度・4 並列)。`batch13.py` は、終わった回を飛ばし、`runs13\STOP` があれば次の回の前に止まります
- 入力トークンの比と信頼区間は `ratio13.py` で出します(問題ごとに平均どうしの比を取り、幾何平均と中央値にまとめ、問題と回を引き直す 2 段の bootstrap)
- `trace13.py <回の名前>` は、1 回の実行で使った道具を、モデルの呼び出しごとの 1 行にして出します(`../docs/benchmarks.md` の「動きの例」)

```powershell
python -I batch13.py --tasks local/v13-tasks-stage2-a.json --conds A,P,M,X --reps 5 --round v13
python -I score13.py --tasks local/v13-tasks-stage2-a.json --compile
python -I ratio13.py --round v13 --tasks local/v13-tasks-stage2-a.json --conds A,P,M,X
python -I trace13.py v13q-C2-createinstance-Q-r4
```

## rg との突き合わせ(`parity.py`)

Copilot を使わずに、「ふつうのエージェントが rg で見つける行が、cxxref の結果のどこにも出ない」ことが無いかを確かめます。名前ごとに `rg -w -F <名前>` の一致行と、`ltq refs` の 3 つの形(全文・短い形の 1 段目・2 段目)と、MCP サーバーが実際に返す文面(`mcp/server.py` の `run_ltq`)、漏れのない一覧(`ltq refs --all`)、MCP の文面にそれが指す一覧のファイルを全部足したもの(`mcp+file`)、そのうち丸ごと読める大きさのファイルだけを足したもの(`mcp+read`)を突き合わせ、rg の行を「1 行ずつ出ている(listed)」「フォルダの件数に入っている(folded)」「上の階層の件数に入っている(area)」「どこにも出ていない(hidden)」「全文にも無い(missing)」に分けます。hidden と missing が 0 であること、`all` と `mcp+file` では全部の行が listed であること、MCP の文面と一覧のファイルの両方に出ている行が 0 であること(合わせて 1 回ずつ)が目標です。rg は PATH の `rg.exe` か、Copilot CLI に同梱のものを使います。

```powershell
python -I parity.py --root <ソースのルート> --index <索引.tsv> --sample 40 --under runtime/ --out parity.tsv
python -I parity.py --root <ソースのルート> --index <索引.tsv> --names Create GetName --no-ignore
```

- `--sample N` は、差分の索引(`.alt.tsv`)に出てくる名前から N 個を、名前のハッシュの順で選びます(同じ索引なら同じ名前)
- `--no-ignore` は、rg に `.gitignore` を無視させます
- `--out` は、MCP の文面で 1 行ずつ出ていない rg の行を、分類つきで書きます
- `--rg-short` は、修飾名で問うときも rg を末尾の名前で引きます(エージェントが呼び出し元を探すときの引き方)。索引が別の記号に解決している行は other に数えます(記号ごとの件数として出る行)

結果(2026-11-11)【実測】:

| | 名前 | rg の行 | MCP: 1 行ずつ | MCP: フォルダの件数 | MCP: 上の階層の件数 | hidden | missing |
|---|---|---|---|---|---|---|---|
| コードベース A(`runtime/` の名前) | 40 | 5,176 | 1,124 | 4,020 | 32 | 0 | 0 |
| 同(`--no-ignore`) | 40 | 5,519 | 1,248 | 4,239 | 32 | 0 | 0 |
| O3DE(30 個 + よくある名前 5 個) | 35 | 24,091 | 548 | 9,197 | 14,346 | 0 | 0 |
| O3DE、修飾名 3 つ(`--rg-short`。rg は末尾の名前で引く) | 3 | 16,034 | 129 | 4,323 | 6,963 | 0 | 0 |

MCP の 1 回の `refs` は、コードベース A で中央値 0.1 秒、O3DE で中央値 2.6 秒・最大 16.6 秒でした(最大はよくある名前。内訳は未調査)。O3DE では、MCP の文面が上限で末尾から切れた名前は 0 でした(前は 3 名)。ただし rg の行の 6 割は「上の階層の件数」にしか出ないので、そこを 1 行ずつ見るには `path` で絞って引き直す必要があります(下の「漏れのない一覧」で、一覧のファイルに 1 行ずつ出るようにしました)。

修飾名 3 つ(`AZ::IO::FileIOBase::Create`・`AZ::Entity::Activate`・`AZ::DynamicModuleHandle::Create`)の rg の行 16,034 のうち 4,619 は、索引が別の記号に解決している行で、`other symbols named` の記号ごとの件数として出ます。2026-11-11 のレビューで見つかった、短い形で場所が消えていた行(テンプレートの中の別の記号に解決された呼び出しと、「別の記号らしい」テキストの一致)は、直した後はフォルダごとの件数に入ります。

### 漏れのない一覧(`refs --all` と、MCP が書く一覧のファイル。2026-11-11)【実測】

件数で残すだけでは、関数の名前や引数を変えるときに直す行が分かりません。`ltq refs --all` は名前を含む行を全部 1 行ずつ並べ、MCP サーバーは、結果に並べきれなかったときにその一覧をファイルに書いて場所を知らせます。C/C++ 以外のファイルも、拡張子や大きさで除かず、rg と同じく中身で見分けるようにしました。

| | 名前 | rg の行 | `refs --all`: 1 行ずつ | MCP の文面+一覧のファイル: 1 行ずつ | 一覧を書いた名前 | 一覧の大きさ(中央値 / 最大) | MCP の 1 回(中央値 / 最大) |
|---|---|---|---|---|---|---|---|
| コードベース A(`runtime/` の名前) | 40 | 5,176 | 5,176 | 5,176 | 4 | 129 KB / 634 KB | 0.05 秒未満 / 0.3 秒 |
| 同(`--no-ignore`) | 40 | 5,519 | 5,519 | 5,519 | 4 | 129 KB / 634 KB | 0.05 秒未満 / 0.3 秒 |
| O3DE(30 個 + よくある名前 4 個 + 修飾名 3 個) | 37 | 24,093 | 24,093 | 24,093 | 16 | 530 KB / 1,953 KB | 0.3 秒 / 15.7 秒 |
| 同(`--no-ignore`、35 個) | 35 | 30,169 | 30,169 | 30,169 | 14 | 170 KB / 1,953 KB | 0.3 秒 / 3.2 秒 |
| O3DE、修飾名 3 つ(`--rg-short`) | 3 | 16,034 | 16,034 | 16,034 | 3 | 1,120 KB / 1,121 KB | 1.3 秒 / 1.5 秒 |

- どの行でも、rg の行はすべて 1 行ずつ出ました(件数だけの行は 0)。修飾名 3 つで、前は記号ごとの件数にしか出なかった 4,619 行(別の記号に解決された行)も、一覧には解決先の記号つきで並びます
- MCP の 1 回の時間は、一覧を書く分(`--all` での出し直し)を含みます。同じ呼び出しの中の検索の結果と行の分類を使い回し、単語を正規表現でなく `find` で探すようにしたので、前の計測(O3DE で中央値 2.6 秒・最大 16.6 秒)より短くなりました。O3DE の最大 15.7 秒は `Create` で、続けて測った `--no-ignore` の回では 1.6 秒でした(先の回はファイルのキャッシュが冷えていたため、とみています【推測】)。UE5 での時間は `../docs/design.md` の「呼び出しをまたいで保つ」にあります
- 一覧は最大で約 2 MB です(`AZ_Assert` の 9,638 行)。モデルに渡す文面ではなく、エージェントが必要な部分を読む・検索するためのファイルです
- rg は既定の設定です。`.` で始まるフォルダの中(`rg --hidden` でだけ出る行)は、ltq も読みません。`.` で始まるファイルは、rg の既定は読みませんが、ltq は `.` で始まらないフォルダにあれば読みます(rg より多く出る側です)

### E2E(Copilot CLI + MCP、2026-11-11)【実測】

O3DE の O2(`AZ::DynamicModuleHandle::Create` の呼び出しを全部挙げる。正解 17 のうち 2 つは無効な `#if` の中、2 つはどの TU も読まないファイル)を、Claude Haiku 5.5(BYOK)で解かせた。

| 条件 | 回 | 正答(最後の列挙) | 入力トークン(平均) | 秒(平均) |
|---|---|---|---|---|
| G(cxxref の MCP、索引の差分あり) | 3 | 3/3 | 80,401 | 36 |
| A(rg だけ) | 1 | 1/1 | 66,393 | 36 |

G の 3 回とも、`refs` の結果に `[inactive: #if ...]` の行があった。O2 は rg 向きの問題で、どちらも正解した。最初に流した回は、ユーザー設定の別のチェックアウトのサーバーが使われていた(上の `--additional-mcp-config` の項)。

漏れのない一覧を足した後(`refs --all`、一覧のファイル、C/C++ 以外のファイルを大きさで除かない、の変更の後)に、同じ問題を同じモデルで流し直した(回の名前は `e2e-list2`)。

| 条件 | 回 | 正答(最後の列挙) | 入力トークン(平均) | 秒(平均) | 失敗したツール呼び出し |
|---|---|---|---|---|---|
| G(cxxref の MCP、索引の差分あり) | 3 | 2/3 | 56,451 | 30 | 0 |

- 3 回とも、最初の `refs` 1 回で正解の 17 か所が全部出た(結果に `== coverage:` の行と一覧のファイルの場所、`[inactive: #if ...]` の行がある)。そのあと grep を 1〜2 回引いて確かめている。一覧のファイルを読んだ回は無かった(17 か所は結果の本文に並んでいて、読む必要が無い問題)
- 不正解とした 1 回は、最後の列挙は正解の 17 か所だが、その前の説明で 1 か所を `Code/AzToolsFramework/.../PythonLoader.cpp:50` と省いたパスで書いていて、採点がそれを別の場所として数えた(挙げた場所そのものは合っている)
- この前に流した 3 回(`e2e-list`)は、3 回とも正解だったが、`refs` が全部 `KeyError: 'local'` で失敗していて、モデルは grep だけで答えていた。インストール済みのプラグイン(cxxref 0.5.1)の古いサーバーが起動されていたため(上の `COPILOT_PLUGIN_DIR_ONLY` の項)。正答の数だけでは分からないので、`collect.py` に失敗したツール呼び出しの数を足した

### 0.5.3 の確かめ(2026-11-12)【実測】

0.5.3(読めないファイルを `== not searched:` に出す、空の名前を断る、長い 1 行の分け目をまたぐ名前を見つける、など)で、速さと E2E を測り直した。

速さ: O3DE で 9 つの名前(`STATE_STARTED`・`Create`・`GetName`・`Activate`・`AddActionToMenu`・`for_each`・`ma_result`・`WINAPI`・`AZ::DynamicModuleHandle::Create`)を、0.5.2 と 0.5.3 で 4 回ずつ引いた(回ごとに版の順を入れ替え、先に 1 回温める)。中央値の合計:

| 形 | 0.5.2 | 0.5.3 | 比 |
|---|---|---|---|
| `ltq refs`(単独。C/C++ 以外のファイルを全部読む) | 35.1 秒 | 35.1 秒 | 1.00 |
| MCP の `refs`、その名前で 1 回目 | 10.6 秒 | 10.9 秒 | 1.02 |
| MCP の `refs`、同じ名前で 2 回目 | 10.4 秒 | 10.6 秒 | 1.02 |

- 名前ごとの比は 0.92〜1.10。いちばん離れたのは MCP の `ma_result`(3.0 秒 → 3.3 秒)。出力の行は、9 名・3 つの形とも 0.5.2 と同じだった
- O3DE では、MCP がプロセスを分けて探す条件(読む合計が 1 GiB 以上)に当たる名前は無い。UE5 では測っていない

E2E: 上と同じ O2 を、同じ条件 G・同じモデルで流した(回の名前は `e2e-053`。このリポジトリの 0.5.3 のサーバー)。

| 条件 | 回 | 正答(採点) | 最後の列挙が正解 | 入力トークン(平均) | 秒(平均) | 失敗したツール呼び出し |
|---|---|---|---|---|---|---|
| G(cxxref の MCP、索引の差分あり) | 3 | 2/3 | 3/3 | 73,101 | 30 | 0 |

- 3 回とも、最初の `refs` 1 回で正解の 17 か所が全部出た。一覧のファイルを読んだ回は無かった
- 採点で不正解とした 1 回は、最後の列挙は正解の 17 か所だが、その前の説明で `Module.cpp:86` とファイル名だけで書いていて、採点がそれを別の場所として数えた(PhysX4 と PhysX5 に同じ名前のファイルがある)
- 入力トークンは回でばらつく(45,228・68,779・105,297)。多かった回は、grep を 4 回引いて確かめている

### 0.5.4 の確かめ(2026-11-12)【実測】

0.5.4(上書きの族を 1 行ずつ出す、結果に並べなかった行の一覧を、エージェントが 1 回で読める大きさのファイルに分ける)で、rg との突き合わせをやり直した。`parity.py` は、サーバーが書いた一覧のファイルを全部読むようにし(前は 1 つ目だけ)、`mcp+read` と、文面と一覧の両方に出ている行の数を足した。

| | 名前 | rg の行 | MCP の文面: 1 行ずつ | 文面+読めるファイル: 1 行ずつ | 文面+一覧のファイル全部: 1 行ずつ | 一覧を書いた名前 | ファイルの数(うち大きくて検索させるもの) | 両方に出ている行 | missing |
|---|---|---|---|---|---|---|---|---|---|
| コードベース A(`runtime/` の名前) | 40 | 5,176 | 1,124 | 1,927 | 5,176 | 4 | 15(2) | 0 | 0 |
| O3DE(30 個 + よくある名前 4 個 + 修飾名 3 個) | 37 | 24,093 | 535 | 1,968 | 24,093 | 16 | 46(10) | 0 | 0 |
| O3DE、修飾名 5 つ(`--rg-short`。virtual の `AZ::Component::Activate`・`Deactivate` を含む) | 5 | 21,863 | 258 | 1,262 | 21,863 | 5 | 14(5) | 0 | 0 |

- どの行でも missing は 0 で、文面と一覧のファイルを合わせると rg の行が全部 1 行ずつ出た。両方に出ている行は 0(文面 + 一覧 = 名前を含む行が 1 回ずつ)
- 読めるファイルの大きさの中央値は約 17.8 KB(上限は 18,000 バイト)。大きいファイルは最大 1.9 MB(O3DE)。MCP の 1 回は、O3DE で中央値 0.4 秒・最大 6.1 秒、コードベース A で中央値 0.05 秒未満・最大 0.4 秒
- 読めるファイルに入るのは 3 つ分(約 54 KB)までなので、よくある名前(`Create`・`AZ_Assert` など)や、上書きが数百ある virtual では、索引が結び付けた行も大きいファイル(grep で探す、と案内するファイル)に回る。O3DE の 37 名では、rg の行のうち索引にある 14,842 行が、文面にも読めるファイルにも 1 行ずつは出ない(コードベース A の 40 名では 1,166 行)。1 行ずつ見るには、大きいファイルを検索するか、`path` で絞って引き直す
- `AZ::Component::Activate` では、文面に並べなかった上書き 637 個が「... 637 more overrides」という数だけで出て、フォルダごとの件数には入らない(突き合わせでは、文面の hidden が 383 行。一覧のファイルには全部出て、うち 127 行は大きいファイルの中)。ほかの 4 つの修飾名と、上の 2 行では hidden は 0
- MCP の `check` に場所を多く渡して結果が上限を超えたときに、末尾を切らなくなっていたのを直した(0.5.4 のレビューで見つけた。`mcp/test/test_server.py` の `test_large_check_result_is_cut`)

### virtual の呼び出しの形(`[dynamic]`・`[static]`・`[super]`。2026-11-12)【実測】

ltidx に `-dynamic`・`-static` の印を足した版(ブランチ `feat/virtual-call-kinds`)で、コードベース A と O3DE の索引を作り直した(元の索引は残し、`a-dyn`・`o3de-dyn` として別に置いた)。

| | TU | 索引の行 | 印のある仮想関数の呼び出し | 印を外して ltq が文面で決めた形: 一致 / 決まらない / 食い違い | 同じ位置に印の違う行 |
|---|---|---|---|---|---|
| コードベース A | 362/367 | 178,268(元と同じ) | 134 | 134 / 0 / 0 | 0 |
| O3DE | 5,696/5,876 | 3,932,823(元より 121 多い) | 116,105 | 56,074 / 60,031 / 0 | 121 |

- 食い違いはどちらも 0。O3DE で決まらない行の大半は `AZ_Assert(...)` のようなマクロの中の呼び出し(`AZ::Debug::ITrace::Assert` など。名前が行に書かれていない)。前の ltidx の索引では、これらは印なしで `not told apart` と数える
- O3DE の 121 か所は、実体化ごとに、仮想関数と同じ修飾名の仮想でない関数(関数テンプレートなど)に解決される呼び出し(`AzNetworking::ISerializer::Serialize`・`PhysX::PhysXSystem::GetScene` など)。ltq は、記号・呼び出し元・位置が同じで印の有無だけ違う行を 1 行(印のある方)にまとめる。最初の版ではオーバーロードの組の候補ごとに印が違い、588 か所あった
- rg との突き合わせ(作り直した索引): コードベース A 40 名(rg の行 5,176)、O3DE 37 名(24,093)と修飾名 5 つ(21,863)で、missing 0、MCP の文面と一覧のファイルの両方に出る行 0、文面と一覧のファイルを合わせると rg の行が全部 1 行ずつ出た。MCP が返す形の内訳と、文面に 1 行ずつ出る行の数(1,124・535・258)は 0.5.4 と同じ
- UE5(前の ltidx の索引のまま)の `FAnimNode_Base::Update_AnyThread`: `calls` の 13 行を 4 `[dynamic]`・9 `[super]` に、上書きへの呼び出し 20 行を 17 `[dynamic]`・3 `[super]` に分けた。MCP の結果は 0.5.4 と同じ形のまま 16,813 → 17,174 バイト。`FAnimInstanceProxy::Initialize` も 0.5.4 と同じ形(文面に 39 行)

## 計測に使うソースの写し

正解の行番号は、ソースの写しに対するものです。写しは計測の外で書き換えないでください。`snapshot.py digest <写し>` の指紋で、写しが変わっていないかを確かめます(`batch.ps1 -Digest` は始める前と各回の後に確かめます)。指紋は、計測の道具が書き換える `.` で始まるフォルダ・ファイル(`.index` `.cdb` `.github` `.mcp.json`)を除いて作ります。

| 写し | 場所(手元) | 指紋(SHA-256) |
|---|---|---|
| コードベース A(2026-11-03 の作業ツリー。未コミットの変更を含む) | `E:\dev\bench-data\cxxref\` の下(フォルダ名は伏せます。`local\codebase-a.json` に書く) | `d15d190d64f53034e5312f6d669708b9a3b6a44b5650cd43687759b8d6598e6a`(1,764 ファイル) |
| コードベース B | `E:\dev\bench-data\cxxref\b` | `58c865fb54c9a4c1d3a01cd500bba354c32c0b2b8371f7966becddcc02f3d805`(1,667 ファイル) |
| コードベース C(X86 だけを、アサーションありの Release で構成) | `E:\dev\bench-data\cxxref\` の下(フォルダ名は伏せます) | `bbacd634c6ad35de17a2f16becc3f5822bfbf4c206ad9a04359a035636338bb1`(10,338 ファイル) |
| O3DE 2605.0(`3db6943`。Ninja・profile・unity build なし・テストなしで構成。`build/` は `.snapshot-exclude` で指紋から除く) | `E:\dev\bench-data\cxxref\o3de-src` | `d26eea97d767cecd8a24c030e1cc8833f1ef269b8e03ef0d210cb134ac50254a`(29,950 ファイル) |

O3DE の問題(O1〜O3)は公開しています(`tasks-o3de.json`・`answer-format-o3de.md`)。O3DE は誰でも同じ版を取ってこられるので、写しの作り方を `../docs/design.md` の「大規模な公開コード」に書きました。正解は索引を使わずに rg と読解で作り、判定の理由を各問の `note` に残しています。

UE5 5.8.4(`5.8.4-release`、`f45729c0b`)の写しは、Epic のアカウントを GitHub に結んだうえで、次の手順で作りました(`E:\dev\bench-data\cxxref\ue5-5.8.4`)。Setup.bat は、依存物を取るほかに再頒布パッケージの導入とエンジンの登録をするので使いません。

1. `git clone --depth 1 --branch 5.8.4-release https://github.com/EpicGames/UnrealEngine.git`
2. `Engine\Binaries\DotNET\GitDependencies\win-x64\GitDependencies.exe --force --exclude=Mac --exclude=Linux --exclude=LinuxArm64 --exclude=Android --exclude=IOS --exclude=TVOS --exclude=VisionOS`(20.4 GB)
3. `Engine\Build\BatchFiles\RunUBT.bat -mode=GenerateClangDatabase -Target="UnrealEditor Win64 Development" -DisableUnity -OutputDir=<写し>\.cdb`(100 秒。40,403 件。ビルドはしない。UHT の生成物は作られる)
4. `python -I expand_rsp.py <写し>\.cdb\compile_commands.rsp.json <写し>\.cdb\compile_commands.json --for-tools`(3 の出力を `compile_commands.rsp.json` に改名してから)。UBT の出力は引数を入れ子の応答ファイル(`@...rsp`)で渡すので、展開してどのツールにも同じものを渡します。`--for-tools` は、VS 同梱の clang 22 の組み込みヘッダーを指す `-resource-dir=` と、依存ファイルを書き出す指定(`/clang:-MD`・`/clang:-MF`)を外します(1.6 GB)

写しを別の場所に移したときは、`snapshot.py rebase <写し>\.cdb <元の場所> <新しい場所>` で `compile_commands.json` の中のパスを書き換えます。

## 注意

- 実行のたびに Copilot の credits を消費します(gpt-6-luna で 1 問 0.1〜0.7 credits)
- 正解の行番号は、上の写しに対するものです。今のコードベース A では行がずれています
