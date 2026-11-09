#!/usr/bin/env node
// 构建：把 index.html 复制进 dist-site/，并生成 dist-site/version.json（含【完整】40 位 commit SHA）。
//
// version.json 就是部署后的"健康接口"：ops/delivery.json 的 health 指向它，交付流水线靠它核对线上到底是哪个提交。
//   {"ok":true,"name":"agent-template","repo":"owner/name","commit":"<40位>","dirty":false,"built_at":"<ISO 时间>"}
//
// commit 的来源（按优先级）：环境变量 GITHUB_SHA（CI）→ `git rev-parse HEAD` → 40 个 0（不在 git 里，dirty=true）。
// 本地有未提交改动时 dirty=true；CI 里恒为 false。
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(here, "..");
const SHA = /^[0-9a-f]{40}$/;
const git = (...args) => spawnSync("git", args, { cwd: ROOT, encoding: "utf8" });

export function resolveCommit(env = process.env) {
  if (SHA.test(env.GITHUB_SHA ?? "")) return { commit: env.GITHUB_SHA, dirty: false };
  const head = git("rev-parse", "HEAD");
  if (head.status === 0 && SHA.test(head.stdout.trim())) {
    const status = git("status", "--porcelain", "--untracked-files=no");
    return { commit: head.stdout.trim(), dirty: status.status === 0 && status.stdout.trim() !== "" };
  }
  return { commit: "0".repeat(40), dirty: true };
}

export function resolveRepo(env = process.env) {
  if (/^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPOSITORY ?? "")) return env.GITHUB_REPOSITORY;
  return JSON.parse(readFileSync(path.join(ROOT, "ops/delivery.json"), "utf8")).repo;
}

export function versionJson(env = process.env, now = new Date()) {
  const { commit, dirty } = resolveCommit(env);
  const repo = resolveRepo(env);
  return { ok: true, name: repo.split("/")[1], repo, commit, dirty, built_at: now.toISOString() };
}

export function build(outDir = path.join(ROOT, "dist-site"), env = process.env) {
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  cpSync(path.join(ROOT, "site/index.html"), path.join(outDir, "index.html"));
  writeFileSync(path.join(outDir, ".nojekyll"), ""); // 不让 Pages 用 Jekyll 处理
  const version = versionJson(env);
  writeFileSync(path.join(outDir, "version.json"), `${JSON.stringify(version, null, 2)}\n`);
  return version;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const version = build();
  console.log(`构建完成：dist-site/（commit ${version.commit.slice(0, 7)}${version.dirty ? "，有未提交改动" : ""}，repo ${version.repo}）`);
}
