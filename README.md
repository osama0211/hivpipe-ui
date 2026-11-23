# HIV pipeline runner

Run any of five HIV-1 assembly pipelines from a browser, on Linux. Pick a
sample, pick a pipeline, press Start. No command line, no Nextflow syntax.

```bash
pip install -r requirements.txt
python app.py          # then open http://localhost:8000
```

Under Windows, run it **inside WSL** and open the address in your Windows
browser. That is not a limitation of this application: Nextflow needs Bash
and a POSIX system, and is supported on Linux, macOS, and on Windows only
through WSL. A Windows-native build cannot run the pipelines at all.

## What it does

| | |
|---|---|
| **Samples** | Scans a reads folder recursively and pairs R1/R2 automatically. |
| **Pipelines** | shiver, V-pipe, SmaltAlign, viral-ngs, HIVGenoPipe — each in its own container, orchestrated by Nextflow. |
| **References** | Upload and manage reference genomes, primers, adapters and panels. |
| **Resources** | One memory budget becomes per-process limits, with a verdict per pipeline. A budget that cannot run the chosen pipeline is refused before Nextflow starts. |
| **Cluster** | Send runs to an HPC over SSH, with key *or* password authentication. |
| **Quality** | Every consensus is checked for structural rearrangement before you see it. |
| **Interpret** | An offline table diagnoses known failures instantly; a language model is an optional fallback. |

## Interpreting a run

Open a run and the **Interpret** panel appears under its log.

**Check offline** runs first and costs nothing. It matches the run against a
built-in table of about thirty known failures — container problems, memory
kills, scheduler rejections, tool quirks, structural QC verdicts — and gives
the cause and what to change. No model, no network, no key, and it cannot
invent a cause that was not there.

**Ask a model** is the fallback for what the table does not cover, and for
open-ended work such as drafting a methods paragraph. It needs an **API key**
from `console.anthropic.com` or `platform.openai.com` — not your claude.ai or
chatgpt.com login, which cannot be used from another program. Or choose
**Ollama** to run a model on the same machine, where nothing leaves it.

By default the model is sent run parameters, metrics, QC verdicts and a log
excerpt, with paths reduced to filenames, sample names replaced by generic
labels, and sequence lines withheld. Consensus statistics are computed
locally so the sequence need not be sent at all. Three checkboxes relax each
of those, and **Show what will be sent** prints the exact payload first.

The assistant does not give drug-resistance or clinical interpretations.
Stanford HIVdb is the validated source for those.

Settings, references and results go in a per-user folder:
`%LOCALAPPDATA%\HIVPipeRunner` on Windows,
`~/Library/Application Support/HIVPipeRunner` on macOS,
`~/.local/share/hivpipe-runner` on Linux.

### What works without anything else installed

Browsing samples, managing reference and primer files, the structural check
on a consensus, and **running pipelines on an HPC cluster** — all built in.

### What needs Docker Desktop

Running the assembly pipelines **on your own computer**. shiver, V-pipe,
viral-ngs and the rest are Linux programs: they cannot run natively on
Windows or macOS, and no amount of packaging changes that. Docker Desktop
supplies the Linux environment they need, and installs its own WSL2 backend
on Windows, so you do not set that up separately.

The application checks for Docker at startup and says so in a banner rather
than failing partway through a run. If your work is on a cluster, you never
need it.

## Memory on a laptop

The Memory to use control sets how much RAM the pipelines may take. It
matters more than it sounds: Docker Desktop on Windows takes a fraction of
the host's RAM by default, and Nextflow refuses to start a process that asks
for more than is available — the run fails before any tool executes.

The app reads what Docker will actually give out and suggests a budget from
that, not from what the laptop has. Where the two differ it says so, since
raising the Docker limit is usually the fix.

Each pipeline carries a realistic floor and a comfortable figure:

| Pipeline | Will not start below | Realistic minimum | Assumes |
|---|---|---|---|
| shiver | 3 GB | 6 GB | 16 GB |
| V-pipe | 2 GB | 4 GB | 8 GB |
| SmaltAlign | 2 GB | 4 GB | 8 GB |
| viral-ngs | 6 GB | 12 GB | 32 GB |
| HIVGenoPipe | 3 GB | 6 GB | 16 GB |

Choosing a budget below the floor is refused with an explanation; between the
floor and the realistic minimum it warns and lets you proceed. The limits are
written to a config file per run, so you can see exactly what was applied in
`runs/<id>/run.config`.

viral-ngs is the one to watch — Trinity genuinely wants more memory than most
laptops have. On an 8 GB machine, prefer shiver or V-pipe locally and send
viral-ngs to the cluster.

## Running from source

```bash
pip install -r requirements.txt
pip install pywebview        # optional: gives a native window
python desktop.py
```

