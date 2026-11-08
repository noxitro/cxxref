# Copilot への使い方の指示(cxxref-instructions.md)を、プロジェクトに足す・消す。足す先は 2 つから選ぶ。
# プラグインは指示を持たない(以前の rule は毎回読まれた)。使うリポジトリだけで、このスクリプトか /cxxref-enable で入れる。
#   Project: <ルート>\.github\copilot-instructions.md。目印のコメント(cxxref:begin / cxxref:end)で囲み、消すときはその間だけを消す。
#            コミットすればチームの全員に効く。目印の外は書き換えない。改行(CRLF/LF)と BOM の有無は、元のファイルに合わせる。
#   Local:   <ルート>\.github\instructions\cxxref.instructions.md を置き、git の .git\info\exclude に足して git から外す。
#            自分の手元の、このリポジトリだけに効く(Copilot CLI には *.local.md のような手元だけの指示ファイルが無いので、この形にした)。
# 引数を省くと、ソースのルートと、どうするかを尋ねる(ダブルクリック用)。
# 拡張の /cxxref-enable(com.github.copilot\extensions\cxxref\extension.mjs)もこのスクリプトを呼ぶ。
[CmdletBinding()]  # 知らない引数をエラーにする(黙って捨てない)
param(
    [string]$Root,                          # ソースのルート(.github のあるフォルダ)
    [ValidateSet('', 'Add', 'Remove', 'Status')]
    [string]$Action = '',                   # Add: 足す(入っていれば今の中身に差し替える)。Remove: 消す。Status: 今の状態を出す
    [ValidateSet('', 'Project', 'Local')]
    [string]$Scope = ''                     # Add では省くと Project。Remove では省くと両方
)
$ErrorActionPreference = 'Stop'
# 出力を受け取る側(拡張の /cxxref-enable)が読めるように、パイプに出すときは UTF-8 にする(画面に出すときは変えない)
if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) }
$Begin = '<!-- cxxref:begin (cxxref が管理。消すときは /cxxref-enable off か setup-instructions.bat で) -->'
$End = '<!-- cxxref:end -->'
$Block = '(?s)(\r?\n)?<!-- cxxref:begin[^\n]*?-->.*?<!-- cxxref:end -->[ \t]*(\r?\n)?'
$LocalMark = '<!-- cxxref:local (cxxref が管理。git から外してある。消すときは /cxxref-enable off か setup-instructions.bat で) -->'
$LocalRel = '.github/instructions/cxxref.instructions.md'
$ExcludeNote = '# cxxref: 手元だけの使い方の指示(setup-instructions.ps1 が足した。消すときも同じスクリプトで)'

if (-not $Root) { $Root = Read-Host 'ソースのルート' }
$Root = (Resolve-Path $Root.Trim('"')).Path
$file = Join-Path $Root '.github\copilot-instructions.md'
$localFile = Join-Path $Root ($LocalRel -replace '/', '\')

function Read-Text([string]$path) {
    # 中身と BOM の有無。無ければ空
    if (-not (Test-Path -LiteralPath $path)) { return @{ Text = ''; Bom = $false } }
    $bytes = [IO.File]::ReadAllBytes($path)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    @{ Text = [Text.UTF8Encoding]::new($false).GetString($bytes, [int]$bom * 3, $bytes.Length - [int]$bom * 3); Bom = $bom }
}

function Save([string]$path, [string]$s, [bool]$bom) {
    New-Item -ItemType Directory (Split-Path $path) -Force | Out-Null
    [IO.File]::WriteAllText($path, $s, [Text.UTF8Encoding]::new($bom))
}

function Body([string]$nl) {
    $src = Join-Path $PSScriptRoot 'cxxref-instructions.md'
    ([IO.File]::ReadAllText($src)).Trim() -replace '\r?\n', $nl
}

function Invoke-Git([string[]]$a) {
    # 失敗したら $null(git が無い・リポジトリでない)
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
    $out = & git -C $Root @a 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    "$out".Trim()
}

$cur = Read-Text $file
$text = $cur.Text
$bom = $cur.Bom
$hasProject = $text -match $Block
$hasLocal = Test-Path -LiteralPath $localFile
if ($hasLocal -and -not ((Read-Text $localFile).Text.Contains('<!-- cxxref:local'))) {
    throw "cxxref が作ったものではない $localFile があります。書き換えも消しもしないので、名前を変えるか消してから実行してください"
}

function Show-State {
    "プロジェクト($file): $(if ($hasProject) { '入っています' } else { '入っていません' })"
    "ローカル($localFile): $(if ($hasLocal) { '入っています' } else { '入っていません' })"
}

# git の除外(.git\info\exclude)。ワークツリーでも元のリポジトリの info\exclude を指す(--git-path)
function Exclude-Info {
    $p = Invoke-Git @('rev-parse', '--git-path', 'info/exclude')
    if (-not $p) { return $null }
    if (-not [IO.Path]::IsPathRooted($p)) { $p = Join-Path $Root $p }
    $prefix = Invoke-Git @('rev-parse', '--show-prefix')  # ルートが git のルートの下のフォルダのとき、そこまでの相対パス(末尾に /)
    @{ Path = [IO.Path]::GetFullPath($p); Line = "/$prefix$LocalRel" }
}

function Exclude-Pattern($ex) {
    '(?m)^' + [regex]::Escape($ExcludeNote) + '\r?\n' + [regex]::Escape($ex.Line) + '[ \t]*(\r?\n|$)'
}

function Add-Local {
    $ex = Exclude-Info
    if ($ex) {
        if ($null -ne (Invoke-Git @('ls-files', '--error-unmatch', '--', $LocalRel))) {
            throw "$LocalRel はリポジトリにコミットされているので、git から外せません(ローカルにできません)"
        }
    }
    $nl = "`n"
    Save $localFile ("---$nl" + 'applyTo: "**"' + "$nl---$nl$LocalMark$nl$(Body $nl)$nl") $false
    if (-not $ex) {
        "ローカルに足しました: $localFile(git のリポジトリではないので、除外はしていません)"
        return
    }
    $e = Read-Text $ex.Path
    if ($e.Text -notmatch (Exclude-Pattern $ex)) {
        $t = $e.Text
        $enl = if ($t -match '\r\n') { "`r`n" } else { "`n" }
        if ($t.Length -gt 0 -and -not $t.EndsWith("`n")) { $t += $enl }
        Save $ex.Path ($t + $ExcludeNote + $enl + $ex.Line + $enl) $e.Bom
    }
    "ローカルに足しました: $localFile(git から外しました: $($ex.Path) に $($ex.Line))"
    if ($null -eq (Invoke-Git @('check-ignore', '-q', '--', $LocalRel))) {
        # .gitignore の ! などで外れなかった
        "注意: git status に出ます。.gitignore で打ち消されているかもしれません($LocalRel)"
    }
}

function Remove-Local {
    if (Test-Path -LiteralPath $localFile) {
        Remove-Item -LiteralPath $localFile
        # 足したときに作ったフォルダが空になったら消す(中身のあるフォルダは残す)
        foreach ($d in @((Split-Path $localFile), (Join-Path $Root '.github'))) {
            if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force)) { Remove-Item -LiteralPath $d }
        }
    }
    $ex = Exclude-Info
    if ($ex -and (Test-Path -LiteralPath $ex.Path)) {
        $e = Read-Text $ex.Path
        $t = [regex]::Replace($e.Text, (Exclude-Pattern $ex), '')
        if ($t -ne $e.Text) { Save $ex.Path $t $e.Bom }
    }
    "ローカルから消しました: $localFile"
}

