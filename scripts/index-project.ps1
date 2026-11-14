# プロジェクトの索引を作る: <Root>\.index\<Name>.tsv(と .tu.tsv / .skip.tsv / .deps.tsv)
# ltidx を最低優先度で実行する。引数を省くとソースのルートだけを尋ねる(ダブルクリック用)。
# compile_commands.json を -Cdb で渡さず、ルートにも無ければ、make-cdb.ps1 で作る(.slnx / .sln / .vcxproj / CMakeLists.txt から)。
# 構成(Debug/Release、プラットフォームなど)ごとに -Name を変えて作ると、ltq と MCP サーバーはすべてをまとめて引く。
# -Update を付けると、前回の後に変わったファイルを読む TU・新しい TU・コマンドの変わった TU だけを解析し直す。
# 作り方を <Name>.tsv.recipe.json に残す。MCP サーバーはそれを使って、ソースが変わると自動で作り直す。
# 索引ができたら、この構成では無効な #if の中を仮に有効にして解析し、差分を <Name>.tsv.alt.tsv に置く(altpass.py。Python が要る)。
[CmdletBinding()]  # 知らない引数をエラーにする(黙って捨てない)
param(
    [string]$Root,                  # ソースのルート(索引のパスの起点。MCP サーバーもここで動かす)
    [string]$Cdb,                   # compile_commands.json のあるフォルダ(省くと、ルートのもの、無ければ作る)
    [string]$Project,               # compile_commands.json を作る元(.slnx / .sln / .vcxproj / CMakeLists.txt)。省くと探す
    [string]$Configuration = 'Debug',
    [string]$Platform = 'x64',
    [string]$CMakeArgs = '',      # CMake の構成に足す引数。複数は空白で区切って 1 つの文字列に(例: "-DA=1 -DB=2")
    [string]$Name = 'index',        # 索引の名前(構成名)。英数字・_・- のみ
    [int]$Jobs = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2)),
    [switch]$Update,                # 前回の索引の後に変わった TU だけを解析し直す(初回は全体を作る)
    [switch]$NoAlt                  # 無効な #if の中の解析(差分パス。altpass.py)をしない
)
$ErrorActionPreference = 'Stop'
# 出力をファイルや別のプロセスへ流すとき(/cxxref-index のログ・MCP サーバーの update.log)は、UTF-8 で色を付けずに書く
# (既定では OEM のコードページ(日本語なら cp932)になり、エラーに色のエスケープが混じる)
if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $PSStyle.OutputRendering = 'PlainText' }
if (-not $Root) { $Root = Read-Host 'ソースのルート' }
if ($Name -notmatch '^[A-Za-z0-9_-]+$') { throw "-Name は英数字・_・- のみ: $Name" }
$Root = (Resolve-Path $Root).Path
if (-not $Cdb -and -not $Project -and (Test-Path "$Root\compile_commands.json")) { $Cdb = $Root }
$mk = $null
if (-not $Cdb) {
    # 毎回作り直す(数秒で済み、増えた・消えたソースも拾える)
    $Cdb = "$Root\.index\cdb-$Configuration-$Platform"
    $mk = @{ Root = $Root; Configuration = $Configuration; Platform = $Platform; Out = $Cdb; CMakeArgs = $CMakeArgs }
    if ($Project) { $mk.Project = $Project }
    & (Join-Path $PSScriptRoot 'make-cdb.ps1') @mk
    $mk.Out = (Resolve-Path $Cdb).Path
}
$Cdb = (Resolve-Path $Cdb).Path
if (-not (Test-Path "$Cdb\compile_commands.json")) { throw "compile_commands.json not found in $Cdb" }
# ltidx.exe は、環境変数 LTIDX、このリポジトリでビルドしたもの、配布物を取ってきたもの(get-ltidx)の順に探す(mcp\autoupdate.py と同じ)
$exe = if ($env:LTIDX) { $env:LTIDX } else {
    @((Join-Path $PSScriptRoot '..\indexer\build\ltidx.exe')) +
        @(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'cxxref\ltidx\ltidx.exe' }) |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if ($env:LTIDX -and -not (Test-Path -LiteralPath $env:LTIDX)) {
    throw "環境変数 LTIDX の ltidx.exe がありません: $env:LTIDX(LTIDX を直すか消してください)"
}
if (-not $exe) {
    throw "ltidx.exe not found. Run $(Join-Path $PSScriptRoot 'get-ltidx.bat') once (or build it with indexer\build.bat)."
}

