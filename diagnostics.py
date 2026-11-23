"""Deterministic diagnosis of a finished run.

Pipeline failures are not open-ended. In practice they cluster into a few
dozen recurring causes, and for those a lookup table beats a language model
on every axis that matters: it is instant, free, works offline, gives the
same answer twice, can be unit tested, and cannot invent a cause that was
not there.

So this runs first. The model in `assistant.py` is the fallback for the tail
this table does not cover, and for open-ended questions like drafting a
methods paragraph.

Adding a rule
-------------
Append to RULES. Each rule needs an id, a title, `when` (what to look at),
`why` (the cause in one or two sentences) and `fix` (what to actually do).
Match on `log` with a regex, on `exit_code`, or with a `check` callable that
is handed the run dictionary. Order does not matter; results are ranked by
weight and then by specificity.

Every rule here comes from a failure that was actually observed, either in
this project's own history or in the documented behaviour of the tools it
drives. Resist adding speculative ones: a wrong confident diagnosis is worse
than none.
"""

from __future__ import annotations

import re

# Weights decide ranking when several rules match. A rule that keys on an
# exit code plus a distinctive string is more trustworthy than one keying on
# a common word, and should be shown first.
STRONG, NORMAL, WEAK = 30, 20, 10


def _qc_verdict(run, want):
    qc = run.get("qc") or {}
    return qc.get("verdict") == want


def _qc_has(run, needle):
    qc = run.get("qc") or {}
    blob = " ".join(str(f) for f in (qc.get("findings") or []))
    return needle.lower() in blob.lower()