function Add-Project {
    $nl = if ($text -match '\r\n') { "`r`n" } else { "`n" }
    $new = "$Begin$nl$(Body $nl)$nl$End$nl"
    if ($hasProject) {
        # 前後の改行は残し、目印の間だけを差し替える
        Save $file ([regex]::Replace($text, $Block, { param($m) $m.Groups[1].Value + $new }, 'None')) $bom
        "プロジェクトの指示を差し替えました: $file"
    } else {
        $t = $text
        if ($t.Length -gt 0 -and -not $t.EndsWith("`n")) { $t += $nl }
        if ($t.Length -gt 0) { $t += $nl }  # 前の中身との間に空行を置く
        Save $file ($t + $new) $bom
        "プロジェクトの指示に足しました: $file(コミットするとチームの全員に効きます)"
    }
}

function Remove-Project {
    $t = [regex]::Replace($text, $Block, '')
    if ($t.Trim().Length -eq 0) {
        # 足したときにファイルを作ったので、空になったら消して元に戻す
        Remove-Item -LiteralPath $file
        $d = Split-Path $file
        if (-not (Get-ChildItem -LiteralPath $d -Force)) { Remove-Item -LiteralPath $d }  # .github も空なら消す
        "プロジェクトの指示から消しました(ほかに中身が無いので、ファイルごと消しました): $file"
    } else {
        Save $file $t $bom
        "プロジェクトの指示から消しました: $file"
    }
}

if (-not $Action) {
    Show-State
    $c = Read-Host '1: ローカルに足す(自分だけ)  2: プロジェクトに足す(コミットすればチーム全員)  3: 消す  (番号を入力)'
    switch ($c.Trim()) {
        '1' { $Action = 'Add'; $Scope = 'Local' }
        '2' { $Action = 'Add'; $Scope = 'Project' }
        '3' { $Action = 'Remove' }
        default { throw "1・2・3 のどれかを入力してください: $c" }
    }
}

switch ($Action) {
    'Status' { Show-State }
    'Add' {
        if (-not $Scope) { $Scope = 'Project' }  # 引数で -Action Add だけを渡していた使い方(setup.ps1 など)はプロジェクト
        if ($Scope -eq 'Local') {
            if ($hasProject) {
                # 同じ指示が二重になるので足さない
                "プロジェクトの指示に入っているので、ローカルには足しません(二重になります): $file"
                'ローカルに移すときは、先にプロジェクトから消してください(/cxxref-enable off project か -Action Remove -Scope Project)'
            } else {
                Add-Local
            }
        } else {
            Add-Project
            if ($hasLocal) { Remove-Local }  # 二重にならないよう、手元だけの分は消す
        }
    }
    'Remove' {
        $did = $false
        if ($Scope -ne 'Local' -and $hasProject) { Remove-Project; $did = $true }
        if ($Scope -ne 'Project' -and $hasLocal) { Remove-Local; $did = $true }
        if (-not $did) { "入っていないので、何もしません$(if ($Scope) { "($Scope)" })" }
    }
}