Where the interface appears depends on what the system can show:

| Environment | What happens |
|---|---|
| Windows or macOS with pywebview | A native application window |
| WSL | Your Windows browser opens; pywebview is skipped |
| Linux server, or no GUI toolkit | The URL is printed; open it yourself |

## Running under Windows

Run it inside WSL and open `http://localhost:8000` in your normal Windows
browser — WSL forwards the port automatically, so nothing extra is needed.

```bash
wsl                                   # or open Ubuntu from the Start Menu
cd /mnt/c/Users/<you>/hivpipe-ui
pip install -r requirements.txt
python3 app.py
```

Keep the reads on the Linux side if you can (`~/reads` rather than
`/mnt/c/...`): the Windows filesystem is reachable from WSL but noticeably
slower, which matters for assembly.

For Docker runs, enable your distribution under Docker Desktop →
Settings → Resources → WSL integration, then check inside WSL that
`docker ps` works.

## Other ways to run it

**Docker Compose** — everything in containers, nothing installed on the host:

```bash
cp .env.example .env && $EDITOR .env
docker compose --profile setup run --rm warm-images
docker compose up -d                  # http://localhost:8000
```

**A desktop window instead of a browser** — `python gui.py` opens the same
application as a native window with no server and no port. It needs tkinter
(`sudo apt install python3-tk`) and a display; on Windows 11 WSLg provides
one. The packaging under `packaging/` builds that window into a standalone
executable. It is kept because it works, but it is not the supported path:
a Windows-native build cannot run Nextflow, so the pipelines would not work
from it.

---


Pick a sample, pick one of five assembly pipelines, press Start. Each pipeline
runs in its own container, orchestrated by Nextflow, and every consensus is
checked for structural rearrangement before you see it.

No command line after `docker compose up`. Nothing leaves the machine.

## Test it first

```bash
pip install -r requirements.txt
curl -s https://get.nextflow.io | bash        # leaves ./nextflow here
bash selftest.sh
```

(`bash selftest.sh` rather than `./selftest.sh` — unzipping on Windows or WSL
drops the executable bit. `chmod +x selftest.sh` fixes it permanently.)

A minute, no containers, no assembly tools. It checks dependencies, confirms
the structural check passes a correct genome and fails a deliberately broken
one, and runs all five pipeline paths through the workflow with stub steps.
Expect `18 passed, 0 failed`.

Do this before pulling images — if something is wrong with the installation,
this tells you in a minute rather than after an hour of downloads.

## Quick start

First check you have Compose v2, not just Docker:

```bash
docker compose version
```

If that says `unknown command`, you have Docker without the Compose plugin —
the `docker.io` package in Ubuntu's own repository does not include it. Add
Docker's repository and install it:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
     -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-compose-plugin
