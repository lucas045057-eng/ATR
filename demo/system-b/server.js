import express from 'express'
import pg from 'pg'

const app = express()
const pool = new pg.Pool({ connectionTimeoutMillis: 3000, query_timeout: 3000 })

try {
  await pool.query('SELECT 1')
} catch (error) {
  console.error(`startup dependency check failed: ${error.name}: ${error.message}`)
  process.exit(1)
}

app.get('/health', (_request, response) => response.json({ status: 'ready', service: 'catalog_api' }))
app.get('/record', async (_request, response) => {
  try {
    const result = await pool.query('SELECT item_key, display_name FROM inventory_items WHERE item_key = $1', ['demo-1'])
    if (!result.rows.length) return response.status(404).json({ detail: 'seed record not found' })
    return response.json(result.rows[0])
  } catch (error) {
    console.error(`read-only record query failed: ${error.name}`)
    return response.status(503).json({ detail: 'inventory dependency unavailable' })
  }
})
app.listen(3000, '0.0.0.0', () => console.log('catalog_api listening on 3000'))
