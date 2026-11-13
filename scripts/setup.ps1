# 使い始めの手順をまとめて行う: LLVM の場所を決める → ltidx をビルドする → (ルートを入れれば)索引を作り、
# 使い方の指示を足す → Copilot CLI に MCP サーバーを登録する。
# 引数を省くと、LLVM の場所とソースのルートを尋ね、指示と登録は Y/n で尋ねる(ダブルクリック用)。ルートを空のまま Enter で、索引づくりは飛ばす。
# 引数を渡したときは尋ねない(LLVM の場所を省いたときだけ尋ねる)。指示と登録は -Instructions / -RegisterCopilot を付けたときだけ行う。
# LLVM の場所は ..\indexer\llvm-root.txt に書き残す(以後 indexer\build.bat を単独で叩いても同じ場所を使う)。
[CmdletBinding()]  # 知らない引数をエラーにする(黙って捨てない)
param(
    [string]$LlvmRoot,              # 公式リリースの clang+llvm-<版>-x86_64-pc-windows-msvc を展開したフォルダ
    [string]$Root,                  # 索引を作るソースのルート(省くと尋ねる。空なら索引づくりを飛ばす)
    [switch]$NoIndex,               # 索引づくりを飛ばす(ルートを尋ねない)
    [switch]$Instructions,          # <Root>\.github\copilot-instructions.md に使い方の指示を足す
    [switch]$RegisterCopilot        # Copilot CLI に MCP サーバーを登録する(登録済みなら何もしない)
)
$ErrorActionPreference = 'Stop'
$indexer = (Resolve-Path (Join-Path $PSScriptRoot '..\indexer')).Path
$rootFile = Join-Path $indexer 'llvm-root.txt'
$interactive = -not $PSBoundParameters.Count
# build.bat は llvm-root.txt を set /p で読むので、cmd のコードページ(OEM)で読み書きする
$enc = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)

# 1. LLVM の場所
$current = $env:LLVM_ROOT
$from = '環境変数 LLVM_ROOT'
if (-not $current -and (Test-Path -LiteralPath $rootFile)) {
    $current = @([IO.File]::ReadAllLines($rootFile, $enc))[0]  # ReadLines は途中でやめるとファイルを開いたままにする
    $from = 'indexer\llvm-root.txt'
}
if (-not $LlvmRoot) {
    if ($current) { "今の LLVM の場所: $current($from)" }
    $LlvmRoot = Read-Host 'LLVM を展開したフォルダ(空のまま Enter で今の場所)'
    if (-not $LlvmRoot) { $LlvmRoot = $current }
}
if (-not $LlvmRoot) { throw 'LLVM の場所を入れてください(clang+llvm-<版>-x86_64-pc-windows-msvc を展開したフォルダ)' }
$LlvmRoot = $LlvmRoot.Trim().Trim('"').TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $LlvmRoot 'lib\cmake\clang\ClangConfig.cmake'))) {
    throw "LLVM が見つかりません(lib\cmake\clang\ClangConfig.cmake がありません): $LlvmRoot`nMSI 版ではなく、公式リリースの .tar.xz を展開したフォルダを指定してください"
}
$LlvmRoot = (Resolve-Path -LiteralPath $LlvmRoot).Path
if ($enc.GetString($enc.GetBytes($LlvmRoot)) -ne $LlvmRoot) {
    # 書くと ? に化け、build.bat を単独で叩いたときに別の場所を読む(英語版の Windows の OEM 437 で日本語など)
    "注意: LLVM の場所に、cmd のコードページ(OEM $($enc.CodePage))で表せない文字があるので、$rootFile には書きません。" +
        "この実行のビルドには使います。indexer\build.bat を単独で叩くときは、環境変数 LLVM_ROOT を設定するか、表せる文字だけの場所に LLVM を置いてください"
} else {
    [IO.File]::WriteAllText($rootFile, "$LlvmRoot`r`n", $enc)
    "LLVM の場所を書きました: $rootFile"
}
if ($env:LLVM_ROOT -and $env:LLVM_ROOT -ne $LlvmRoot) {
    "注意: 環境変数 LLVM_ROOT($env:LLVM_ROOT)が設定されています。indexer\build.bat を単独で叩くと、そちらが優先されます"
}

