"""make-cdb.ps1 と index-project.ps1 のテスト。一時フォルダに Visual Studio と CMake の小さなプロジェクトを作って通す。
Windows と、C++ のツールを入れた Visual Studio が要る(無ければ飛ばす)。index-project は ltidx.exe も要る。

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, '..')
LTIDX = os.path.join(HERE, '..', '..', 'indexer', 'build', 'ltidx.exe')
VSWHERE = os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
PWSH = shutil.which('pwsh')

VCXPROJ = '''<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Debug|x64"><Configuration>Debug</Configuration><Platform>x64</Platform></ProjectConfiguration>
    <ProjectConfiguration Include="Release|x64"><Configuration>Release</Configuration><Platform>x64</Platform></ProjectConfiguration>
  </ItemGroup>
  <PropertyGroup Label="Globals"><ProjectGuid>{6A0D3E1C-2B4F-4C8A-9E5D-1F2A3B4C5D6E}</ProjectGuid><Keyword>Win32Proj</Keyword></PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props" />
  <PropertyGroup><ConfigurationType>Application</ConfigurationType><CharacterSet>Unicode</CharacterSet></PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />
  <PropertyGroup><IncludePath>$(ProjectDir)vcdir;$(IncludePath)</IncludePath></PropertyGroup>
  <ItemDefinitionGroup>
    <ClCompile>
      <AdditionalIncludeDirectories>inc;%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>
      <PreprocessorDefinitions Condition="'$(Configuration)'=='Debug'">ONLY_DEBUG;%(PreprocessorDefinitions)</PreprocessorDefinitions>
      <LanguageStandard>stdcpp20</LanguageStandard>
    </ClCompile>
  </ItemDefinitionGroup>
  <ItemGroup><ClCompile Include="a.cpp" /><ClCompile Include="b.cpp" /></ItemGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />
</Project>
'''
A_CPP = '#include "h.h"\n#include "v.h"\nint main() {\n#ifdef ONLY_DEBUG\n  Debug();\n#endif\n  return F() + V();\n}\n'


def has_vs():
    if os.name != 'nt' or not PWSH or not os.path.exists(VSWHERE):
        return False
    r = subprocess.run([VSWHERE, '-latest', '-products', '*', '-requires',
                        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


@unittest.skipUnless(has_vs(), 'Windows, pwsh and Visual Studio with the C++ tools are required')
class MakeCdbTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')  # MSBuild の中間ファイルが 260 文字を超えないよう短く
        self.write('inc/h.h', 'inline int F() { return 1; }\n')
        self.write('vcdir/v.h', 'inline int V() { return 2; }\n')  # 「VC++ ディレクトリ」(IncludePath)だけにある
        self.write('a.cpp', 'void Debug();\n' + A_CPP)
        self.write('b.cpp', '#include "h.h"\nint G() { return F(); }\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, text):
        fp = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def ps(self, script, *args):
        r = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', os.path.join(SCRIPTS, script)]
                           + list(args), capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def cdb(self, out):
        with open(os.path.join(out, 'compile_commands.json'), encoding='utf-8-sig') as f:
            return {os.path.basename(e['file']): e for e in json.load(f)}

    def check_vs(self, project, cfg='Debug'):
        out = os.path.join(self.root, 'out-' + cfg)
        self.ps('make-cdb.ps1', '-Root', self.root, '-Project', os.path.join(self.root, project),
                '-Configuration', cfg, '-Out', out)
        c = self.cdb(out)
        self.assertEqual(set(c), {'a.cpp', 'b.cpp'})
        cmd = c['a.cpp']['command']
        self.assertIn('/Iinc', cmd)
        self.assertIn('/std:c++20', cmd)
        self.assertIn('/D UNICODE', cmd)
        # 「VC++ ディレクトリ」に足したフォルダは /I(/imsvc にするとシステムのヘッダー扱いで索引に載らない)。VS・SDK の既定は /imsvc
        self.assertRegex(cmd, r'"/I[^"]*vcdir"')
        self.assertNotRegex(cmd, r'/imsvc[^"]*vcdir')
        self.assertIn('/imsvc', cmd)
        self.assertEqual('/D ONLY_DEBUG' in cmd, cfg == 'Debug')
        return out

    def test_vcxproj(self):
        self.write('p.vcxproj', VCXPROJ)
        self.check_vs('p.vcxproj')
        self.check_vs('p.vcxproj', 'Release')

    def test_slnx(self):
        self.write('p.vcxproj', VCXPROJ)
        self.write('p.slnx', '<Solution>\n  <Project Path="p.vcxproj" />\n</Solution>\n')
        self.check_vs('p.slnx')

    def test_solution_in_subfolder(self):
        # ルートの直下に無ければ、下のフォルダの C++ のソリューションを使う(C# だけのもの・. で始まるフォルダのものは見ない)
        for rel in ('a.cpp', 'b.cpp', 'inc/h.h', 'vcdir/v.h'):
            with open(os.path.join(self.root, rel), encoding='utf-8') as f:
                self.write('runtime/' + rel, f.read())
        self.write('runtime/p.vcxproj', VCXPROJ)
        self.write('runtime/runtime.slnx', '<Solution>\n  <Project Path="p.vcxproj" />\n</Solution>\n')
        self.write('Editor/Studio.slnx', '<Solution>\n  <Project Path="S/S.csproj" />\n</Solution>\n')
        self.write('runtime/.vs/copy.slnx', '<Solution>\n  <Project Path="../p.vcxproj" />\n</Solution>\n')
        out = os.path.join(self.root, 'out')
        log = self.ps('make-cdb.ps1', '-Root', self.root, '-Out', out)
        self.assertEqual(set(self.cdb(out)), {'a.cpp', 'b.cpp'}, log)

        # 2 つあれば、どちらかを -Project で選ぶよう言って止まる
        self.write('tools/t/q.sln', 'Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "q", "q.vcxproj", "{1}"\n')
        r = subprocess.run([PWSH, '-NoProfile', '-File', os.path.join(SCRIPTS, 'make-cdb.ps1'), '-Root', self.root, '-Out', out],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('-Project', r.stdout + r.stderr)
        self.assertIn('runtime.slnx', r.stdout + r.stderr)
        self.assertIn('q.sln', r.stdout + r.stderr)

    def test_sln_maps_configuration(self):
        # ソリューションの Debug|x64 を、プロジェクトの Release|x64 に対応づけた .sln
        self.write('p.vcxproj', VCXPROJ)
        g = '{6A0D3E1C-2B4F-4C8A-9E5D-1F2A3B4C5D6E}'
        self.write('p.sln', 'Microsoft Visual Studio Solution File, Format Version 12.00\n'
                   f'Project("{{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}}") = "p", "p.vcxproj", "{g}"\nEndProject\n'
                   'Global\n\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
                   f'\t\t{g}.Debug|x64.ActiveCfg = Release|x64\n\tEndGlobalSection\nEndGlobal\n')
        out = os.path.join(self.root, 'out-sln')
        self.ps('make-cdb.ps1', '-Root', self.root, '-Out', out)  # -Project を省くとルートの .sln を探す
        self.assertNotIn('ONLY_DEBUG', self.cdb(out)['a.cpp']['command'])

    def test_broken_project_does_not_hide_others(self):
        # 1 つのプロジェクトが評価エラー(Import 先が無い)でも、ほかのプロジェクトの分は作る
        self.write('p.vcxproj', VCXPROJ)
        self.write('broken.vcxproj', VCXPROJ.replace('<Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />',
                                                    '<Import Project="missing\\nuget.targets" />'))
        self.write('p.slnx', '<Solution>\n  <Project Path="p.vcxproj" />\n  <Project Path="broken.vcxproj" />\n</Solution>\n')
        out = os.path.join(self.root, 'out-broken')
        log = self.ps('make-cdb.ps1', '-Root', self.root, '-Out', out)
        self.assertEqual(set(self.cdb(out)), {'a.cpp', 'b.cpp'}, log)

    def test_platform_the_project_lacks(self):
        # Win32 しか持たないプロジェクトを x64 で読まない(MSBuild は、構成の設定が抜けたコマンドラインを黙って返す)
        win32 = VCXPROJ.replace('x64', 'Win32').replace('<LanguageStandard>', '<PreprocessorDefinitions>ONLYWIN32;%(PreprocessorDefinitions)</PreprocessorDefinitions><LanguageStandard>')
        self.write('w.vcxproj', win32)
        self.write('p.vcxproj', VCXPROJ)
        self.write('p.slnx', '<Solution>\n  <Project Path="p.vcxproj" />\n  <Project Path="w.vcxproj" />\n</Solution>\n')
        out = os.path.join(self.root, 'out-plat')
        r = subprocess.run([PWSH, '-NoProfile', '-File', os.path.join(SCRIPTS, 'make-cdb.ps1'), '-Root', self.root,
                            '-Out', out], capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('w.vcxproj', r.stdout + r.stderr)  # 読まなかったことを警告する(pwsh は警告を標準出力に出す)
        self.assertNotIn('ONLYWIN32', json.dumps(self.cdb(out)))
        # .slnx の <Platform> の対応があれば、そのプラットフォームで読む
        self.write('p.slnx', '<Solution>\n  <Project Path="w.vcxproj">\n    <Platform Solution="*|x64" Project="Win32" />\n'
                   '  </Project>\n</Solution>\n')
        self.ps('make-cdb.ps1', '-Root', self.root, '-Out', out)
        self.assertIn('ONLYWIN32', self.cdb(out)['a.cpp']['command'])

    def test_cmake(self):
        self.write('CMakeLists.txt', 'cmake_minimum_required(VERSION 3.20)\nproject(p CXX)\n'
                   'add_executable(p a.cpp b.cpp)\ntarget_include_directories(p PRIVATE inc vcdir)\n'
                   'target_compile_definitions(p PRIVATE $<$<CONFIG:Debug>:ONLY_DEBUG> A=${A} B=${B})\n')
        out = os.path.join(self.root, 'out-cmake')
        self.ps('make-cdb.ps1', '-Root', self.root, '-Out', out, '-CMakeArgs', '-DA=1 -DB=2')
        c = self.cdb(out)
        self.assertEqual(set(c), {'a.cpp', 'b.cpp'})
        cmd = c['a.cpp']['command']
        self.assertIn('ONLY_DEBUG', cmd)
        self.assertIn('-DA=1', cmd)
        self.assertIn('-DB=2', cmd)  # 2 つ目の引数も CMake に届く

    @unittest.skipUnless(os.path.exists(LTIDX), 'ltidx.exe is not built')
    def test_index_project_makes_cdb(self):
        # compile_commands.json が無いルートでも、index-project だけで索引ができる
        self.write('p.vcxproj', VCXPROJ)
        log = self.ps('index-project.ps1', '-Root', self.root, '-Jobs', '2')
        d = os.path.join(self.root, '.index')
        with open(os.path.join(d, '.gitignore'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines()[-1], '*')  # 索引を git に入れない
        with open(os.path.join(d, 'index.tsv.tu.tsv'), encoding='utf-8') as f:
            tus = f.read()
        with open(os.path.join(d, 'index.tsv'), encoding='utf-8') as f:
            text = f.read()
        with open(os.path.join(d, 'cdb-Debug-x64', 'compile_commands.json'), encoding='utf-8-sig') as f:
            cdb = f.read()
        info = f'\nroot={self.root}\n{log}\n{tus}\n{text[:2000]}\n{cdb[:3000]}'
        self.assertEqual([line.split('\t')[1] for line in tus.splitlines()[1:]], ['1', '1'], info)
        rows = [line.split('\t') for line in text.splitlines()]
        self.assertTrue(any(r[0] == 'V' and r[1] == 'call' for r in rows), info)  # IncludePath のヘッダーも解決できた
        self.assertTrue(any(r[0] == 'Debug' and r[1] == 'call' for r in rows), info)
        # 「VC++ ディレクトリ」のヘッダーの中の定義も索引にある
        self.assertTrue(any(r[0] == 'V' and r[1] == 'def' and r[3].startswith('vcdir/v.h:') for r in rows), info)

        # 作り方の記録から、MCP サーバーの自動更新が作り直せる。新しい TU を足すと compile_commands.json から作り直す
        with open(os.path.join(d, 'index.tsv.recipe.json'), encoding='utf-8') as f:
            recipe = json.load(f)
        self.assertEqual(recipe['makeCdb']['Out'], os.path.join(d, 'cdb-Debug-x64'))
        self.write('c.cpp', '#include "h.h"\nint H() { return F(); }\n')
        self.write('p.vcxproj', VCXPROJ.replace('<ClCompile Include="b.cpp" />',
                                                '<ClCompile Include="b.cpp" /><ClCompile Include="c.cpp" />'))
        r = subprocess.run([sys.executable, '-I', os.path.join(SCRIPTS, '..', 'mcp', 'autoupdate.py'), '--dir', d, '--full'],
                           capture_output=True, text=True)
        with open(os.path.join(d, 'update.log'), encoding='utf-8', errors='replace') as f:
            ulog = f.read()
        self.assertEqual(r.returncode, 0, ulog)
        with open(os.path.join(d, 'index.tsv.tu.tsv'), encoding='utf-8') as f:
            self.assertIn('c.cpp', f.read(), ulog)


if __name__ == '__main__':
    unittest.main()
