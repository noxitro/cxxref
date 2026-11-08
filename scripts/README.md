# scripts — 索引を作る・Copilot に登録する・使い方の指示を足す

## 目的

対象プロジェクトの索引を、手作業を少なく作るための .bat です。

| ファイル | 内容 |
|---|---|
| `index-project.bat` | 索引を `<ルート>\.index\` に作る。`compile_commands.json` が無ければ `make-cdb` で作ってから解析する |
| `make-cdb.bat` | Visual Studio のソリューション・プロジェクトか CMake のプロジェクトから、`compile_commands.json` を作る(ビルドはしない) |
| `register-copilot.bat` | Copilot CLI のユーザー設定に MCP サーバーを登録する |
| `setup-instructions.bat` | プロジェクトの `.github\copilot-instructions.md` に、使い方の指示を足す・消す |

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

直下にどれも無ければ、2 階層下までのフォルダから、C++ のプロジェクト(`.vcxproj`)を含む `.slnx`・`.sln` を探して、1 つだけならそれを使います(`runtime\runtime.slnx` と C# の `Editor\Studio.slnx` が並ぶような形)。`.` で始まるフォルダと `node_modules`・`vcpkg_installed`・`packages` は見ません。2 つ以上あれば `-Project` で選びます。

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

`compile_commands.json` は毎回作り直します(Visual Studio のソリューションなら数秒)。増えた・消えたソースも拾えます。作ったものは `<ルート>\.index\cdb-<構成>-<プラットフォーム>\` に置きます。

作り方(`compile_commands.json` の場所・作り方の引数・並列数)を `<ルート>\.index\<名前>.tsv.recipe.json` に残します。MCP サーバーはこれを使って、ソースが変わると索引を自動で作り直します(`../mcp/README.md` の「索引の自動更新」)。

解析にエラーのあった TU があっても、索引ができれば終了コード 0 で終わります(どの TU かは `.index\<名前>.tsv.tu.tsv` の ok=0 の行)。索引を作れなかったら 2 です(前の索引は残ります)。

`make-cdb.bat` だけを使うこともできます(引数は `-Root`・`-Project`・`-Configuration`・`-Platform`・`-CMakeArgs`・`-Out`)。

### ビルド済みの ltidx を取ってくる(`get-ltidx.bat`)

LLVM と Visual Studio で ltidx をビルドせずに使うときは、`get-ltidx.bat` をダブルクリックします。GitHub のリリースから、`plugin.json` の版の配布物(`cxxref-ltidx-win-x64.zip`。ltidx.exe・clang の組み込みヘッダー・ライセンス)を取ってきて、sha256 を確かめてから `%LOCALAPPDATA%\cxxref\ltidx` に置きます。途中で失敗したら前のものを残します。

- `-Version 0.5.0` で版を、`-Zip <zip>` で手元の zip を、`-Dest <フォルダ>` で置き場所を指定できます
- 配布物は `package-ltidx.ps1` が作ります(CI がタグのときにリリースに載せます)。ライセンスの表示(LLVM のライセンス文と、`llvm-notices.py` が LLVM のソースから作る別ライセンスの部品の表示)は CI が渡します。手元で `pwsh -File package-ltidx.ps1` で作った `dist\` の zip は、表示が足りないので配らないでください(そう警告します)

### 使い方の指示を足す・消す(`setup-instructions.bat`)

`setup-instructions.bat` をダブルクリックし、ソースのルートを入れます。今の状態(入っているかどうか)が出るので、番号を選びます。

- **1: 足す**: [../bench/mcp-instructions.md](../bench/mcp-instructions.md) の中身を、`<ルート>\.github\copilot-instructions.md` の末尾に足します。ファイルが無ければ作ります。すでに入っていれば、最新の中身に差し替えます
- **2: 消す**: 足した部分だけを消します。ほかに中身が無くなったら、ファイルごと消します

足した部分は `<!-- cxxref:begin ... -->` と `<!-- cxxref:end -->` の目印で囲みます。目印の外(自分で書いた指示)は書き換えません。目印の間を手で書き換えても、次に「足す」か「消す」を選ぶと上書き・削除されます。改行(CRLF/LF)と BOM の有無は、元のファイルに合わせます。

聞かずに実行するときは引数で指定します。

```bat
setup-instructions.bat -Root D:\src\game -Action Add
setup-instructions.bat -Root D:\src\game -Action Remove
```

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
- `setup`(リポジトリ直下の `setup.bat` の中身)
  - LLVM でないフォルダを渡すと、ビルドに進まず `indexer\llvm-root.txt` も書き換えないこと(pwsh だけで走ります)
  - 通しで、索引・`.index\.gitignore`・使い方の指示ができること。Copilot CLI への登録は、未登録なら行い、このリポジトリの `server.py` で登録済みなら何もせず、別の場所の `server.py` で登録されていても `-RegisterCopilot` が無ければ登録し直さないこと。Copilot CLI は偽物の `copilot.cmd` に差し替えて確かめるので、本物の設定には触れません(Visual Studio・LLVM・ビルド済みの `ltidx.exe` が要ります)
  - 引数無し(ダブルクリック)のときの問い合わせ: 空のまま Enter で前の LLVM の場所を使うこと、引用符と末尾の `\` を外すこと、登録の Y/n(空なら Y)。空白と ASCII でない文字(日本語版の Windows なら日本語、英語版なら é)を含む LLVM の場所を書いた `llvm-root.txt` を、`build.bat`(cmd の `set /p`)が読み戻せること。cmd のコードページで表せない文字を含む場所は、`llvm-root.txt` に書かずに知らせること。一時フォルダに写した `setup.ps1` と、偽物の `build.bat`・LLVM で確かめるので、本物のビルドと `indexer\llvm-root.txt` には触れません(pwsh だけで走ります)
- `llvm-notices.py` が、LLVM のソースの写しから各部品の表示(目印を含むコメント)を写すこと。目印が見つからなければ、欠けた表示を作らずに失敗すること(ネットワークは使いません)
- `package-ltidx` で作った zip を `get-ltidx` で取り込むと、ltidx.exe・組み込みヘッダー・版・ライセンスの表示が置かれ、その ltidx.exe で索引が作れること。sha256 が合わない zip は取り込まず、前のものを残すこと(ネットワークは使いません。ビルド済みの ltidx.exe と LLVM が要ります)
- `setup-instructions` で足して消すと元のファイルに戻ること(改行・BOM・自分で書いた指示を含む)、2 回足すと差し替わること(この確かめは pwsh だけで走ります)
