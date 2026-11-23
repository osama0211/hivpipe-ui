/*
 * SmaltAlign — maps reads with smalt, calls a consensus, then remaps onto
 * that consensus and repeats. Four iterations by default.
 *
 * It has no paired-end flag, so R1 and R2 are concatenated before mapping.
 */

process SMALTALIGN {
    tag "${sample}"
    label 'assembly'
    container params.containers.smaltalign
    publishDir "${params.outdir}/smaltalign", mode: 'copy'

    input:
    tuple val(sample), path(r1), path(r2), val(paired)
    path reference

    output:
    tuple val(sample), path("${sample}_consensus.fasta"), emit: consensus
    path "*.{bam,bai,vcf}"                              , emit: intermediates, optional: true
    path "versions.yml"                                 , emit: versions

    script:
    def ref = reference.name != 'NO_REFERENCE' ? reference : '/opt/smaltalign/refs/HXB2.fasta'
    def iterations = params.smaltalign_iterations ?: 4
    """
    # Concatenate mates: SmaltAlign takes one read file per sample.
    if [ "${paired}" = "true" ]; then
        cat ${r1} ${r2} > ${sample}.fastq.gz
    else
        cp ${r1} ${sample}.fastq.gz
    fi

    SmaltAlign.sh -r ${ref} -n ${iterations} -t ${task.cpus} .

    CONS=\$(ls *_cons.fasta 2>/dev/null | head -1)
    [ -n "\$CONS" ] || CONS=\$(ls *consensus*.fasta 2>/dev/null | head -1)
    [ -n "\$CONS" ] || { echo "SmaltAlign produced no consensus"; exit 1; }
    cp "\$CONS" ${sample}_consensus.fasta

    cat <<-VERSIONS > versions.yml
    "${task.process}":
        smalt: \$(smalt version 2>&1 | grep -i version | head -1 || echo 'unknown')
        samtools: \$(samtools --version 2>&1 | head -1 || echo 'unknown')
    VERSIONS
    """

    stub:
    """
    printf '>%s\\nACGT\\n' ${sample} > ${sample}_consensus.fasta
    echo '"${task.process}": {smaltalign: stub}' > versions.yml
    """
}
