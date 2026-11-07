# C++ のシンボルの検索(mcp-language-server)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `mcp-language-server` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず mcp-language-server を使ってください。

- 引けるもの: 名前を指定した参照(`references`)と定義のソース(`definition`)、位置を指定した型と宣言(`hover`)、ファイルの診断(`diagnostics`)
- 呼び出し箇所を探すときは、最初に `references` を呼びます。引数 `symbolName` には関数の素の名前だけを渡します(例: `{"symbolName": "Update"}`)。このツールは clangd の workspace/symbol の結果を名前の完全一致で絞るので、`app::Scheduler::Update` や `Scheduler::Update` のような修飾名では、記号があっても `No references found` になります
- 結果の読み方:
  - 同じ名前の記号(別のクラスの同名のメンバーも)すべてについて、それぞれの参照が続けて返ります。どの記号の参照かは書かれていません。宣言は入りません
  - ファイルごとに、`---` の後にパス、`References in File: N`、`At: L<行>:C<列>, ...`(どちらも 1 始まり)が並び、その後に前後の行付きのソースが付きます
- 個々の参照は clangd が型を解決したものですが、同じ名前の別の記号の参照が混ざるので、そのままでは答えに使えません。まず `definition`(`{"symbolName": "<素の名前>"}`)で、その名前の記号がいくつあるかと、それぞれの `Container Name`(属するクラス)を確かめます。記号が 1 つだけなら、`references` の結果はそのまま答えに使えます。複数あるときは、答えに入れる候補の行ごとに `hover`(`filePath` は結果のパス、`line`・`column` は `At:` の L と C をそのまま)を呼び、呼び出し先の宣言がどのクラスのものかを確かめます
- 迷ったとき: 0 件なら、まず修飾名を渡していないかを確かめます。素の名前でも 0 件なら、rg で候補の行を探し、`hover` で呼び出し先を確かめます
