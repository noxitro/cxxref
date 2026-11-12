// node --test com.github.copilot/extensions/cxxref/lib.test.mjs(mcp/test/test_plugin.py が流す)。/cxxref-index・/cxxref-enable の SDK に依らない部分
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { enableRoot, escapeMarkdown, findLtidx, gitTop, indexRoot, lastCwd, makeNotifier, parseEnableArgs, parseIndexArgs, parseProgress, pathKey, pluginRoot, progressNote, projectChoices, savedProject, runsUnderCli, statusHint, targetRoot, wantDialog } from "./lib.mjs";

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

test("runsUnderCli: 拡張に渡された SDK が CLI の配布物の中なら CLI、そうでなければアプリ", () => {
  const no = () => false;  // SDK の親を見る判定は使わない
  const dist = String.raw`C:\tools\copilot\pkg\win32-x64\1.0.0`;
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk`, COPILOT_CLI_DIST_DIR: dist }, no), true);
  // \\?\ 付き・/ 区切り・大文字小文字・末尾の \ の違いは同じ場所とみなす
  const slashed = dist.toUpperCase().replaceAll("\\", "/");
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: "\\\\?\\" + slashed + "/copilot-sdk", COPILOT_CLI_RESOLVED_DIST_DIR: `${dist}\\` }, no), true);
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`C:\Program Files\GitHub Copilot\sdk`, COPILOT_CLI_RESOLVED_DIST_DIR: dist, COPILOT_CLI_PATH: "x" }, no), false);
  // 名前の頭が同じだけの別のフォルダは中とみなさない
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}-other\sdk`, COPILOT_CLI_DIST_DIR: dist }, no), false);
  // 指定された場所(DIST_DIR)が読めずに別の場所を読み込んだ: 実際に読み込んだ場所(RESOLVED)の下なら CLI
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\broken`, COPILOT_CLI_RESOLVED_DIST_DIR: dist }, no), true);
  // 配布物の場所が渡されなければ、SDK の親が CLI の配布物に見えるかで決める
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk` }, (d) => pathKey(d) === pathKey(dist)), true);
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`C:\app\copilot-sdk`, COPILOT_CLI_PATH: "x" }, no), false);
  // SDK の場所が分からなければアプリとみなす(CLI の印があっても。アプリも渡す)
  assert.equal(runsUnderCli({ COPILOT_CLI_PATH: String.raw`C:\copilot.exe` }, no), false);
  assert.equal(runsUnderCli({}, no), false);
});

test("runsUnderCli: ジャンクション越しの配布物も実体で比べる。npm-loader.js のある親は CLI の配布物とみなす", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    const real = join(d, "pkg", "1.0.0");
    mkdirSync(join(real, "copilot-sdk"), { recursive: true });
    const link = join(d, "current");
    symlinkSync(real, link, "junction");
    const no = () => false;
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(real, "copilot-sdk"), COPILOT_CLI_DIST_DIR: link }, no), true);
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(link, "copilot-sdk"), COPILOT_CLI_RESOLVED_DIST_DIR: real }, no), true);
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(real, "copilot-sdk") }), false);
    writeFileSync(join(real, "npm-loader.js"), "");
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(link, "copilot-sdk") }), true);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("wantDialog: CXXREF_DISPLAY で決める。auto(既定)はアプリのときだけ", () => {
  const cli = { COPILOT_SDK_PATH: String.raw`C:\d\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\d` };
  const app = { COPILOT_SDK_PATH: String.raw`C:\app\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\d` };
  assert.equal(wantDialog(cli), false);
  assert.equal(wantDialog(app), true);
  assert.equal(wantDialog({ ...app, CXXREF_DISPLAY: "timeline" }), false);
  assert.equal(wantDialog({ ...cli, CXXREF_DISPLAY: "dialog" }), true);
  assert.equal(wantDialog({ ...cli, CXXREF_DISPLAY: "auto" }), false);
  assert.equal(wantDialog({}, true), true);  // アプリかどうかを渡せば、それを使う
});

