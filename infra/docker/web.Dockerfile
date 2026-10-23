# Pinned by digest for reproducible builds; bump tag and digest together (docker buildx imagetools inspect).
FROM node:22.23.3-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS build

# Web UI (A4, F-M1): the production Next.js build, served by `next start` under compose so it comes
# back after a reboot like every other service. Same Node major as the host (nvm 22).
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1 \
    PNPM_HOME=/pnpm \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PATH="/pnpm:$PATH"

# pnpm comes from the packageManager pin in package.json; the store is a BuildKit cache so rebuilds
# do not re-download.
RUN corepack enable

COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store,id=cryptonite-pnpm,sharing=locked \
    pnpm config set store-dir /pnpm/store && pnpm install --frozen-lockfile

COPY apps/web ./

# next.config.mjs bakes its rewrites into the build, so the API origin is a build argument too.
ARG API_ORIGIN=http://api:8000
ENV API_ORIGIN=$API_ORIGIN
# Optional WebSocket URL baked into the client bundle (staging only); empty keeps host:8000.
ARG NEXT_PUBLIC_WS_URL=
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
# .next/cache is build-time only (webpack/SWC cache); it must not ship in the runtime image.
RUN pnpm build && pnpm prune --prod --ignore-scripts && rm -rf .next/cache

FROM node:22.23.3-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

WORKDIR /app

ARG API_ORIGIN=http://api:8000
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    API_ORIGIN=$API_ORIGIN

COPY --from=build --chown=1000:1000 /app/package.json /app/next.config.mjs ./
COPY --from=build --chown=1000:1000 /app/node_modules ./node_modules
COPY --from=build --chown=1000:1000 /app/public ./public
COPY --from=build --chown=1000:1000 /app/.next ./.next

# uid 1000 is the image's `node` user.
USER 1000:1000

EXPOSE 3000
# Bind every interface inside the container; compose publishes it on 127.0.0.1 only.
CMD ["node_modules/.bin/next", "start", "-H", "0.0.0.0", "-p", "3000"]
