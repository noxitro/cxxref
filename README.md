# cxxref

[![CI](https://github.com/noxitro/cxxref/actions/workflows/ci.yml/badge.svg)](https://github.com/noxitro/cxxref/actions/workflows/ci.yml)

clang(libtooling)で事前に作った C++ の参照索引を、GitHub Copilot などのエージェントが少ない往復とトークンで引けるようにする道具です。Windows 専用です。

Copilot で C++ の呼び出し箇所を探すと、LSP(clangd)を渡してもほとんど使われず、使わせても ripgrep より多くのトークンを使いました。ripgrep は速いものの、同名の関数・コメント内の一致・テンプレート内の呼び出しで誤答しがちです。このリポジトリは、clang の解析結果を事前に索引にしておき、エージェントには「1 回の呼び出しで、答えと、答えに入らない一致の理由まで」返します。

計測(Copilot CLI 1.0.105、gpt-6-luna、各問 5 回(O3DE は 3 回)。C++ の記号の呼び出し箇所を探す問題)の結果:

| コードベース | 手段 | 正答 | 入力トークン平均 | credits 平均 |
|---|---|---|---|---|
| コードベース A(非公開)(ゲームエンジン、367 TU。4 問) | rg だけ | 9/20 | 40k | 0.26 |
| | **MCP(使い方の指示あり)** | **20/20** | **19k(−52%)** | **0.12** |
| B(公開されている OSS の C++ ライブラリ、194 TU。4 問) | rg だけ | 20/20 | 92k | 0.52 |
| | **MCP(使い方の指示あり)** | **20/20** | **33k(−64%)** | **0.23** |
| C(公開されている大規模な OSS のコンパイラ基盤、2,350 TU。3 問) | rg だけ | 9/15 | 144k | 0.77 |
| | **MCP(使い方の指示あり)** | **15/15** | **23k(−84%)** | **0.20** |
| [O3DE](https://github.com/o3de/o3de) 2605.0(公開のゲームエンジン、5,876 TU。3 問。問題と正解は [bench/tasks-o3de.json](bench/tasks-o3de.json)) | rg だけ | 8/9 | 129k | 0.65 |
| | **MCP(使い方の指示あり)** | **9/9** | **59k(−54%)** | **0.37** |

- コードが大きいほど差が開きます。rg だけでは、同じ名前の関数が多いと取りこぼし(C の `print` は 389 種)、通読のトークンが増えます
- MCP は、使い方の指示が無くても使われます(コードベース A で 20 回中 19 回正答、トークン −36%)。clangd の LSP は、渡しても使われませんでした(20 回中 1 回)。使えと指示すると正答は上がりました(rg だけの 9/20 → 15/20)が、入力トークンは rg だけの 1.7 倍でした(clangd が動いていることを確かめて測り直した値。[docs/design.md](docs/design.md) の「LSP の測り直し」)
- rg のほうが向く問題(マクロ・文字列・`.vcxproj` の設定)では、MCP を渡しても正答は落ちず(15/15)、ツールも呼ばれませんでした。入力トークンは 4 割増えます(使い方の指示とツールの定義、1 回あたり約 740 トークンを、モデルを呼ぶたびに送り直す分)
- O3DE では、修飾名で rg でも絞れる対照の問題(O2)だけ、MCP のほうが入力が 3.6 倍多くなりました(正答は同じ)。同じ名前が多い問題(O1・O3)では −58〜81% です。O3DE の索引づくりは 24 並列で 22 分でした
- [Serena](https://github.com/oraios/serena)(clangd を MCP で渡す)とも比べました。O3DE(各問 2 回)では、Serena + 使い方の指示が 5/6 正答・入力 193k・1 回 239 秒、cxxref が 6/6・50k・39 秒です。Serena の索引づくりは 10.6 時間で、6 回のうち 4 回で要求が時間切れになりました。コードベース A でも、指示なしの Serena は 12 回とも使われませんでした([docs/design.md](docs/design.md) の「Serena との比較」「大規模な公開コード」)
- ほかの C++ 向け MCP サーバー 5 つ(clang_index_mcp・clast・mcp-language-server・clangd-mcp・clangd-mcp-server)とも、Claude Haiku 5.5 で比べました(コードベース A の 4 問 × 3 回)。どれにも cxxref と同じくらい手厚い使い方の指示を渡すと、入力トークンは cxxref が rg だけの −27%、clangd-mcp が −19%、ほかの 4 つは +5〜36% でした。正答は cxxref が 12/12、clangd-mcp と clangd-mcp-server が 11/12、ほかは 8〜10/12 で、この回数では差と言えません。指示を cxxref にだけ手厚くした最初の比較(v12)では「rg だけより少なかったのは cxxref だけ」でしたが、それは指示の差によるところがありました([docs/design.md](docs/design.md) の「既存の C++ 向け MCP サーバーとの比較」「公平に近い条件での測り直し」)
- Unreal Engine 5.8.4(40,172 TU)でも、Claude Haiku 5.5 で比べました(3 問 × 3 回)。正答は rg だけと同じ 8/9、入力トークンは −44% でしたが、**1 回の時間は 2.3 倍**(61 秒 → 139 秒)です。この規模では索引の問い合わせが 1 回 1 分前後かかりました(その後、MCP サーバーが索引・ソースの一覧・単語のフィルタ・名前の目次を呼び出しの間で保つようにして、1 回 1.8〜8.5 秒に縮めました。Copilot での測り直しはまだです)。索引づくりは 16 並列で 5.1 時間・メモリ最大 48 GB、索引の大きさは合わせて 54 GB でした。clangd 系の 2 つ(clangd-mcp・clangd-mcp-server)は、clangd が索引を読み込むのに数分かかり、ほぼ grep で解きました(正答 8/9・9/9、入力は rg だけの +19%・+28%)。clang_index_mcp(4 並列)と clast(1 スレッド)は、UE5 全体の索引を作るのに 23 時間・58 時間かかる見込みで、測れませんでした([docs/design.md](docs/design.md) の「Unreal Engine 5 での計測」)
- 索引の後にコードを変えても、MCP サーバーが変わった TU だけを作り直してから答えます(.cpp の変更でコードベース A 5 秒・C 13 秒)

上の表は、呼び出し箇所を探すことだけを聞いた問題です。ふだん頼みそうな作業で、v0.5.1(計測の時点で公開中)のプラグインを入れただけの状態(既定の道具一式、gpt-6-luna、各 5 回)も測りました。

- **呼び出し側を直す・影響を調べる・説明する・ログやテストを足す、というコードベース A の 8 問では、入力トークンが rg だけより 26% 増えました**(95% 信頼区間 +2〜59%、1 回の時間は 75 秒 → 113 秒)。正しさは変わりませんでした。`refs` で呼び出し箇所が分かっても、直すにはそのファイルを開くので読む量は減らず、モデルを呼ぶ回数が増えた分だけ入力が増えます。cxxref が効くのは、「どこで使われているか」に答えれば済む問いです
- 記録を読み直すと、v0.5.1 には 2 つの問題がありました。1 つは、索引の自動更新が裏で走っている間、Windows でエージェントがソースに書き込めない不具合です(ltidx がファイルをマップしたまま読んでいたため)。もう 1 つは、rule が説明やログの追加でも `refs` を呼ばせていたことです。2 つを直した版(計測の時点では未公開。0.5.2 に、ltidx の直しと、`/cxxref-enable` で入れる使い方の指示として入っています)を、A・v0.5.1 と同じ時期に交互に測ると、入力は rg だけの 0.98 倍(95% 信頼区間 0.75〜1.28)、v0.5.1 は 1.07 倍でした。v0.5.1 の比は、前の時期の 1.26 倍から変わっていて、時期による揺れが大きいことになります。編集の失敗は 5 回から 0 回になり、影響の調査の 2 問では入力が rg だけの 0.52・0.86 倍でした。呼び出し側を直す問題では、rg だけより多いままです。作業全体で減るとは言えません
- **C++ 以外のリポジトリ**(Python・C#・TypeScript の 9 問)では、プラグインは有効になっても `refs` はほぼ呼ばれず(30 回中 1 回)、上乗せはモデルを呼ぶ 1 回あたり約 950 トークン(入力の約 5%)でした。作業全体の入力の差は、回ごとのばらつきに埋もれて測れませんでした(rg だけとの比 0.985)

トークンの使いみち: コードベース A で cxxref を使う回では、入力トークンの 7〜8 割が、システムの指示・ツールの定義・問題文を、モデルを呼ぶたびに送り直す分でした。rg だけの回は、読んだファイルと検索の結果が文脈に積もる分が大半を占めます。cxxref は答えと「答えに入らない一致の理由」を 1 回で返すので、ファイルを読み直す回数と、モデルを呼ぶ回数が減ります。逆に、rg でも絞れるのに索引の結果が大きくなる問題(O3DE の O2 では 1 回の実行で約 44,000 文字)では、結果そのものが入力を押し上げます([docs/design.md](docs/design.md) の「入力トークンの内訳」)。

計測の限界: 呼び出し箇所を探す問題は、私たちが作った少数(5 つのコードベースで計 22 問)です。ふだんの作業に近い問題(上の 8 問と C++ 以外の 9 問)は、対象を乱数で選び、問題文を別のエージェントに書かせましたが、1 つの C++ のコードベースでしか測っていません。エージェントは Copilot CLI、OS は Windows だけで、モデルは gpt-6-luna と Claude Haiku 5.5 だけです。ほかの MCP サーバーは、Windows で動かすために手を入れたものがあります(`bench/ext/*.patch`)。正答率の差は、トークンの差ほどはっきりしていません。

導入するかを決めるための詳しい結果(問題文・渡した指示・エージェントが道具をどの順に使ったか・悪い結果)は、[docs/benchmarks.md](docs/benchmarks.md) にまとめています。

計測の手順は [bench/README.md](bench/README.md)、詳細は [docs/design.md](docs/design.md) にあります。まだ試作の段階です。

対象は C++ です(同じ clang で解析できる C と Objective-C++ のソースも索引に入ります)。ほかの言語は扱いません。

## 構成

| フォルダ | 内容 |
|---|---|
| [indexer](indexer/README.md) | `ltidx`: `compile_commands.json` の全 TU を解析し、参照の TSV を作る(C++、Windows) |
| [query](query/README.md) | `ltq`: 索引の問い合わせ CLI(Python) |
| [mcp](mcp/README.md) | MCP サーバー: `ltq` の `refs` / `check` を Copilot のツールとして渡す(Python) |
| `setup.bat` | 使い始めの手順 2〜5(LLVM の場所を決めて ltidx をビルドし、索引を作り、使い方の指示を足し、Copilot CLI に登録する)をまとめて行う |
| [scripts](scripts/README.md) | `index-project.bat`: プロジェクトの索引を作る(`compile_commands.json` が無ければ作る。`-Update` で変わった TU だけ作り直す)。`make-cdb.bat`: Visual Studio のソリューションか CMake から `compile_commands.json` を作る。`register-copilot.bat`: Copilot CLI に MCP サーバーを登録する。`setup-instructions.bat`: 使い方の指示を足す・消す(ローカルかプロジェクトを選ぶ)。`get-ltidx.bat`: ビルド済みの ltidx をリリースから取ってくる(LLVM 無しで使う) |
| `plugin.json`・`mcp.json`・`com.github.copilot\extensions\`・`.github\plugin\marketplace.json` | このリポジトリを Copilot CLI のプラグイン(cxxref)として入れるための定義(下の「チームで使う」) |
| `run-tests.bat` | すべてのテストを流す(下の「動作確認」) |
| [bench](bench/README.md) | Copilot CLI での検索手段の比較(rg・LSP・索引・CLI・MCP) |
| [docs](docs/design.md) | 設計と計測の記録 |

## 使い始める

1. **LLVM を用意する**: 公式リリースの `clang+llvm-<版>-x86_64-pc-windows-msvc.tar.xz` を展開します(MSI 版は不可)。動作を確かめた版は 23.1.4 です

**手順 2〜5 は `setup.bat` でまとめてできます**: ダブルクリックし、LLVM の展開先とソースのルートを入れます。LLVM の場所を `indexer\llvm-root.txt` に書き、ltidx をビルドし、索引を作ります。続けて、使い方の指示を足すか、Copilot CLI に登録するかを Y/n で尋ねます(登録は、未登録か、別の場所の `server.py` で登録されているときだけ尋ねます)。2 回目からは、空のまま Enter で前の LLVM の場所を使います(LLVM の場所に cmd のコードページで表せない文字、たとえば英語版の Windows での日本語があると、`llvm-root.txt` には書かず、そう知らせます)。ルートを空のまま Enter にすると、索引づくりと指示を飛ばします
   - 引数でも渡せます: `setup.bat -LlvmRoot <LLVM> -Root <ルート> -Instructions -RegisterCopilot`。引数を渡したときは尋ねず、指示と登録は `-Instructions`・`-RegisterCopilot` を付けたときだけ行います。`-NoIndex` で索引づくりを飛ばします

2. **ltidx をビルドする**: `indexer\build.bat` をダブルクリックします。LLVM の場所は、環境変数 `LLVM_ROOT` か、`indexer\llvm-root.txt`(1 行目に展開先のフォルダを書く。git には入りません)で指定します
3. **索引を作る**: `scripts\index-project.bat` をダブルクリックし、ソースのルート(`.sln`・`.slnx`・`CMakeLists.txt` のあるフォルダ)を入れます。`compile_commands.json` は、ソリューションか CMake から自動で作ります(ビルドはしません)。`<ルート>\.index\` に索引ができます。`.index` の中に「全部を無視する」`.gitignore` を置くので、プロジェクトの `.gitignore` に足す必要はありません
   - コードを変えた後は、MCP サーバーが呼ばれたときに、変わった TU だけを自動で作り直します([mcp/README.md](mcp/README.md) の「索引の自動更新」)。手で作り直すときは `-Update` を付けます
   - Debug/Release など構成を足すときは `-Configuration Release -Name release` のように名前を変えて作ると、まとめて引けます
   - 細かい指定(ソリューションが複数ある、CMake のオプションを渡す、手元の `compile_commands.json` を使う)は [scripts/README.md](scripts/README.md) を参照してください
4. **Copilot に登録する**: Copilot CLI なら `scripts\register-copilot.bat` をダブルクリックします(中身は次の 1 行です)。以後、どのプロジェクトでも作業フォルダの `.index` を自動で使います。作業フォルダに無ければ、親のフォルダへさかのぼって探します(ソースのルートより下で Copilot を起動しても使えます)

   ```bat
   copilot mcp add code-ref-index -- python -I <このリポジトリ>\mcp\server.py
   ```

   VS Code や、プロジェクト単位の登録は [mcp/README.md](mcp/README.md) を参照してください
   - MCP サーバーの名前は、旧名の `code-ref-index` のままです。ツールの名前(`code-ref-index-refs` など)がこの名前から付き、計測はこの名前で行ったためです
5. **使い方の指示を足す(推奨)**: `scripts\setup-instructions.bat` をダブルクリックし、ソースのルートを入れて番号を選びます。[scripts/cxxref-instructions.md](scripts/cxxref-instructions.md) の内容が入ります。やめるときは同じ .bat で「3: 消す」を選ぶと、足した分だけが消えます
   - 「1: ローカルに足す」: 自分の手元だけに効きます。`.github\instructions\cxxref.instructions.md` を置き、`.git\info\exclude` に足して git から外します(`git status` に出ません)
   - 「2: プロジェクトに足す」: `.github\copilot-instructions.md` に入れます。コミットすると、チームの全員に効きます
   - プラグインの拡張を使うなら、セッションの中で `/cxxref-enable on` でも同じことができます(下の「`/cxxref-enable`」)

## チームで使う(Copilot CLI のプラグイン)

このリポジトリは、そのまま Copilot CLI のプラグイン `cxxref` として入れられます。中身は MCP サーバー(`mcp.json`)と、スラッシュコマンド `/cxxref-index`・`/cxxref-enable` を足す拡張(`com.github.copilot\extensions\cxxref`)です。使い方の指示は持ちません。指示は、索引を使うリポジトリだけで `/cxxref-enable on` で入れます(以前の版は rule として持っていて、プラグインが有効なリポジトリのすべてで毎回読まれていました)。LLVM の用意と ltidx のビルドは要りません(ltidx はリリースの配布物を使います)。Python 3.10 以降・pwsh・Visual Studio(`compile_commands.json` を作るのに使います)は要ります。

マーケットプレイスに載せるだけでは使えません。全体の流れは次のとおりです(詳しくは下の各節)。

| 誰が | いつ | すること |
|---|---|---|
| チームのマーケットプレイスの管理者 | 1 回(版を上げるときも) | マーケットプレイスに cxxref を載せる |
| 各メンバー | PC で 1 回 | プラグインを入れる(手で入れるか、リポジトリの設定で自動で入れる)。`/cxxref-index`・`/cxxref-enable` を使うなら、機能フラグ `EXTENSIONS` を有効にする |
| 各メンバー | リポジトリ(手元の作業フォルダ)ごとに 1 回 | セッションで `/cxxref-index` を実行して索引を作る(ltidx が無ければ先に取ってくる。モデルは呼ばない)。拡張を使わないなら `get-ltidx.bat` と `index-project.bat`。索引は git に入れないので各自が作る。規模によって数分 |
| 各メンバー(または代表が 1 回) | リポジトリごとに 1 回 | `/cxxref-enable on` で使い方の指示を入れる(自分だけ)。チームで揃えるなら、代表が `/cxxref-enable on project` で入れてコミットする |

その後は、コードを変えても MCP サーバーが変わった TU だけを作り直すので、手で作り直す必要はありません。

### チームのマーケットプレイスに載せる

チームのマーケットプレイス(`.github\plugin\marketplace.json` を置いた GitHub のリポジトリ)の `plugins` に、このリポジトリを版のタグで参照する項目を足します。写しは置きません。

```json
{
  "name": "cxxref",
  "source": { "source": "github", "repo": "noxitro/cxxref", "ref": "v0.5.5" },
  "description": "C++ reference index (clang libtooling) as MCP tools",
  "version": "0.5.5"
}
```

- `ref` は `v<版>` のタグにします。タグの付いた版には、ltidx の配布物がリリースに載っています。`"sha": "<40 桁>"` でコミットに固定することもできます
- チームのマーケットプレイスが無いときは、このリポジトリ自身がマーケットプレイスです(`copilot plugin marketplace add noxitro/cxxref` の後、`copilot plugin install cxxref@cxxref`)

### 各自が使い始める

1. **入れる**: 次のどちらかです
   - 索引を使うリポジトリに `.github\copilot\settings.json` を置いてコミットします。そのリポジトリで Copilot CLI を起動すると、セッションの始めに自動で入り、そのリポジトリで有効になります(GitHub のマーケットプレイスで確かめました。ローカルのフォルダのマーケットプレイスでは入りませんでした)

     ```json
     {
       "extraKnownMarketplaces": { "<マーケットプレイス名>": { "source": { "source": "github", "repo": "<org>/<マーケットプレイスのリポジトリ>" } } },
       "enabledPlugins": { "cxxref@<マーケットプレイス名>": true }
     }
     ```

   - 手で入れます: `copilot plugin marketplace add <org>/<マーケットプレイスのリポジトリ>` の後、`copilot plugin install cxxref@<マーケットプレイス名>`
2. **索引を作る(リポジトリごと)。セッションの中で `/cxxref-index` を実行するのがいちばん簡単です**(下の「`/cxxref-index`」)。ltidx が無ければ先に取ってきて、作業中のリポジトリの索引を裏で作ります。モデルは呼ばないので、トークンを使いません。拡張を使わない(使えない)ときは、次の 2 つを手で行います
   - 手で行うときの ltidx の取得(PC で 1 回。プラグインを更新したらもう一度): プラグインのフォルダ(`%USERPROFILE%\.copilot\installed-plugins\<マーケットプレイス名>\cxxref`)の `scripts\get-ltidx.bat` をダブルクリックします。リリースから plugin.json の版の配布物を取ってきて、sha256 を確かめてから `%LOCALAPPDATA%\cxxref\ltidx` に置きます
   - 手で行うときの索引づくり(リポジトリごと): 同じフォルダの `scripts\index-project.bat` をダブルクリックし、ソースのルートを入れます(上の「使い始める」の手順 3 と同じです)。セッションの中なら、`!` を付けるとモデルを通さずに動きます: `!"%USERPROFILE%\.copilot\installed-plugins\<マーケットプレイス名>\cxxref\scripts\index-project.bat" -Root .`
   - 以後はコードを変えても、MCP サーバーが変わった TU だけを作り直します
   - 索引の無いリポジトリで `refs` が呼ばれると、MCP サーバーは「索引が無い」と答え、作り方のコマンドを絶対パスで添えます。使い方の指示は、作る前にユーザーに確かめるようモデルに指示しています
3. **使い方の指示を入れる(リポジトリごと)**: セッションの中で `/cxxref-enable on` を実行します(下の「`/cxxref-enable`」)。自分の手元だけに入り、git には出ません。チームで揃えるなら `/cxxref-enable on project` で入れてコミットします
   - 入れないと、モデルはツールの説明だけを頼りに `refs` を使うかどうかを決めます。計測(v13、指示なしの条件 M)では、`refs` を呼んだのは 40 回中 24 回でした

### `/cxxref-index`(索引を作るスラッシュコマンド)

プラグインの拡張(`com.github.copilot\extensions\cxxref\extension.mjs`)が足すコマンドです。Copilot CLI のセッションで `/cxxref-index` と打つと、モデルを呼ばずに次を行います。

- ltidx.exe が無ければ、リリースから取ってくる(`scripts\get-ltidx.ps1` と同じ)。取ってきた ltidx の版(`%LOCALAPPDATA%\cxxref\ltidx\version.txt`)がプラグインの版と違うとき(プラグインを更新したときなど)も取り直す。取り直せなければ(ltidx を使っている最中など)、そう出して手元のもので続ける
- 作業フォルダ(または、親へさかのぼって `.index` のあるフォルダ)の索引を、裏で作る。索引がすでにあれば、変わった TU だけを作り直す(何も変わっていなければ、すぐ終わる)。`/cxxref-index full` で最初から作り直す。`/cxxref-index <ルート>`・`/cxxref-index full <ルート>` でルートを指定できる(`full` という名前のフォルダは `./full` と書く)
- ltidx の出力の形式が変わった版に替わったときは、変わった TU だけでなく全 TU を作り直す(形式の版を TU ごとの記録に入れている)。プラグインを更新したら、`/cxxref-index` を 1 回実行すれば、新しい ltidx を取ってきて索引も新しい形式に揃う。MCP サーバーも、取ってきた ltidx が古いと分かれば、最初の結果にそう書き添える
- `compile_commands.json` の作り元(`.slnx`・`.sln`・`CMakeLists.txt`・`.vcxproj`。探し方は `scripts\README.md`)の候補が複数あれば、CLI のダイアログで選ばせる(モデルは呼ばない)。選んだものは作り方の記録(`.index\index.tsv.recipe.json`)に残るので、次からは聞かない。選び直すときは、その記録を消してから実行する
- 始めたとき・終わったとき(かかった秒数)・失敗したとき(ログの場所)を、セッションに出す。ログは `<ルート>\.index\cxxref-index.log`。同時に 2 つは走らせない
- 走っている間は、進み具合をセッションに出す。`compile_commands.json` を用意している間はそう出し(1 分を超えると 1 分ごとに経過秒数も)、ltidx が始まると、解析した TU の数・割合・解析エラーの数を、およそ 1 割進むごとに出す(行が増えすぎないよう、前の行から 10 秒以上あける)。元は ltidx が TU ごとに書く `<ルート>\.index\index.log`(`[n/全体] ok|NG ファイル` の行)。ターミナルで細かく追うなら `Get-Content -Wait <ルート>\.index\index.log`

**Copilot CLI の拡張は実験的機能で、機能フラグ `EXTENSIONS` が有効なときだけ読み込まれます。** `%USERPROFILE%\.copilot\settings.json` に `"feature_flags": { "enabled": ["EXTENSIONS"] }` を足す(すでに `feature_flags` があれば、`enabled` の配列に `"EXTENSIONS"` を足す)か、環境変数 `COPILOT_CLI_ENABLED_FEATURE_FLAGS=EXTENSIONS` を設定してから起動します。拡張はセッションが始まってから読み込まれるので、入力欄に `/cx` と打って一覧に出てから実行します(一覧に出る前に送ると、ただの依頼としてモデルに送られます)。

**GitHub Copilot app(デスクトップ アプリ)で使うとき**は、次の点が CLI と違います。

- アプリは、拡張がセッションに出す知らせのうちエラーしか表示しません(ふつうの知らせと警告は出ない)。そのため、アプリで動いていると分かったときは、結果をダイアログにも出します。ダイアログのボタンは、どれを押しても閉じるだけです。進み具合(`/cxxref-index` の途中の % など)はダイアログにしないので、アプリでは出ません。出し方は環境変数 `CXXREF_DISPLAY` で変えられます: `auto`(既定。アプリのときだけダイアログにも出す)、`dialog`(いつもダイアログにも出す)、`timeline`(ダイアログを出さない)。アプリかどうかは、拡張に渡される SDK の場所(`COPILOT_SDK_PATH`)が CLI の配布物(`COPILOT_CLI_DIST_DIR`)の中かどうかで見分けます
- アプリで信頼していないリポジトリでは、拡張が読み込まれません。`/cxxref-index` などを送っても何も起きません(モデルも呼ばれません)。アプリでそのリポジトリを信頼してから使います
- アプリを起動した直後の新規セッションでは、入力欄に `/cx` と打っても一覧に出ないことがあります。アプリは、どれかのセッションで拡張が読み込まれた後で、そのコマンドを一覧に出すようです。出ないときは、既存のセッションを 1 つ開いてから新規セッションを作ります

確かめたこと(Copilot CLI 1.0.105 のランタイムを SDK から画面なしで動かした): この形式(Agent Plugins 1.0)のプラグインで拡張が見つかり、`/cxxref-index` が一覧に出ること。小さなプロジェクトで実行すると索引ができ、2 回目は変わった分だけの作り直しになること。どちらもモデルの呼び出しは 0 回(`totalUserRequests` 0、`totalNanoAiu` 0)。作り元を選ぶダイアログは、Copilot CLI 1.0.107 のランタイムを SDK から画面なしで動かして確かめた(ソリューションが 2 つのプロジェクトで、選択肢が 2 つ出る・やめると何も作らない・選ぶとそれで索引ができ、2 回目は聞かない。どれもモデルの呼び出しは 0 回)。進み具合の表示も同じやり方で確かめた(240 TU のプロジェクトで、6%・58% の 2 行が出てから、できたと出る。モデルの呼び出しは 0 回)。GitHub Copilot app(ランタイム 1.0.106-8)では、信頼済みのリポジトリの新規セッションで、最初の発言の `/cxxref-enable status` の結果がダイアログに出ることを確かめた(ダイアログを付ける前は何も出なかった)。アプリでの `/cxxref-index` と、Copilot CLI の画面では、まだ試していません。

### `/cxxref-enable`(使い方の指示を足す・消すスラッシュコマンド)

同じ拡張が足すコマンドです。`scripts\setup-instructions.ps1` を動かすだけで、モデルは呼びません。足す先を、ローカル(自分だけ)かプロジェクト(コミットすればチーム全員)から選べます。

| 打つもの | すること |
|---|---|
| `/cxxref-enable` | 今の状態(ローカル・プロジェクトのどちらに入っているか)を出す。続けて、入っていなければ `on` / `on project` を、入っていれば `off` を案内する(拡張のコマンドには引数のヒントを付けられないため) |
| `/cxxref-enable on` | ローカルに足す。`.github\instructions\cxxref.instructions.md` を置き、`.git\info\exclude` に足して git から外す |
| `/cxxref-enable on project` | プロジェクトに足す。`.github\copilot-instructions.md` の末尾に、目印で囲んで入れる(目印の外は書き換えない) |
| `/cxxref-enable off` | 入っている方(両方なら両方)から消す。`off local` / `off project` で片方だけ |

- ルートは、作業フォルダの git のルートです(リポジトリでなければ作業フォルダ)。最後にルートを書けば、そこに足します(`/cxxref-enable on D:\src\game`)
- 二重にはしません。プロジェクトに入っていればローカルには足さず、プロジェクトに足すとローカルの分を消します
- ローカルの置き場所(`cxxref.instructions.md`)がコミットされているリポジトリでは、git から外せないので、ローカルには足しません
- Copilot CLI には、Claude Code の `CLAUDE.local.md` のような「手元だけの指示ファイル」がありません(1.0.107 の本体の文字列で探しました)。そのため、ふつうの指示ファイルを git から外す形にしています。置いたファイルが指示として見つかることは `copilot instruction list` の「Repository instructions」で確かめました(1.0.107)。中身がモデルに渡ったかまでは確かめていません
- 指示はセッションの始めに読まれるので、新しいセッションから効きます。今のセッションの途中から効くかは確かめていません
- 入れる中身は [scripts/cxxref-instructions.md](scripts/cxxref-instructions.md) です(以前のプラグインの rule と同じもの)。プラグインを使わず MCP サーバーだけを登録しているリポジトリでは、`setup-instructions.bat` で同じことができます

`setup.bat` の Copilot CLI への登録との関係: cxxref プラグインが入っていれば、`setup.bat` は MCP サーバーを登録しません。手で登録した `code-ref-index` が残っていても、同じ名前のプラグインのものが使われます(公式の優先順位)。要らなければ `copilot mcp remove code-ref-index` で消せます。

プラグインとして入れると、MCP サーバーの作業フォルダはプラグインの場所になります(Copilot CLI 1.0.105 で確かめました)。そのため、サーバーは作業中のフォルダを、ツールの引数 `root`(使い方の指示がモデルに渡させます)か、渡されなければ Copilot CLI のセッションの記録から決めます([mcp/README.md](mcp/README.md))。

### 版を出す(このリポジトリの保守)

`plugin.json` と `.github\plugin\marketplace.json` の `version` を上げて、main に入れます(PR のマージか push)。CI が、その版のリリースがまだ無ければ、そのコミットに `v<版>` のタグを付け、ltidx の配布物(`cxxref-ltidx-win-x64.zip` と `.sha256`)をリリースに載せます。タグを手で付ける必要はありません(手で `v<版>` のタグを push しても、同じようにリリースを作ります)。2 つの `version` が食い違う、または手で付けたタグが `version` と食い違うと、リリースを作らずに止まります。

版を上げたら、必ずこの流れでリリースまで出します。`/cxxref-index` と MCP サーバーは、取ってきた ltidx の版をプラグインの版と比べるので、版だけ上がってリリースが無いと、`/cxxref-index` のたびに取り直しに失敗して警告が出ます。

## 動作確認

`run-tests.bat` をダブルクリックすると、すべてのテストが流れ、最後に `ALL PASS` と出ます。

| 対象 | 内容 | LLVM |
|---|---|---|
| ltq(`query\test`) | 合成した小さな索引で、構成の統合・行の付け直し・仮想関数・Shift_JIS のソースを確かめる | 不要 |
| MCP サーバー(`mcp\test`) | `server.py` を起動し、`initialize`・`tools/list`・`refs`・`check` を JSON-RPC で呼ぶ | 不要 |
| make-cdb・index-project(`scripts\test`) | 一時フォルダに作った `.vcxproj`・`.slnx`・`.sln`・CMake のプロジェクトから `compile_commands.json` を作り、`index-project` だけで索引ができることを確かめる | 不要(Visual Studio の C++ ツールが要る。index-project の確認には `ltidx.exe` も要る) |
| ltidx(`indexer\test`) | サンプルを索引にして期待値と比べる。Shift_JIS・UTF-16 のソース、仮想関数の上書き、`-update` と全体の作り直しの一致 | ビルド済みの `ltidx.exe` が要る(無ければ飛ばす) |

GitHub Actions でも、push と pull request のたびに同じテストを流します(ltidx は LLVM の公式配布物でビルドします)。

ltidx をビルドしたら、テストが作った索引で `ltq` を試せます。

```bat
query\ltq.cmd --root indexer\test --index indexer\test\out\sample.tsv refs A::Reset
```

`calls` に `sample.cpp:7` の 1 件、`text matches ... not in the index` にコメント内(5 行目)と無効な `#if` の中(9 行目)の一致が理由付きで出れば、動いています。

## コードが変わったとき・実行時に決まる呼び出し

- **索引の後にコードを変えた**: `ltq` と MCP サーバーは、変わったファイルの行を今のファイルから探して付け直し、`[moved from line N]` などの注記と、変わったファイルの一覧を出します。正確な結果には `scripts\index-project.bat ... -Update` で作り直します。変わったファイルを読む TU・新しい TU・コマンドの変わった TU だけを解析します(コードベース B で全体 16 秒に対し 1.5 秒)
- **仮想関数**: 基底クラスのポインタや参照越しの呼び出しは、clang が基底のメソッドに解決します。`refs` は `virtual dispatch` の節に、上書きの関係と、実行時に問い合わせの関数へ届きうる基底経由の呼び出しを出します。呼び出しには、実行時の型で動く関数が決まるもの(`[dynamic]`)と、`Base::f()`・`Super::f()` のようにその関数そのものを呼ぶもの(`[static]`。上書きの中から基底を呼ぶものは `[super]`)の印が付きます
- **関数ポインタ・`std::function`・コールバック**: 関数を取得した場所は `other references` に出ます。どこから呼ばれるかは、索引では分かりません
- **文字列での呼び出し・リフレクション・DLL の動的ロード・スクリプトからの呼び出し**: 索引に出ません。「0 件」は「使われていない」を意味しません

## 前提

- Windows、Visual Studio 2026 の C++ ツール(ltidx のビルドと `compile_commands.json` の生成)、PowerShell 7(`pwsh`)
- Python 3.10 以降(ltq と MCP サーバー。標準ライブラリだけを使います)

## ライセンス

[MIT](LICENSE)