// makeNotifier に渡す偽のセッション。ダイアログは、テストが close() を呼ぶまで開いたまま
function fakeSession({ elicitation = true, fail = "" } = {}) {
  const s = { logs: [], dialogs: [], opens: [], capabilities: { ui: { elicitation } } };
  s.log = async (message, options) => { s.logs.push({ message, ...options }); };
  s.ui = {
    elicitation: async (p) => {
      s.dialogs.push(p.message);
      if (fail) throw new Error(fail);
      return new Promise((done) => s.opens.push(() => done({ action: "accept" })));
    },
  };
  s.close = () => s.opens.shift()();
  return s;
}
const tick = () => new Promise((r) => setImmediate(r));
const appEnv = { COPILOT_SDK_PATH: String.raw`C:\no-such\app\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\no-such\cli` };
const cliEnv = { COPILOT_SDK_PATH: String.raw`C:\no-such\cli\copilot-sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\no-such\cli` };

test("makeNotifier: アプリでは tell の文をダイアログにも出す(error と進み具合は出さない)。\\ は二重にする", async () => {
  const s = fakeSession();
  const n = makeNotifier(() => s, appEnv);
  await n.tell("できました E:\\p\\.index");
  await n.tell("取り直せません", "warning");
  await n.tell("失敗", "error");
  await n.say("10%");
  await tick();
  assert.deepEqual(s.dialogs, ["できました E:\\p\\\\.index"]);  // 前のを閉じるまで次は出さない
  s.close(); await tick();
  assert.deepEqual(s.dialogs, ["できました E:\\p\\\\.index", "取り直せません"]);
  s.close(); await n.settled();
  assert.equal(s.dialogs.length, 2);
  assert.deepEqual(s.logs.map((l) => [l.message, l.level]), [
    ["できました E:\\p\\\\.index", undefined], ["取り直せません", "warning"], ["失敗", "error"], ["10%", undefined]]);
});

test("makeNotifier: アプリでダイアログを出せなければ、error で出し直す。出せるようになったらダイアログに戻る", async () => {
  const s = fakeSession({ elicitation: false });
  const n = makeNotifier(() => s, appEnv);
  await n.tell("状態 E:\\p\\.index");
  await n.settled();
  assert.deepEqual(s.dialogs, []);
  assert.deepEqual(s.logs.at(-1), { message: "状態 E:\\p\\.index\n(このホストはダイアログを出せません)", ephemeral: true, level: "error" });
  s.capabilities.ui.elicitation = true;
  await n.tell("次");
  await tick();
  assert.deepEqual(s.dialogs, ["次"]);
  s.close(); await n.settled();

  const f = fakeSession({ fail: "timeout" });
  const m = makeNotifier(() => f, appEnv);
  await m.tell("できました");
  await m.settled();
  assert.equal(f.logs.at(-1).message, "できました\n(ダイアログを出せませんでした: timeout)");
  assert.equal(f.logs.at(-1).level, "error");
});

test("makeNotifier: CLI ではダイアログを出さない。CXXREF_DISPLAY=dialog なら出す(\\ はそのまま、失敗しても出し直さない)", async () => {
  const s = fakeSession();
  const n = makeNotifier(() => s, cliEnv);
  await n.tell("できました");
  await n.settled();
  assert.deepEqual(s.dialogs, []);

  const f = fakeSession({ fail: "x" });
  const m = makeNotifier(() => f, { ...cliEnv, CXXREF_DISPLAY: "dialog" });
  await m.tell("E:\\p\\.index");
  await m.settled();
  assert.deepEqual(f.dialogs, ["E:\\p\\.index"]);
  assert.deepEqual(f.logs.map((l) => l.level), [undefined]);  // タイムラインに出ているので、error で出し直さない
});
