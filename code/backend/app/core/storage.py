"""Object storage on top of the S3 API (Garage, self-hosted and S3-compatible).

Only the operations the application needs are exposed, so the rest of the code
never touches boto3 directly and can be tested against a fake implementation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.core.exceptions import StorageError

logger = logging.getLogger(__name__)

#: Methods a browser needs on an object: GET for the bytes, HEAD for the size
#: and content type before the download starts.
CORS_ALLOWED_METHODS: tuple[str, ...] = ("GET", "HEAD")

#: Clip the CORS rule's lifetime so a tightened origin list takes effect within
#: the hour instead of whenever the browser's cache feels like it.
CORS_MAX_AGE_SECONDS: int = 3600

#: Content types we are willing to declare to S3. The client-supplied value is
#: never forwarded, so a spoofed upload cannot end up stored (and later served)
#: as, say, ``text/html``.
CONTENT_TYPE_SUFFIX: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}

#: Fallback when a client sends a generic content type. Matched against the
#: *sanitised* suffix of the uploaded filename only.
ALLOWED_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
)


class StorageBackend(Protocol):
    """Structural type the services depend on (easy to fake in tests)."""

    def upload_stream(self, fileobj: BinaryIO, key: str, content_type: str) -> str: ...

    def download_to(self, key: str, destination: Path) -> Path: ...

    def delete(self, key: str) -> None: ...

    def presigned_url(self, key: str, expires_in: int | None = None) -> str: ...


class ObjectStorage:
    """Thin wrapper over an S3-compatible bucket."""

    def __init__(
        self,
        client: Any,
        bucket: str,
        signing_client: Any | None = None,
    ) -> None:
        self.client = client
        self.bucket = bucket
        # A separate client is used purely to sign URLs, so the host baked into
        # them is one the browser can actually resolve.
        self.signing_client = signing_client if signing_client is not None else client

    def upload_stream(self, fileobj: BinaryIO, key: str, content_type: str) -> str:
        """Upload ``fileobj`` to ``key`` and return the key.

        ``upload_fileobj`` streams in chunks instead of buffering the whole
        frame, which matters when a single UAV image is tens of megabytes.
        """
        try:
            self.client.upload_fileobj(
                fileobj,
                self.bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not store object '{key}': {exc}") from exc
        return key

    def download_to(self, key: str, destination: Path) -> Path:
        """Fetch an object to a local path (the worker's scratch directory)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, str(destination))
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not download object '{key}': {exc}") from exc
        return destination

    def delete(self, key: str) -> None:
        """Best-effort delete, used to roll back a partially stored batch."""
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not delete object '{key}': {exc}") from exc

    def presigned_url(self, key: str, expires_in: int | None = None) -> str:
        """Time-limited GET URL, so image bytes never proxy through the API."""
        try:
            return self.signing_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in or settings.S3_PRESIGNED_URL_TTL_SECONDS,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not sign a URL for '{key}': {exc}") from exc

    def object_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False
        except BotoCoreError as exc:
            raise StorageError(f"Could not inspect object '{key}': {exc}") from exc

    def ensure_bucket(self) -> None:
        """Create the bucket when missing (idempotent)."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except (BotoCoreError, ClientError) as exc:
                raise StorageError(f"Could not create bucket '{self.bucket}': {exc}") from exc

    def ensure_cors(self, allowed_origins: Sequence[str]) -> bool:
        """Let the given browser origins read objects in this bucket.

        A presigned URL authorises the *request*, not the JavaScript that made
        it: a cross-origin ``fetch()`` of a ``.glb`` is unreadable unless the
        response carries ``Access-Control-Allow-Origin``, so the bucket - not
        the API - has to answer this. An ``<img>`` tag needs none of it, which
        is why the frames worked while the model did not.

        Idempotent: the stored rules are read first and written only when they
        differ. Returns whether a write happened. Garage implements CORS as a
        per-bucket S3 sub-resource (``PutBucketCors``); it has no equivalent in
        ``garage.toml``, so this is the only way to set it.
        """
        if not allowed_origins:
            return False

        desired = _cors_rule(list(allowed_origins))
        try:
            existing = self.client.get_bucket_cors(Bucket=self.bucket).get("CORSRules", [])
        except (BotoCoreError, ClientError):
            # No rules configured yet is the normal first-run answer; anything
            # else shows up again on the write below, with a better message.
            existing = []

        if _rules_equal(existing, [desired]):
            return False

        try:
            self.client.put_bucket_cors(
                Bucket=self.bucket, CORSConfiguration={"CORSRules": [desired]}
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not set CORS on bucket '{self.bucket}': {exc}") from exc
        return True


def _cors_rule(allowed_origins: list[str]) -> dict[str, Any]:
    """The single rule both buckets get: read-only, any header, cached."""
    return {
        "AllowedOrigins": allowed_origins,
        # Only read methods, so a leaked URL can never be turned into a write.
        "AllowedMethods": list(CORS_ALLOWED_METHODS),
        "AllowedHeaders": ["*"],
        # A cross-origin reader can see the content type without a HEAD of its
        # own, which is how a loader learns whether it got a mesh or an error.
        "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
        "MaxAgeSeconds": CORS_MAX_AGE_SECONDS,
    }


def _rules_equal(existing: Sequence[dict[str, Any]], desired: Sequence[dict[str, Any]]) -> bool:
    """Compare CORS configurations ignoring key and element order.

    Garage is free to return the rule's keys in any order, so a naive ``==``
    would report a difference on every boot and rewrite the bucket each time.
    """
    if len(existing) != len(desired):
        return False

    def signature(rule: dict[str, Any]) -> tuple[Any, ...]:
        return (
            tuple(sorted(rule.get("AllowedOrigins", []))),
            tuple(sorted(rule.get("AllowedMethods", []))),
            tuple(sorted(rule.get("AllowedHeaders", []))),
            tuple(sorted(rule.get("ExposeHeaders", []))),
            rule.get("MaxAgeSeconds"),
        )

    return sorted(signature(rule) for rule in existing) == sorted(
        signature(rule) for rule in desired
    )


def build_client(endpoint_url: str | None = None) -> Any:
    client: Any = boto3.client(
        "s3",
        endpoint_url=endpoint_url or settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION,
        use_ssl=settings.S3_USE_SSL,
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            # Garage serves path-style addressing; virtual-host style would try
            # to resolve <bucket>.garage, which does not exist in compose.
            s3={"addressing_style": "path"},
        ),
    )
    return client


# Cached **per process**. A client must never be shared across a fork: the
# Celery worker uses the prefork pool, and an inherited client would reuse a
# socket handle and TLS state belonging to the parent process.
_clients: dict[str, Any] = {}
_clients_pid: int | None = None


def get_s3_client(*, public: bool = False) -> Any:
    """Return the cached client, rebuilding it after a fork or a config change."""
    global _clients, _clients_pid
    pid = os.getpid()
    if _clients_pid != pid:
        _clients = {}
        _clients_pid = pid

    key = "public" if public else "internal"
    if key not in _clients:
        endpoint = (
            settings.S3_PUBLIC_ENDPOINT_URL or settings.S3_ENDPOINT_URL
            if public
            else settings.S3_ENDPOINT_URL
        )
        _clients[key] = build_client(endpoint)
    return _clients[key]


def _signing_client() -> Any | None:
    """A public-endpoint client, or ``None`` when no separate endpoint is set."""
    if not settings.S3_PUBLIC_ENDPOINT_URL:
        return None
    return get_s3_client(public=True)


def get_image_storage() -> ObjectStorage:
    """Storage handle for the inspection-images bucket."""
    return ObjectStorage(
        get_s3_client(), settings.S3_BUCKET_IMAGES, signing_client=_signing_client()
    )


def get_model_storage() -> ObjectStorage:
    """Storage handle for the 3D (.glb) bucket."""
    return ObjectStorage(
        get_s3_client(), settings.S3_BUCKET_MODELS, signing_client=_signing_client()
    )


def ensure_storage_cors(origins: Sequence[str] | None = None) -> dict[str, bool]:
    """Apply the browser-facing CORS rules to both buckets.

    Returns ``{bucket: changed}``. Raises :class:`StorageError` when a bucket
    refuses the configuration, so callers can decide whether that is fatal - at
    API startup it is not: a stack that cannot reach the object store still
    serves every other endpoint, and only the cross-origin ``.glb`` fetch is
    affected.
    """
    allowed = list(settings.S3_CORS_ALLOWED_ORIGINS if origins is None else origins)
    return {
        storage.bucket: storage.ensure_cors(allowed)
        for storage in (get_image_storage(), get_model_storage())
    }


def apply_storage_cors() -> None:
    """Best-effort :func:`ensure_storage_cors` for the application lifespan.

    Runs on every boot because the alternative is a bowl of silent CORS errors
    in the browser the first time someone opens a project with a 3D model. The
    write is idempotent, so a correctly configured bucket costs one GET.
    """
    if not settings.S3_CORS_ALLOWED_ORIGINS:
        return
    try:
        changed = ensure_storage_cors()
    except StorageError as exc:
        logger.warning("Could not apply the bucket CORS rules: %s", exc)
        return

    written = [bucket for bucket, did_write in changed.items() if did_write]
    if written:
        logger.info(
            "Applied bucket CORS for %s (origins: %s)",
            ", ".join(written),
            ", ".join(settings.S3_CORS_ALLOWED_ORIGINS),
        )


def image_object_key(project_id: Any, image_id: Any, suffix: str) -> str:
    """Canonical key layout: ``projects/<project>/images/<image><ext>``.

    Prefixing with the project id lets bucket policies and lifecycle rules act
    per project, and keeps keys collision-free without a global index.
    """
    return f"projects/{project_id}/images/{image_id}{suffix}"


def cover_object_key(project_id: Any, suffix: str) -> str:
    """Canonical key of a project's cover photo: ``projects/<project>/cover<ext>``.

    A fixed basename rather than a generated id: a project has exactly one
    cover, so a re-upload replaces the object instead of accumulating copies.
    The key stays under the project's own prefix, which is the invariant
    ``PATCH /projects/{id}`` enforces on ``main_photo_s3_path``.
    """
    return f"projects/{project_id}/cover{suffix}"
