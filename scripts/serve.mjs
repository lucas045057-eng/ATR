#!/usr/bin/env node
// 零依赖的静态文件服务器：本地预览（scripts/init.sh）和端到端测试（scripts/site-e2e.mjs）共用。
//   node scripts/serve.mjs [目录=dist-site] [端口=8080]     只监听 127.0.0.1
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

export function startServer(dir, port = 0) {
  const root = path.resolve(dir);
  const server = createServer(async (request, response) => {
    const send = (status, body, type = "text/plain; charset=utf-8") => {
      response.writeHead(status, { "content-type": type, "cache-control": "no-store", "x-content-type-options": "nosniff" });
      response.end(request.method === "HEAD" ? undefined : body);
    };
    if (request.method !== "GET" && request.method !== "HEAD") return send(405, "只支持 GET/HEAD");
    let pathname;
    try {
      pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    } catch {
      return send(400, "地址不合法");
    }
    if (pathname.includes("\0")) return send(400, "地址不合法");
    let file = path.join(root, pathname);
    if (file !== root && !file.startsWith(root + path.sep)) return send(403, "不允许访问目录之外的文件"); // 防 ../ 穿越
    try {
      if ((await stat(file)).isDirectory()) file = path.join(file, "index.html");
      const body = await readFile(file);
      return send(200, body, TYPES[path.extname(file)] ?? "application/octet-stream");
    } catch {
      return send(404, "没有这个文件");
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [dir = "dist-site", port = "8080"] = process.argv.slice(2);
  const { port: actual } = await startServer(dir, Number(port));
  console.log(`预览：http://127.0.0.1:${actual}/  （目录 ${path.resolve(dir)}，Ctrl-C 退出）`);
}
