// cxxref の Copilot CLI 拡張。モデルは呼ばない(スクリプトを裏で動かし、結果をセッションに出すだけ)。
//
// /cxxref-index [ルート]  作業中のリポジトリの索引を作る。ltidx.exe が無ければ、先に scripts\get-ltidx.ps1 で取ってくる。
//                         索引がすでにあれば、変わった TU だけを作り直す(-Update)。裏で動かし、終わったらそう出す。
//                         走っている間は、進み具合(compile_commands.json の用意・解析した TU の数)をときどき出す。
//                         ルートを省くと、作業フォルダから親へさかのぼって .index のあるフォルダ、無ければ作業フォルダ。
//                         compile_commands.json の作り元(.slnx など)の候補が複数あれば、ダイアログで選ばせる。
//                         選んだものは作り方の記録に残り、次からは聞かない。
// /cxxref-enable on|off|status [local|project] [ルート]
//                         使い方の指示を足す・消す(scripts\setup-instructions.ps1)。on は省くとローカル(自分だけ。git から外す)、
//                         project は .github\copilot-instructions.md(コミットすればチーム全員)。off は省くと両方から消す。
//                         引数なしは今の状態を出す。ルートを省くと、作業フォルダの git のルート、無ければ作業フォルダ。
//
// 拡張は Copilot CLI の実験的機能で、機能フラグ EXTENSIONS が有効なときだけ読み込まれる。
import { joinSession } from "@github/copilot-sdk/extension";
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { enableRoot, escapeMarkdown, findLtidx, lastCwd, parseEnableArgs, parseProgress, pluginRoot, progressNote, projectChoices, savedProject, statusHint, targetRoot } from "./lib.mjs";

function run(script, args, log) {
  return new Promise((done) => {
    const fd = openSync(log, "a");
    const p = spawn("pwsh", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...args], {
      stdio: ["ignore", fd, fd],
      windowsHide: true,
    });
    p.on("error", (err) => { closeSync(fd); done({ code: null, error: err }); });
    p.on("exit", (code) => { closeSync(fd); done({ code }); });
  });
}

