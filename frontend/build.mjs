import { copyFile } from 'node:fs/promises'

await copyFile('index.html', 'dist/index.html')
