"""Model3D business logic."""

from __future__ import annotations

import uuid
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.storage import StorageBackend, get_model_storage
from app.db.models import Model3D
from app.repositories.model3d import Model3DRepository
from app.schemas.model3d import Model3DRead

#: Object name a stored mesh carries inside its project's 3D prefix, and the
#: content type it is served with.
#:
#: A fixed basename rather than whatever the uploader called its file, for the
#: same reason the cover photo has one: a project has a single active model, so a
#: second build replaces the object instead of leaving the previous one behind as
#: an orphan nothing points at. The *rows* still accumulate - see
#: :meth:`latest_for_project` - so a re-run keeps its history, it just names the
#: same object.
MESH_OBJECT_NAME = "odm_textured_model_geo.glb"
#: What a ``.glb`` is served as: the type the viewer's loader expects, and the
#: one the worker's own upload declares.
MESH_CONTENT_TYPE = "model/gltf-binary"


class Model3DService:
    """Registration, lookup and delivery of reconstructed 3D models."""

    def __init__(self, session: Session, storage: StorageBackend | None = None) -> None:
        self.session = session
        self.models_3d = Model3DRepository(session)
        # The models bucket, not the images one: a reconstruction is a different
        # object class with its own lifecycle. Injectable, like everywhere else,
        # so tests never need a live S3.
        self.storage = storage if storage is not None else get_model_storage()

    def register(self, project_id: uuid.UUID, s3_path: str) -> Model3D:
        """Record a reconstructed mesh that has already been stored.

        The object is uploaded before this is called, so that a failed upload
        never leaves a row pointing at nothing.
        """
        prefix = f"projects/{project_id}/3d/"
        if not s3_path.startswith(prefix):
            raise ValidationError(f"3D model key must live under '{prefix}'.")

        model = self.models_3d.create({"project_id": project_id, "s3_path": s3_path})
        self.session.commit()
        return model

    def store_mesh(self, project_id: uuid.UUID, fileobj: BinaryIO) -> Model3D:
        """Store a finished mesh that was built outside the worker, and register it.

        The 3D page drives NodeODM from the browser, so the ``.glb`` it gets back
        exists in that tab's memory and nowhere else: without this call a reload
        loses the reconstruction and the page offers to build it again from
        scratch. This is the only way the bytes reach storage - the API never
        sees the NodeODM task, and the worker is not involved in that path.

        Object before row, in that order: a registered mesh that is not actually
        stored would be indistinguishable from one the viewer can load, while an
        orphaned object costs storage and nothing else.
        """
        key = f"projects/{project_id}/3d/{MESH_OBJECT_NAME}"
        fileobj.seek(0)
        self.storage.upload_stream(fileobj, key, MESH_CONTENT_TYPE)
        return self.register(project_id, key)

    def latest_for_project(self, project_id: uuid.UUID) -> Model3D | None:
        """The project's active mesh - the newest one, since a re-run adds a row
        rather than replacing the previous reconstruction."""
        return self.models_3d.latest_for_project(project_id)

    def present(self, model: Model3D) -> Model3DRead:
        """Attach a presigned URL for the mesh so a browser can load it.

        The viewer has no S3 credential: the API signs a URL for the .glb and
        the bytes go straight from the storage endpoint to the GPU.
        """
        return Model3DRead.model_validate(model).model_copy(
            update={
                "url": self.storage.presigned_url(model.s3_path),
                "url_expires_in": settings.S3_PRESIGNED_URL_TTL_SECONDS,
            }
        )