docker compose version
```

Then:

```bash
docker compose --profile setup run --rm warm-images   # once: pull and build pipelines
docker compose up -d
```

Open http://localhost:8000.

The warm-images step takes a while — viral-ngs alone is several GB. Doing it
up front means the first real run does not stall on a download.

Point it at your own data by setting these before `docker compose up`:

```bash
export HIVPIPE_DATA=/scratch/olzhas/reads    # your FASTQ files
export HIVPIPE_RUNS=/scratch/olzhas/runs     # results
export HIVPIPE_REFS=/scratch/olzhas/refs     # references and primers
export HIVPIPE_WORK=/scratch/olzhas/work     # Nextflow work directory
```

## A note on best5

The synthesized pipeline that takes the best step from each of these five is
a separate project, not an option here. This tool compares the five as they
are; mixing in a sixth that is derived from them would muddle what is being
compared.

## Why containers per pipeline, not one big image

shiver needs IVA, smalt and BLAST. viral-ngs needs Trinity and a JVM. V-pipe
drives its own conda environments. Resolving all of that into a single image
produces something enormous and fragile, and a version bump in one tool breaks
the others.

Nextflow starts the right container per process instead. Each pipeline keeps
the dependency tree its authors tested, and swapping a version means editing
one line in `nextflow.config`.

| Pipeline | Image |
|---|---|
| shiver | `ghcr.io/hcovlab/dshiver` — the published build from Zsichla et al. |
| V-pipe | `ghcr.io/cbg-ethz/v-pipe` |
| viral-ngs | `quay.io/broadinstitute/viral-assemble` |
| SmaltAlign | built here, `containers/smaltalign/` |
| HIVGenoPipe | built here, `containers/hivgenopipe/` |

## Reference files and primers

The Reference files panel takes four kinds of FASTA, uploaded through the
browser:

- **Reference genome** — for mapping and for the structural check
- **Primers** — your amplification primers, trimmed from reads
- **Adapters** — sequencing adapters
- **Reference panel** — the multi-sequence panel shiver searches for its
  closest reference

Each pipeline only uses some of these, and the panel greys out the ones the
selected pipeline ignores. Uploads are validated as FASTA and stored under
`refs/<kind>/`, so they persist and are reusable across runs.

If you leave a slot empty, the pipeline's container default applies.

## The structural check

Genome fraction, mismatch rate and gap count can all look excellent while a
consensus has its genome in the wrong order. A block can be duplicated, moved
several kilobases from where it belongs, or dropped entirely, and none of the
usual summary metrics will show it.

The check walks windows along a reference and asks where each one landed in
the consensus. Windows sitting far from the dominant offset are a
translocation. Windows appearing twice, closer together than the terminal
repeats, are a duplication. Runs of absent windows are missing blocks.

It needs no truth set. Viral genome organisation is fixed, so a consensus with
*vif* sequence inside *gag* is detectably wrong without knowing the sample's
true sequence — which is what makes it usable on routine clinical samples,
where a benchmark sequence never exists.

Verdicts: **pass** (colinear), **warn** (missing sequence worth checking),
**fail** (rearrangement found — do not submit for resistance interpretation
without inspecting it first).

A run can exit 0 and still fail this check. When that happens the status bar
says "Finished, consensus unusable" rather than showing a green result.

## Running on an HPC cluster

Switch the Samples panel from **This machine** to **HPC cluster**. The app
keeps running on your laptop; the pipelines run on the cluster.

What happens when you press Start: the workflow files and any reference FASTA
you selected are copied to the cluster, Nextflow is launched there detached
from the SSH session, each pipeline step is submitted to the scheduler, the
log streams back live, and the results are pulled down when it finishes. You
can close the browser; the job keeps going.

Reads are read from the cluster's own filesystem and never transferred, so
this works on datasets far too large to copy.

### Signing in

Pick **SSH key or agent** or **Password** in the Sign in with field.

**SSH key or agent** shells out to your system `ssh`, so `~/.ssh/config`
applies — ProxyJump through a bastion, ssh-agent, hardware keys and host
aliases all behave exactly as they do when you connect by hand. If
`ssh yourcluster` works in your terminal, it works here. This is the better
option when your cluster allows it: nothing has to be typed again after a
restart, and it survives two-factor setups.

A jump host goes in `~/.ssh/config`, and you give the alias as the host:

```
Host hpc
    HostName login.cluster.example
    User olzhas
    ProxyJump bastion.example
    IdentityFile ~/.ssh/id_ed25519
```

**Password** is for clusters that do not allow keys. It needs `paramiko`,
which is in `requirements.txt`; the system `ssh` client refuses to read a
password from anything but a terminal, so this path uses a Python SSH library
instead.

The password is held in memory for as long as the app is running and is never
written to `hpc.yaml` or any log. You re-enter it after a restart, and
**Forget password** clears it immediately. `~/.ssh/config` does not apply on
this path, so a jump host has to be reachable directly.

Two-factor login is not supported with a password. If your cluster asks for a
code or token as well, use a key.

### Settings

| Field | What it is |
|---|---|
| Host, User, Port | or just a `~/.ssh/config` alias in Host |
| Sign in with | SSH key or agent, or Password |
| SSH key | leave blank to use ssh-agent |
| Password | kept in memory only, never written to disk |
| Working directory | where run folders are created on the cluster |
| Reads folder | where your FASTQ files already live there |
| Scheduler | Slurm, PBS, SGE, or none to run on the login node |
| Partition, Account | passed to the scheduler |
| Containers | Singularity is usual on HPC; Docker rarely available |
| Modules | `module load` lines run before Nextflow |
| Nextflow command | a full path if it is not on PATH |

**Test connection** checks each of these separately and tells you which one
failed, rather than giving you one opaque error.

Settings are saved to `hpc.yaml` next to the app. It holds no password — only
a host, paths, and optionally a key path — but it does describe your cluster,
so do not commit it.

### Notes

- Cancelling stops Nextflow on the login node and runs `scancel` on the jobs
  it had submitted.
- Losing the network does not kill a run. The app keeps retrying, and the job
  continues on the cluster regardless.
- Run folders stay on the cluster under the working directory, so you can
  inspect `work/` for a failed task or `-resume` by hand.
- A run marked `interrupted` was being watched when the app stopped. The
  cluster job itself probably finished; its folder is still there.

## Running without Docker

Nextflow and Python are enough if the pipelines are already installed:

```bash
curl -s https://get.nextflow.io | bash && sudo mv nextflow /usr/local/bin/
pip install -r requirements.txt
sed -i 's/^profile: docker/profile: local/' settings.yaml
python app.py
```

Profiles in `nextflow.config`:

| Profile | Use |
|---|---|
| `docker` | default; containers on the local Docker daemon |
| `singularity` | HPC without Docker |
| `slurm` | submits each process as a Slurm job, with Singularity |
| `local` | no containers; tools must be on PATH |
| `stub` | exercises the workflow with no tools at all |
| `lowmem` | add-on; keeps assembly within laptop limits |

Combine them: `-profile slurm,lowmem`.

For Slurm, set `HIVPIPE_QUEUE` to your partition.

## The workflow on its own

The UI is a convenience. The workflow runs standalone:

```bash
nextflow run main.nf -profile docker \
  --sample DRR030147 \
  --reads1 reads/DRR030147_1.fastq.gz \
  --reads2 reads/DRR030147_2.fastq.gz \
  --pipeline shiver \
  --reference refs/HXB2.fasta \
  --primers refs/primers/my_primers.fasta \
  --outdir results/DRR030147
