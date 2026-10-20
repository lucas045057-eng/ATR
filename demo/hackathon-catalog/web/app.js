import express from 'express';

const app = express();
app.get('/health', (_request, response) => response.json({status: 'ok'}));