# 2. ltidx をビルドする(2 回目以降は変わった分だけ)
''
'== ltidx をビルドします'
$env:LLVM_ROOT = $LlvmRoot  # この実行の中だけ。llvm-root.txt と同じ場所を使わせる
& cmd /c "`"$indexer\build.bat`""
if ($LASTEXITCODE -ne 0) { throw "ltidx のビルドに失敗しました(終了コード $LASTEXITCODE)" }
"ビルドしました: $indexer\build\ltidx.exe"

function Confirm-Step([string]$q) { (Read-Host "$q (Y/n)") -notmatch '^\s*n' }

# 3. 索引を作る(.index は git に入れない)。使い方の指示を足す
if (-not $Root -and $interactive) {
    ''
    $Root = Read-Host '索引を作るソースのルート(.sln・.slnx・CMakeLists.txt のあるフォルダ。空のまま Enter で飛ばす)'
}
if ($NoIndex -or -not $Root) {
    '索引づくりは飛ばしました。後で scripts\index-project.bat で作れます'
} else {
    $Root = (Resolve-Path -LiteralPath $Root.Trim().Trim('"')).Path
    ''
    '== 索引を作ります'
    & (Join-Path $PSScriptRoot 'index-project.ps1') -Root $Root  # .index\.gitignore も置く(索引を git に入れない)
    ''
    if ($Instructions -or ($interactive -and (Confirm-Step "使い方の指示を $Root\.github\copilot-instructions.md に足しますか"))) {
        & (Join-Path $PSScriptRoot 'setup-instructions.ps1') -Root $Root -Action Add
    } else {
        '使い方の指示は足しませんでした。後で scripts\setup-instructions.bat で足せます'
    }
}

# 4. Copilot CLI に MCP サーバーを登録する(PC で 1 回。未登録か、別の場所の server.py で登録されているときだけ)
''
$server = (Resolve-Path (Join-Path $PSScriptRoot '..\mcp\server.py')).Path
if (-not (Get-Command copilot -ErrorAction SilentlyContinue)) {
    'Copilot CLI が見つからないので、登録は飛ばしました(VS Code から使うときは mcp\README.md)'
    return
}
# copilot は copilot.ps1 / copilot.cmd。pwsh から .ps1 を呼ぶと -- が消えるので、cmd から呼ぶ
# cxxref プラグイン(チームのマーケットプレイスなどから入れたもの)は、skill で同じ索引を引く。MCP サーバーも足すと、
# ツールの定義(毎回約 600 トークン)を二重に送るだけなので、入っていれば登録しない
$plugins = try { @(& cmd /c 'copilot plugin list --json 2>nul' | Out-String | ConvertFrom-Json) } catch { @() }
if ($plugins | Where-Object { $_.name -eq 'cxxref' -and $_.enabled -ne $false }) {
    'Copilot CLI には cxxref プラグインが入っているので、MCP サーバーの登録は要りません(プラグインの skill が同じ索引を引きます)'
    return
}
$info = & cmd /c 'copilot mcp get code-ref-index 2>&1' | Out-String
$registered = $LASTEXITCODE -eq 0
if ($registered -and $info.IndexOf($server, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
    "Copilot CLI には登録済みです: $server"
    return
}
$q = if ($registered) { '別の場所の server.py で登録されています。このリポジトリのものに登録し直しますか' } else { 'Copilot CLI に MCP サーバー(code-ref-index)を登録しますか' }
if (-not ($RegisterCopilot -or ($interactive -and (Confirm-Step $q)))) {
    '登録は飛ばしました。後で scripts\register-copilot.bat で登録できます'
    return
}
$py = & cmd /c 'python -c "import sys; print(sys.version_info >= (3, 10))" 2>nul'
if ($py -ne 'True') { throw 'MCP サーバーには Python 3.10 以降が要ります(python が PATH に無いか、古い版です)' }
& cmd /c 'copilot mcp remove code-ref-index >nul 2>&1'
& cmd /c "copilot mcp add code-ref-index -- python -I `"$server`""
if ($LASTEXITCODE -ne 0) { throw "Copilot CLI への登録に失敗しました(終了コード $LASTEXITCODE)" }
"Copilot CLI に登録しました: $server"
