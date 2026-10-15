"""User request/response contracts.

``password_hash`` is never exposed: the read schema simply has no such field, so
it cannot leak through a response even by accident.

The profile block (names, country, age, phone) lives in a mixin shared by every
*write* contract, so an account an Admin creates and a profile its owner edits
later are validated by exactly the same rules. :class:`UserRead` lists its
fields explicitly instead of inheriting the mixin: a read schema must not carry
validators, or a row written by raw SQL would turn a GET into a 500.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import MAX_PASSWORD_BYTES
from app.db.models import UserRole

#: Country is stored as an ISO 3166-1 alpha-2 code, so the client can render the
#: name, flag and phone prefix from its own table instead of us shipping i18n
#: data. Enforced rather than free text: "Ukraine", "UA" and "uk" would
#: otherwise all end up in the column and break every later group-by.
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")

#: Deliberately permissive: what is stored is a display string, not a number we
#: do arithmetic on. The digit count (7..15, the ITU E.164 range) is the part
#: that actually matters.
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s\-().]{4,30}$")
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15

MIN_AGE = 0
MAX_AGE = 120


def _collapse(value: object) -> object:
    """Trim, and collapse internal runs of whitespace to a single space.

    Only strings are touched; anything else is returned untouched so the field
    type check still produces its own error. Blank input becomes ``None``
    because a cleared form field means "no value", and storing ``""`` next to
    ``NULL`` would break every later ``IS NULL`` check.
    """
    if not isinstance(value, str):
        return value
    cleaned = " ".join(value.split())
    return cleaned or None


def _check_password_length(value: str) -> str:
    """bcrypt counts bytes, not characters, and raises above 72.

    Turning that into a clean 422 with a useful message is why every write
    schema validates this before the service ever hashes anything.
    """
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return value


class UserBase(BaseModel):
    email: EmailStr
    role: UserRole = UserRole.INSPECTOR


class ProfileFields(BaseModel):
    """Editable profile block, shared by the write contracts.

    Every field is optional, so a PATCH only sends what actually changed and a
    profile can stay half-filled while the Inspector is onboarding.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = Field(default=None, max_length=100, examples=["Dmytro"])
    last_name: str | None = Field(default=None, max_length=100, examples=["Yakovenko"])
    country: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code, stored upper-case.",
        examples=["UA"],
    )
    age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE, examples=[22])
    phone_number: str | None = Field(
        default=None,
        max_length=32,
        description="Display string; 7-15 digits with optional '+', spaces and separators.",
        examples=["+380 67 123 45 67"],
    )

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _normalise_name(cls, value: object) -> object:
        # Collapsing runs of whitespace here also stops "  Anna  " from being
        # rejected by max_length on a name that is legitimately 8 characters.
        return _collapse(value)

    @field_validator("country", mode="after")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # No min/max length on the field itself, so that this message - rather
        # than a bare "at most 2 characters" - is what the form displays.
        collapsed = _collapse(value)
        if collapsed is None:
            return None
        # Up-cased rather than rejected: a lowercase code is the same country,
        # and normalising here is what keeps the column (and any later group-by
        # on it) consistent.
        code = str(collapsed).upper()
        if not COUNTRY_CODE_PATTERN.match(code):
            raise ValueError("country must be an ISO 3166-1 alpha-2 code, e.g. 'UA'")
        return code

    @field_validator("phone_number", mode="after")
    @classmethod
    def _normalise_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _collapse(value)
        if cleaned is None:
            return None
        if not isinstance(cleaned, str) or not PHONE_PATTERN.match(cleaned):
            raise ValueError("phone_number must be a phone number such as '+380 67 123 45 67'")
        digits = sum(character.isdigit() for character in cleaned)
        if not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS:
            raise ValueError(
                f"phone_number must contain between {MIN_PHONE_DIGITS} and "
                f"{MAX_PHONE_DIGITS} digits, got {digits}"
            )
        return cleaned


class UserCreate(UserBase, ProfileFields):
    """Payload for registering a user.

    ``extra="forbid"`` makes a client that tries to smuggle in ``id``,
    ``password_hash`` or any unknown key fail with 422 instead of having the
    field silently dropped.

    Names are accepted here because an Admin provisioning an Inspector account
    usually has at least a name; everything else can be filled in later from the
    profile page. ``is_active`` is absent on purpose - a freshly created account
    is always active.
    """

    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, value: str) -> str:
        return _check_password_length(value)


class UserProfileUpdate(ProfileFields):
    """Self-service profile edit (``PATCH /users/me``).

    Deliberately narrower than :class:`UserUpdate`: ``email``, ``role`` and
    ``is_active`` are absent because they are account-level attributes the UI
    keeps on the Admin surface. The path is account-scoped, so ``user_id`` is
    taken from the authenticated principal - never from the payload.

    ``profile_photo_s3_path`` is the object key returned by the photo upload
    flow; the service pins it to ``users/<id>/`` so a caller cannot point their
    avatar at somebody else's object.
    """

    password: str | None = Field(default=None, min_length=8, max_length=MAX_PASSWORD_BYTES)
    profile_photo_s3_path: str | None = Field(default=None, max_length=1024)

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_password_length(value)


class UserUpdate(UserProfileUpdate):
    """Partial update of an account, used by the Admin surface.

    Inherits the profile block and the password change, and adds the fields that
    decide what an account *is* rather than how it looks. ``role`` and
    ``is_active`` are re-checked in the service: a user who can PATCH their own
    row must not be able to promote themselves or challenge the deactivation.
    """

    email: EmailStr | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class UserRead(UserBase):
    """Public representation of a user."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    first_name: str | None = None
    last_name: str | None = None
    country: str | None = None
    age: int | None = None
    phone_number: str | None = None
    profile_photo_s3_path: str | None = None
    created_at: datetime
    updated_at: datetime