// 出力を受け取る版(setup-instructions.ps1 はパイプには UTF-8 で出す)
function runText(script, args) {
  return new Promise((done) => {
    const p = spawn("pwsh", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    let out = "";
    p.stdout.setEncoding("utf8");
    p.stderr.setEncoding("utf8");
    p.stdout.on("data", (s) => { out += s; });
    p.stderr.on("data", (s) => { out += s; });
    p.on("error", (err) => done({ code: null, error: err, out }));
    p.on("close", (code) => done({ code, out }));
  });
}

// 終わり方の説明(pwsh を起動できなかったときは、その理由)
const why = (r) => (r.error ? `pwsh を起動できません(PowerShell 7 が要ります): ${r.error.message}` : `終了コード ${r.code}`);

let running = null; // 走っている索引づくり(同時に 1 つだけ)

const tell = (message, level) =>
  session.log(level ? message : escapeMarkdown(message), { ephemeral: true, ...(level ? { level } : {}) });

// compile_commands.json の作り元(index-project.ps1 の -Project)。{ project }(""なら index-project に探させる)か、
// やめるときは { stop: true }。候補が複数あれば、CLI のダイアログで選ばせる(モデルは呼ばない)。
// 前に選んだもの(作り方の記録)があれば、聞かずにそれを使う
async function chooseProject(plugin, root) {
  if (existsSync(join(root, "compile_commands.json"))) return { project: "" };
  const saved = savedProject(root);
  if (saved) return { project: saved };
  const r = await runText(join(plugin, "scripts", "make-cdb.ps1"), ["-Root", root, "-ListProjects"]);
  if (r.code !== 0) return { project: "" };  // 探せなかったわけは、index-project のログに出る
  const choices = projectChoices(root, r.out);
  if (choices.length <= 1) return { project: "" };
  const labels = choices.map((c) => c.label);
  if (!session.capabilities?.ui?.elicitation) {
    const cmd = `pwsh -File "${join(plugin, "scripts", "index-project.ps1")}" -Root "${root}" -Project "${choices[0].path}"`;
    await tell(`cxxref: compile_commands.json の作り元の候補が複数あり、この CLI では選ぶダイアログを出せません: ${labels.join(", ")}\n` +
      `ターミナルで -Project を付けて作ってください(次からは /cxxref-index がそれを使います)。例: ${cmd}`, "error");
    return { stop: true };
  }
  const picked = await session.ui.select(`cxxref: 索引を作る元を選んでください(${root})`, labels);
  if (picked == null) {
    await tell("cxxref: 索引づくりをやめました");
    return { stop: true };
  }
  return { project: choices.find((c) => c.label === picked).path };
}

// index-project を走らせている間、ltidx のログ(.index\index.log)を数秒おきに見て、進み具合を画面に出す。
// 出すかどうかは progressNote が決める(段階が変わったとき・TU が 1 割進んだときなど)。止める関数を返す
function watchProgress(ltLog, t1) {
  let state = {};
  const tick = () => {
    let snap = { phase: "cdb" };
    try {
      if (statSync(ltLog).mtimeMs >= t1) {  // 前の実行のログは見ない(ltidx が始まると書き直される)
        const p = parseProgress(readFileSync(ltLog, "utf8"));
        if (!p) return;  // ltidx は始まったが、まだ 1 つも終わっていない
        snap = { phase: "tu", ...p };
      }
    } catch { }  // まだ無い(compile_commands.json を用意している)
    const now = Date.now();
    const r = progressNote(state, snap, now, Math.round((now - t1) / 1000));
    state = r.state;
    if (r.note) tell(r.note).catch(() => { });
  };
  const timer = setInterval(tick, 3000);
  return () => clearInterval(timer);
}

async function indexCommand(ctx) {
  // await の前に印を付ける(続けて呼ばれても、2 つ目は始めない)
  if (running) {
    await tell(`cxxref: 索引を作っている途中です(${running})。終わるまで待ってください`, "warning");
    return;
  }
  running = "準備中";
  try {
    const plugin = pluginRoot(dirname(fileURLToPath(import.meta.url)));
    const cwd = lastCwd(await session.getEvents()) || process.cwd();
    const t = targetRoot(ctx.args, cwd);
    if (t.error) {
      await tell(`cxxref: ${t.error}`, "error");
      return;
    }
    const root = t.root;
    running = root;
    // ltidx を取ってくる前に選ばせる(やめたときに、取ってくる手間をかけない)
    const pick = await chooseProject(plugin, root);
    if (pick.stop) return;
    const t0 = Date.now();
    let lt = findLtidx(plugin);
    if (lt.error) {
      await tell(`cxxref: ${lt.error}(LTIDX を直すか消してください)`, "error");
      return;
    }
    if (!lt.path) {
      // 取ってくる前は、ルートに .index を作らない(失敗したときに空の .index が残ると、そこが索引のルートと見なされる)
      const log = join(tmpdir(), "cxxref-get-ltidx.log");
      await tell("cxxref: ltidx.exe が無いので、先にリリースから取ってきます(scripts\\get-ltidx.ps1)");
      const r = await run(join(plugin, "scripts", "get-ltidx.ps1"), [], log);
      lt = findLtidx(plugin);
      if (r.code !== 0 || !lt.path) {
        await tell(`cxxref: ltidx を取ってこられませんでした(${why(r)})。ログ: ${log}`, "error");
        return;
      }
    }
    const dir = join(root, ".index");
    mkdirSync(dir, { recursive: true });
    const log = join(dir, "cxxref-index.log");
    const update = existsSync(join(dir, "index.tsv.deps.tsv"));
    await tell(`cxxref: 索引を${update ? "作り直し" : "作り"}始めました(${root})。裏で動かすので、作業を続けてかまいません。ログ: ${log}`);
    // index-project.ps1 は、索引ができたら 0、できなければ 2 で終える(ltidx の失敗や異常終了も 2)
    const args = ["-Root", root, ...(pick.project ? ["-Project", pick.project] : []), ...(update ? ["-Update"] : [])];
    const stop = watchProgress(join(dir, "index.log"), Date.now());
    const r = await run(join(plugin, "scripts", "index-project.ps1"), args, log).finally(stop);
    const sec = Math.round((Date.now() - t0) / 1000);
    if (r.code === 0) {
      await tell(`cxxref: 索引ができました(${sec} 秒、${dir})。以後は MCP サーバーの refs / check がこの索引を使い、コードを変えても自動で作り直します`);
    } else {
      await tell(`cxxref: 索引づくりに失敗しました(${why(r)}、${sec} 秒)。ログ: ${log}`, "error");
    }
  } finally {
    running = null;
  }
}

async function enableCommand(ctx) {
  const a = parseEnableArgs(ctx.args);
  if (a.error) {
    await tell(`cxxref: ${a.error}`, "error");
    return;
  }
  const plugin = pluginRoot(dirname(fileURLToPath(import.meta.url)));
  const cwd = lastCwd(await session.getEvents()) || process.cwd();
  const t = enableRoot(a.root, cwd);
  if (t.error) {
    await tell(`cxxref: ${t.error}`, "error");
    return;
  }
  const args = ["-Root", t.root, "-Action", a.action, ...(a.scope ? ["-Scope", a.scope] : [])];
  const r = await runText(join(plugin, "scripts", "setup-instructions.ps1"), args);
  const out = r.out.trim();
  if (r.code !== 0) {
    await tell(`cxxref: 指示を${a.action === "Remove" ? "消せ" : "変えられ"}ませんでした(${why(r)})${out ? `\n${out}` : ""}`, "error");
    return;
  }
  // 指示はセッションの始めに読まれる(今のセッションの途中から効くかは確かめていない)。変えたときだけ添える
  const changed = /(足し|差し替え|消し)ました/.test(out);
  const hint = a.action === "Status" ? statusHint(out) : "";
  const note = changed ? "\n新しいセッションから効きます(copilot instruction list で確かめられます)" : hint ? `\n${hint}` : "";
  await tell(`cxxref: ${out}${note}`);
}

const session = await joinSession({
  commands: [
    {
      // 拡張のコマンドには引数のヒントを付けられないので、説明文の先頭に引数を書く
      name: "cxxref-index",
      description: "[ルート] — C++ の参照索引(cxxref)を作る・作り直す。裏で動かす(モデルは呼ばない)",
      handler: (ctx) => {
        indexCommand(ctx).catch((err) => tell(`cxxref: ${err?.message ?? err}`, "error"));
      },
    },
    {
      name: "cxxref-enable",
      description: "on|off|status [local|project] [ルート] — 使い方の指示を足す・消す。on は省くとローカル(自分だけ)(モデルは呼ばない)",
      handler: (ctx) => {
        enableCommand(ctx).catch((err) => tell(`cxxref: ${err?.message ?? err}`, "error"));
      },
    },
  ],
});
