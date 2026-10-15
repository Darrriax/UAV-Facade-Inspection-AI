"""Photogrammetry via NodeODM (OpenDroneMap).

Turns a set of overlapping frames into a textured 3D mesh. NodeODM is the
memory-hungry part of the pipeline, so the request asks for the cheapest
reconstruction profile that still yields a textured model, and the whole step is
treated as *optional*: a failure here must not undo the defect analysis, which
is the thesis's primary output.

pyodm handles the multipart upload, the option encoding and the API-version
handshake, which is why it is used instead of hand-rolled HTTP calls.

Asset format: ODM emits a textured ``.obj`` with an ``.mtl`` and one or more
texture images. Those are converted to a single self-contained ``.glb`` before
upload (that is the ``Model3D`` contract), and the OBJ trio is uploaded only if
the conversion cannot run - an hour of reconstruction must not be lost to a
non-essential converter step.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.exceptions import StorageError
from app.core.storage import StorageBackend, get_model_storage

logger = logging.getLogger(__name__)

#: Sidecar files a textured OBJ needs to be renderable. Only used on the
#: fallback path: the .obj alone would produce a mesh with no material and no
#: texture image, so the trio has to stay together.
MESH_SIDECAR_SUFFIXES: tuple[str, ...] = (".obj", ".mtl", ".png", ".jpg", ".jpeg")

#: ODM's full-3D texturing output. The 2.5D alternative lives in
#: ``odm_texturing_25d`` and is a *planar* mesh - not what the Model3D table is
#: for - so it is only ever used as a last resort, and with a warning.
MESH_3D_STAGE = "odm_texturing"
MESH_25D_STAGE = "odm_texturing_25d"


class PhotogrammetryError(Exception):
    """NodeODM was unreachable, rejected the task, or produced no mesh."""


class ProcessingStopped(Exception):
    """The user stopped the project while a reconstruction was running.

    Separate from :class:`PhotogrammetryError` because it is not a failure: the
    reconstruction is cancelled on purpose, and no output is expected.
    """


class PhotogrammetryService:
    """Client + asset handling for one NodeODM instance."""

    def __init__(
        self,
        storage: StorageBackend | None = None,
        *,
        node: Any | None = None,
    ) -> None:
        self.storage = storage if storage is not None else get_model_storage()
        # Injectable so the client logic can be tested without a live server.
        self._node = node
        # Why the last ``reconstruct`` returned no mesh, so the worker can show
        # the user the real cause instead of a guess.
        self.last_error: str | None = None

    # ------------------------------------------------------------------ client
    def _connect(self) -> Any:
        if self._node is not None:
            return self._node
        try:
            from pyodm import Node
        except ImportError as exc:  # pragma: no cover - only if ML deps are absent
            raise PhotogrammetryError("pyodm is not installed in this image") from exc

        node = Node(
            settings.NODEODM_HOST,
            settings.NODEODM_PORT,
            timeout=settings.NODEODM_REQUEST_TIMEOUT_SECONDS,
        )
        self._node = node
        return node

    def is_available(self) -> bool:
        """Cheap reachability probe, so a missing service is a log line not a stack trace."""
        try:
            self._connect().info()
            return True
        except Exception as exc:  # noqa: BLE001 - any transport error means "not available"
            logger.warning(
                "NodeODM at %s:%s is not reachable (%s)",
                settings.NODEODM_HOST,
                settings.NODEODM_PORT,
                exc,
            )
            return False

    @staticmethod
    def reconstruction_options() -> dict[str, Any]:
        """The fast/low-memory option set sent to NodeODM.

        pyodm encodes this as ``[{"name": k, "value": v}, ...]``; every key must
        be a real ODM option, because NodeODM forwards them to the ODM command
        line, where an unknown name is a hard failure rather than a warning.

        NodeODM is the memory bottleneck: default settings run a full dense
        reconstruction that will exhaust a workstation's RAM, so feature and
        point-cloud quality are pinned to the lowest tiers and the output mesh
        is capped. ``skip-orthophoto``/``skip-report`` drop the two products this
        pipeline does not consume (the orthophoto and ODM's own PDF report),
        which is where most of the saved time comes from.

        Note what is *not* here: ``fast-orthophoto``, whose name suggests exactly
        these savings. ODM implements it by setting ``skip_3dmodel``, which would
        mean no textured mesh at all - the one product this method exists to
        obtain.
        """
        return {
            "feature-quality": settings.NODEODM_FEATURE_QUALITY,
            "pc-quality": settings.NODEODM_PC_QUALITY,
            "mesh-size": settings.NODEODM_MESH_SIZE,
            "skip-orthophoto": settings.NODEODM_SKIP_ORTHOPHOTO,
            "skip-report": settings.NODEODM_SKIP_REPORT,
        }

    # ------------------------------------------------------------------ public
    def reconstruct(
        self,
        project_id: Any,
        image_paths: Sequence[Path],
        workdir: Path,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str | None:
        """Run a reconstruction and upload the resulting model.

        Returns the S3 key of the uploaded asset, or ``None`` when the step was
        skipped or failed.

        **This method never raises.** Every failure mode - NodeODM disabled,
        unreachable, too few frames, rejected task, timeout, an output with no
        textured mesh, a failed conversion, a storage error - is logged and
        reported as ``None``. The caller runs the 3D step after the defect
        analysis has been committed, and a failed optional step must not turn a
        successful analysis run into a failed task.

        ``should_stop`` is polled while waiting on NodeODM: when it returns true
        the reconstruction is cancelled and ``None`` is returned. Without it a
        stop pressed during this phase would hold the worker (and the machine)
        for the rest of an hour-long reconstruction.

        ``on_progress`` is called with NodeODM's own completion percentage
        (0-100) whenever it changes, and is the only way the API can show how far
        a run has got: the percentage exists inside ODM's process, and this is
        the one place that asks for it. Exceptions it raises are not caught here
        - it is the caller's own reporting code, and swallowing its bugs would
        hide the *reason* a progress bar never moved.
        """
        if not settings.NODEODM_ENABLED:
            logger.info("NodeODM disabled (NODEODM_ENABLED=false); skipping 3D reconstruction")
            return None

        if len(image_paths) < settings.NODEODM_MIN_IMAGES:
            logger.info(
                "Skipping 3D reconstruction for project %s: %d image(s), %d required",
                project_id,
                len(image_paths),
                settings.NODEODM_MIN_IMAGES,
            )
            return None

        if not self.is_available():
            return None

        self.last_error = None
        node = self._connect()
        options = self.reconstruction_options()
        logger.info(
            "Submitting %d image(s) for project %s to NodeODM with options %s",
            len(image_paths),
            project_id,
            options,
        )

        try:
            task = node.create_task([str(path) for path in image_paths], options=options)
        except Exception as exc:  # noqa: BLE001 - transport/validation failure
            logger.error("NodeODM rejected the task for project %s: %s", project_id, exc)
            self.last_error = f"NodeODM rejected the task: {exc}"
            return None

        logger.info("NodeODM task %s created for project %s", task.uuid, project_id)

        try:
            self._wait_for_completion(task, project_id, should_stop, on_progress)
            output_dir = self._download(task, workdir)
        except ProcessingStopped:
            # Cancelled on purpose: there is no output to fetch, and this is not a
            # failure of the pipeline - the frames keep their results and only the
            # optional mesh is missing.
            self._remove_task(task)
            return None
        except PhotogrammetryError as exc:
            logger.error("3D reconstruction failed for project %s: %s", project_id, exc)
            self.last_error = f"3D reconstruction failed: {exc}"
            self._remove_task(task)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected NodeODM failure for project %s: %s", project_id, exc)
            self.last_error = f"NodeODM failed: {exc}"
            self._remove_task(task)
            return None

        self._remove_task(task)

        try:
            return self._upload_mesh(project_id, output_dir)
        except StorageError as exc:
            logger.error("Could not store the 3D model for project %s: %s", project_id, exc)
            self.last_error = f"The model could not be stored: {exc}"
            return None
        except PhotogrammetryError as exc:
            # ODM finished but produced nothing usable (a dataset that solved no
            # camera poses, for instance). That is a failed *optional* step.
            logger.error("3D reconstruction for project %s produced no mesh: %s", project_id, exc)
            self.last_error = f"NodeODM produced no mesh: {exc}"
            return None
        except Exception as exc:  # noqa: BLE001 - "never raises" is this method's contract
            logger.exception("Unexpected failure while storing the 3D model for %s", project_id)
            self.last_error = f"Storing the 3D model failed: {exc}"
            return None

    # ------------------------------------------------------------------ stages
    def _wait_for_completion(
        self,
        task: Any,
        project_id: Any,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        """Block until NodeODM finishes, enforcing an overall deadline.

        pyodm's own ``max_retries``/``retry_timeout`` govern transient network
        errors, not total runtime, so the deadline is enforced from the status
        callback - otherwise a stuck task would pin a Celery worker forever. The
        same callback is where a user's stop is noticed: it is the only code that
        runs while the reconstruction is in progress. It is also the only place
        that sees ODM's percentage, hence ``on_progress``.
        """
        started = time.monotonic()
        deadline = started + settings.NODEODM_TASK_TIMEOUT_SECONDS
        last_progress = -1

        def on_status(info: Any) -> None:
            nonlocal last_progress
            progress = getattr(info, "progress", None)
            if progress is not None and int(progress) != last_progress:
                last_progress = int(progress)
                logger.info(
                    "Project %s: NodeODM progress %s%% (status %s)",
                    project_id,
                    last_progress,
                    getattr(info, "status", "?"),
                )
                if on_progress is not None:
                    on_progress(last_progress)
            if should_stop is not None and should_stop():
                logger.info("Project %s: stop requested; cancelling the reconstruction", project_id)
                self._cancel(task, project_id)
                raise ProcessingStopped("stopped by the user")
            if time.monotonic() > deadline:
                # Cancel before giving up: removing the task does not stop ODM,
                # which would keep the CPU and the RAM busy long after the worker
                # has moved on.
                self._cancel(task, project_id)
                raise PhotogrammetryError(
                    f"timed out after {settings.NODEODM_TASK_TIMEOUT_SECONDS}s "
                    "(NODEODM_TASK_TIMEOUT_SECONDS); the NodeODM task was cancelled"
                )

        task.wait_for_completion(status_callback=on_status)

    @staticmethod
    def _cancel(task: Any, project_id: Any) -> None:
        """Tell NodeODM to abandon a reconstruction. Never raises.

        ``cancel`` rather than only ``remove``: cancelling stops the ODM process
        (which is what frees the CPU and the gigabytes of RAM), while removing
        only deletes the task afterwards.
        """
        try:
            cancelled = task.cancel()
            logger.info(
                "NodeODM task %s for project %s: cancel %s",
                getattr(task, "uuid", "?"),
                project_id,
                "accepted" if cancelled else "refused (already finished?)",
            )
        except Exception:  # noqa: BLE001 - cleanup only, never worth raising
            logger.warning(
                "Could not cancel the NodeODM task for project %s", project_id, exc_info=True
            )

    def _download(self, task: Any, workdir: Path) -> Path:
        # `download_assets` fetches all.zip and extracts it into the destination.
        output_dir = workdir / "odm_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        task.download_assets(str(output_dir))
        logger.info("NodeODM assets downloaded to %s", output_dir)
        return output_dir

    def _upload_mesh(self, project_id: Any, output_dir: Path) -> str:
        """Upload the reconstructed model and return its object key.

        The preferred asset is a single self-contained ``.glb``, because that is
        what the ``Model3D`` contract and the Three.js viewer expect: one object
        that renders on its own. NodeODM asks ODM for one directly (it runs ODM
        with ``--gltf``), so usually nothing has to be converted; when it is
        absent the OBJ is converted here, and when *that* is impossible the
        textured OBJ with its sidecars is stored instead. Each step down is
        logged, and none of them loses a finished reconstruction.
        """
        asset = self._find_mesh_asset(output_dir)
        if asset is None:
            raise PhotogrammetryError(
                f"no textured mesh ({settings.NODEODM_MESH_FILENAME}) in the NodeODM output"
            )

        prefix = f"projects/{project_id}/3d"

        # ODM's own GLB: no conversion needed, so ``NODEODM_EXPORT_GLB`` does not
        # apply - it only governs converting an OBJ ourselves.
        if asset.suffix.lower() == ".glb":
            key = f"{prefix}/{asset.name}"
            self._upload_file(asset, key, "model/gltf-binary")
            logger.info("Uploaded 3D model for project %s: %s", project_id, key)
            return key

        glb = self._to_glb(asset) if settings.NODEODM_EXPORT_GLB else None
        if glb is not None:
            key = f"{prefix}/{glb.name}"
            self._upload_file(glb, key, "model/gltf-binary")
            logger.info("Uploaded 3D model for project %s: %s", project_id, key)
            return key

        uploaded: list[str] = []
        for path in self._mesh_files(asset):
            key = f"{prefix}/{path.name}"
            self._upload_file(path, key, self._content_type(path.suffix))
            uploaded.append(key)

        mesh_key = f"{prefix}/{asset.name}"
        logger.info("Uploaded 3D model for project %s: %s", project_id, mesh_key)
        logger.debug("3D model sidecars uploaded: %s", uploaded)
        return mesh_key

    def _upload_file(self, path: Path, key: str, content_type: str) -> None:
        with path.open("rb") as handle:
            self.storage.upload_stream(handle, key, content_type)

    @staticmethod
    def _to_glb(mesh: Path) -> Path | None:
        """Convert the textured OBJ into a self-contained GLB, or ``None``.

        Returning ``None`` rather than raising is the contract: this runs after a
        reconstruction that may have taken tens of minutes, so a converter that
        cannot handle a particular mesh must not cost the caller that result.
        The caller falls back to uploading the OBJ and its sidecars.

        ``trimesh`` is imported inside the function for the same reason
        ``ultralytics`` is in ``InferenceService``: the API image does not install
        the ML stack, and this module has to stay importable there.
        """
        try:
            import trimesh

            # force="scene" keeps the material/texture binding from the .mtl -
            # that binding is what makes the exported GLB self-contained.
            scene = trimesh.load(mesh, file_type="obj", force="scene")
            payload = scene.export(file_type="glb") if scene is not None else None
        except Exception as exc:  # noqa: BLE001 - any converter failure means "no GLB"
            logger.warning(
                "Could not convert %s to GLB (%s); uploading the OBJ and its "
                "sidecars instead",
                mesh.name,
                exc,
            )
            return None

        if not payload:
            logger.warning(
                "GLB conversion of %s produced no data; uploading the OBJ instead",
                mesh.name,
            )
            return None

        target = mesh.with_suffix(".glb")
        target.write_bytes(payload)
        logger.info("Converted %s into %s (%d bytes)", mesh.name, target.name, len(payload))
        return target

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _find_mesh_asset(output_dir: Path) -> Path | None:
        """Locate the most usable mesh in the extracted ODM output.

        The order matters and is explicit, because ODM writes *two* textured
        meshes when the orthophoto stage runs: the real 3D one in
        ``odm_texturing/`` and a 2.5D (planar) one in ``odm_texturing_25d/``.
        A plain recursive glob would pick between them by filesystem order.

        1. the full-3D ``.glb`` (self-contained, no conversion needed)
        2. the full-3D ``.obj`` (+ its .mtl and texture)
        3. anything else, including the 2.5D mesh - loudly, as a last resort
        """
        stem = Path(settings.NODEODM_MESH_FILENAME).stem
        for candidate in (
            output_dir / MESH_3D_STAGE / f"{stem}.glb",
            output_dir / MESH_3D_STAGE / settings.NODEODM_MESH_FILENAME,
        ):
            if candidate.is_file():
                return candidate

        # Sorted, so the choice is reproducible across runs.
        for pattern in ("*.glb", "*.obj"):
            found = sorted(output_dir.rglob(pattern))
            if found:
                if MESH_25D_STAGE in str(found[0]):
                    logger.warning(
                        "Only ODM's 2.5D mesh (%s) is available - it is a planar "
                        "model, not a full 3D reconstruction",
                        found[0],
                    )
                return found[0]
        return None

    @staticmethod
    def _mesh_files(mesh: Path) -> list[Path]:
        stem = mesh.stem
        files = [
            sibling
            for sibling in mesh.parent.iterdir()
            if sibling.is_file()
            and sibling.suffix.lower() in MESH_SIDECAR_SUFFIXES
            and sibling.stem.startswith(stem.split(".")[0])
        ]
        # Always include the mesh itself, even if the stem heuristic missed it.
        if mesh not in files:
            files.append(mesh)
        return sorted(set(files))

    @staticmethod
    def _content_type(suffix: str) -> str:
        return {
            ".glb": "model/gltf-binary",
            ".obj": "model/obj",
            ".mtl": "model/mtl",
            ".png": "image/png",
        }.get(suffix.lower(), "image/jpeg")

    @staticmethod
    def _remove_task(task: Any) -> None:
        """Free the NodeODM disk/queue slot; failure here is not worth raising."""
        try:
            task.remove()
        except Exception:  # noqa: BLE001 - cleanup only
            logger.warning("Could not remove NodeODM task %s", getattr(task, "uuid", "?"))


__all__ = ["ProcessingStopped", "PhotogrammetryError", "PhotogrammetryService"]
