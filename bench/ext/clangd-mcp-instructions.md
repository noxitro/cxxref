# C++ のシンボルの検索(clangd-mcp)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `clangd-mcp` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clangd-mcp を使ってください。

- 参照の検索(`find_references`・`get_callers` など)は、clangd が型を解決した結果です。同じ名前の別のクラスのメンバーと区別できます

(clangd-mcp の README には、エージェント向けの推奨の指示がありません。上は Serena の条件と同じ書き方にそろえたものです)
