# Stentor runtime image: Debian forky (Qt runtime via the qdmr package) + patched dmrconf from the
# stentor-dmrconf stage image + the Shack web UI (/ui).
#   docker build -t stentor:$(cat VERSION) .
ARG DMRCONF_IMAGE=stentor-dmrconf:0.15.1-p2
FROM ${DMRCONF_IMAGE} AS dmrconf
FROM debian:forky-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="Stentor" \
      org.opencontainers.image.description="Self-hosted web CPS for DMR radios and Pi-Star/WPSD hotspots (qdmr-based)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 QT_QPA_PLATFORM=offscreen PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends qdmr python3 python3-venv ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dmrconf /out/dmrconf /usr/local/bin/dmrconf
COPY --from=dmrconf /out/libdmrconf.so* /usr/local/lib/
RUN ldconfig && dmrconf --version
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY VERSION /app/VERSION
COPY stentor/ /app/stentor/
COPY serve_http.py /app/serve_http.py
WORKDIR /app
ENV PYTHONPATH=/app HOST=0.0.0.0 PORT=8000 DATA_DIR=/data BACKUP_DIR=/backups
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3)" || exit 1
CMD ["python", "/app/serve_http.py"]
