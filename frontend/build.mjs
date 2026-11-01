import { copyFile, readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

function samePath(left, right) {
  const normalize = value => {
    const path = resolve(value)
    return process.platform === 'win32' ? path.toLowerCase() : path
  }
  return normalize(left) === normalize(right)
}

function gitCommit() {
  try {
    const toplevel = execFileSync('git', ['rev-parse', '--show-toplevel'], {
      cwd: '..', encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    if (!samePath(toplevel, resolve('..'))) return ''
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: '..', encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
  } catch {
    return ''
  }
}

let html = await readFile('index.html', 'utf8')
for (const extension of ['js', 'css']) {
  const original = `assets/app.${extension}`
  const digest = createHash('sha256').update(await readFile(`dist/${original}`)).digest('hex').slice(0, 16)
  const versioned = `assets/app-${digest}.${extension}`
  await copyFile(`dist/${original}`, `dist/${versioned}`)
  html = html.replace(`/${original}`, `/${versioned}`)
}
await writeFile('dist/index.html', html)
const manifest = {
  version: JSON.parse(await readFile('package.json', 'utf8')).version,
  commit: gitCommit(),
  built_at: new Date().toISOString(),
  source: 'npm run build',
}
await writeFile('dist/build.json', JSON.stringify(manifest, null, 2) + '\n')
