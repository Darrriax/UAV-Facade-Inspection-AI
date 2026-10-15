"""3D-model response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Model3DRead(BaseModel):
    """A reconstructed mesh, ready to render.

    ``s3_path`` is the stored object key and ``url`` is a short-lived signed URL
    for the same object: the viewer needs the bytes, and a browser cannot
    authenticate against the S3 endpoint on its own. The key is still returned
    because it is the stable identity of the asset, while the URL expires.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    s3_path: str
    created_at: datetime
    updated_at: datetime

    #: Presigned GET URL of the ``.glb`` (or the OBJ fallback asset).
    url: str = ""
    #: Lifetime of ``url`` in seconds.
    url_expires_in: int = 0
