# SmaltAlign has no published image, so this builds one.
# smalt + samtools + bcftools + R, plus the scripts from the upstream repo.

FROM mambaorg/micromamba:1.5.8

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates procps && rm -rf /var/lib/apt/lists/*

RUN micromamba install -y -n base -c conda-forge -c bioconda \
        smalt=0.7.6 \
        samtools=1.19 \
        bcftools=1.19 \
        seqtk \
        r-base \
        python=3.11 \
    && micromamba clean -a -y

ENV PATH=/opt/conda/bin:/opt/smaltalign:$PATH

RUN git clone --depth 1 https://github.com/medvir/SmaltAlign.git /opt/smaltalign \
    && chmod +x /opt/smaltalign/*.sh /opt/smaltalign/*.R 2>/dev/null || true

# Reference panel lives here so the Nextflow module has a default to fall back on.
RUN mkdir -p /opt/smaltalign/refs
COPY refs/ /opt/smaltalign/refs/

WORKDIR /data
