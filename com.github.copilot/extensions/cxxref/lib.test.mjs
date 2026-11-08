// node --test com.github.copilot/extensions/cxxref/lib.test.mjs(mcp/test/test_plugin.py が流す)。/cxxref-index・/cxxref-enable の SDK に依らない部分
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { enableRoot, escapeMarkdown, findLtidx, gitTop, indexRoot, lastCwd, parseEnableArgs, pluginRoot, projectChoices, savedProject, targetRoot } from "./lib.mjs";

test("projectChoices: フルパスの行を、ルートからの相対パスの選択肢にする", () => {
  const root = join(tmpdir(), "nox");
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
  assert.equal(escapeMarkdown("E:\\src\\Nox\\.index\\a.log"), "E:\\src\\Nox\\\\.index\\a.log");
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
