# Fallback shiver image.
#
# Prefer ghcr.io/hcovlab/dshiver, which is the published dockerised build
# from the Zsichla benchmark paper. This exists for the case where you need
# to pin a different shiver version or patch it.

FROM mambaorg/micromamba:1.5.8

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates procps wget && rm -rf /var/lib/apt/lists/*

RUN micromamba install -y -n base -c conda-forge -c bioconda \
        python=3.11 \
        blast=2.15 \
        bowtie2 \
        samtools=1.19 \
        mafft \
        fastaq \
        trimmomatic \
        iva \
        smalt \
    && micromamba clean -a -y

ENV PATH=/opt/conda/bin:/opt/shiver:$PATH \
    SHIVER_CONFIG=/opt/shiver/config.sh

RUN git clone --depth 1 https://github.com/ChrisHIV/shiver.git /opt/shiver \
    && chmod +x /opt/shiver/*.sh

RUN mkdir -p /opt/shiver/data/refs /opt/shiver/data/adapters /opt/shiver/data/primers
COPY refs/ /opt/shiver/data/refs/

WORKDIR /data
