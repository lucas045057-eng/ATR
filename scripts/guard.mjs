#!/usr/bin/env node
// 质量基线守卫：PR 只要改动了「质量基线」，就判 CI 失败。
//
// 保护的是这几样东西——它们决定「什么算做完」，一旦能随手改，闸门就形同虚设：
//   * AGENTS.md        —— 审查要点与硬约束
//   * Makefile         —— make check 到底跑什么
//   * .github/workflows/** —— CI 本身
//   * features.json 里每个功能项的 verification —— 验证命令（改动它 = 改判定标准）
//
// 允许改的：features.json 的 state / issue / verified（正常施工需要），以及新增功能项。
//
// 需要改动基线时：让丞相审批，审批通过后给 PR 打 `baseline-approved` 标签，本守卫放行。
// 这样「判失败」和「要人批准」是同一件事——不批就红，批了才绿。
//
// 用法：node scripts/guard.mjs
//   环境变量：GUARD_BASE（默认 origin/main）、GITHUB_BASE_REF（Actions 自动提供）、
//            GITHUB_EVENT_PATH（读 PR 标签用）

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";

const PROTECTED_FILES = ["AGENTS.md", "Makefile"];
const PROTECTED_DIRS = [".github/workflows/"];
const FEATURES = "features.json";
const BYPASS_LABEL = "baseline-approved";

const git = (...args) => execFileSync("git", args, { encoding: "utf8" }).trim();

function labelsOfPullRequest() {
  const path = process.env.GITHUB_EVENT_PATH;
  if (!path || !existsSync(path)) return [];
  try {
    const event = JSON.parse(readFileSync(path, "utf8"));
    return (event.pull_request?.labels ?? []).map((label) => label.name);
  } catch {
    return [];
  }
}

const base = process.env.GITHUB_BASE_REF ? `origin/${process.env.GITHUB_BASE_REF}` : process.env.GUARD_BASE || "origin/main";

let baseSha;
try {
  baseSha = git("merge-base", base, "HEAD");
} catch {
  console.log(`[guard] 找不到基线 ${base}（不是 PR 上下文，或没拉全历史），跳过守卫。`);
  process.exit(0);
}

const changed = git("diff", "--name-only", `${baseSha}..HEAD`).split("\n").filter(Boolean);
if (!changed.length) {
  console.log("[guard] 相对基线没有文件改动，跳过。");
  process.exit(0);
}

// 带 UTF-8 BOM 的 JSON 会让 JSON.parse 直接失败（PowerShell 的 Set-Content -Encoding UTF8 就会写 BOM）。
// 这类问题不靠人记，扫一遍全仓库的 JSON。
const bomFiles = [];
for (const file of git("ls-files", "*.json").split("\n").filter(Boolean)) {
  if (!existsSync(file)) continue;
  const head = readFileSync(file).subarray(0, 3);
  if (head[0] === 0xef && head[1] === 0xbb && head[2] === 0xbf) bomFiles.push(file);
}
if (bomFiles.length) {
  console.error("[guard] 下面这些 JSON 文件带 UTF-8 BOM，JSON.parse 会失败：");
  for (const f of bomFiles) console.error(`  - ${f}`);
  console.error("修法：sed -i '1s/^\\xEF\\xBB\\xBF//' <文件>");
  process.exit(1);
}

const labels = labelsOfPullRequest();
const approved = labels.includes(BYPASS_LABEL);

const problems = [];
for (const file of changed) {
  if (PROTECTED_FILES.includes(file)) problems.push(`${file} 是质量基线文件`);
  else if (PROTECTED_DIRS.some((dir) => file.startsWith(dir))) problems.push(`${file} 属于 CI 工作流`);
}

if (changed.includes(FEATURES)) {
  try {
    const before = JSON.parse(git("show", `${baseSha}:${FEATURES}`)).features ?? [];
    const after = JSON.parse(readFileSync(FEATURES, "utf8")).features ?? [];
    const beforeById = new Map(before.map((f) => [f.id, f.verification]));
    for (const f of after) {
      const was = beforeById.get(f.id);
      if (was !== undefined && was !== f.verification) {
        problems.push(`${FEATURES}: 功能项 ${f.id} 的验证命令被改动（${was} → ${f.verification}）`);
      }
    }
  } catch (error) {
    problems.push(`${FEATURES} 无法与基线比对：${error.message}`);
  }
}

if (!problems.length) {
  console.log(`[guard] 通过：相对 ${base} 改了 ${changed.length} 个文件，没有触碰质量基线。`);
  process.exit(0);
}

if (approved) {
  console.log("[guard] 触碰了质量基线，但 PR 带 baseline-approved 标签（丞相已批准），放行：");
  for (const p of problems) console.log(`  - ${p}`);
  process.exit(0);
}

console.error("[guard] 这个 PR 改动了质量基线，判定失败：");
for (const p of problems) console.error(`  - ${p}`);
console.error("");
console.error("为什么要拦：这些文件决定「什么算做完」。要是施工能自己把验证命令调松、");
console.error("或把 make check 改小、或改掉 CI，闸门就等于没有——历史上已经发生过一次");
console.error("（把 node --test 缩成只跑单个测试文件）。");
console.error("");
console.error(`确实需要改的话：请丞相审批，通过后给这个 PR 打 \`${BYPASS_LABEL}\` 标签，本检查就会放行。`);
process.exit(1);