RULES = [
    # ---------------------------------------------------------- container
    {
        "id": "docker-daemon-down",
        "title": "Docker is installed but not running",
        "log": r"Cannot connect to the Docker daemon|"
               r"docker daemon is not running|"
               r"error during connect.*docker_engine",
        "weight": STRONG,
        "why": "Nextflow asked Docker to start a container and nothing "
               "answered. On Windows and macOS this almost always means "
               "Docker Desktop is not open.",
        "fix": ["Start Docker Desktop and wait for the whale icon to settle.",
                "On Linux: sudo systemctl start docker",
                "Check the This machine tab — it reports the daemon state."],
    },
    {
        "id": "image-missing-local",
        "title": "A locally built image has not been built",
        "log": r"(manifest for|repository) (hivpipe|best5)[\w/.-]*"
               r"(:[\w.-]+)? not found|"
               r"pull access denied for (hivpipe|best5)",
        "weight": STRONG,
        "why": "Two of the images are not published anywhere; they are "
               "built on your machine. Docker tried to pull one from a "
               "registry instead, because it was not there to find.",
        "fix": ["docker compose --profile setup run --rm warm-images",
                "or build them directly with the docker build commands in "
                "the README.",
                "Tags must match exactly — a mismatch looks identical to a "
                "missing image."],
    },
    {
        "id": "singularity-pull-failed",
        "title": "A container image could not be pulled on the cluster",
        "log": r"Failed to pull singularity image|"
               r"Singularity pull failed|"
               r"FATAL:\s+While making image from oci registry|"
               r"error fetching image|"
               r"unable to retrieve auth token|"
               r"Error writing blob",
        "weight": STRONG,
        "why": "Nextflow converts each Docker image to a Singularity image "
               "before the first run that needs it. That step downloads "
               "several GB from a public registry and needs outbound HTTPS, "
               "space in the cache directory, and time. Clusters often "
               "restrict outbound traffic from compute nodes.",
        "fix": ["Pull the images once on the login node, which usually has "
                "network access, before running: "
                "singularity pull docker://ghcr.io/hcovlab/dshiver:latest",
                "Check free space: the cache needs several GB "
                "(df -h and quota -s).",
                "If the cluster uses an HTTP proxy, export https_proxy "
                "before the run.",
                "The runner now sets NXF_SINGULARITY_CACHEDIR to a folder "
                "under your cluster working directory, so images are "
                "downloaded once rather than per run."],
    },
    {
        "id": "image-pull-failed",
        "title": "An image could not be downloaded",
        "log": r"manifest unknown|pull access denied|"
               r"Error response from daemon:.*(not found|unauthorized)|"
               r"failed to resolve reference",
        "weight": NORMAL,
        "why": "Docker could not fetch a published image. Usually no "
               "internet, a proxy in the way, or a tag that no longer "
               "exists upstream.",
        "fix": ["Check the connection, then: docker pull ghcr.io/hcovlab/dshiver",
                "Behind a corporate proxy, configure it in Docker Desktop's "
                "settings.",
                "Pre-pull everything once: docker compose --profile setup "
                "run --rm warm-images"],
    },
    {
        "id": "compose-v1",
        "title": "Docker Compose v2 is missing",
        "log": r"unknown flag: --profile|"
               r"docker: 'compose' is not a docker command",
        "weight": STRONG,
        "why": "Ubuntu's docker.io package ships without the Compose v2 "
               "plugin, and the old docker-compose script does not "
               "understand --profile.",
        "fix": ["Install Docker's own repository, then: sudo apt install "
                "docker-compose-plugin",
                "Verify with: docker compose version"],
    },
    # ---------------------------------------------------------- resources
    {
        "id": "oom",
        "title": "A step was killed for using too much memory",
        "log": r"OOMKilled|exit status 137|Killed\s*$|"
               r"Command exit status:\s*137",
        "exit_code": 137,
        "weight": STRONG,
        "why": "The container asked for more memory than it was allowed and "
               "the kernel killed it. Under WSL a container typically gets "
               "about half the host's RAM, so a 16 GB laptop often offers "
               "only 8 GB.",
        "fix": ["Raise the memory budget on the Run tab if the machine has "
                "the headroom.",
                "Raise Docker Desktop's memory limit: Settings → Resources.",
                "Under WSL, set memory= in .wslconfig and run wsl --shutdown.",
                "Or send the run to a cluster from the Cluster tab."],
    },
    {
        "id": "nf-memory-request",
        "title": "More memory was requested than exists",
        "log": r"Process requirement exceeds available memory|"
               r"exceeds available memory",
        "weight": STRONG,
        "why": "Nextflow refused the process before running anything, "
               "because the per-process memory limit is above what the "
               "machine can offer.",
        "fix": ["Lower the memory budget on the Run tab.",
                "The Resources panel shows what each step would get — the "
                "assembly step is the one that decides."],
    },
    {
        "id": "nf-cpu-request",
        "title": "More CPUs were requested than exist",
        "log": r"Process requirement exceeds available CPUs|"
               r"exceeds available cpus",
        "weight": STRONG,
        "why": "The thread count is higher than the number of cores "
               "Nextflow can see.",
        "fix": ["Lower Threads on the Run tab to at most the core count "
                "shown beside it."],
    },
    {
        "id": "disk-full",
        "title": "The disk is full",
        "log": r"No space left on device|disk quota exceeded",
        "weight": STRONG,
        "why": "Assembly intermediates are large and the Nextflow work "
               "directory keeps every one of them.",
        "fix": ["Delete old work directories — they are safe to remove once "
                "a run has finished.",
                "Point work_dir in settings.yaml at a bigger disk.",
                "Under WSL the virtual disk can also need compacting."],
    },
    # ----------------------------------------------------------- nextflow
    {
        "id": "nf-missing",
        "title": "Nextflow was not found",
        "log": r"Nextflow was not found|nextflow: command not found",
        "exit_code": 127,
        "weight": STRONG,
        "why": "The application could not find the nextflow launcher.",
        "fix": ["curl -s https://get.nextflow.io | bash",
                "Move it onto PATH, or set HIVPIPE_NEXTFLOW to its full path."],
    },
    {
        "id": "nextflow-windows-no-wsl",
        "title": "Nextflow cannot run on Windows without WSL",
        "log": r"Nextflow cannot run on Windows directly|"
               r"No WSL distribution was found",
        "weight": STRONG + 10,
        "why": "Nextflow requires Bash and a POSIX system. It is supported "
               "on Linux, macOS, and on Windows only through WSL. The "
               "window runs fine on Windows; the pipelines cannot.",
        "fix": ["In PowerShell: wsl --install -d Ubuntu, then restart.",
                "Open Ubuntu once to finish setup, then install Nextflow "
                "inside it.",
                "Or skip local runs entirely and use the Cluster tab, where "
                "Nextflow runs on the cluster."],
    },
    {
        "id": "nextflow-not-in-wsl",
        "title": "Nextflow is not installed inside WSL",
        "log": r"WSL is present but Nextflow is not installed",
        "weight": STRONG + 10,
        "why": "WSL is there, but Nextflow has to live inside it, because "
               "Windows cannot execute the launcher.",
        "fix": ["Open Ubuntu and run:",
                "  sudo apt update && sudo apt install -y default-jre-headless",
                "  curl -s https://get.nextflow.io | bash",
                "  sudo mv nextflow /usr/local/bin/",
                "Then press Start again."],
    },
    {
        "id": "docker-not-in-wsl",
        "title": "Docker is not visible from inside WSL",
        "log": r"docker: command not found|"
               r"docker: not found",
        "weight": STRONG,
        "why": "On Windows, Nextflow runs inside WSL and starts containers "
               "from there, so the docker command has to be available "
               "inside the distribution, not only in Windows.",
        "fix": ["Docker Desktop → Settings → Resources → WSL integration, "
                "and enable your distribution.",
                "Apply & Restart, then check inside Ubuntu: docker ps"],
    },
    {
        "id": "java-wrong-version-cluster",
        "title": "The cluster's Java is missing or the wrong version",
        "log": r"Cannot find Java or it's a wrong version|"
               r"please make sure that Java \d+ or later",
        "weight": STRONG + 10,
        "why": "Nextflow runs on the JVM and needs Java 17 or later. The "
               "`java` on the cluster's default PATH is older than that, or "
               "absent. `JAVA_HOME` being empty in the message is the usual "
               "sign that no Java module was loaded.",
        "fix": ["On the cluster, run: module -t avail 2>&1 | grep -i java",
                "Add the module it names — something like openjdk/17 or "
                "java/17 — to Modules on the Cluster tab, then run again.",
                "Test connection now checks Java and names the available "
                "modules, so it will catch this before a run.",
                "If the cluster has no Java module, ask its administrators "
                "which one provides Java 17."],
    },
    {
        "id": "java-missing",
        "title": "Java is missing",
        "log": r"Unable to find java|java: command not found|"
               r"JAVA_HOME is not set",
        "weight": STRONG,
        "why": "Nextflow runs on the JVM and there is no Java on PATH.",
        "fix": ["Locally: sudo apt install default-jre-headless",
                "On a cluster: load a Java module instead — see the "
                "Cluster tab's Modules box."],
    },
    {
        "id": "nf-strict-syntax",
        "title": "Nextflow version is stricter than the workflow expects",
        "log": r"Unexpected input: '(switch|case|if)'|"
               r"Script compilation error|"
               r"unqualified type name|"
               r"Invalid method invocation `call` with arguments",
        "weight": STRONG,
        "why": "Nextflow 25 and later reject syntax that earlier versions "
               "accepted — top-level statements outside workflow blocks, "
               "switch statements, closures used as functions.",
        "fix": ["This workflow is tested on 24.10.4 and 26.10.0; if you see "
                "this, the bundled copy is out of date.",
                "Check your version with: nextflow -version",
                "Pin an older one if needed: "
                "NXF_VER=24.10.4 nextflow run ..."],
    },
    {
        "id": "name-collision",
        "title": "Two input files share a name",
        "log": r"input file name collision|"
               r"Process `[^`]+` input file name collision",
        "weight": STRONG,
        "why": "Nextflow stages inputs into one directory per task, so two "
               "files arriving with the same name would overwrite each "
               "other. Optional inputs sharing a placeholder are the usual "
               "cause.",
        "fix": ["Give each optional input its own placeholder file.",
                "Or stage them into numbered subdirectories with "
                "stageAs: 'inputs/?/*'."],
    },
    {
        "id": "stub-no-output",
        "title": "The run finished instantly and produced placeholder files",
        "check": lambda r: (r.get("status") == "complete"
                            and "stub" in str(r.get("profile", "")).lower()),
        "log": r"profile=stub|-stub-run",
        "weight": WEAK,
        "why": "The stub profile exercises the workflow's wiring without "
               "running any tool. The outputs are placeholders, not results.",
        "fix": ["Switch profile to docker in settings.yaml for a real run.",
                "The stub profile is for checking the installation only."],
    },
    # -------------------------------------------------------------- input
    {
        "id": "missing-reference",
        "title": "A required reference file is missing",
        "log": r"Missing --panel|Missing --reference|"
               r"No such file or directory.*\.(fasta|fa|fas|fna)|"
               r"checkIfExists",
        "weight": NORMAL,
        "why": "A reference, panel, primer or adapter file named for this "
               "run was not found at the path recorded for it.",
        "fix": ["Re-add the file on the References tab — a file deleted "
                "from disk still appears in an old run's record.",
                "shiver needs a panel to select its reference from; use the "
                "LANL HIV-1 subtype alignment."],
    },
    {
        "id": "bad-fastq",
        "title": "A FASTQ file is unreadable or truncated",
        "log": r"gzip: .*unexpected end of file|"
               r"invalid block type|not in gzip format|"
               r"Malformed FASTQ|unexpected end of stream",
        "weight": STRONG,
        "why": "The reads file is corrupt or was not fully downloaded.",
        "fix": ["Check it: gzip -t yourfile.fastq.gz",
                "Re-download or re-copy the file if that reports an error.",
                "A zero-byte file looks the same way."],
    },
    {
        "id": "permission-denied",
        "title": "Permission denied writing output",
        "log": r"Permission denied|EACCES|"
               r"cannot create directory.*Permission",
        "weight": NORMAL,
        "why": "The work or results directory is not writable by the user "
               "running the pipeline. With Docker this is often a user-id "
               "mismatch between the container and the host.",
        "fix": ["Check ownership of the runs and work directories.",
                "The docker profile already passes -u $(id -u):$(id -g); on "
                "a shared machine the directory may belong to someone else."],
    },
    # ------------------------------------------------------------ cluster
    {
        "id": "ssh-auth",
        "title": "The cluster rejected the login",
        "log": r"Permission denied \(publickey|"
               r"Authentication failed|"
               r"Too many authentication failures|"
               r"Host key verification failed",
        "weight": STRONG,
        "why": "SSH could not authenticate to the cluster.",
        "fix": ["On the Cluster tab, Test connection reports exactly which "
                "step failed.",
                "If the cluster uses a password rather than a key, switch "
                "Authentication to password and hold it.",
                "Host key verification failed means the cluster's key "
                "changed — connect once by hand to accept it."],
    },
    {
        "id": "nextflow-not-on-cluster-path",
        "title": "Nextflow is installed but not on PATH in the job's shell",
        "log": r"submit\.sh: line \d+: nextflow: command not found|"
               r"nextflow: command not found|"
               r"is not on PATH in this job's shell",
        "weight": STRONG + 10,
        "why": "The job script is started by a non-login shell, which reads "
               "neither ~/.bashrc nor ~/.bash_profile. Nextflow installed "
               "without root lives in ~/.local/bin or a conda environment "
               "under $HOME — both added to PATH by exactly those files. So "
               "`nextflow -version` works when you log in and fails inside "
               "the job.",
        "fix": ["Run `which nextflow` on the cluster and put that full path "
                "in the Nextflow box on the Cluster tab. This works on any "
                "version.",
                "From runner 1.1.6 on, the job script sources your login "
                "files itself and reports the resolved path as "
                "'# nextflow: ...' at the top of every run log.",
                "If the cluster provides it as a module, add that module to "
                "the Modules box instead: module -t avail 2>&1 | grep -i "
                "nextflow"],
    },
    {
        "id": "shiver-not-on-path",
        "title": "shiver is in the container but not on its PATH",
        "log": r"shiver_init\.sh: command not found|"
               r"shiver_(align_contigs|map_reads)\.sh: command not found|"
               r"shiver was not found at .* inside the container",
        "weight": STRONG + 10,
        "why": "The hcovlab image installs shiver at /shiver and declares a "
               "plain default PATH, so its scripts cannot be found by name. "
               "The image is fine; the call has to name the directory.",
        "fix": ["This is fixed from runner 1.1.5 on: the module puts shiver's "
                "directory on PATH itself.",
                "On an image with a different prefix, set SHIVER_HOME to the "
                "directory holding shiver_init.sh.",
                "To check where it lives: singularity exec --cleanenv <image> "
                "find / -name shiver_init.sh"],
    },
    {
        "id": "shiver-double-slash-path",
        "title": "A reference path was built as '..//' and resolves nowhere",
        "log": r"\.\./(/|opt/shiver|shiver/)|MISSING ARG FILE: \.\./+/",
        "weight": STRONG,
        "why": "A relative prefix was glued onto a path that was already "
               "absolute, giving '..//opt/...'. shiver then reports a missing "
               "input for a file that is present.",
        "fix": ["Fixed from runner 1.1.5 on.",
                "Until then, pass every reference explicitly — a supplied "
                "file takes the relative path and never hits the fallback."],
    },
    {
        "id": "shiver-no-panel",
        "title": "shiver needs a reference panel and none was given",
        "log": r"shiver needs a reference panel|needs a reference panel\.",
        "weight": STRONG,
        "why": "shiver corrects each contig against the closest entry in a "
               "panel of references, so the panel decides the consensus. "
               "shiver ships no panel on purpose — the right one depends on "
               "the diversity of your cohort — so there is nothing sensible "
               "to default to.",
        "fix": ["Upload an alignment of HIV-1 references covering the "
                "subtypes you expect under References, then pick it in the "
                "Reference panel slot.",
                "shiver's own documentation uses the LANL group M compendium "
                "genome alignment, e.g. HIV1_COM_<year>_genome_DNA.fasta "
                "from hiv.lanl.gov — choose GENOME and DNA, not pol or "
                "protein.",
                "It must be an ALIGNMENT, not a set of unaligned genomes; "
                "mafft --auto in.fasta > panel.fasta if needed.",
                "On the command line it is --refs_fasta."],
    },
    {
        "id": "slurm-bad-partition",
        "title": "The partition name does not exist on this cluster",
        "log": r"invalid partition specified|Invalid partition name specified",
        "weight": STRONG + 10,
        "why": "sbatch was given a partition the cluster does not have. "
               "Partition names are site-specific — there is no standard "
               "set, and a name that works on one cluster is rejected on "
               "the next.",
        "fix": ["List the real ones on the cluster: sinfo -h -o '%P' | sort -u",
                "Put one of those in Partition or queue on the Cluster tab — "
                "or leave that box empty, which lets the cluster choose its "
                "own default.",
                "Test connection now checks the partition against the "
                "cluster's list before a run."],
    },
    {
        "id": "slurm-rejected",
        "title": "SLURM refused the job",
        "log": r"sbatch: error|Invalid account|"
               r"Requested node configuration is not available|"
               r"Invalid qos specification",
        "weight": STRONG,
        "why": "The scheduler rejected the submission — usually an account "
               "name the cluster does not recognise, or a resource request "
               "no node can satisfy.",
        "fix": ["Check the account on the Cluster tab against sacctmgr.",
                "Lower the memory or CPU request if no node is that large."],
    },
    {
        "id": "module-missing",
        "title": "A cluster module could not be loaded",
        "log": r"Unable to locate a modulefile|"
               r"module: command not found|Lmod has detected",
        "weight": NORMAL,
        "why": "A module named on the Cluster tab does not exist on that "
               "system, or the module system is not initialised in a "
               "non-interactive shell.",
        "fix": ["Run module avail on the cluster and copy the exact names.",
                "Remove modules that are not needed — fewer is safer."],
    },
    {
        "id": "singularity-missing",
        "title": "Singularity or Apptainer is not available",
        "log": r"singularity: command not found|"
               r"apptainer: command not found",
        "weight": STRONG,
        "why": "The cluster profile uses containers and the runtime is not "
               "on PATH on the compute nodes.",
        "fix": ["Add singularity or apptainer to the modules list on the "
                "Cluster tab.",
                "Some clusters only provide it on compute nodes, not the "
                "login node."],
    },
    # -------------------------------------------------------- tool quirks
    {
        "id": "ngshmmalign-gzip",
        "title": "ngshmmalign cannot read gzipped reads",
        "log": r"Malformed FASTQ header",
        "weight": NORMAL,
        "why": "ngshmmalign does not accept gzipped FASTQ and reports it as "
               "a malformed header, which points at the wrong thing.",
        "fix": ["Decompress the reads before mapping.",
                "This is a known quirk of the tool, not a problem with your "
                "data."],
    },
    {
        "id": "ngshmmalign-underscore",
        "title": "ngshmmalign rejected a reference header",
        "log": r"ngshmmalign.*(terminate|exception|std::stoi)|"
               r"stoi.*invalid",
        "weight": WEAK,
        "why": "ngshmmalign parses reference headers by splitting on "
               "underscores and expects the second field to be a number, "
               "mirroring SPAdes-style contig names. A header such as "
               "sample_scaffold_from_X crashes it.",
        "fix": ["Strip underscores from the reference header before "
                "mapping.",
                "A header of one plain word is always safe."],
    },
    {
        "id": "phred-autodetect",
        "title": "Quality encoding could not be detected",
        "log": r"Error determining quality encoding|"
               r"Unable to detect quality encoding|"
               r"input file corrupted.*phred",
        "weight": NORMAL,
        "why": "Trimmomatic and SPAdes guess the quality encoding from the "
               "first reads, and the guess fails on short or low-diversity "
               "input.",
        "fix": ["Pass the encoding explicitly: -phred33 for Trimmomatic, "
                "--phred-offset 33 for SPAdes.",
                "Nearly all modern Illumina data is phred33."],
    },
    {
        "id": "shiver-config",
        "title": "shiver could not find its configuration",
        "log": r"config\.sh.*(No such file|not found)|"
               r"shiver_init.*failed",
        "weight": NORMAL,
        "why": "shiver's initialisation step needs a config.sh and the "
               "adapter and primer files it names.",
        "fix": ["Leave --shiver_config unset to use the container's own "
                "default.",
                "If you supply one, upload the adapters and primers it "
                "refers to on the References tab."],
    },
    # ------------------------------------------- results that are not errors
    {
        "id": "qc-structural-fail",
        "title": "The consensus is full length but its genome is out of order",
        "check": lambda r: _qc_verdict(r, "fail"),
        "weight": STRONG,
        "why": "The structural check compares gene order against a "
               "reference. A failure means sequence is displaced, "
               "duplicated or absent — the file can still be full length "
               "with no uncalled bases, which is why length and N count do "
               "not catch it.",
        "fix": ["Do not submit this consensus for resistance interpretation "
                "— the gene coordinates will be wrong.",
                "Compare against another pipeline's output for the same "
                "sample; agreement between others points at this one.",
                "Check the assembler's contigs to see whether the disorder "
                "starts before or during reference construction."],
    },
    {
        "id": "qc-structural-warn",
        "title": "The structural check raised a warning",
        "check": lambda r: _qc_verdict(r, "warn"),
        "weight": NORMAL,
        "why": "Something is irregular but below the threshold for failure "
               "— often a short absent block at a genome end, where "
               "coverage usually drops anyway.",
        "fix": ["Look at which region the finding names before deciding.",
                "A gap outside the genes you care about may not matter."],
    },
    {
        "id": "qc-missing-block",
        "title": "A block of the genome is missing entirely",
        "check": lambda r: _qc_has(r, "absent") or _qc_has(r, "missing"),
        "weight": NORMAL,
        "why": "A stretch of the reference has no counterpart anywhere in "
               "the consensus. If it covers protease or reverse "
               "transcriptase, resistance interpretation for those genes "
               "will fail or be wrong.",
        "fix": ["Check which coordinates are named against HXB2 gene "
                "boundaries: PR 2253-2549, RT 2550-4229, IN 4230-5096.",
                "Low coverage over that region in the original reads is the "
                "benign explanation; check depth before assuming assembly "
                "error."],
    },
    {
        "id": "many-uncalled",
        "title": "A large part of the consensus is uncalled",
        "check": lambda r: bool(r.get("consensus_stats"))
                           and r["consensus_stats"].get("length", 0) > 0
                           and (r["consensus_stats"].get("N", 0)
                                + r["consensus_stats"].get("no_coverage_?", 0))
                               / r["consensus_stats"]["length"] > 0.10,
        "weight": NORMAL,
        "why": "More than a tenth of the positions could not be called. "
               "That is a coverage problem in the reads, not usually a "
               "pipeline fault.",
        "fix": ["Check read depth and the total data size for this sample.",
                "Lowering the minimum depth will call more positions, and "
                "call some of them wrongly.",
                "A sample this sparse may simply need re-sequencing."],
    },
    {
        "id": "cancelled",
        "title": "The run was cancelled",
        "check": lambda r: r.get("status") == "cancelled",
        "weight": STRONG,
        "why": "Someone pressed Cancel, or the application was asked to "
               "stop this run.",
        "fix": ["Start it again if that was not intended.",
                "Partial output in the results folder is incomplete and "
                "should not be used."],
    },
    {
        "id": "interrupted",
        "title": "The application closed while this run was going",
        "check": lambda r: r.get("status") == "interrupted",
        "weight": STRONG,
        "why": "The run was still active when the application last exited. "
               "Nextflow may have carried on and finished it in the "
               "background, or it may have stopped with the session.",
        "fix": ["Check the results folder — the output may be complete.",
                "Nextflow can resume where it stopped rather than starting "
                "over, which saves the work already done."],
    },
]


