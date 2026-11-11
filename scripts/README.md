# scripts — 索引を作る・Copilot に登録する・使い方の指示を足す

## 目的

対象プロジェクトの索引を、手作業を少なく作るための .bat です。

| ファイル | 内容 |
|---|---|
| `index-project.bat` | 索引を `<ルート>\.index\` に作る。`compile_commands.json` が無ければ `make-cdb` で作ってから解析する |
| `make-cdb.bat` | Visual Studio のソリューション・プロジェクトか CMake のプロジェクトから、`compile_commands.json` を作る(ビルドはしない) |
| `register-copilot.bat` | Copilot CLI のユーザー設定に MCP サーバーを登録する |
| `setup-instructions.bat` | 使い方の指示を足す・消す。自分だけ(`.github\instructions\cxxref.instructions.md` を git から外して置く)か、プロジェクト(`.github\copilot-instructions.md`)かを選ぶ |

## 前提

- Windows、Visual Studio(C++ のツール)、PowerShell 7(`pwsh`)
- `ltidx.exe`(`index-project` が使う)。`indexer\build.bat` でビルドしたものか、`get-ltidx.bat` で取ってきたもの(`%LOCALAPPDATA%\cxxref\ltidx`)。両方あればビルドしたものを使い、環境変数 `LTIDX` があればそれを使います(`LTIDX` の指す先に無ければ、ほかを探さずに止まります)

## 使い方

### いちばん簡単な使い方

`index-project.bat` をダブルクリックし、ソースのルートを入れます。ルートの直下から、次の順に探して `compile_commands.json` を作ります。

1. `.slnx`(1 つだけのとき)
2. `.sln`(1 つだけのとき)
3. `CMakeLists.txt`
4. `.vcxproj`(1 つだけのとき)

直下にどれも無ければ、2 階層下までのフォルダから、C++ のプロジェクト(`.vcxproj`)を含む `.slnx`・`.sln` を探して、1 つだけならそれを使います(`runtime\runtime.slnx` と C# の `Editor\Studio.slnx` が並ぶような形)。`.` で始まるフォルダと `node_modules`・`vcpkg_installed`・`packages` は見ません。2 つ以上あれば `-Project` で選びます。(Copilot CLI の `/cxxref-index` は、候補が複数あればダイアログで選ばせます)。

ルートに `compile_commands.json` があれば、それを使います。

索引は `<ルート>\.index\` にできます。`.index` の中に「全部を無視する」`.gitignore`(中身は `*`)を置くので、索引は git に入りません。プロジェクトの `.gitignore` は書き換えません。

### 指定するとき

```bat
index-project.bat -Root D:\src\game
index-project.bat -Root D:\src\game -Project D:\src\game\Game.sln -Configuration Release -Name release
index-project.bat -Root D:\src\game -Update
index-project.bat -Root D:\src\lib -CMakeArgs "-DBUILD_EXAMPLES=ON -DBUILD_TESTS=OFF"
index-project.bat -Root D:\src\game -Cdb D:\build\game
```

| 引数 | 内容 |
|---|---|
| `-Root` | ソースのルート。索引のパスの起点で、MCP サーバーもここで動かす |
| `-Project` | `compile_commands.json` を作る元(`.slnx`・`.sln`・`.vcxproj`・`CMakeLists.txt`)。ルートの直下に複数あるときに指定する |
| `-Configuration` / `-Platform` | 構成とプラットフォーム(既定 `Debug` / `x64`)。`.sln` では、ソリューションの構成から各プロジェクトの構成への対応を使う |
| `-CMakeArgs` | CMake の構成に足す引数。複数は 1 つの文字列に空白で区切って書く(そのまま CMake のコマンドラインに入る) |
| `-Cdb` | 手元の `compile_commands.json` のあるフォルダ。指定すると作らない |
| `-Name` | 索引の名前(既定 `index`)。構成ごとに名前を変えて作ると、ltq と MCP サーバーがまとめて引く |
| `-Update` | 前回の後に変わったファイルを読む TU・新しい TU・コマンドの変わった TU だけを解析し直す |
| `-Jobs` | 並列数(既定は論理コア数の半分)。解析は最低優先度で走る |
| `-NoAlt` | 差分パス(下)を走らせない。前に作った差分(`<名前>.tsv.alt.tsv`・`.alt.json`)は消す |

索引を作った後に、続けて差分パス(`altpass.py`)が走ります。作った構成では無効な `#if` の中を、条件を仮に有効にして(書き換えた写しを `ltidx -overlay` で元のパスに見せて)、そのファイルを読む TU を 1 つだけ解析し直し、元は無効だった行の分だけを `<名前>.tsv.alt.tsv` に書きます。ltq と MCP サーバーは、その行に `[inactive: #if ...]` を付けて返します。外から `-D` で渡すマクロや別のコンパイラ向けの分岐も、構成を増やさずに拾えます。O3DE(5,876 TU、索引 897 MB)では、16 並列で 852 秒、差分は 6.6 MB(本体の 0.7%)で、無効な行の 87.2% を解析できました【実測】。Python が無いときと、差分パスが失敗したときは、知らせるだけで索引はそのまま使えます(無効な行は、文字列の一致としてだけ出ます)。

