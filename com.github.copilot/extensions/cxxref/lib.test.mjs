// node --test com.github.copilot/extensions/cxxref/lib.test.mjs(mcp/test/test_plugin.py が流す)。/cxxref-index・/cxxref-enable の SDK に依らない部分
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { enableRoot, escapeMarkdown, findLtidx, gitTop, indexRoot, lastCwd, parseEnableArgs, parseIndexArgs, parseProgress, pluginRoot, progressNote, projectChoices, savedProject, statusHint, targetRoot } from "./lib.mjs";

test("projectChoices: フルパスの行を、ルートからの相対パスの選択肢にする", () => {
  const root = join(tmpdir(), "cxxref-lib-test");
  const out = `${join(root, "runtime", "runtime.slnx")}\r\n${join(root, "tools", "t", "q.sln")}\r\n\r\n`;
  assert.deepEqual(projectChoices(root, out), [
    { label: join("runtime", "runtime.slnx"), path: join(root, "runtime", "runtime.slnx") },
    { label: join("tools", "t", "q.sln"), path: join(root, "tools", "t", "q.sln") },
  ]);
  assert.deepEqual(projectChoices(root, ""), []);
});

test("savedProject: 前の作り方に残した -Project(ファイルがあるときだけ)", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    assert.equal(savedProject(d), "");
    mkdirSync(join(d, ".index"));
    const sln = join(d, "a.slnx");
    const recipe = (p) => writeFileSync(join(d, ".index", "index.tsv.recipe.json"), "﻿" + JSON.stringify({ makeCdb: { Root: d, Project: p } }));
    recipe(sln);
    assert.equal(savedProject(d), "");  // ファイルが無い
    writeFileSync(sln, "<Solution />");
    assert.equal(savedProject(d), sln);
    writeFileSync(join(d, ".index", "index.tsv.recipe.json"), JSON.stringify({ makeCdb: null }));
    assert.equal(savedProject(d), "");  // 手元の compile_commands.json を使った
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("escapeMarkdown: 記号の前の \\ だけを二重にする", () => {
  assert.equal(escapeMarkdown("E:\\src\\Proj\\.index\\a.log"), "E:\\src\\Proj\\\\.index\\a.log");
  assert.equal(escapeMarkdown("scripts\\get-ltidx.ps1(C:\\_x)"), "scripts\\get-ltidx.ps1(C:\\\\_x)");
});

function tree() {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  mkdirSync(join(d, "plugin", "scripts"), { recursive: true });
  writeFileSync(join(d, "plugin", "plugin.json"), "{}");
  writeFileSync(join(d, "plugin", "scripts", "index-project.ps1"), "");
  mkdirSync(join(d, "plugin", "com.github.copilot", "extensions", "cxxref"), { recursive: true });
  mkdirSync(join(d, "repo", ".index"), { recursive: true });
  mkdirSync(join(d, "repo", "src", "sub"), { recursive: true });
  return d;
}

test("pluginRoot は拡張のフォルダから plugin.json のあるフォルダまでさかのぼる", () => {
  const d = tree();
  try {
    assert.equal(pluginRoot(join(d, "plugin", "com.github.copilot", "extensions", "cxxref")), join(d, "plugin"));
    assert.throws(() => pluginRoot(join(d, "repo", "src")));
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("findLtidx: LTIDX が無いファイルなら取ってこない(error)。LOCALAPPDATA が無ければ相対パスを探さない", () => {
  const d = tree();
  try {
    const plugin = join(d, "plugin");
    assert.match(findLtidx(plugin, { LTIDX: join(d, "none.exe") }).error, /LTIDX/);
    assert.deepEqual(findLtidx(plugin, {}), {});
    const got = join(d, "app", "cxxref", "ltidx");
    mkdirSync(got, { recursive: true });
    writeFileSync(join(got, "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, { LOCALAPPDATA: join(d, "app") }).path, join(got, "ltidx.exe"));
    mkdirSync(join(plugin, "indexer", "build"), { recursive: true });
    writeFileSync(join(plugin, "indexer", "build", "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, { LOCALAPPDATA: join(d, "app") }).path, join(plugin, "indexer", "build", "ltidx.exe"));
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("findLtidx: 取ってきた ltidx の版がプラグインと違えば stale(ビルドしたもの・LTIDX のものは見ない)", () => {
  const d = tree();
  try {
    const plugin = join(d, "plugin");
    writeFileSync(join(plugin, "plugin.json"), '{"name": "cxxref", "version": "0.6.0"}');
    const got = join(d, "app", "cxxref", "ltidx");
    mkdirSync(got, { recursive: true });
    writeFileSync(join(got, "ltidx.exe"), "");
    const env = { LOCALAPPDATA: join(d, "app") };
    assert.deepEqual(findLtidx(plugin, env).stale, { have: "不明", want: "0.6.0" });  // version.txt の無い古い配布物
    writeFileSync(join(got, "version.txt"), "0.5.1\n");
    assert.deepEqual(findLtidx(plugin, env).stale, { have: "0.5.1", want: "0.6.0" });
    writeFileSync(join(got, "version.txt"), "0.6.0\n");
    assert.deepEqual(findLtidx(plugin, env), { path: join(got, "ltidx.exe") });
    writeFileSync(join(got, "version.txt"), "0.5.1\n");
    assert.deepEqual(findLtidx(plugin, { ...env, LTIDX: join(got, "ltidx.exe") }), { path: join(got, "ltidx.exe") });
    mkdirSync(join(plugin, "indexer", "build"), { recursive: true });
    writeFileSync(join(plugin, "indexer", "build", "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, env).stale, undefined);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseIndexArgs: 先頭の full で最初から作り直す。残りはルート", () => {
  assert.deepEqual(parseIndexArgs(""), { full: false, root: "" });
  assert.deepEqual(parseIndexArgs("full"), { full: true, root: "" });
  assert.deepEqual(parseIndexArgs(" FULL  E:\\src\\proj "), { full: true, root: "E:\\src\\proj" });
  assert.deepEqual(parseIndexArgs("fullpath"), { full: false, root: "fullpath" });
  assert.deepEqual(parseIndexArgs("./full"), { full: false, root: "./full" });
  assert.deepEqual(parseIndexArgs('"E:\\a b"'), { full: false, root: '"E:\\a b"' });
});

test("lastCwd は最後の作業フォルダを返す", () => {
  const ev = [{ type: "session.start", data: { context: { cwd: "C:\\a" } } }, { type: "session.context_changed", data: { cwd: "C:\\b" } }];
  assert.equal(lastCwd(ev), "C:\\b");
  assert.equal(lastCwd([]), "");
});

test("targetRoot: 引数(引用符・相対)か、親へさかのぼって .index のあるフォルダ", () => {
  const d = tree();
  try {
    const repo = join(d, "repo");
    assert.equal(indexRoot(join(repo, "src", "sub")), repo);
    assert.equal(targetRoot("", join(repo, "src", "sub")).root, repo);
    assert.equal(targetRoot(`"${join(repo, "src")}"`, d).root, join(repo, "src"));
    assert.equal(targetRoot("src", repo).root, join(repo, "src"));
    assert.match(targetRoot("none", repo).error, /フォルダがありません/);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseEnableArgs: on は省くとローカル、off は省くと両方、引数なしは状態", () => {
  assert.deepEqual(parseEnableArgs(""), { action: "Status", scope: "", root: "" });
  assert.deepEqual(parseEnableArgs("on"), { action: "Add", scope: "Local", root: "" });
  assert.deepEqual(parseEnableArgs("ON Project"), { action: "Add", scope: "Project", root: "" });
  assert.deepEqual(parseEnableArgs("off"), { action: "Remove", scope: "", root: "" });
  assert.deepEqual(parseEnableArgs("off local D:\\src\\game"), { action: "Remove", scope: "Local", root: "D:\\src\\game" });
  assert.deepEqual(parseEnableArgs('on "D:\\my src"'), { action: "Add", scope: "Local", root: '"D:\\my src"' });
  assert.deepEqual(parseEnableArgs("status project"), { action: "Status", scope: "", root: "" });
  assert.match(parseEnableArgs("enable").error, /使い方/);
});

test("statusHint: 入っていなければ on を、入っていれば off を案内する", () => {
  const state = (p, l) => `プロジェクト(E:\\src\\.github\\copilot-instructions.md): ${p}\r\nローカル(E:\\src\\.github\\instructions\\cxxref.instructions.md): ${l}`;
  assert.match(statusHint(state("入っていません", "入っていません")), /on project/);
  assert.match(statusHint(state("入っています", "入っていません")), /off/);
  assert.match(statusHint(state("入っていません", "入っています")), /off/);
  assert.equal(statusHint("何か別の出力"), "");
});

test("enableRoot: 引数が無ければ git のルート、リポジトリでなければ作業フォルダ", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    const real = (p) => realpathSync.native(p).toLowerCase();
    mkdirSync(join(d, "plain"));
    assert.equal(real(enableRoot("", join(d, "plain")).root), real(join(d, "plain")));
    let git = true;
    try { execFileSync("git", ["init", "-q", join(d, "repo")], { stdio: "ignore" }); } catch { git = false; }
    if (git) {
      mkdirSync(join(d, "repo", "src"));
      assert.equal(real(gitTop(join(d, "repo", "src"))), real(join(d, "repo")));
      assert.equal(real(enableRoot("", join(d, "repo", "src")).root), real(join(d, "repo")));
      assert.equal(enableRoot("src", join(d, "repo")).root, join(d, "repo", "src"));  // 引数があればそこ
    }
    assert.equal(gitTop(join(d, "none")), "");
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseProgress: いちばん進んだ TU と、解析エラーの数(前後した行・ほかの行は無視)", () => {
  const log = "[3/322] ok a.cpp\r\nwarning: something\r\n[9/322] NG b.cpp\r\n[7/322] ok c.cpp\r\n[ 10/322] ok d.cpp\r\n";
  assert.deepEqual(parseProgress(log), { done: 9, total: 322, ng: 1 });
  assert.equal(parseProgress("compile_commands.json: x\r\n"), null);
  assert.equal(parseProgress(""), null);
});

test("progressNote: 段階が変わったとき・1 割ごと(10 秒あけて)・compile_commands.json は 1 分ごとに出す", () => {
  let s = {};
  const step = (snap, now, sec) => { const r = progressNote(s, snap, now, sec); s = r.state; return r.note; };
  assert.match(step({ phase: "cdb" }, 0, 0), /compile_commands\.json を用意しています$/);
  assert.equal(step({ phase: "cdb" }, 30000, 30), "");
  assert.match(step({ phase: "cdb" }, 61000, 61), /\(61 秒\)/);
  assert.match(step({ phase: "tu", done: 5, total: 100, ng: 0 }, 62000, 62), /5\/100\(5%、62 秒\)/);
  assert.equal(step({ phase: "tu", done: 9, total: 100, ng: 0 }, 80000, 80), "");      // 1 割に届かない
  assert.equal(step({ phase: "tu", done: 30, total: 100, ng: 2 }, 65000, 65), "");     // 10 秒たっていない
  assert.match(step({ phase: "tu", done: 30, total: 100, ng: 2 }, 73000, 73), /30\/100\(30%、解析エラー 2、73 秒\)/);
  assert.equal(step({ phase: "tu", done: 100, total: 100, ng: 2 }, 99000, 99), "");    // 終わりは別に出す
});