```

That matters for the scale-up: looping this over 40 samples needs no web app,
and `-resume` picks up where an interrupted batch stopped.

The check is also importable on its own:

```python
from qc import run_qc
report = run_qc("consensus.fasta", "refs/HXB2.fasta")
print(report["verdict"], report["summary"])
```

Or from the shell, with an exit code you can branch on:

```bash
bin/structural_qc.py --consensus cons.fasta --reference refs/HXB2.fasta --fail-on fail
```

## Calibrating the check

`tests/make_rearrangement.py` injects a rearrangement of known kind, size and
position. It is the self-test's fixture generator, and it is also how you find
out where detection stops:

```bash
for bp in 50 100 200 400 800 1600; do
    tests/make_rearrangement.py --input refs/HXB2.fasta \
        --output sweep/r_${bp}.fasta --kind replacement \
        --size $bp --source 2600 --dest 7000
    printf '%5s bp  ' $bp
    bin/structural_qc.py --consensus sweep/r_${bp}.fasta \
        --reference refs/HXB2.fasta | head -1
done
```

`--kind replacement` is the shape real misassemblies take: a region is lost
and the space filled with a duplicate of somewhere else, so the consensus
keeps its length and looks complete.

Feeding the same files to a resistance algorithm instead of this check shows
where *that* stops noticing — which is a different and more consequential
boundary, since a consensus can be rearranged and still be interpreted.

## Files

```
docker-compose.yml   the stack
Dockerfile           app image: web UI + Nextflow + the check
main.nf              workflow: one sample, one pipeline, then QC
nextflow.config      container images, resource labels, profiles
modules/*.nf         one module per pipeline, plus qc.nf
containers/          Dockerfiles for pipelines without published images
bin/structural_qc.py the check as a command
qc.py                the check as a library
app.py               web server and run manager
static/index.html    the interface
settings.yaml        paths and defaults, overridable by environment
```

## Security

`docker-compose.yml` mounts the Docker socket into the app container so
Nextflow can start sibling containers. That grants control of the host Docker
daemon, which is effectively root on the host.

The port is bound to `127.0.0.1` for that reason. Run this on a machine you
control, and put authentication in front of it before exposing it further. On
shared HPC, use the `slurm` profile with Singularity instead — no socket is
needed.

## Notes

- A run marked `interrupted` was executing when the server stopped. Its output
  folder and Nextflow work directory are intact; `-resume` would pick it up.
- Stopping a run signals the process group, so Nextflow shuts its tasks down
  in order rather than leaving containers behind.
- `runs/<id>/` holds `command.txt`, `run.log`, `nextflow.log`, `run.json` and
  `output/`. The trace, timeline and report Nextflow writes are under
  `output/_trace/`.

## License

This project's own code — the Nextflow workflows, the QC scripts, the web UI
and the cluster launcher — is MIT licensed. See [LICENSE](LICENSE).

That covers the wrapper, not what it wraps. The five assembly pipelines and
the container images they run in are separate works under their own licenses,
and nothing here changes or extends those terms:

| Tool | Where it comes from |
|---|---|
| shiver | ChrisHIV/shiver, run via `ghcr.io/hcovlab/dshiver` |
| V-pipe | cbg-ethz/V-pipe, run via `ghcr.io/cbg-ethz/v-pipe` |
| SmaltAlign | SmaltAlign, built locally from `containers/smaltalign/` |
| viral-ngs | Broad Institute viral-ngs (`broadinstitute/viral-assemble`) |
| HIVGenoPipe | HIVGenoPipe, built locally from `containers/hivgenopipe/` |

If you publish results produced with this runner, cite the pipelines
themselves — this repository orchestrates them, it does not replace them.
`versions.yml` in each run's output records the versions actually used.

`refs/HXB2.fasta` is the HIV-1 HXB2 reference (GenBank K03455). Files under
`demo_data/` derive from the public accession DRR030147 plus synthetic reads
for testing. No patient-derived data is included, and none should be added —
see the `.gitignore` entries for `data/`.
