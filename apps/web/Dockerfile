# Ezra dashboard: Next.js standalone server. EZRA_API_URL and EZRA_API_TOKEN are
# read at request time by the server-side proxy, so one image fits any deployment.
FROM node:22-bookworm-slim AS deps
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-bookworm-slim AS build
WORKDIR /web
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /web/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runtime
WORKDIR /web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=node /web/.next/standalone ./
COPY --from=build --chown=node /web/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
