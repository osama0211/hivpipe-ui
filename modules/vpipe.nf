/*
 * V-pipe — ngshmmalign maps reads to a user-supplied reference; the
 * Snakemake workflow handles QC, mapping and consensus calling.
 */

process VPIPE {
    tag "${sample}"
    label 'assembly'
    container params.containers.vpipe
    publishDir "${params.outdir}/vpipe", mode: 'copy'

    input:
    tuple val(sample), path(r1), path(r2), val(paired)
    path reference

    output:
    tuple val(sample), path("${sample}_consensus.fasta"), emit: consensus
    path "results/**"                                   , emit: intermediates, optional: true
    path "versions.yml"                                 , emit: versions

    script:
    def ref = reference.name != 'NO_REFERENCE' ? reference : '/opt/vpipe/resources/hiv/HXB2.fasta'
    """
    # V-pipe expects samples/<name>/<date>/raw_data/
    mkdir -p samples/${sample}/20000101/raw_data
    cp ${r1} samples/${sample}/20000101/raw_data/
    ${paired ? "cp ${r2} samples/${sample}/20000101/raw_data/" : ''}

    printf '%s\\t%s\\t%s\\n' ${sample} 20000101 250 > samples.tsv

    cat <<-CONFIG > vpipe.config
    [general]
    virus_base_config = 'hiv'
    threads = ${task.cpus}

    [input]
    samples_file = 'samples.tsv'
    reference = '${ref}'
    CONFIG

    snakemake -s \${VPIPE_SNAKEFILE:-/opt/vpipe/workflow/Snakefile} \\
        --cores ${task.cpus} --configfile vpipe.config --rerun-incomplete

    CONS=\$(find results -name 'ref_majority*.fasta' | head -1)
    [ -n "\$CONS" ] || CONS=\$(find results -name '*consensus*.fasta' | head -1)
    [ -n "\$CONS" ] || { echo "V-pipe produced no consensus"; exit 1; }
    cp "\$CONS" ${sample}_consensus.fasta

    cat <<-VERSIONS > versions.yml
    "${task.process}":
        snakemake: \$(snakemake --version 2>&1 || echo 'unknown')
    VERSIONS
    """

    stub:
    """
    printf '>%s\\nACGT\\n' ${sample} > ${sample}_consensus.fasta
    echo '"${task.process}": {vpipe: stub}' > versions.yml
    """
}
