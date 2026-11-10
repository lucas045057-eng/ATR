#!/usr/bin/env node
// 功能清单 features.json 的唯一读写入口。
//
// 铁律：state 变成 passing 只能由本脚本的 verify 在"验证命令真的退出 0"之后写入，同时写下 verified.digest；
// CI 的 recheck 会重跑每个 passing 项的验证命令，并核对 digest。所以手工把 state 改成 passing、
// 或者改了 behavior / verification 却不重新 verify，都会让 make check 失败。
//
//   node scripts/features.mjs validate              只检查格式和 digest，不跑命令
//   node scripts/features.mjs list                  列出所有功能项
//   node scripts/features.mjs verify <id>           跑该项的验证命令；成功 → passing，失败且原来是 passing → 退回 active
//   node scripts/features.mjs set-state <id> <s>    把状态改成 not_started / active / blocked（不允许改成 passing）
//   node scripts/features.mjs recheck               重跑所有 passing 项（CI 用），不写文件，任何一项失败就退出码 1
//
// 环境变量：FEATURES_FILE（默认仓库根的 features.json）、FEATURE_TIMEOUT_MS（单个验证命令的超时，默认 300000）
import { createHash } from "node:crypto";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const STATES = ["not_started", "active", "blocked", "passing"];
const ITEM_KEYS = ["id", "behavior", "verification", "state", "issue", "verified"];
const TRIVIAL = /^\s*(true|:|exit\s+0)\s*;?\s*$/; // 恒成功的"验证"不算验证

const here = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(here, "..");
const FILE = process.env.FEATURES_FILE ?? path.join(ROOT, "features.json");

/** 功能项的指纹：id、行为、验证命令任何一个被改动，旧的 passing 就失效。 */
export const digestOf = (item) =>
  createHash("sha256").update(`${item.id}\n${item.behavior}\n${item.verification}`).digest("hex");

/** 返回问题列表（空 = 合格）。只看格式与 digest，不跑命令。 */
export function validate(data) {
  const problems = [];
  if (!data || typeof data !== "object" || !Array.isArray(data.features)) {
    return ['顶层必须是 {"features": [...]}'];
  }
  const ids = new Set();
  for (const [index, item] of data.features.entries()) {
    const where = `features[${index}]${item?.id ? `(${item.id})` : ""}`;
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      problems.push(`${where} 不是对象`);
      continue;
    }
    for (const key of Object.keys(item)) if (!ITEM_KEYS.includes(key)) problems.push(`${where} 有不认识的字段 ${key}`);
    if (typeof item.id !== "string" || !/^F\d+$/.test(item.id)) problems.push(`${where} 的 id 要写成 F1、F2…`);
    else if (ids.has(item.id)) problems.push(`${where} 的 id 重复`);
    else ids.add(item.id);
    for (const key of ["behavior", "verification"]) {
      if (typeof item[key] !== "string" || !item[key].trim()) problems.push(`${where} 缺 ${key}`);
    }
    if (typeof item.verification === "string" && TRIVIAL.test(item.verification)) {
      problems.push(`${where} 的 verification 是恒成功的命令，不算验证`);
    }
    if (!STATES.includes(item.state)) problems.push(`${where} 的 state 只能是 ${STATES.join(" / ")}`);
    if (!("issue" in item) || !(item.issue === null || (Number.isInteger(item.issue) && item.issue > 0))) {
      problems.push(`${where} 的 issue 要么是 null，要么是正整数（Issue 号）`);
    }
    if (item.state === "passing") {
      const v = item.verified;
      if (!v || typeof v !== "object" || typeof v.at !== "string" || typeof v.digest !== "string") {
        problems.push(`${where} 是 passing 却没有 verified：passing 只能由 scripts/verify-feature.sh 写入`);
      } else if (v.digest !== digestOf(item)) {
        problems.push(`${where} 的 verified.digest 对不上：behavior/verification 改过或 state 是手工改的，重新跑 scripts/verify-feature.sh ${item.id}`);
      }
    }
  }
  return problems;
}

export const load = (file = FILE) => JSON.parse(readFileSync(file, "utf8"));

