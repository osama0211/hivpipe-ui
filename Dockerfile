# The app container: web interface, Nextflow, and the structural check.
#
# It does not contain any assembly pipeline. Nextflow starts those in their
# own containers through the Docker socket mounted by docker-compose, which
# is why this image stays small and why the pipelines never share a
# dependency tree.

FROM python:3.12-slim

ARG NEXTFLOW_VERSION=24.10.4

ENV PYTHONUNBUFFERED=1 \
    NXF_HOME=/opt/nextflow \
    NXF_OFFLINE=false \
    PATH=/opt/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
        curl ca-certificates procps \
        docker.io openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Nextflow is pinned: a workflow that ran yesterday should run today.
RUN mkdir -p /opt/bin /opt/nextflow \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v${NEXTFLOW_VERSION}/nextflow-${NEXTFLOW_VERSION}-dist" \
       -o /opt/bin/nextflow \
    && chmod +x /opt/bin/nextflow \
    && chmod -R 777 /opt/nextflow

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py assistant.py diagnostics.py hpc.py resources.py qc.py settings.yaml ./
COPY main.nf nextflow.config ./
COPY modules/ ./modules/
COPY bin/ ./bin/
COPY assets/ ./assets/
COPY static/ ./static/

# The QC process runs in this same image; Nextflow puts bin/ on PATH, but
# a copy next to the module import path keeps it working either way.
RUN cp qc.py bin/qc.py && chmod +x bin/*.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/config')" || exit 1

CMD ["python", "app.py"]
