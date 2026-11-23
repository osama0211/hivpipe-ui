#!/usr/bin/env nextflow
/*
 * HIV-1 consensus assembly — one sample through one chosen pipeline,
 * followed by a structural check on whatever consensus comes out.
 *
 * Each pipeline runs in its own container, so they never share a dependency
 * tree. Adding or swapping one means editing its module, not this file.
 */

nextflow.enable.dsl = 2

params.sample     = null          // sample name
params.reads1     = null          // forward reads
params.reads2     = null          // reverse reads, or null for single-end
params.pipeline   = 'shiver'      // shiver | vpipe | smaltalign | viralngs | hivgenopipe
params.outdir     = 'results'
params.reference  = null          // reference FASTA for assembly and QC
params.primers    = null          // primer FASTA, optional
params.adapters   = null          // adapter FASTA, optional
params.refs_fasta = null          // multi-sequence reference panel for shiver

include { SHIVER }         from './modules/shiver'
include { VPIPE }          from './modules/vpipe'
include { SMALTALIGN }     from './modules/smaltalign'
include { VIRALNGS }       from './modules/viralngs'
include { HIVGENOPIPE }    from './modules/hivgenopipe'
include { STRUCTURAL_QC }  from './modules/qc'

// Optional inputs are staged as placeholder files when absent. Each kind needs
// its own placeholder name — Nextflow rejects two staged inputs sharing one.
def optionalFile(value, placeholder) {
    return value ? file(value) : file("${projectDir}/assets/${placeholder}")
}

workflow {

    if (!params.sample) error "Missing required parameter --sample"
    if (!params.reads1) error "Missing required parameter --reads1"

    def known = ['shiver', 'vpipe', 'smaltalign', 'viralngs', 'hivgenopipe']
    if (!(params.pipeline in known)) {
        error "Unknown --pipeline '${params.pipeline}'. Choose one of: ${known.join(', ')}"
    }

    // A single-end sample carries an empty second read file so every process
    // can take the same input shape.
    def r2 = optionalFile(params.reads2, 'NO_MATE')

    reads = Channel.of(
        tuple(params.sample, file(params.reads1), r2, params.reads2 as boolean)
    )

    reference  = Channel.value(optionalFile(params.reference,  'NO_REFERENCE'))
    primers    = Channel.value(optionalFile(params.primers,    'NO_PRIMERS'))
    adapters   = Channel.value(optionalFile(params.adapters,   'NO_ADAPTERS'))
    refs_panel = Channel.value(optionalFile(params.refs_fasta, 'NO_PANEL'))

    // if/else rather than switch: the strict workflow syntax introduced in
    // Nextflow 25 does not accept switch statements, and this form compiles
    // on both the old and new parsers.
    if (params.pipeline == 'shiver') {
        assembled = SHIVER(reads, refs_panel, adapters, primers)
    }
    else if (params.pipeline == 'vpipe') {
        assembled = VPIPE(reads, reference)
    }
    else if (params.pipeline == 'smaltalign') {
        assembled = SMALTALIGN(reads, reference)
    }
    else if (params.pipeline == 'viralngs') {
        assembled = VIRALNGS(reads, reference, adapters)
    }
    else {
        assembled = HIVGENOPIPE(reads, reference)
    }

    STRUCTURAL_QC(assembled.consensus, reference)

    // Declared inside the workflow: the strict syntax in Nextflow 25 and
    // later rejects top-level statements in a script file.
    workflow.onComplete = {
        log.info(workflow.success
            ? "Finished. Results in ${params.outdir}"
            : "Failed after ${workflow.duration}: ${workflow.errorMessage ?: 'see the log above'}")
    }
}