New-Item -ItemType Directory "$Root\.index" -Force | Out-Null
# 索引は git に入れない。プロジェクトの .gitignore は書き換えず、.index の中に「全部を無視する」.gitignore を置く
if (-not (Test-Path -LiteralPath "$Root\.index\.gitignore")) {
    [IO.File]::WriteAllText("$Root\.index\.gitignore", "# cxxref の索引(index-project.bat が置いた)。git に入れない`n*`n")
}
$out = "$Root\.index\$Name.tsv"
# MCP サーバーが、ソースの変更を見つけたときに同じやり方で作り直すための記録(mcp\autoupdate.py が読む)。
# makeCdb は compile_commands.json を作ったときの make-cdb.ps1 の引数。手元のものを使ったときは null(作り直さない)
$recipe = [ordered]@{ version = 1; root = $Root; cdb = $Cdb; out = $out; jobs = $Jobs; makeCdb = $mk; alt = -not $NoAlt }
$recipe | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath "$out.recipe.json" -Encoding utf8NoBOM
# この PC で作った記録として、指紋をリポジトリの外(%LOCALAPPDATA%\cxxref)に登録する。skill から呼ぶ CLI は、登録した記録の
# 索引だけを自動で作り直す(リポジトリに置かれた記録で ltidx や make-cdb を動かさない)。Python が無ければ登録しない
$pyTrust = @('python', 'py') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if ($pyTrust) { & $pyTrust -I (Join-Path $PSScriptRoot '..\mcp\autoupdate.py') --trust $out | Out-Null }
$t0 = Get-Date
$ltidxArgs = @('-p', "`"$Cdb`"", '-root', "`"$Root`"", '-o', "`"$out`"", '-j', $Jobs)
if ($Update) {
    if (Test-Path "$out.deps.tsv") { $ltidxArgs += '-update' }
    else { '前回の索引(.deps.tsv 付き)が無いので、全体を作ります' }
}
$p = Start-Process -FilePath (Resolve-Path -LiteralPath $exe).Path -ArgumentList $ltidxArgs `
    -NoNewWindow -PassThru -RedirectStandardError "$Root\.index\$Name.log"
try { $p.PriorityClass = 'Idle' } catch { }  # すぐ終わった(引数の誤りなど)ときは設定できない。終了コードは取れる
$p.WaitForExit()
Get-Content "$Root\.index\$Name.log" | Where-Object { $_ -notmatch '^\[' }  # TU ごとの進み具合を除く
"{0} 秒。索引: {1}" -f [int]((Get-Date) - $t0).TotalSeconds, $out
# ltidx の終了コード: 0 = すべての TU が通った、1 = 解析にエラーのあった TU がある(索引はできている)、それ以外 = 作れなかった
# (2、または STATUS_STACK_OVERFLOW などの異常終了)。このスクリプトは、索引ができたら 0、できなければ 2 で終える(/cxxref-index が見る)
if ($p.ExitCode -eq 1) {
    "解析にエラーのあった TU があります(.index\$Name.tsv.tu.tsv の ok=0)。その TU だけにある参照は索引に入りません"
} elseif ($p.ExitCode -ne 0) {
    "索引を作れませんでした(ltidx の終了コード $($p.ExitCode)。.index\$Name.log を見てください)。前の索引はそのまま残っています"
    exit 2
}
# 差分パス: 無効な #if の中の呼び出しや定義を、索引の差分にする。失敗しても索引はできているので、知らせるだけ
if (-not $NoAlt) {
    $py = @('python', 'py') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
    if (-not $py) {
        "Python が見つからないので、無効な #if の中の解析(差分パス)を飛ばしました。無効な行は、文字列の一致としてだけ出ます"
    } else {
        $t1 = Get-Date
        & $py -I (Join-Path $PSScriptRoot 'altpass.py') --index $out --ltidx (Resolve-Path -LiteralPath $exe).Path
        if ($LASTEXITCODE -ne 0) { "無効な #if の中の解析(差分パス)に失敗しました(終了コード $LASTEXITCODE)。索引はできています" }
        else { "差分パス {0} 秒" -f [int]((Get-Date) - $t1).TotalSeconds }
    }
} else {
    # 前に作った差分は、今の索引と行番号が合わないことがあるので消す(残すと古い行が [inactive] として混ざる)
    foreach ($f in @("$out.alt.tsv", "$out.alt.json")) {
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force }
    }
}
exit 0
