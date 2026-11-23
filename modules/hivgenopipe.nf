/*
 * HIVGenoPipe — gag-pol only, building a sample-specific reference in real
 * time from a hybrid of de novo and reference-based assembly.
 *
 * It is itself a Nextflow pipeline, so this process invokes it rather than
 * reimplementing its steps.
 */

process HIVGENOPIPE {
    tag "${sample}"
    label 'assembly'
    container params.containers.hivgenopipe
    publishDir "${params.outdir}/hivgenopipe", mode: 'copy'

    input:
    tuple val(sample), path(r1), path(r2), val(paired)
    path reference

    output:
    tuple val(sample), path("${sample}_consensus.fasta"), emit: consensus
    path "results/**"                                   , emit: intermediates, optional: true
    path "versions.yml"                                 , emit: versions

    script:
    """
    cat <<-CSV > samplesheet.csv
    sample,fastq_1,fastq_2
    ${sample},\$(readlink -f ${r1}),${paired ? "\$(readlink -f ${r2})" : ''}
    CSV

    nextflow run \${HIVGENOPIPE_DIR:-/opt/HIVGenoPipe} \\
        --input samplesheet.csv \\
        --outdir results \\
        -profile standard \\
        -ansi-log false

    CONS=\$(find results -name '*consensus*.fasta' | head -1)
    [ -n "\$CONS" ] || { echo "HIVGenoPipe produced no consensus"; exit 1; }
    cp "\$CONS" ${sample}_consensus.fasta

    cat <<-VERSIONS > versions.yml
    "${task.process}":
        nextflow: \$(nextflow -version 2>&1 | grep -oP 'version \\K\\S+' || echo 'unknown')
    VERSIONS
    """

    stub:
    """
    printf '>%s\\nACGT\\n' ${sample} > ${sample}_consensus.fasta
    echo '"${task.process}": {hivgenopipe: stub}' > versions.yml
    """
}
