/*
 * Structural check on the assembled consensus.
 *
 * Runs whatever the chosen pipeline produced through a colinearity test
 * against the reference. Genome fraction and mismatch rate can both look
 * excellent while the genome is in the wrong order; this is what catches
 * that. Runs in the app's own container — no bioinformatics dependencies.
 */

process STRUCTURAL_QC {
    tag "${sample}"
    label 'light'
    container params.containers.app
    publishDir "${params.outdir}/qc", mode: 'copy'

    input:
    tuple val(sample), path(consensus)
    path reference

    output:
    tuple val(sample), path("${sample}_qc.json"), emit: report
    path "${sample}_qc.txt"                     , emit: summary

    script:
    def ref = reference.name != 'NO_REFERENCE' ? "--reference ${reference}" : ''
    """
    structural_qc.py --consensus ${consensus} ${ref} \\
        --json ${sample}_qc.json --text ${sample}_qc.txt
    """

    stub:
    """
    echo '{"verdict":"pass","summary":"stub","findings":[]}' > ${sample}_qc.json
    echo 'stub' > ${sample}_qc.txt
    """
}
