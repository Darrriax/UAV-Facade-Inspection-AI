"""Environment-driven application configuration (12-factor).

Every value is overridable through environment variables or a local ``.env``
file. ``docker-compose.yml`` injects the container-oriented values, so the
defaults here are meant for running the app directly on a developer machine.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    PROJECT_NAME: str = "Automated Building Facade Inspection System"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    # Comma-separated list or JSON array; the Vue dev server is allowed by
    # default. NoDecode stops pydantic-settings from JSON-parsing the raw env
    # value, so the validator below can accept `a,b` as well.
    BACKEND_CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:3000",
    ]

    # ----------------------------------------------------------- PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "facade"
    POSTGRES_PASSWORD: str = "facade"
    POSTGRES_DB: str = "facade"
    # Escape hatch: a complete DSN, e.g. postgresql+psycopg://user:pass@host:5432/db
    DATABASE_URL: str | None = None

    # -------------------------------------------------------- connection pool
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_ECHO: bool = False

    # ----------------------------------------------------------- Redis/Celery
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB_BROKER: int = 0
    REDIS_DB_RESULT: int = 1
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    # Run tasks inline (useful for tests) instead of dispatching to a worker.
    CELERY_TASK_ALWAYS_EAGER: bool = False

    # ------------------------------- object storage (Garage, S3-compatible)
    S3_ENDPOINT_URL: str = "http://localhost:3900"
    # Dev-only defaults matching docker-compose.yml. Garage requires the key id
    # to be "GK" followed by 12 hex-encoded bytes.
    S3_ACCESS_KEY_ID: str = "GK0123456789abcdef01234567"
    S3_SECRET_ACCESS_KEY: str = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    S3_REGION: str = "garage"
    # Endpoint used when *signing* URLs for the browser. Inside compose the API
    # reaches Garage as http://garage:3900, but that hostname means nothing to a
    # browser on the host, so presigned URLs would be quietly unusable. Empty
    # means "same as S3_ENDPOINT_URL".
    S3_PUBLIC_ENDPOINT_URL: str = ""
    S3_USE_SSL: bool = False
    S3_BUCKET_IMAGES: str = "facade-images"
    S3_BUCKET_MODELS: str = "facade-models"
    S3_PRESIGNED_URL_TTL_SECONDS: int = 3600
    # Browser origins allowed to read objects straight from the buckets, applied
    # as the buckets' CORS configuration at startup. This is a storage-side
    # concern and not the same as BACKEND_CORS_ORIGINS: an ``<img>`` tag renders
    # a cross-origin response without CORS at all, but the 3D viewer fetches the
    # ``.glb`` with ``fetch()`` (three.js GLTFLoader), and a cross-origin fetch
    # is only readable when the *bucket* answers with Access-Control-Allow-Origin.
    #
    # ``*`` stays safe because CORS is not what protects these objects: every URL
    # is presigned and short-lived, so an unsigned request is refused with 403
    # whatever its Origin. Narrow this to the deployed origin(s) for production.
    S3_CORS_ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = ["*"]

    # ------------------------------------------------------------- security
    # MUST be replaced in any non-local environment.
    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --------------------------------------------------------- AI inference
    # Trained YOLOv9-c segmentation weights (CUBIT-InSeg: crack, spalling).
    # The container's /app is the bind-mounted source tree, so this path is
    # code/backend/model/best.pt on a developer's host.
    YOLO_MODEL_PATH: str = "/app/model/best.pt"
    # "cpu" here, or "cuda" to run on a GPU - the same device string is handed
    # straight to `model.predict(device=...)`, which raises on a device that is
    # not actually present, so CUDA is opted into rather than probed for.
    YOLO_DEVICE: str = "cpu"
    # Inference parameters as validated experimentally by the project author.
    # They are deliberately NOT Ultralytics' defaults (0.25 / 0.5): defect
    # detection on the facade frames was tuned at these values, and the whole
    # pipeline and the site's reported results depend on them.
    YOLO_CONFIDENCE_THRESHOLD: float = 0.01
    YOLO_IOU_THRESHOLD: float = 0.7
    YOLO_IMAGE_SIZE: int = 1280
    #: Decode masks back to the *original* frame resolution instead of the
    #: (letterboxed) inference size, so the polygons stored and drawn follow the
    #: real defect boundary rather than the network input scale.
    YOLO_RETINA_MASKS: bool = True
    #: Upper bound per frame, so one pathological prediction cannot flood the DB.
    YOLO_MAX_DETECTIONS_PER_IMAGE: int = 300
    #: When the weights are missing, emit synthetic defects instead of failing
    #: the batch. Intended for pipeline testing before best.pt is available.
    YOLO_ALLOW_SYNTHETIC_FALLBACK: bool = True

    # ------------------------------------------------ photogrammetry (NodeODM)
    NODEODM_ENABLED: bool = True
    NODEODM_HOST: str = "nodeodm"
    NODEODM_PORT: int = 3000
    #: Per-HTTP-request timeout; image uploads are several MB each.
    NODEODM_REQUEST_TIMEOUT_SECONDS: int = 120
    #: Overall reconstruction budget. The reduced profile below is minutes on a
    #: few dozen frames, but a larger set can run far longer.
    NODEODM_TASK_TIMEOUT_SECONDS: int = 14400
    #: Photogrammetry needs overlapping frames; below this a reconstruction is
    #: doomed, so the step is skipped rather than burning an hour to fail.
    NODEODM_MIN_IMAGES: int = 3
    # Fast/low-memory profile. NodeODM is memory-hungry: full-quality dense
    # reconstruction on a GPU-less host will exhaust RAM long before it finishes.
    #
    # `medium` and not the cheapest tier, measured on a 3-frame facade set:
    # `lowest` extracted ~150 SIFT features per frame and matched 0 pairs, `low`
    # also aborted with "could not process this dataset", and `medium`
    # reconstructed it in well under a minute. Cheaper settings therefore do not
    # save time - they make the 3D step a silent no-op. Raise this for
    # photogrammetry-grade output; lower it only for datasets with generous
    # overlap, and after checking a reconstruction actually completes.
    NODEODM_FEATURE_QUALITY: str = "medium"
    NODEODM_PC_QUALITY: str = "lowest"
    NODEODM_MESH_SIZE: int = 50_000
    # Only the 3D asset is wanted, so the orthophoto and the PDF report are
    # skipped - they are pure extra work for this pipeline.
    #
    # Deliberately NOT the `fast-orthophoto` option that would seem to express
    # the same intent: ODM implements it by setting `skip_3dmodel = True`
    # ("Fast orthophoto is turned on, automatically setting --skip-3dmodel"),
    # so enabling it would guarantee that no textured mesh is ever produced.
    NODEODM_SKIP_ORTHOPHOTO: bool = True
    NODEODM_SKIP_REPORT: bool = True
    #: Convert the textured OBJ (+ .mtl + texture images) into a single
    #: self-contained .glb before upload. When the conversion is unavailable the
    #: OBJ and its sidecars are uploaded instead - a completed reconstruction is
    #: never thrown away because of a converter problem (see PhotogrammetryService).
    NODEODM_EXPORT_GLB: bool = True
    #: NodeODM asset name for the textured mesh inside the downloaded output.
    NODEODM_MESH_FILENAME: str = "odm_textured_model_geo.obj"

    # ------------------------------------------------- pipeline recovery
    #: A run claims a frame by flipping it to `processing`, and a worker that
    #: dies mid-run (the OOM killer, a `docker compose restart`, a hard stop)
    #: cannot hand its claim back - the row stays `processing` with nobody
    #: working on it. Age is the only evidence available, so a frame claimed
    #: longer ago than this is treated as orphaned and requeued when the project
    #: is started again.
    #:
    #: Deliberately far longer than any legitimate claim: a frame sits in
    #: `processing` for its download plus its inference, and a whole batch is
    #: claimed up front, so the last frame of a large sortie can wait minutes
    #: after the first. Reclaiming a frame that is genuinely being analysed would
    #: have two runs store results for it.
    PROCESSING_STALE_AFTER_SECONDS: int = 30 * 60

    # ------------------------------------------------------------ bulk upload
    # Bounds on a single multipart upload request. A UAV sortie produces
    # hundreds of high-resolution frames, so the limits are generous but finite:
    # an unbounded request is a cheap denial-of-service.
    UPLOAD_MAX_FILES_PER_REQUEST: int = 100
    UPLOAD_MAX_FILE_BYTES: int = 25 * 1024 * 1024  # 25 MiB
    #: A reconstructed mesh is one object per project rather than hundreds, and a
    #: textured building can run to tens of megabytes, so it gets its own ceiling
    #: instead of the per-frame one - which would reject a perfectly good model.
    UPLOAD_MAX_MODEL_BYTES: int = 256 * 1024 * 1024  # 256 MiB

    # ------------------------------------------------------------ validators
    @field_validator("BACKEND_CORS_ORIGINS", "S3_CORS_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_origin_list(cls, value: object) -> object:
        """Accept ``a,b`` as well as a JSON array from the environment."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    # ------------------------------------------------------------ computed
    @property
    def database_url(self) -> str:
        """Resolved SQLAlchemy DSN (explicit ``DATABASE_URL`` wins)."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        # URL.create escapes user/password correctly, unlike string interpolation.
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        ).render_as_string(hide_password=False)

    @property
    def celery_broker_url(self) -> str:
        return self.CELERY_BROKER_URL or (
            f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB_BROKER}"
        )

    @property
    def celery_result_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or (
            f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB_RESULT}"
        )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (also usable as a FastAPI dependency)."""
    return Settings()


settings = get_settings()