`compile_commands.json` は毎回作り直します(Visual Studio のソリューションなら数秒)。増えた・消えたソースも拾えます。作ったものは `<ルート>\.index\cdb-<構成>-<プラットフォーム>\` に置きます。

作り方(`compile_commands.json` の場所・作り方の引数・並列数)を `<ルート>\.index\<名前>.tsv.recipe.json` に残します。MCP サーバーはこれを使って、ソースが変わると索引を自動で作り直します(`../mcp/README.md` の「索引の自動更新」)。

解析にエラーのあった TU があっても、索引ができれば終了コード 0 で終わります(どの TU かは `.index\<名前>.tsv.tu.tsv` の ok=0 の行)。索引を作れなかったら 2 です(前の索引は残ります)。

`make-cdb.bat` だけを使うこともできます(引数は `-Root`・`-Project`・`-Configuration`・`-Platform`・`-CMakeArgs`・`-Out`)。`-ListProjects` を付けると、作らずに、`-Project` を省いたときの候補を 1 行に 1 つ出します。

### ビルド済みの ltidx を取ってくる(`get-ltidx.bat`)

LLVM と Visual Studio で ltidx をビルドせずに使うときは、`get-ltidx.bat` をダブルクリックします。GitHub のリリースから、`plugin.json` の版の配布物(`cxxref-ltidx-win-x64.zip`。ltidx.exe・clang の組み込みヘッダー・ライセンス)を取ってきて、sha256 を確かめてから `%LOCALAPPDATA%\cxxref\ltidx` に置きます。途中で失敗したら前のものを残します。

- `-Version 0.5.0` で版を、`-Zip <zip>` で手元の zip を、`-Dest <フォルダ>` で置き場所を指定できます
- 配布物は `package-ltidx.ps1` が作ります(CI が、版の上がったコミットが main に入ったとき、またはタグの push のときにリリースに載せます)。ライセンスの表示(LLVM のライセンス文と、`llvm-notices.py` が LLVM のソースから作る別ライセンスの部品の表示)は CI が渡します。手元で `pwsh -File package-ltidx.ps1` で作った `dist\` の zip は、表示が足りないので配らないでください(そう警告します)

### 使い方の指示を足す・消す(`setup-instructions.bat`)

`setup-instructions.bat` をダブルクリックし、ソースのルートを入れます。今の状態(ローカル・プロジェクトのどちらに入っているか)が出るので、番号を選びます。足す中身は [cxxref-instructions.md](cxxref-instructions.md) です(以前のプラグインの rule と同じもの。プラグインは指示を持たないので、使うリポジトリにここから入れます)。

- **1: ローカルに足す(自分だけ)**: `<ルート>\.github\instructions\cxxref.instructions.md` を置き、git の `.git\info\exclude` に 1 組(説明の行とパス)を足して git から外します。`git status` に出ないので、コミットに混ざりません。すでにあれば、最新の中身に差し替えます
- **2: プロジェクトに足す(コミットすればチーム全員)**: `<ルート>\.github\copilot-instructions.md` の末尾に足します。ファイルが無ければ作ります。すでに入っていれば、最新の中身に差し替えます
- **3: 消す**: 入っている方(両方なら両方)から、足した分だけを消します。足したときに作ったファイルとフォルダは、ほかに中身が無ければ消します。ローカルでは `.git\info\exclude` に足した 1 組も消します

二重にはしません。プロジェクトに入っていればローカルには足さず、プロジェクトに足すとローカルの分を消します。

プロジェクトでは、足した部分を `<!-- cxxref:begin ... -->` と `<!-- cxxref:end -->` の目印で囲みます。目印の外(自分で書いた指示)は書き換えません。目印の間を手で書き換えても、次に「足す」か「消す」を選ぶと上書き・削除されます。改行(CRLF/LF)と BOM の有無は、元のファイルに合わせます。

ローカルのファイルには `<!-- cxxref:local ... -->` の目印を入れます。目印の無い同じ名前のファイル(自分で作ったもの)があれば、書き換えも消しもせずに止まります。その場所がコミットされているリポジトリでも、git から外せないので止まります。Copilot CLI には手元だけの指示ファイル(Claude Code の `CLAUDE.local.md` のようなもの)が無いので、この形にしました。

聞かずに実行するときは引数で指定します。`-Action Add` で `-Scope` を省くとプロジェクト、`-Action Remove` で省くと両方です。

```bat
setup-instructions.bat -Root D:\src\game -Action Add -Scope Local
setup-instructions.bat -Root D:\src\game -Action Add -Scope Project
setup-instructions.bat -Root D:\src\game -Action Remove
setup-instructions.bat -Root D:\src\game -Action Status
```

Copilot CLI のプラグインの拡張を使うなら、セッションの中で `/cxxref-enable on`(ローカル)・`/cxxref-enable on project`・`/cxxref-enable off` でも同じことができます(../README.md の「`/cxxref-enable`」)。

## 仕組みと制約

- **Visual Studio**: MSBuild の `GetClCommandLines` ターゲット(Visual Studio が IntelliSense に使うもの)を、全プロジェクトに 1 回の MSBuild でまとめて呼び、各 .cpp の cl のコマンドラインを取り出します。プロジェクトのプロパティの設定(include パス・マクロ・言語の版・強制インクルードなど)は、cl に渡るとおりに入ります
  - 「VC++ ディレクトリ」の include パスは cl のコマンドラインに無いので足します。`IncludePath`(インクルード ディレクトリ)に自分で足したフォルダは `/I`、`ExternalIncludePath`(外部インクルード ディレクトリ)と Visual Studio・Windows SDK の既定のフォルダは `/imsvc`(システムのヘッダー扱い)です。**`/imsvc` のフォルダの中のヘッダーは索引に載りません**
  - `.sln` は、ソリューションの構成から各プロジェクトの構成への対応を使います。`.slnx` は、プロジェクトの中の `<BuildType>`・`<Platform>` の対応を使い、無ければ同じ名前で読みます(`x86` は `Win32`)
  - 要求した構成・プラットフォームを持たないプロジェクトは読まず、警告に出します(MSBuild はエラーにせず、その構成の設定が抜けたコマンドラインを返すため)
  - プロジェクトごとに分けて呼ぶので、1 つが読めなくても(NuGet・vcpkg の未復元で `Import` 先が無いなど)、ほかのプロジェクトの分は作り、読めなかったものを警告に出します
- **CMake**: Visual Studio 同梱の CMake と Ninja で、構成だけを `<出力先>\cmake` に行います(`CMAKE_EXPORT_COMPILE_COMMANDS=ON`)
- どちらもビルドはしません。**ビルドで生成されるヘッダー**(コード生成・シェーダーの埋め込みなど)は無いままなので、それを読む TU は解析に失敗します。一度ビルドしてから索引を作ると解決します。失敗した TU は ltq の出力の先頭に出ます
- MSBuild の中間ファイルのパスが 260 文字を超えると、CMake の Visual Studio ジェネレーターなどが失敗します。深いフォルダでは短いパスに置いてください

## テスト

`test\run_test.bat` をダブルクリックします。一時フォルダに `.vcxproj`・`.slnx`・`.sln`・CMake の小さなプロジェクトを作り、次を確かめます。Visual Studio の C++ ツールが要ります。

- `compile_commands.json` の中身(include パス・マクロ・構成とプラットフォームの対応・CMake の引数)
- 壊れたプロジェクトが混ざっていても、ほかのプロジェクトの分ができること
- 要求したプラットフォームを持たないプロジェクトを読まないこと
- `index-project` だけで索引ができ、「VC++ ディレクトリ」のヘッダーの定義も索引に載ること。`.index\.gitignore` を置くこと。残した作り方の記録から、足した TU を含めて作り直せること
- 索引の後に差分パスが走り、無効な `#if` の中の呼び出しが `.alt.tsv` に入ること。`-NoAlt` では走らず、前の差分を消すこと(`altpass.py` そのものの条件の書き換え・パスの重ね方は `test_altpass.py`)
- `setup`(リポジトリ直下の `setup.bat` の中身)
  - LLVM でないフォルダを渡すと、ビルドに進まず `indexer\llvm-root.txt` も書き換えないこと(pwsh だけで走ります)
  - 通しで、索引・`.index\.gitignore`・使い方の指示ができること。Copilot CLI への登録は、未登録なら行い、このリポジトリの `server.py` で登録済みなら何もせず、別の場所の `server.py` で登録されていても `-RegisterCopilot` が無ければ登録し直さないこと。Copilot CLI は偽物の `copilot.cmd` に差し替えて確かめるので、本物の設定には触れません(Visual Studio・LLVM・ビルド済みの `ltidx.exe` が要ります)
  - 引数無し(ダブルクリック)のときの問い合わせ: 空のまま Enter で前の LLVM の場所を使うこと、引用符と末尾の `\` を外すこと、登録の Y/n(空なら Y)。空白と ASCII でない文字(日本語版の Windows なら日本語、英語版なら é)を含む LLVM の場所を書いた `llvm-root.txt` を、`build.bat`(cmd の `set /p`)が読み戻せること。cmd のコードページで表せない文字を含む場所は、`llvm-root.txt` に書かずに知らせること。一時フォルダに写した `setup.ps1` と、偽物の `build.bat`・LLVM で確かめるので、本物のビルドと `indexer\llvm-root.txt` には触れません(pwsh だけで走ります)
- `llvm-notices.py` が、LLVM のソースの写しから各部品の表示(目印を含むコメント)を写すこと。目印が見つからなければ、欠けた表示を作らずに失敗すること(ネットワークは使いません)
- `package-ltidx` で作った zip を `get-ltidx` で取り込むと、ltidx.exe・組み込みヘッダー・版・ライセンスの表示が置かれ、その ltidx.exe で索引が作れること。sha256 が合わない zip は取り込まず、前のものを残すこと(ネットワークは使いません。ビルド済みの ltidx.exe と LLVM が要ります)
- `setup-instructions` で足して消すと元のファイルに戻ること(改行・BOM・自分で書いた指示を含む)、2 回足すと差し替わること。ローカルに足すと `git status` に出ず、消すと `.git\info\exclude` も元に戻ること(この確かめは pwsh と git だけで走ります)
