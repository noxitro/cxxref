# compile_commands.json を作る: <Out>\compile_commands.json(既定 <Root>\.index\cdb-<構成>-<プラットフォーム>)
# 対象は Visual Studio のソリューション(.slnx / .sln)・プロジェクト(.vcxproj)か、CMake のプロジェクト(CMakeLists.txt)。
# -Project を省くと、ルートの直下から .slnx → .sln → CMakeLists.txt → .vcxproj の順に探す。
#   直下に無ければ、2 階層下までのフォルダにある、.vcxproj を含む .slnx / .sln(1 つだけのとき)。
# Visual Studio: MSBuild の GetClCommandLines(VS の IntelliSense が使うターゲット)で、各 .cpp の cl のコマンドラインを取り出す。
#   ビルドはしない。「VC++ ディレクトリ」の include パスはコマンドラインに無いので足す(IncludePath は /I、
#   ExternalIncludePath と VS・SDK の既定のフォルダは /imsvc。/imsvc の中のヘッダーは索引に載らない)。
# CMake: VS 同梱の CMake と Ninja で構成だけ行う(CMAKE_EXPORT_COMPILE_COMMANDS=ON)。ビルドで生成されるヘッダーは無いままになる。
[CmdletBinding()]
param(
    [string]$Root,                      # ソースのルート
    [string]$Project,                   # .slnx / .sln / .vcxproj / CMakeLists.txt(のフォルダ)。省くと探す
    [string]$Configuration = 'Debug',
    [string]$Platform = 'x64',
    [string]$Out,                       # compile_commands.json を置くフォルダ
    [string]$CMakeArgs = ''           # CMake の構成に足す引数。複数は 1 つの文字列に空白で区切って書く(-CMakeArgs "-DA=1 -DB=2")
)
$ErrorActionPreference = 'Stop'
# 出力をファイルや別のプロセスへ流すとき(/cxxref-index のログ・MCP サーバーの update.log)は、UTF-8 で色を付けずに書く
# (既定では OEM のコードページ(日本語なら cp932)になり、エラーに色のエスケープが混じる)
if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $PSStyle.OutputRendering = 'PlainText' }
if (-not $Root) { $Root = Read-Host 'ソースのルート' }
$Root = (Resolve-Path $Root).Path
if (-not $Out) { $Out = Join-Path $Root ".index\cdb-$Configuration-$Platform" }

