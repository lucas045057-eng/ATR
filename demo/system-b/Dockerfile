FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund
COPY server.js ./server.js
USER node
EXPOSE 3000
CMD ["node", "server.js"]
