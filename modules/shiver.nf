/*
 * shiver — de novo contigs, corrected and gap-filled against the closest
 * reference from a panel, then reads remapped to that tailored reference.
 *
 * Container: the hcovlab dockerised build, which bundles shiver's seven
 * dependencies and runs on Python 3.
 *
 * Layout note. That image installs shiver at /shiver and declares a plain
 * default PATH, so none of shiver's scripts can be found by name — they are
 * called through SHIVER_HOME below. It bundles no reference panel, no
 * adapters and no primers; only /shiver/ExampleInput. An earlier version of
 * this module assumed /opt/shiver with a data/ tree, which is the layout of
 * the fallback Dockerfile under containers/shiver, not of this image.
 */

process SHIVER {
    tag "${sample}"
    label 'assembly'
    container params.containers.shiver
    publishDir "${params.outdir}/shiver", mode: 'copy'

    input:
    tuple val(sample), path(r1), path(r2), val(paired)
    path refs_panel
    path adapters
    path primers

    output:
    tuple val(sample), path("${sample}_consensus.fasta"), emit: consensus
    path "work/**"                                      , emit: intermediates, optional: true
    path "versions.yml"                                 , emit: versions

    script:
    // The script runs from work/, one level below the staged inputs, so a
    // staged file needs a ../ prefix. Resolve each argument to its final
    // form here rather than gluing a prefix on in the shell: '../' in front
    // of an absolute container path gives '..//shiver/...', which resolves
    // nowhere.
    def panel  = refs_panel.name != 'NO_PANEL'    ? "../${refs_panel}" : ''
    def adapt  = adapters.name   != 'NO_ADAPTERS' ? "../${adapters}"   : ''
    def prim   = primers.name    != 'NO_PRIMERS'  ? "../${primers}"    : ''
    def mate2  = paired ? "${r2}" : ''
    """
    # shiver's scripts are not on PATH in the hcovlab image; they sit in a
    # directory that the image declares nothing about. Put it on PATH once
    # here so the calls below read as ordinary commands, and let a site
    # override it for an image with a different prefix.
    SHIVER_HOME="\${SHIVER_HOME:-/shiver}"
    if [ ! -x "\$SHIVER_HOME/shiver_init.sh" ]; then
        echo "shiver was not found at \$SHIVER_HOME inside the container." >&2
        echo "Set SHIVER_HOME to the directory holding shiver_init.sh." >&2
        exit 1
    fi
    export PATH="\$SHIVER_HOME:\$PATH"
    CONFIG="\${SHIVER_CONFIG:-\$SHIVER_HOME/config.sh}"

    # The reference panel decides which reference each contig is corrected
    # against, so it determines the consensus. The image ships none, and
    # falling back to shiver's toy ExampleInput alignment would produce a
    # consensus that looks plausible and means nothing. Fail instead.
    PANEL="${panel}"
    if [ -z "\$PANEL" ]; then
        echo "shiver needs a reference panel: an ALIGNMENT of HIV-1 references" >&2
        echo "covering the subtypes you expect. Pass it with --refs_fasta." >&2
        echo "The container bundles none, and \$SHIVER_HOME/ExampleInput holds" >&2
        echo "only shiver's demonstration alignment, which is not fit for" >&2
        echo "assembly you intend to interpret." >&2
        exit 1
    fi

    # Adapters and primers drive read trimming. Shiver requires both as
    # arguments, so when none are given fall back to the image's examples --
    # but say so loudly, and record it in versions.yml so the provenance
    # travels with the output rather than living only in a lost console log.
    ADAPT="${adapt}"
    PRIM="${prim}"
    ADAPT_SRC=supplied
    PRIM_SRC=supplied
    if [ -z "\$ADAPT" ]; then
        ADAPT=\$(ls "\$SHIVER_HOME"/ExampleInput/*[Aa]dapter*.fasta 2>/dev/null | head -1)
        ADAPT_SRC="container example (\$ADAPT)"
        echo "WARNING: no adapters given; using shiver's example adapters." >&2
    fi
    if [ -z "\$PRIM" ]; then
        PRIM=\$(ls "\$SHIVER_HOME"/ExampleInput/*[Pp]rimer*.fasta 2>/dev/null | head -1)
        PRIM_SRC="container example (\$PRIM)"
        echo "WARNING: no primers given; using shiver's example primers." >&2
    fi
    [ -n "\$ADAPT" ] && [ -n "\$PRIM" ] || {
        echo "No adapters or primers, and the container has no examples to" >&2
        echo "fall back on. Pass --adapters and --primers." >&2
        exit 1; }

    mkdir -p work && cd work

    # PANEL, ADAPT and PRIM are resolved above: a supplied file is relative
    # to this directory, an example is an absolute container path.
    shiver_init.sh init_dir "\$CONFIG" "\$PANEL" "\$ADAPT" "\$PRIM"

    iva -f ../${r1} ${paired ? "-r ../${mate2}" : ''} -t ${task.cpus} contigs_dir \\
        || { echo "iva produced no contigs"; exit 1; }

    shiver_align_contigs.sh init_dir "\$CONFIG" \\
        contigs_dir/contigs.fasta ${sample}

    # shiver only emits *_cut_wRefs.fasta when the contigs needed trimming.
    REFS=${sample}_cut_wRefs.fasta
    [ -f "\$REFS" ] || REFS=${sample}_raw_wRefs.fasta

    shiver_map_reads.sh init_dir "\$CONFIG" \\
        contigs_dir/contigs.fasta ${sample} ${sample}.blast \\
        "\$REFS" ../${r1} ${paired ? "../${mate2}" : ''}

    cd ..
    # Prefer the remapped consensus; fall back to the gap-filled round one.
    CONS=\$(ls work/*remap_consensus*.fasta 2>/dev/null | head -1)
    [ -n "\$CONS" ] || CONS=\$(ls work/*ConsensusRound1_GapsFilled*.fasta 2>/dev/null | head -1)
    [ -n "\$CONS" ] || { echo "shiver produced no consensus"; exit 1; }
    cp "\$CONS" ${sample}_consensus.fasta

    # Record which inputs were actually used, not just the tool versions.
    # A consensus is only interpretable against the panel it was built from,
    # and whether the trimming inputs were real or examples decides whether
    # the result belongs in a benchmark at all.
    cat <<-VERSIONS > versions.yml
    "${task.process}":
        shiver: \$(cat "\$SHIVER_HOME"/info/*ersion* 2>/dev/null | head -1 || echo 'unknown')
        iva: \$(iva --version 2>&1 | head -1 || echo 'unknown')
        shiver_home: "\$SHIVER_HOME"
        reference_panel: "${refs_panel.name}"
        adapters: "\$ADAPT_SRC"
        primers: "\$PRIM_SRC"
    VERSIONS
    """

    stub:
    """
    mkdir -p work
    printf '>%s\\nACGT\\n' ${sample} > ${sample}_consensus.fasta
    echo '"${task.process}": {shiver: stub}' > versions.yml
    """
}
