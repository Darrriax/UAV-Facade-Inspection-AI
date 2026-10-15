# Running the whole system

Everything - the SPA, the API, the inference worker, PostgreSQL, Redis, Garage
(S3) and NodeODM - runs on one machine, in Docker, from one command. The only
host-side prerequisite is Docker itself.

## 1. Start it

```bash
docker compose up -d --build          # from the repository root
```

That is the whole setup. There is no migration to apply, no account to create and
no dev server to start by hand: a one-shot `bootstrap` container does those
before the API and the worker are allowed to start (see
[What the command does](#what-the-command-does)).

| Service    | Role                                              | Reached at                                    |
| ---------- | ------------------------------------------------- | --------------------------------------------- |
| `frontend` | Vue SPA, served by the Vite dev server            | <http://localhost:5173>                        |
| `api`      | FastAPI                                           | <http://localhost:8000/docs>                   |
| `worker`   | Celery: YOLO detection, then the 3D reconstruction | (no port - `docker compose logs -f worker`)   |
| `postgres` | database                                          | `localhost:5433` (published for host tooling)  |
| `redis`    | Celery broker                                     | `localhost:6379`                               |
| `garage`   | S3 storage for frames and `.glb` models           | <http://localhost:3900>                        |
| `nodeodm`  | OpenDroneMap photogrammetry                       | <http://localhost:3000/info>                   |
| `bootstrap`| one-shot: migrations, buckets, seed accounts      | exits 0 - `docker compose logs bootstrap`      |

The `frontend` runs the *dev* server, not a static build, and the source tree is
bind-mounted: a save on the host hot-reloads in the browser, exactly like
`uvicorn --reload` on the API.

Check it came up, then open the SPA:

```bash
docker compose ps                     # every service should reach (healthy)
open http://localhost:5173            # macOS; xdg-open elsewhere
```

### Accounts it creates for you

| Sign in as             | Password              | What you land on                              |
| ---------------------- | --------------------- | --------------------------------------------- |
| `admin@example.com`    | `admin-secret-1`      | `/admin/inspectors` - create/deactivate Inspectors |
| `inspector@example.com`| `inspector-secret-1`  | `/dashboard` - the project flow                |

Both are dev-only defaults from `BOOTSTRAP_*` in the root `.env`. The first Admin
cannot be created through the API - `/users` already requires an Admin, and the
Admin it would need does not exist yet - which is why the stack seeds it. Change
the passwords in `.env` before the first `up`, or create more accounts later:

```bash
docker compose exec api python -m app.cli create-user \
  --email someone@example.com --role inspector        # prompts for the password
```

Set `BOOTSTRAP_ADMIN_EMAIL=` (empty) in `.env` to skip that seed entirely; the
same for `BOOTSTRAP_INSPECTOR_EMAIL`.

## What the command does

1. **Builds three images.** `api` and `bootstrap` share one; the `worker` is the
   only image with the AI stack (CPU torch + ultralytics, ~2 GB - several minutes
   on the first build) and the `frontend` is a Node image running Vite.
2. **Starts the infrastructure** and waits for real healthchecks: PostgreSQL
   accepts connections, Redis answers `PING`, NodeODM serves `/info`, and Garage
   has a cluster layout *and* its buckets - so a `healthy` API means object
   storage is genuinely usable.
3. **Runs `bootstrap` once.** In order:
   - `alembic upgrade head` - the schema is migrated here and nowhere else;
   - `app.cli init-storage` - creates the buckets if they are missing and
     (re)applies their CORS rules, which is what lets the browser fetch a
     presigned `.glb` itself;
   - `app.cli create-user --if-exists skip` for the two seed accounts.

   The API and the worker declare `depends_on: bootstrap:
   service_completed_successfully`, which is deliberate: a migration that fails
   fails a *container you can read the log of*, and nothing then boots against a
   half-migrated schema. It also means the migration is applied once per `up`
   rather than retried on every API restart.
4. **Starts the API, the worker and the frontend.** `frontend` waits for a
   healthy `api` so the first `/api` call does not land on a booting proxy target.

Everything in step 3 is idempotent, so a second `docker compose up -d` (no
`--build` needed unless the code or the lockfile changed) is a fast no-op:
the log will say `already exists` for both accounts, which is normal.

## Prerequisites

```bash
docker compose version        # Compose v2 - the `docker compose` plugin, not `docker-compose`
```

Give Docker at least 8 GB of RAM: NodeODM uses several gigabytes once it starts a
reconstruction, and the worker image is a large build. Node itself is only needed
if you want to run the frontend *outside* Docker (see
[below](#running-the-frontend-on-the-host)).

## Configuration (optional)

Docker Compose reads a `.env` file next to `docker-compose.yml`, and every value
in it has a dev default - the stack starts without one. To get an annotated
template:

```bash
cp code/backend/.env.example .env      # then edit
```

Worth knowing about:

- `JWT_SECRET_KEY` - the API and the worker both read it. They share one
  `.env`, so this is only a trap if you change it while the containers are
  running: `docker compose up -d` re-reads it.
- `S3_PUBLIC_ENDPOINT_URL` - the endpoint baked into presigned URLs, so it must
  be a host the *browser* can resolve (`http://localhost:3900`). The containers
  talk to Garage directly over the compose network; the shared `.env`
  deliberately does **not** set the internal endpoint, because
  `http://localhost:3900` inside a container means the container itself.
- `POSTGRES_PORT` - the *published* port defaults to **5433**, not Postgres' own
  5432. A native PostgreSQL install already listening on 5432 is common, and that
  collision aborts the whole `up`. Set this (in `.env`) to move it.
- `FRONTEND_PORT` - the published port for the SPA; the dev server inside the
  container always listens on 5173.
- `VITE_WATCH_POLLING` - `true` here, because Docker Desktop does not forward the
  host's filesystem events into a container, so hot reload would silently stop
  without polling. Set it to `false` on a Linux host.
- `BOOTSTRAP_ADMIN_*`, `BOOTSTRAP_INSPECTOR_*` - the seed accounts described above.

## Trained YOLO weights (optional but recommended)

The worker loads `YOLO_MODEL_PATH`, which defaults to `/app/model/best.pt` - i.e.
`code/backend/model/best.pt` on the host, because `./code/backend` is bind-mounted
at `/app`. No rebuild and no compose change is involved:

```bash
mkdir -p code/backend/model
cp yolov9c_cubit_e136_best.pt code/backend/model/best.pt
docker compose restart worker          # if the worker had already loaded a model
```

Without the file, the pipeline still runs end to end but the worker logs

```text
YOLO weights unavailable (weights are not a readable file at '/app/model/best.pt')
- emitting SYNTHETIC defects for <image>.png. These rows are not real detections.
```

Synthetic rows exist so the UI can be developed against something. They are
**not** model output - never measure accuracy on them.

### The CUBIT-InSeg weights

The dataset authors' `best.pt` (YOLOv9-c segmentation, 300 epochs, classes
`crack`/`spalling`) is a checkpoint of a *patched* ultralytics: its blocks are
pickled under `ultralytics.nn.extra_modules.block`, a path upstream ultralytics
has never defined, and the YOLOv9 classes it names were later renamed
(`RepNCSP` -> `RepCSP`, `RepNBottleneck` -> `RepBottleneck`) or dropped
(`RepConvN`). Loading it on the installed version fails with
`ModuleNotFoundError: No module named 'ultralytics.nn.extra_modules'` before any
inference runs. `app/services/legacy_yolov9.py` is the bridge that fixes that:
it defines the five blocks and registers the module path just before the
checkpoint is loaded, so such a file works unchanged. Two consequences:

- these checkpoints also need `dill` to unpickle - it is a pinned dependency of
the worker image for exactly this reason;
- their inference parameters are fixed at the values the checkpoint was tuned
at: `YOLO_IMAGE_SIZE=1280`, `YOLO_CONFIDENCE_THRESHOLD=0.01`,
`YOLO_IOU_THRESHOLD=0.7`, `YOLO_RETINA_MASKS=true` ([config.py](code/backend/app/core/config.py)).
These are not Ultralytics' defaults (`conf=0.25`, `iou=0.5`) - the low
confidence threshold is what surfaces the faint crack/spalling masks, and
`retina_masks` returns the masks at the original frame size. Changing any of
them changes what the site detects, so treat them as part of the model, not as
dialable knobs.

The weights are cached per worker process, keyed by the path - after replacing
the file the worker has to be restarted (see the `cp` note above); a running
process keeps serving the model it already loaded.

## Schema changes

The migrations are applied by `bootstrap` on every `up`, so a fresh clone and an
existing database both converge with the same command. Generating a new revision
is still explicit (and still the right place to review generated DDL):

```bash
docker compose exec api alembic revision --autogenerate -m "add something"
docker compose exec api alembic upgrade head          # or just: docker compose up -d
docker compose exec api alembic current               # confirm the revision
```

## Running the frontend on the host (optional)

The container is the default because it needs no local Node install. To use the
host's toolchain instead - e.g. for the Vue Devtools extension - stop the
container first, since both want the same port:

```bash
docker compose stop frontend
cd code/frontend && npm install && npm run dev      # http://localhost:5173
```

The dev server proxies `/api` to `http://localhost:8000`, which is the default
`VITE_API_PROXY_TARGET`; the container overrides it with `http://api:8000`. Set
`VITE_API_BASE_URL` in `code/frontend/.env.local` only when the API is on another
origin.

Two notes: Vite binds `localhost`, which resolves to IPv6 `::1` on macOS - fine
in a browser, but `curl http://127.0.0.1:5173` can fail while
`curl http://localhost:5173` succeeds. And if 5173 is taken, host Vite picks the
next free port while the container is started with `--strictPort` and fails
loudly instead; **read the port from the log line** either way.

## Everyday commands

```bash
docker compose logs -f api worker      # follow the API and the inference worker
docker compose logs -f frontend        # Vite: compile errors, hot-reload events
docker compose restart api             # after a backend edit that uvicorn's --reload missed
docker compose stop worker             # hold frames in `pending` to exercise the progress UI
docker compose start worker            # and let the batch drain
docker compose up -d --scale worker=3  # one long CPU-bound task per process: scale out, not up
docker compose down                    # stop and remove containers (volumes survive)
docker compose down -v                 # also drop the database, buckets and ODM data
```

Checks to run before a change is considered done - in the container, so no local
Node is needed:

```bash
docker compose exec frontend npm run typecheck     # vue-tsc
docker compose exec frontend npm run build         # typecheck + production bundle
```

Changed `package.json`? The container installs from the lockfile at build time, so
run `docker compose exec frontend npm install` (or rebuild the frontend) to
update the `node_modules` volume it keeps to itself.

## Troubleshooting

| Symptom | Cause and fix |
| ------- | ------------- |
| `Error response from daemon: ... address already in use` for 5432 | A native PostgreSQL is already running. The compose default is 5433 for exactly this reason; if 5433 is taken too, set `POSTGRES_PORT` in `.env`. (`API_PORT`, `REDIS_PORT`, `GARAGE_PORT`, `NODEODM_PORT`, `FRONTEND_PORT` move the others.) |
| `docker compose up` stops after the port error | Compose aborts the whole `up`. Fix the colliding port and run the same command again - nothing is lost. |
| The SPA does not load on 5173, `docker compose logs frontend` shows a port error | The container runs Vite with `--strictPort`: it refuses to move ports rather than silently listening somewhere the published mapping does not cover. Free 5173, or set `FRONTEND_PORT`. |
| `/api/...` returns 404 or a Vite HTML page from the SPA | The API is down or the proxy target is wrong. `docker compose ps api`, `docker compose logs api`, and check `VITE_API_PROXY_TARGET`. |
| `docker compose ps` shows `api` as `starting` or unhealthy | `docker compose logs api` - the container's healthcheck is a socket probe on port 8000, and it does not pass while the app is still importing. `/health` is not a route; use `docker compose ps` or `/docs`. |
| The API or the worker never starts, `bootstrap` exited non-zero | A migration or a seed failed: `docker compose logs bootstrap`. Fix the cause and run `docker compose up -d` again. |
| Upload succeeds but frames sit at `Pending` forever | Nothing is consuming the queue: `docker compose ps worker` / `docker compose logs worker`. If the upload response had `task_id: null`, the broker was unreachable at upload time - the API logs the reason. |
| `SYNTHETIC defects` in the worker log | The trained weights are not at `code/backend/model/best.pt` - see above. |
| Edits on the host do not reload in the browser | The watcher is not seeing the event: confirm `VITE_WATCH_POLLING=true` for the `frontend` service (`docker compose config \| grep VITE_WATCH`) and look for `[vite] hmr update` in `docker compose logs -f frontend`. |
| The 3D step never finishes | NodeODM needs overlapping frames (≥ `NODEODM_MIN_IMAGES`, default 3) and is slow even on the reduced profile - tens of minutes for a real sortie. Watch `docker compose logs -f worker` (it prints NodeODM progress) and <http://localhost:3000/info>. The 2D results are independent of it. |
| 3D step fails with `could not process this dataset` | ODM matched too few features: it needs more, better-overlapping frames, or a higher `NODEODM_FEATURE_QUALITY`. Measured on a 3-frame facade set: `lowest` (~150 features per frame) and `low` both abort, `medium` reconstructs. The stage log is in the task directory inside the container: `docker compose exec nodeodm sh -c 'tail -40 /var/www/data/<task-uuid>/task_output.txt'` - fetch the uuid from the worker log before the task is removed. |
| `Only ODM's 2.5D mesh ... is available` in the worker log | ODM produced `odm_texturing_25d/` but no full 3D model - the reconstruction did not solve a real 3D scene. The uploaded asset is a planar mesh: check the dataset overlap. |
| The 3D page shows a model, then it is gone after a reload | The mesh never reached storage. The 3D page reconstructs in the browser and then uploads the `.glb` to `POST /projects/{id}/model3d/mesh`; when that call fails the page says so above the viewer, and the model exists only in that tab. Check the upload in `docker compose logs api` and the registration with `docker compose exec postgres psql -U facade -d facade -c 'select s3_path, created_at from models_3d order by created_at desc limit 3'`. A reconstruction ODM finished but nobody stored can be recovered from its task directory: `docker compose exec nodeodm ls /var/www/data/<task-uuid>/odm_texturing/`. |
| Login fails with a fresh token from the CLI | `JWT_SECRET_KEY` differs between the API and the worker, or the `.env` was changed after the containers started: `docker compose up -d` to re-read it. |
| `alembic: can't find revision` | The migration file is on the host but the container has a stale bind mount: `docker compose restart api`. |
| A name or dependency the frontend cannot resolve after pulling | The container keeps its own `node_modules` volume: `docker compose exec frontend npm install`, or `docker compose up -d --build frontend`. |
