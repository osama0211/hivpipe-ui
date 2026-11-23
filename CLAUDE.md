# HIV assembly pipeline runner — project context

Read this before writing or changing anything. Every fact below cost at
least one failed cluster run to learn. None of it is inferable from the
code, and most of it contradicts what a reasonable person would assume.

## What this project is

Nextflow pipelines that assemble HIV-1 whole genomes from paired Illumina
reads, run on a SLURM cluster through Singularity containers, for a
benchmark comparing five published pipelines (shiver, V-pipe, SmaltAlign,
viral-ngs, HIVGenoPipe) plus a composite pipeline that takes one component
from each.

The output is a manuscript. That sets the bar: every number must be
traceable to a pinned tool version and a named input. A result that looks
plausible but came from an unrecorded default is worse than no result.

## The hard rules

These exist because each was violated once and cost a day.

1. **Never write a path into a container you have not listed.**
   Open the image and look: `singularity exec --cleanenv <img> ls /`.
   Container layouts are not guessable and upstream images change.

2. **Never pin a container by `:latest` or `:master`.** Use the digest.
   An unpinned tag means the benchmark's "shiver" is whatever was built
   most recently, which is not a reproducible claim.

3. **Never pull an image during a run.** Pull deliberately, once, with
   retries, into the shared cache. A registry hiccup must not be able to
   kill a run twelve minutes in.

4. **Never silently default anything that changes results.** A missing
   reference panel, adapter set or primer set must fail loudly or be
   recorded in `versions.yml`. Silent fallbacks produce consensus
   sequences that look fine and mean nothing.

5. **Test against a mock before testing on the cluster.** A bug found by
   `nf-test` costs seconds; the same bug found by SLURM costs ten minutes
   plus a queue wait.

6. **Log the resolved path of every external tool.** `# nextflow:
   /path/actually/used` at the top of a run log turns a future mystery
   into one line.

## Cluster facts: the target SLURM cluster

A university HPC: Lmod modules, BeeGFS home, Singularity. The host, user
and paths live in `hpc.yaml`, which is deliberately untracked.

- Scheduler: SLURM. **There is no partition named `normal`.** Do not set
  a default partition — leave it unset so SLURM picks the site default.
  `sinfo -h -o '%P'` lists the real ones.
- Module system: Lmod. `module` is a **shell function** from
  `/etc/profile.d`, not a program.
- `/usr/bin/java` is too old for Nextflow. `module load Java/17` gives
  Temurin 17.0.15. Nextflow needs Java 17–26, at install time and run time.
- Nextflow is a **user install** at `~/.local/bin/nextflow`. It is not a
  module. `module -t avail | grep -i nextflow` returns nothing.
- Singularity is at `/usr/bin/singularity`. No module needed.
- Home is BeeGFS, 84 TB, effectively no quota. `/` has ~33 GB for
  `$TMPDIR` during SIF builds.
- ghcr.io **drops HTTP/2 streams mid-pull** from this network:
  `PROTOCOL_ERROR; received from peer`. Large multi-layer images fail
  more often. Retry loops work; the layer cache resumes.

## Non-login shell behaviour — the subtlest trap here

A job script started over SSH or by `setsid` is **not** a login shell. In
that shell, by default:

- `module` is undefined, so every `module load` is a silent no-op.
- `~/.bashrc` and `~/.bash_profile` are unread, so anything the user put
  on their own `PATH` — `~/.local/bin`, conda — is absent. This is where
  Nextflow lives.

So a job script must, before `set -u` and with output discarded:

1. Source `/etc/profile` **first**. It runs `/etc/profile.d/*` in order,
   which matters because the `module` function and `MODULEPATH` are often
   defined in two different files. Sourcing only `modules.sh` and
   stopping leaves `MODULEPATH` empty, and `module load` then fails in a
   way that looks exactly like the module not existing.
2. Then fall back to `$MODULESHOME/init/bash`, `$LMOD_PKG/init/bash`,
   `/etc/profile.d/lmod.sh` and friends if `module` is still undefined.
