#!/usr/bin/env node
// 把从模板创建出来的新仓库"改名"：只改 ops/delivery.json 里写死仓库名的两处（repo、health.url），幂等。
//   node scripts/set-repo.mjs <owner/name>
// 立项脚本（M3）在新仓库里第一件事就是跑它。Pages 项目站点的地址规则：
//   https://<owner 小写>.github.io/<name>/version.json     （仓库名是 <owner>.github.io 时在根路径，本脚本不处理）
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function rename(delivery, repo) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error("要写成 owner/name");
  const [owner, name] = repo.split("/");
  const next = structuredClone(delivery);
  next.repo = repo;
  if (next.health) next.health.url = `https://${owner.toLowerCase()}.github.io/${name}/version.json`;
  return next;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const repo = process.argv[2];
  const file = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../ops/delivery.json");
  try {
    const next = rename(JSON.parse(readFileSync(file, "utf8")), repo ?? "");
    writeFileSync(`${file}.tmp`, `${JSON.stringify(next, null, 2)}\n`);
    renameSync(`${file}.tmp`, file);
    console.log(`ops/delivery.json → repo=${next.repo}${next.health ? `，health=${next.health.url}` : ""}`);
  } catch (error) {
    console.error(`用法：node scripts/set-repo.mjs <owner/name>（${error.message}）`);
    process.exit(2);
  }
}
