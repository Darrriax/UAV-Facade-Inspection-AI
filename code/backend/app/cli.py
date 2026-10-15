"""Administrative CLI.

Bootstrapping the first Admin cannot go through the API: ``/users`` requires
being an Admin already, and the Admin it would need does not exist yet.
This module is that out-of-band path.

    docker compose run --rm api python -m app.cli create-user \\
        --email admin@example.com --password 'change-me' --role admin

    docker compose run --rm api python -m app.cli init-storage

Useful against a real deployment (e.g. Garage) where the compose bucket-init
container does not run.

``create-user --if-exists skip`` is what the compose ``bootstrap`` service calls:
since that container runs on every ``docker compose up``, creating the seed
accounts has to be a no-op on the second boot rather than an error.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from pydantic import ValidationError as PydanticValidationError

from app.core.config import settings
from app.core.exceptions import ConflictError, DomainError
from app.core.storage import ensure_storage_cors, get_image_storage, get_model_storage
from app.db.database import SessionLocal
from app.db.models import UserRole
from app.schemas.user import UserCreate
from app.services.user import UserService


def _cmd_create_user(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Password: ")
    try:
        payload = UserCreate(email=args.email, password=password, role=UserRole(args.role))
    except (PydanticValidationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        try:
            user = UserService(session).create(payload)
        except ConflictError as exc:
            # The account is already there. Only `skip` turns that into success:
            # an interactive caller asking for a specific user still wants to
            # hear that the email is taken.
            if args.if_exists == "skip":
                print(f"already exists: {exc}")
                return 0
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except DomainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print(f"created {user.role.value} <{user.email}> id={user.id}")
    return 0


def _cmd_init_storage(_: argparse.Namespace) -> int:
    """Create the buckets if they are missing and grant browser read access.

    Both steps are idempotent. The CORS rules are what let the browser fetch a
    presigned ``.glb`` itself; they are reapplied here so a deployment pointed at
    an existing bucket converges without restarting the API.
    """
    for name, storage in (
        (settings.S3_BUCKET_IMAGES, get_image_storage()),
        (settings.S3_BUCKET_MODELS, get_model_storage()),
    ):
        try:
            storage.ensure_bucket()
        except DomainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"bucket ready: {name}")

    try:
        changed = ensure_storage_cors()
    except DomainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    origins = ", ".join(settings.S3_CORS_ALLOWED_ORIGINS) or "(none)"
    for bucket, did_write in changed.items():
        state = "rules applied" if did_write else "rules already current"
        print(f"cors: {bucket} - {state} (origins: {origins})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user = subparsers.add_parser("create-user", help="Create an account")
    create_user.add_argument("--email", required=True)
    create_user.add_argument(
        "--password",
        default=None,
        help="Prompted for interactively when omitted, so it stays out of shell history.",
    )
    create_user.add_argument(
        "--role",
        choices=[role.value for role in UserRole],
        default=UserRole.INSPECTOR.value,
    )
    create_user.add_argument(
        "--if-exists",
        choices=["error", "skip"],
        default="error",
        help=(
            "What to do when the email is already registered. `skip` exits 0 and "
            "is what makes an automated bootstrap rerunnable."
        ),
    )
    create_user.set_defaults(func=_cmd_create_user)

    init_storage = subparsers.add_parser(
        "init-storage", help="Create the object-storage buckets if missing"
    )
    init_storage.set_defaults(func=_cmd_init_storage)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