3. Then source the user's own login files — first of `.bash_profile`,
   `.bash_login`, `.profile`, then `.bashrc` where conda init lands.

All of this before `set -u`: profile scripts reference unset variables and
print banners, and neither should abort the run or pollute the log.

## Container layouts, as actually verified

### ghcr.io/hcovlab/dshiver
- shiver's scripts: **`/shiver/`** — `shiver_init.sh`,
  `shiver_align_contigs.sh`, `shiver_map_reads.sh`, `shiver_funcs.sh`.
- **Nothing is on `PATH`.** The image declares a plain default `PATH`.
  Call them via a `SHIVER_HOME` variable, default `/shiver`.
- Config: **`/shiver/config.sh`**. Not `/opt/shiver/config.sh`.
- `/opt` is **empty**.
- `iva` is on `PATH` at `/usr/local/bin/iva`.
- **No reference panel, no adapters, no primers.** Only
  `/shiver/ExampleInput/`, which holds `adapters_Illumina.fasta` and
  `primers_GallEtAl2012.fasta` — those are real and usable if the lab used
  the Gall 2012 scheme — and a demonstration alignment that is **not** fit
  for any assembly you intend to interpret.
- Current upstream shiver lays out as `bin/` + `data/example_input/`, so
  this image is older than upstream. Pin it.

### Others
Not yet verified. Verify before writing their modules. Record findings here.

## shiver's reference panel — the scientific requirement

`shiver_init.sh` takes a panel as a mandatory argument. It is **an
alignment of many HIV-1 genomes**, not a reference genome.

shiver BLASTs the de novo contigs against the panel, picks the entry most
similar to *this sample*, and builds a tailored reference from it. That
per-sample reference choice is shiver's entire differentiator in the
benchmark. The panel is the menu it chooses from, so:

- It must be an **alignment** (equal lengths, gapped), not unaligned
  genomes. `mafft --auto in.fasta > panel.fasta` if needed.
- **DNA**, not protein. **GENOME**, not pol.
- It must contain the subtypes in the cohort. For FSU/Central Asia that
  means **A6 and CRF63_02A6** alongside A1 and B. shiver can only choose
  what is present; an absent subtype gets corrected against something
  more distant, which shows up as elevated mismatch rates easily
  misread as a real finding.
- Source: LANL group M compendium genome alignment,
  `HIV1_COM_<year>_genome_DNA.fasta`, from hiv.lanl.gov. shiver's own
  documentation uses exactly this.

**This must never default.** Fail with a message naming `--refs_fasta`.

## Nextflow specifics learned here

- `NXF_SINGULARITY_CACHEDIR` must point **outside** the per-run
  directory, or every run re-downloads every image.
- A background launcher must not record `$!` after `setsid`: setsid forks
  and exits immediately, so that pid is dead within milliseconds and a
  poller will call a healthy run failed. **The job script writes its own
  pid** (`echo $$ > launch.pid`) as its first act.
- A status poller that finds no process and no exit-code file should
  wait a few seconds and look again — that gap is usually the moment
  between the last command and `echo $? > exit_code`.
- Inside a process script running from a subdirectory, a staged input
  takes a `../` prefix and an absolute container path must not. Resolve
  each argument to its final form in Groovy; do not glue a prefix on in
  the shell, or an absolute fallback becomes `..//opt/...` and resolves
  nowhere.

## Reference coordinates

HXB2 (K03455), 9719 bp: PR 2253–2549, RT 2550–4229, IN 4230–5096.

## Conventions

- Pipelines live in their own repo; anything that drives them references
  them by git tag (`nextflow run <org>/<repo> -r vX.Y.Z`) rather than
  vendoring a copy. Two copies of a pipeline drift, and the drift is
  silent.
- Every change gets an `nf-test` case before it is called done.
- `versions.yml` records tool versions **and the inputs that determine
  the result** — the panel, and whether adapters/primers were supplied or
  fell back.
