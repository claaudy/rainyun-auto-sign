FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN python3 -m pip install --no-cache-dir numpy==2.2.6 opencv-python-headless==4.12.0.88
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
USER 65534:65534
WORKDIR /tmp
ENTRYPOINT ["python3"]
