# C++ のシンボルの検索(clang-index-mcp)

このフォルダの C++ は、libclang で解析した索引を、MCP サーバー `clang-index-mcp` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clang-index-mcp を使ってください。

- このフォルダのプロジェクトは、すでに読み込んで索引を作ってあります。`set_project` を呼び直す必要はありません
- 引けるもの: 関数の呼び出し元(`find_incoming_calls`)と呼び出し先(`find_outgoing_calls`)、記号の検索(`find_symbols_by_pattern`)、クラスの情報と継承(`get_class_info`・`get_class_hierarchy`)
- 呼び出し箇所を探すときは、最初に `find_incoming_calls` を呼びます。関数の素の名前を `function_name` に、メンバー関数ならクラスの名前を `class_name` に分けて渡します(例: `{"function_name": "Update", "class_name": "Renderer"}`)。`class_name` は末尾の名前だけで照合されるので、名前空間は付けても付けなくても同じです。別の名前空間に同じ名前のクラスがあると、その両方の呼び出しが返ります
- 結果の各部分:
  - `call_sites`: 呼び出しの位置(`file`・`line`・`column`)と、それが書かれている関数(`caller`)。libclang が呼び出し先を解決した呼び出し式だけで、宣言・定義や、`&Class::Method` のような関数ポインターの取得は入りません
  - `callers`: 呼び出しを含む関数の一覧(その関数の定義の範囲付き)。呼び出しの行ではありません
  - `total_call_sites`: `call_sites` の件数
- `call_sites` はそのまま答えに使えます。同じ名前を rg でソースから取り直して確かめる必要はありません。コメントの中(`/* */` を含む)の同名は解析されないので出てきません。rg で見つけた行が `call_sites` に無いときは、それがコメントの中か、別のクラスの同名でないかを確かめ、そうなら答えに入れません
- 迷ったとき: `call_sites` が空なら、`find_symbols_by_pattern`(`{"symbol_name": "<クラスか関数の名前>"}`)で名前が索引にあるかを確かめます。名前があるのに空のときは、テンプレートの中の呼び出し(テンプレート引数に依存する基底クラスのメンバーを呼ぶ `Base::Clear()` など)のように、libclang が呼び出し先を決められず索引に入っていないことがあります。その場合は rg で探し、ソースを読んで呼び出し先を判断します
