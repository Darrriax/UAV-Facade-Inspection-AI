# The SPA's dev server, containerised so that the whole application - frontend
# included - comes up with a single `docker compose up`.
#
# It is a *dev* image on purpose. The stack's other services are dev-shaped too
# (uvicorn --reload, a source bind mount), and hot reload is what the frontend is
# actually worked on with, so compose bind-mounts the host tree over /app and the
# browser keeps reloading on save.
#
# The image is therefore built to be usable in two ways at once:
#
#   * with no mount at all (`docker run -p 5173:5173 facade-inspection-frontend`),
#     which is why the source is copied in;
#   * with the compose mounts, where /app is the host tree - and where
#     node_modules is deliberately *not* the host's. Compose puts a named volume
#     over /app/node_modules and Docker seeds a new volume from this image, so the
#     container always has linux binaries built from the lockfile, while the host
#     keeps its own (macOS) install untouched.
FROM node:22-alpine

WORKDIR /app

# Dependencies first, and from the lockfile: this layer is reused until the
# lockfile changes, and .dockerignore keeps the host's node_modules out of the
# build context (copying it would bake macOS binaries into a linux image).
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# Compose overrides this with --host 0.0.0.0 --strictPort; the same flags are
# repeated here so a plain `docker run` on a published port behaves identically.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
