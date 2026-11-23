# HIVGenoPipe is itself a Nextflow pipeline, so this image carries Nextflow
# plus the pipeline source. Its own processes resolve through conda inside.

FROM mambaorg/micromamba:1.5.8

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates procps curl openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ARG NEXTFLOW_VERSION=24.10.4
RUN curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v${NEXTFLOW_VERSION}/nextflow-${NEXTFLOW_VERSION}-dist" \
      -o /usr/local/bin/nextflow && chmod +x /usr/local/bin/nextflow

RUN git clone --depth 1 https://github.com/LHRI-Bioinformatics/HIVGenoPipe.git /opt/HIVGenoPipe

ENV HIVGENOPIPE_DIR=/opt/HIVGenoPipe \
    NXF_HOME=/tmp/nextflow \
    PATH=/opt/conda/bin:$PATH

WORKDIR /data