export function save(data, file = FILE) {
  const temp = `${file}.tmp-${process.pid}`;
  writeFileSync(temp, `${JSON.stringify(data, null, 2)}\n`);
  renameSync(temp, file); // 同目录 rename 是原子的，写一半不会留下坏文件
}

/** 跑一项的验证命令（bash -c，仓库根目录）。 */
export function runVerification(item, { quiet = false } = {}) {
  const result = spawnSync("bash", ["-c", item.verification], {
    cwd: ROOT,
    stdio: quiet ? "ignore" : "inherit",
    timeout: Number(process.env.FEATURE_TIMEOUT_MS ?? 300_000),
  });
  return result.status === 0;
}

/** 把一次验证的结果写进数据：只有 ok 才会变成 passing。纯函数，方便测试。 */
export function applyResult(data, id, ok, now = new Date().toISOString()) {
  const item = data.features.find((f) => f.id === id);
  if (!item) throw new Error(`没有功能项 ${id}`);
  if (ok) {
    item.state = "passing";
    item.verified = { at: now, digest: digestOf(item) };
  } else if (item.state === "passing") {
    item.state = "active"; // 回归：原来过的现在不过了
    delete item.verified;
  }
  return item;
}

function main(argv) {
  const [command, id, arg] = argv;
  const data = load();
  const problems = validate(data);
  if (command === "validate" || command === "recheck" || command === "list") {
    if (problems.length) {
      console.error(`features.json 不合格（${problems.length} 处）：\n- ${problems.join("\n- ")}`);
      return 1;
    }
  }
  if (command === "validate") {
    console.log(`features.json 合格：${data.features.length} 项，passing ${data.features.filter((f) => f.state === "passing").length} 项`);
    return 0;
  }
  if (command === "list") {
    for (const f of data.features) console.log(`${f.id}\t${f.state}\t${f.behavior}`);
    return 0;
  }
  if (command === "recheck") {
    const passing = data.features.filter((f) => f.state === "passing");
    const failed = [];
    for (const item of passing) {
      console.log(`[recheck] ${item.id}：${item.verification}`);
      if (!runVerification(item)) failed.push(item.id);
    }
    if (failed.length) {
      console.error(`passing 项复核失败：${failed.join("、")}。这些功能已经不满足验证命令，修复后重新 verify，或把状态退回 active。`);
      return 1;
    }
    console.log(`[recheck] ${passing.length} 个 passing 项全部复核通过`);
    return 0;
  }
  if (command === "verify") {
    if (!id) return usage();
    const item = data.features.find((f) => f.id === id);
    if (!item) return fail(`没有功能项 ${id}`);
    // 这一项自己的 digest 对不上没关系（verify 就是来重算的）：校验时先把它当作未通过
    const probe = structuredClone(data);
    const target = probe.features.find((f) => f.id === id);
    if (target.state === "passing") Object.assign(target, { state: "active", verified: undefined });
    const bad = validate(probe);
    if (bad.length) return fail(`features.json 有其它问题，先修好：\n- ${bad.join("\n- ")}`);
    console.log(`[verify] ${id}：${item.verification}`);
    const ok = runVerification(item);
    applyResult(data, id, ok);
    save(data);
    console.log(ok ? `[verify] ${id} → passing` : `[verify] ${id} 验证失败，state=${data.features.find((f) => f.id === id).state}`);
    return ok ? 0 : 1;
  }
  if (command === "set-state") {
    if (!id || !arg) return usage();
    if (arg === "passing") return fail("passing 只能由 verify 写入，不能手工设置");
    if (!STATES.includes(arg)) return fail(`state 只能是 ${STATES.join(" / ")}`);
    const item = data.features.find((f) => f.id === id);
    if (!item) return fail(`没有功能项 ${id}`);
    item.state = arg;
    delete item.verified;
    save(data);
    console.log(`${id} → ${arg}`);
    return 0;
  }
  return usage();
}

const fail = (message) => (console.error(message), 1);
const usage = () => fail("用法：features.mjs validate | list | verify <id> | set-state <id> <not_started|active|blocked> | recheck");

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)));
}