function Find-Project {
    foreach ($pat in '*.slnx', '*.sln', 'CMakeLists.txt', '*.vcxproj') {
        $f = @(Get-ChildItem -LiteralPath $Root -Filter $pat -File | Where-Object { $_.Name -like $pat })  # *.sln が .slnx に当たらないように
        if ($f.Count -eq 1) { return $f[0].FullName }
        if ($f.Count -gt 1) { throw "$pat が複数あります。-Project で選んでください: $($f.Name -join ', ')" }
    }
    # 直下に無ければ、2 階層下までのフォルダから C++ のプロジェクト(.vcxproj)を含むソリューションを探す
    # (ルートに C# と C++ のソリューションが別のフォルダで並ぶ形)。. で始まるフォルダと依存物のフォルダは見ない。
    # 開けないフォルダ(名前が . で終わる「$(SolutionDir)..」のように、Win32 のパスでは届かないものなど)は飛ばす
    $skip = 'node_modules', 'vcpkg_installed', 'packages'
    $ok = { $_.Name -notlike '.*' -and $_.Name -notlike '*.' -and $skip -notcontains $_.Name }
    $dirs = @(Get-ChildItem -LiteralPath $Root -Directory | Where-Object $ok)
    $dirs += @($dirs | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Directory -ErrorAction SilentlyContinue } | Where-Object $ok)
    $sols = @($dirs | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -ErrorAction SilentlyContinue } |
        Where-Object { $_.Name -like '*.slnx' -or $_.Name -like '*.sln' } |
        Where-Object { (Get-Content -LiteralPath $_.FullName -Raw) -match '\.vcxproj' })
    if ($sols.Count -eq 1) { Write-Host "ルートの直下に無いので、$($sols[0].FullName) を使います"; return $sols[0].FullName }
    $rel = $sols | ForEach-Object { $_.FullName.Substring($Root.TrimEnd('\').Length + 1) }
    if ($sols.Count -gt 1) { throw "C++ のソリューションが下のフォルダに複数あります。-Project で選んでください: $($rel -join ', ')" }
    throw "ルートの直下に .slnx / .sln / CMakeLists.txt / .vcxproj が無く、2 階層下までに C++ のソリューションもありません。-Project で指定してください"
}
if (-not $Project) { $Project = Find-Project }
$Project = (Resolve-Path $Project).Path
if (Test-Path -LiteralPath $Project -PathType Container) { $Project = Join-Path $Project 'CMakeLists.txt' }

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'C++ のツールを入れた Visual Studio が見つかりません' }
New-Item -ItemType Directory $Out -Force | Out-Null
$cdb = Join-Path $Out 'compile_commands.json'

if ((Split-Path $Project -Leaf) -eq 'CMakeLists.txt') {
    $cm = "$vs\Common7\IDE\CommonExtensions\Microsoft\CMake"
    $src = Split-Path $Project
    $build = Join-Path $Out 'cmake'
    $arch = if ($Platform -eq 'Win32') { 'x86' } else { $Platform }
    # cmake と ninja はフルパスで渡す(cmd の 1 行の %PATH% は vcvarsall の前に展開されるので、PATH は書き換えない)
    $cfg = "`"$cm\CMake\bin\cmake.exe`" -S `"$src`" -B `"$build`" -G Ninja `"-DCMAKE_MAKE_PROGRAM=$cm\Ninja\ninja.exe`"" +
           " -DCMAKE_BUILD_TYPE=$Configuration -DCMAKE_EXPORT_COMPILE_COMMANDS=ON $CMakeArgs"
    # vcvarsall.bat は中で vswhere.exe を PATH から呼ぶ
    $env:PATH = "$(Split-Path $vswhere);$env:PATH"
    cmd /c "call `"$vs\VC\Auxiliary\Build\vcvarsall.bat`" $arch >nul && $cfg"
    if ($LASTEXITCODE -ne 0) { throw "CMake の構成に失敗しました(終了コード $LASTEXITCODE)" }
    Copy-Item (Join-Path $build 'compile_commands.json') $cdb -Force
    $n = @((Get-Content -LiteralPath $cdb -Raw | ConvertFrom-Json)).Count
    "compile_commands.json: $cdb($n TU)"
    return
}

# ---- Visual Studio ----

# .vcxproj が持つ構成(Debug|x64 など)
function Get-ProjectConfigs([string]$Path) {
    $x = [xml](Get-Content -LiteralPath $Path -Raw)
    @($x.GetElementsByTagName('ProjectConfiguration') | ForEach-Object { $_.Include })
}
# ソリューションの対応(Solution="Debug|*" のような * を含む形)が、要求した構成に当たるか
function Test-SolutionPattern([string]$Pattern, [string]$Cfg, [string]$Plat) {
    $pc, $pp = $Pattern -split '\|', 2
    ($pc -eq '*' -or $pc -eq $Cfg) -and ($null -eq $pp -or $pp -eq '*' -or $pp -eq $Plat)
}

$projs = [Collections.Generic.List[object]]::new()   # 読むプロジェクト: Path, Cfg, Plat
$skipped = [Collections.Generic.List[string]]::new() # 要求した構成を持たないプロジェクト
$solPlat = if ($Platform -eq 'x86') { 'Win32' } else { $Platform }
function Add-Project([string]$Path, [string]$Cfg, [string]$Plat) {
    if (-not (Test-Path -LiteralPath $Path)) { $skipped.Add("$Path(ファイルが無い)"); return }
    $have = Get-ProjectConfigs $Path
    if ($have -and $have -notcontains "$Cfg|$Plat") {
        # 構成が無いと MSBuild はエラーにせず、その構成の設定(include・マクロ)が抜けたコマンドラインを返すので読まない
        $skipped.Add("$Path($Cfg|$Plat が無い。あるのは $($have -join ', '))")
        return
    }
    $projs.Add([pscustomobject]@{ Path = $Path; Cfg = $Cfg; Plat = $Plat })
}

$ext = [IO.Path]::GetExtension($Project).ToLower()
$dir = Split-Path $Project
if ($ext -eq '.vcxproj') {
    Add-Project $Project $Configuration $solPlat
} elseif ($ext -eq '.slnx') {
    # <Project Path="..."> の中の <BuildType Solution="Release|*" Project="Debug" /> と
    # <Platform Solution="*|x86" Project="Win32" /> で、プロジェクトの構成・プラットフォームが変わる
    foreach ($p in ([xml](Get-Content -LiteralPath $Project -Raw)).GetElementsByTagName('Project')) {
        if ($p.Path -notlike '*.vcxproj') { continue }
        $cfg, $plat = $Configuration, $solPlat
        foreach ($c in $p.ChildNodes) {
            if ($c.LocalName -eq 'BuildType' -and (Test-SolutionPattern $c.Solution $Configuration $Platform)) { $cfg = $c.Project }
            if ($c.LocalName -eq 'Platform' -and (Test-SolutionPattern $c.Solution $Configuration $Platform)) { $plat = $c.Project }
        }
        Add-Project ([IO.Path]::GetFullPath((Join-Path $dir $p.Path))) $cfg $plat
    }
} elseif ($ext -eq '.sln') {
    $text = Get-Content -LiteralPath $Project -Raw
    foreach ($m in [regex]::Matches($text, 'Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"([^"]+\.vcxproj)",\s*"(\{[^}]+\})"')) {
        $path = [IO.Path]::GetFullPath((Join-Path $dir $m.Groups[1].Value))
        # ソリューションの構成から各プロジェクトの構成へ(例: Debug|x86 -> Debug|Win32)。対応が無ければ同じ名前で読む
        $re = [regex]::Escape($m.Groups[2].Value) + '\.' + [regex]::Escape("$Configuration|$Platform") + '\.ActiveCfg\s*=\s*([^|\r\n]+)\|([^\r\n]+)'
        $a = [regex]::Match($text, $re, 'IgnoreCase')
        if ($a.Success) { Add-Project $path $a.Groups[1].Value.Trim() $a.Groups[2].Value.Trim() }
        else { Add-Project $path $Configuration $solPlat }
    }
} else {
    throw "対応していない種類です: $Project"
}
foreach ($s in $skipped) { Write-Warning "読まないプロジェクト: $s" }
if ($projs.Count -eq 0) { throw "読める C++ のプロジェクト(.vcxproj)がありません: $Project" }

# 全プロジェクトの GetClCommandLines を 1 回の MSBuild で呼ぶ。プロジェクトごとに分けて呼ぶ(Condition の %(Identity) で
# バッチになる)ので、1 つが評価エラー(Import 先が無いなど)でも、ほかのプロジェクトの結果は残る
$proj = Join-Path $Out 'get-cl.proj'
$items = ($projs | ForEach-Object {
    $e = [Security.SecurityElement]::Escape($_.Path) -replace '%', '%25' -replace ';', '%3B' -replace '\$', '%24' -replace '@', '%40'
    "    <P Include=`"$e`" Properties=`"Configuration=$($_.Cfg);Platform=$($_.Plat)`" />"
}) -join "`n"
@"
<Project>
  <ItemGroup>
$items
  </ItemGroup>
  <Target Name="Cl" Returns="@(Cl)">
    <MSBuild Projects="@(P)" Condition="'%(P.Identity)' != ''" Targets="GetClCommandLines;GetIncludePath" ContinueOnError="true">
      <Output TaskParameter="TargetOutputs" ItemName="Cl" />
    </MSBuild>
  </Target>
</Project>
"@ | Set-Content -LiteralPath $proj -Encoding utf8
# GetIncludePath は各プロジェクトの IncludePath と ExternalIncludePath を返す(ForceImportAfterCppTargets で足す)
$props = Join-Path $Out 'get-include.targets'
@'
<Project>
  <Target Name="GetIncludePath" Returns="@(_CriInclude)">
    <ItemGroup>
      <_CriInclude Include="$(MSBuildProjectFullPath)" IncludePath="$(IncludePath)" ExternalIncludePath="$(ExternalIncludePath)" />
    </ItemGroup>
  </Target>
</Project>
'@ | Set-Content -LiteralPath $props -Encoding utf8
$msb = "$vs\MSBuild\Current\Bin\amd64\MSBuild.exe"
$json = & $msb $proj -nologo -m -getTargetResult:Cl "-p:ForceImportAfterCppTargets=$props"
$code = $LASTEXITCODE
try { $res = ($json -join "`n") | ConvertFrom-Json } catch { $res = $null }
$all = @(if ($res) { $res.TargetResults.Cl.Items })
if (-not $all) { $json | Select-Object -Last 20; throw "MSBuild から結果が取れませんでした(終了コード $code)" }

# VS と Windows SDK の既定のフォルダ(システムのヘッダー)
$sysDirs = @($vs, "${env:ProgramFiles(x86)}\Windows Kits", "${env:ProgramFiles(x86)}\Microsoft SDKs") |
    ForEach-Object { $_.TrimEnd('\').ToLower() + '\' }
function Test-System([string]$d) { $l = $d.ToLower(); [bool]($sysDirs | Where-Object { $l.StartsWith($_) }) }
$inc = @{}
foreach ($i in $all | Where-Object { $_.PSObject.Properties.Name -contains 'ExternalIncludePath' }) {
    $split = { param($v) @($v -split ';' | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') } | Select-Object -Unique) }
    $external = & $split $i.ExternalIncludePath
    $include = & $split $i.IncludePath
    $flags = foreach ($d in $include) {
        if ($external -contains $d -or (Test-System $d)) { " `"/imsvc$d`"" } else { " `"/I$d`"" }
    }
    $flags = @($flags) + @(foreach ($d in $external) { if ($include -notcontains $d) { " `"/imsvc$d`"" } })
    $inc["$($i.MSBuildSourceProjectFile)".ToLower()] = $flags -join ''
}
$entries = [Collections.Generic.List[object]]::new()
$done = @{}
foreach ($i in $all | Where-Object { $_.PSObject.Properties.Name -contains 'Files' }) {
    $key = "$($i.MSBuildSourceProjectFile)".ToLower()
    $done[$key] = $true
    foreach ($f in $i.Files -split ';' | Where-Object { $_ }) {
        $entries.Add([ordered]@{ directory = $i.WorkingDirectory; file = $f; command = "cl.exe $($i.Identity)$($inc[$key]) `"$f`"" })
    }
}
$failed = @($projs | Where-Object { -not $done.ContainsKey($_.Path.ToLower()) } | ForEach-Object { $_.Path })
if ($entries.Count -eq 0) { $json | Select-Object -Last 20; throw 'cl のコマンドラインが 1 つも取れませんでした' }
ConvertTo-Json -InputObject $entries.ToArray() -Depth 3 | Set-Content -LiteralPath $cdb -Encoding utf8
if ($failed) { Write-Warning "コマンドラインが取れなかったプロジェクト($($failed.Count) 件。評価のエラーか、C++ のソースが無い): $($failed -join ', ')" }
"compile_commands.json: $cdb($($entries.Count) TU)"
