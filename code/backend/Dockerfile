# Two images from one file:
#
#   target: api     -> FastAPI only
#   target: worker  -> FastAPI + torch/ultralytics/pyodm
#
# The split matters: the AI stack is ~2 GB and takes minutes to install. Only
# the worker runs inference, so making the API carry it would slow every rebuild
# of the service that is edited most often.
#
#   docker build --target api    -t facade-api    .
#   docker build --target worker -t facade-worker .

# --------------------------------------------------------------------- shared
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# psycopg 3 publishes manylinux wheels, so no compiler toolchain is needed.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# ------------------------------------------------------------------------ api
FROM base AS api

COPY . .
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --------------------------------------------------------------------- worker
FROM base AS worker

# System libraries the cv2 native module links against (verified with
# `ldd cv2.abi3.so`): libGL.so.1, libglib-2.0.so.0, libgthread-2.0.so.0 and
# libxcb.so.1. `opencv-python-headless` still needs these - "headless" means no
# GUI toolkit, not no system dependencies - and without them `import cv2` fails
# at task time, which would look like a model bug rather than a packaging one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch. Installing the default PyPI wheel would add the CUDA runtime
# (~2 GB) that this deployment never uses.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --index-url "${TORCH_INDEX_URL}" torch torchvision

COPY requirements-ml.txt ./
RUN pip install -r requirements-ml.txt

COPY . .

# ultralytics downloads font/asset files on first import in some versions; give
# the non-root user a writable home so that cannot fail at task time.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/model \
    && chown -R appuser:appuser /app
USER appuser

# Default command; docker-compose overrides it with the celery invocation.
CMD ["celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO"]
