import { copyFile, readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'

let html = await readFile('index.html', 'utf8')
for (const extension of ['js', 'css']) {
  const original = `assets/app.${extension}`
  const digest = createHash('sha256').update(await readFile(`dist/${original}`)).digest('hex').slice(0, 16)
  const versioned = `assets/app-${digest}.${extension}`
  await copyFile(`dist/${original}`, `dist/${versioned}`)
  html = html.replace(`/${original}`, `/${versioned}`)
}
await writeFile('dist/index.html', html)
