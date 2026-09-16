FROM node:22-alpine AS deps
RUN corepack enable
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-contract/package.json packages/api-contract/
RUN pnpm install --frozen-lockfile

FROM deps AS build
COPY packages packages
COPY apps/web apps/web
RUN pnpm -C apps/web build

FROM node:22-alpine
WORKDIR /srv
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
