/*
 * viral-ngs — Trinity assembly, contigs ordered and oriented against a
 * reference, then gaps imputed from that reference.
 */

process VIRALNGS {
    tag "${sample}"
    label 'assembly_large'
    container params.containers.viralngs
    publishDir "${params.outdir}/viralngs", mode: 'copy'

    input:
    tuple val(sample), path(r1), path(r2), val(paired)
    path reference
    path adapters

    output:
    tuple val(sample), path("${sample}_consensus.fasta"), emit: consensus
    path "*.contigs.fasta"                              , emit: intermediates, optional: true
    path "versions.yml"                                 , emit: versions

    script:
    def ref   = reference.name != 'NO_REFERENCE' ? reference : '/opt/viral-ngs/refs/HXB2.fasta'
    def adapt = adapters.name != 'NO_ADAPTERS' ? adapters  : '/opt/viral-ngs/refs/trim_clip.fasta'
    """
    # viral-ngs works from unaligned BAM.
    picard FastqToSam \\
        F1=${r1} ${paired ? "F2=${r2}" : ''} \\
        O=${sample}.bam SM=${sample} \\
        || read_utils.py fastq_to_bam ${r1} ${paired ? r2 : ''} ${sample}.bam --sampleName ${sample}

    assembly.py assemble_trinity ${sample}.bam ${adapt} \\
        ${sample}.contigs.fasta --threads ${task.cpus}

    assembly.py order_and_orient ${sample}.contigs.fasta ${ref} \\
        ${sample}.ordered.fasta

    assembly.py impute_from_reference ${sample}.ordered.fasta ${ref} \\
        ${sample}_consensus.fasta --newName ${sample}

    cat <<-VERSIONS > versions.yml
    "${task.process}":
        viral-ngs: \$(assembly.py --version 2>&1 || echo 'unknown')
    VERSIONS
    """

    stub:
    """
    printf '>%s\\nACGT\\n' ${sample} > ${sample}_consensus.fasta
    echo '"${task.process}": {viralngs: stub}' > versions.yml
    """
}