def _consensus_stats(path):
    """Base composition of a consensus, for the coverage rules.

    Computed here rather than taken from the caller so that a rule can rely
    on it being present. Kept cheap: these files are about 10 kb.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            seq = "".join(l.strip() for l in fh if not l.startswith(">"))
    except OSError:
        return None
    if not seq:
        return None
    up = seq.upper()
    return {"length": len(seq),
            "called_bases": sum(up.count(b) for b in "ACGT"),
            "N": up.count("N"),
            "no_coverage_?": up.count("?"),
            "gaps": up.count("-")}


def prepare(run, profile=None):
    """A copy of the run with the derived fields the rules expect."""
    enriched = dict(run or {})
    if profile is not None:
        enriched.setdefault("profile", profile)
    if "consensus_stats" not in enriched and enriched.get("consensus"):
        stats = _consensus_stats(enriched["consensus"])
        if stats:
            enriched["consensus_stats"] = stats
    return enriched


def _match_log(rule, log):
    pattern = rule.get("log")
    if not pattern or not log:
        return None
    m = re.search(pattern, log, re.IGNORECASE | re.MULTILINE)
    return m.group(0).strip()[:160] if m else None


def diagnose(run, log="", profile=None):
    """Everything this table can say about one run, best match first.

    Returns a list of dicts with id, title, why, fix, weight and the log
    line that triggered it, if any.
    """
    run = prepare(run, profile)
    found = []
    for rule in RULES:
        evidence, hit = None, False

        line = _match_log(rule, log)
        if line:
            hit, evidence = True, line

        code = rule.get("exit_code")
        if code is not None and run.get("exit_code") == code:
            hit = True
            evidence = evidence or f"exit code {code}"

        check = rule.get("check")
        if check is not None:
            try:
                if check(run):
                    hit = True
                    evidence = evidence or "matched on the run's own record"
            except Exception:                               # noqa: BLE001
                pass            # a rule must never break the whole report

        if hit:
            weight = rule.get("weight", NORMAL)
            # A rule that matched both a log line and an exit code is more
            # certain than one that matched only a regex.
            if line and code is not None and run.get("exit_code") == code:
                weight += 10
            found.append({"id": rule["id"], "title": rule["title"],
                          "why": rule["why"], "fix": list(rule["fix"]),
                          "weight": weight, "evidence": evidence})

    found.sort(key=lambda d: -d["weight"])
    return found


def summarise(run, log="", profile=None):
    """A readable report, or an empty string when nothing matched."""
    hits = diagnose(run, log, profile)
    if not hits:
        return ""
    out = []
    for i, h in enumerate(hits, 1):
        out.append(f"{i}. {h['title']}")
        out.append("")
        out.append(f"   {h['why']}")
        out.append("")
        for f in h["fix"]:
            out.append(f"   - {f}")
        if h["evidence"] and not h["evidence"].startswith("matched on"):
            out.append("")
            out.append(f"   matched: {h['evidence']}")
        out.append("")
    return "\n".join(out).rstrip()


def no_match_advice(run):
    """What to say when the table has nothing."""
    status = run.get("status")
    if status == "complete":
        return ("Nothing known went wrong with this run, and the quality "
                "checks raised nothing.\n\nIf something still looks off, "
                "ask the model below — this table only covers failures that "
                "have been seen before.")
    return ("This failure does not match anything in the built-in table.\n\n"
            "That usually means it is specific to your data or your system. "
            "Ask the model below, which can read the whole log, or send the "
            "log to whoever maintains the pipeline.")
