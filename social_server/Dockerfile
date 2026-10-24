FROM node:20-bookworm-slim

ENV NODE_ENV=production

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install --omit=dev

COPY server.js ./
COPY src ./src

CMD ["node", "server.js"]