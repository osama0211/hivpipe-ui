"""HIV pipeline runner — a local web UI that drives a Nextflow workflow.

Start it with:   python app.py
Then open:       http://localhost:8000

Every run is `nextflow run main.nf`, which starts the chosen pipeline in its
own container and then runs the structural check on whatever consensus comes
out. Paths come from settings.yaml, overridable by environment variables so
docker-compose can set them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import assistant
import diagnostics
import hpc
import resources

# Inside a PyInstaller bundle the workflow files are unpacked to a temporary
# directory, which is where main.nf and the modules live. HIVPIPE_APP_DIR is
# set by desktop.py; the fallback covers running from a checkout.
HERE = Path(os.environ.get("HIVPIPE_APP_DIR")
            or getattr(sys, "_MEIPASS", "")
            or Path(__file__).resolve().parent)

PIPELINES = [
    # `uses` is advisory — what the pipeline can take. `requires` is enforced
    # before a run starts. shiver corrects each contig against the closest
    # entry in the reference panel, so the panel decides the consensus; with
    # none, shiver either refuses or silently falls back to its demonstration
    # alignment, and the result looks plausible while meaning nothing.
    {"id": "shiver", "name": "shiver",
     "blurb": "De novo contigs corrected against a tailored reference",
     "image": "ghcr.io/hcovlab/dshiver:latest",
     "uses": ["reference", "primers", "adapters", "panel"],
     "requires": ["panel"]},
    {"id": "vpipe", "name": "V-pipe",
     "blurb": "ngshmmalign to a fixed reference, Snakemake workflow",
     "image": "ghcr.io/cbg-ethz/v-pipe:master",
     "uses": ["reference"]},
    {"id": "smaltalign", "name": "SmaltAlign",
     "blurb": "Iterative read mapping back onto its own consensus",
     "image": "hivpipe/smaltalign:1.0.0",
     "uses": ["reference"]},
    {"id": "viralngs", "name": "viral-ngs",
     "blurb": "Reference-ordered contigs with gap imputation",
     "image": "quay.io/broadinstitute/viral-assemble:2.4.1.0",
     "uses": ["reference", "adapters"]},
    {"id": "hivgenopipe", "name": "HIVGenoPipe",
     "blurb": "Nextflow, gag-pol, real-time sample-specific reference",
     "image": "hivpipe/hivgenopipe:1.0.0",
     "uses": ["reference"]},
]

# Uploaded reference material, by kind.
REF_KINDS = {
    "reference": "Reference genome",
    "primers": "Primers",
    "adapters": "Adapters",
    "panel": "Reference panel",
}


# ---------------------------------------------------------------- config


def state_dir():
    """Where settings the user changes are written.

    Never next to the executable: in a packaged install that is Program Files
    or /Applications, which a normal user cannot write to.
    """
    env = os.environ.get("HIVPIPE_RUNS_DIR")
    if env:
        return Path(env).parent
    return HERE


STATE_DIR = state_dir()


def load_settings():
    data = yaml.safe_load((HERE / "settings.yaml").read_text()) or {}
    env = {
        "data_dir": "HIVPIPE_DATA_DIR",
        "runs_dir": "HIVPIPE_RUNS_DIR",
        "ref_dir": "HIVPIPE_REFS_DIR",
        "work_dir": "HIVPIPE_WORK_DIR",
        "profile": "HIVPIPE_PROFILE",
        "threads": "HIVPIPE_THREADS",
    }
    for key, var in env.items():
        if os.environ.get(var):
            data[key] = os.environ[var]
    return data


SETTINGS = load_settings()


def abspath(p, default):
    p = Path(os.path.expanduser(str(p or default)))
    return p if p.is_absolute() else (HERE / p).resolve()


DATA_DIR = abspath(SETTINGS.get("data_dir"), "./demo_data")
RUNS_DIR = abspath(SETTINGS.get("runs_dir"), "./runs")
REF_DIR = abspath(SETTINGS.get("ref_dir"), "./refs")
WORK_DIR = abspath(SETTINGS.get("work_dir"), "./work")
PROFILE = str(SETTINGS.get("profile", "docker"))
THREADS = int(SETTINGS.get("threads", 8))
MAX_CONCURRENT = int(SETTINGS.get("max_concurrent", 1))

for d in (RUNS_DIR, WORK_DIR, REF_DIR):
    d.mkdir(parents=True, exist_ok=True)
for kind in REF_KINDS:
    (REF_DIR / kind).mkdir(parents=True, exist_ok=True)

NEXTFLOW = (os.environ.get("HIVPIPE_NEXTFLOW")
            or shutil.which("nextflow")
            or str(HERE / "nextflow"))

# ---------------------------------------------------------------- cluster

HPC_FILE = STATE_DIR / "hpc.yaml"


def load_hpc():
    if HPC_FILE.exists():
        try:
            return yaml.safe_load(HPC_FILE.read_text()) or {}
        except yaml.YAMLError:
            pass
    return dict(SETTINGS.get("hpc") or {})


def save_hpc(config):
    HPC_FILE.write_text(yaml.safe_dump(config, sort_keys=False))


HPC_CONFIG = load_hpc()


def cluster():
    return hpc.Cluster(HPC_CONFIG)

FASTQ_SUFFIX = re.compile(r"\.(fastq|fq)(\.gz)?$", re.I)
FASTA_SUFFIX = re.compile(r"\.(fa|fasta|fas|fna)(\.gz)?$", re.I)
PAIR_TOKEN = re.compile(r"([._])(R?)([12])(?=[._])|([._])(R?)([12])$", re.I)
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# ---------------------------------------------------------------- samples


def _pair_key(stem):
    m = None
    for m in PAIR_TOKEN.finditer(stem):
        pass  # the last match is the one closest to the extension
    if not m:
        return stem, None
    return stem[:m.start()] + stem[m.end():], int(m.group(3) or m.group(6))


def group_fastq(entries, root):
    """Turn (size, path) pairs into paired samples. Shared by local and remote."""
    groups = {}
    for size, path in entries:
        name = os.path.basename(path)
        key, mate = _pair_key(FASTQ_SUFFIX.sub("", name))
        folder = os.path.dirname(path)
        rel = os.path.relpath(folder, root) if root else "."
        gid = f"{rel}/{key}" if rel not in (".", "") else key
        g = groups.setdefault(gid, {"id": gid, "name": key, "folder": rel,
                                    "r1": None, "r2": None, "single": None, "bytes": 0})
        g["bytes"] += size
        if mate == 1:
            g["r1"] = path
        elif mate == 2:
            g["r2"] = path
        else:
            g["single"] = path

    out = []
    for g in groups.values():
        if g["r1"] and not g["r2"]:
            g["single"], g["r1"] = g["r1"], None
        paired = bool(g["r1"] and g["r2"])
        out.append({
            "id": g["id"], "name": g["name"], "folder": g["folder"],
            "paired": paired,
            "r1": g["r1"] or g["single"] or "",
            "r2": g["r2"] or "",
            "size_mb": round(g["bytes"] / 1e6, 1),
            "layout": "Paired-end" if paired else "Single-end",
        })
    return sorted(out, key=lambda s: s["name"].lower())


def discover_remote_samples():
    c = cluster()
    if not c.configured or not c.data_dir:
        return []
    return group_fastq(c.samples(), c.data_dir)


def discover_samples():
    if not DATA_DIR.exists():
        return []

    groups = {}
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file() or not FASTQ_SUFFIX.search(path.name):
            continue
        key, mate = _pair_key(FASTQ_SUFFIX.sub("", path.name))
        folder = str(path.parent.relative_to(DATA_DIR))
        gid = f"{folder}/{key}" if folder != "." else key
        g = groups.setdefault(gid, {"id": gid, "name": key, "folder": folder,
                                    "r1": None, "r2": None, "single": None, "bytes": 0})
        g["bytes"] += path.stat().st_size
        if mate == 1:
            g["r1"] = str(path)
        elif mate == 2:
            g["r2"] = str(path)
        else:
            g["single"] = str(path)

    out = []
    for g in groups.values():
        if g["r1"] and not g["r2"]:
            g["single"], g["r1"] = g["r1"], None
        paired = bool(g["r1"] and g["r2"])
        out.append({
            "id": g["id"], "name": g["name"], "folder": g["folder"],
            "paired": paired,
            "r1": g["r1"] or g["single"] or "",
            "r2": g["r2"] or "",
            "size_mb": round(g["bytes"] / 1e6, 1),
            "layout": "Paired-end" if paired else "Single-end",
        })
    return sorted(out, key=lambda s: s["name"].lower())


# ---------------------------------------------------------------- references


def list_references():
    out = {}
    for kind in REF_KINDS:
        files = []
        for p in sorted((REF_DIR / kind).glob("*")):
            if p.is_file() and FASTA_SUFFIX.search(p.name):
                files.append({"name": p.name, "path": str(p),
                              "size_kb": round(p.stat().st_size / 1024, 1)})
        # Loose files in the top-level refs folder count as reference genomes.
        if kind == "reference":
            for p in sorted(REF_DIR.glob("*")):
                if p.is_file() and FASTA_SUFFIX.search(p.name):
                    files.append({"name": p.name, "path": str(p),
                                  "size_kb": round(p.stat().st_size / 1024, 1)})
        out[kind] = files
    return out


def default_reference():
    for name in ("HXB2.fasta", "HXB2.fa", "reference.fasta"):
        p = REF_DIR / name
        if p.exists():
            return str(p)
    files = list_references()["reference"]
    return files[0]["path"] if files else None


# ---------------------------------------------------------------- runs

RUNS = {}
RUNS_LOCK = threading.Lock()
QUEUE = []


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_path(run_id):
    return RUNS_DIR / run_id


def save_run(run):
    d = run_path(run["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(run, indent=2))


def load_runs():
    for meta in sorted(RUNS_DIR.glob("*/run.json")):
        try:
            run = json.loads(meta.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if run.get("status") in ("running", "cancelling"):
            run["status"] = "interrupted"
            run["finished_at"] = run.get("finished_at") or now_iso()
        RUNS[run["id"]] = run


def write_run_config(run):
    """Pin this run's memory and CPU limits in a config Nextflow reads."""
    path = run_path(run["id"]) / "run.config"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resources.render_config(
        run.get("memory_gb") or resources.budget_gb(run.get("location", "local")),
        run.get("threads"),
        run.get("location", "local"),
    ))
    return path


def nextflow_command(run):
    outdir = run_path(run["id"]) / "output"
    cmd = [
        NEXTFLOW, "-log", str(run_path(run["id"]) / "nextflow.log"),
        "run", str(HERE / "main.nf"),
        "-profile", PROFILE,
        "-c", str(write_run_config(run)),
        "-work-dir", str(WORK_DIR / run["id"]),
        "-ansi-log", "false",
        "--sample", run["sample"]["name"],
        "--reads1", run["sample"]["r1"],
        "--pipeline", run["pipeline_id"],
        "--outdir", str(outdir),
    ]
    # The stub profile only turns containers off; selecting the stub blocks
    # themselves is a separate flag.
    if PROFILE == "stub":
        cmd.append("-stub-run")
    if run["sample"]["r2"]:
        cmd += ["--reads2", run["sample"]["r2"]]
    for kind, flag in (("reference", "--reference"), ("primers", "--primers"),
                       ("adapters", "--adapters"), ("panel", "--refs_fasta")):
        value = run["refs"].get(kind)
        if value:
            cmd += [flag, value]

    return cmd


def find_outputs(run):
    """Locate the consensus and QC report Nextflow published."""
    outdir = run_path(run["id"]) / "output"
    consensus = next(iter(sorted(outdir.rglob("*_consensus.fasta"))), None)
    report = None
    qc_json = next(iter(sorted(outdir.rglob("*_qc.json"))), None)
    if qc_json:
        try:
            report = json.loads(qc_json.read_text())
        except (json.JSONDecodeError, OSError):
            report = None
    return (str(consensus) if consensus else None), report


def execute(run_id):
    with RUNS_LOCK:
        run = RUNS[run_id]
        run["status"] = "running"
        run["started_at"] = now_iso()
        save_run(run)

    d = run_path(run_id)
    d.mkdir(parents=True, exist_ok=True)
    cmd = nextflow_command(run)
    (d / "command.txt").write_text(" ".join(cmd) + "\n")
    log_path = d / "run.log"

    try:
        with open(log_path, "w", buffering=1) as log:
            log.write(f"# {run['pipeline_name']} on {run['sample']['name']}\n")
            log.write(f"# {' '.join(cmd)}\n\n")
            log.flush()
            proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(d), start_new_session=True,
                env={**os.environ,
                     "NXF_ANSI_LOG": "false",
                     # Keeps the version ping out of the run log; it fails
                     # noisily on machines without outbound internet.
                     "NXF_DISABLE_CHECK_LATEST": "true"},
            )
            with RUNS_LOCK:
                run["pid"] = proc.pid
                save_run(run)
            code = proc.wait()
    except FileNotFoundError:
        with open(log_path, "a") as log:
            log.write("\n[runner] Nextflow was not found. Install it with\n"
                      "  curl -s https://get.nextflow.io | bash\n"
                      "or set HIVPIPE_NEXTFLOW to its path.\n")
        code = 127
    except Exception as exc:  # noqa: BLE001
        with open(log_path, "a") as log:
            log.write(f"\n[runner] {exc}\n")
        code = -1

    consensus, report = find_outputs(run) if code == 0 else (None, None)

    with RUNS_LOCK:
        run["exit_code"] = code
        run["finished_at"] = now_iso()
        run["pid"] = None
        if run.get("status") == "cancelling":
            run["status"] = "cancelled"
        else:
            run["status"] = "complete" if code == 0 else "failed"
        run["consensus"] = consensus
        run["qc"] = report
        save_run(run)

    schedule()


def execute_remote(run_id):
    """Drive a run on the cluster: stage, launch, poll, retrieve."""
    with RUNS_LOCK:
        run = RUNS[run_id]
        run["status"] = "running"
        run["started_at"] = now_iso()
        save_run(run)

    d = run_path(run_id)
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "run.log"
    c = cluster()
    profile = HPC_CONFIG.get("profile") or (
        "slurm" if c.scheduler == "slurm" else "singularity")

    def log(text):
        with open(log_path, "a") as fh:
            fh.write(text)

    log(f"# {run['pipeline_name']} on {run['sample']['name']}\n")
    log(f"# cluster {c.destination}:{c.remote_dir}  profile {profile}\n\n")

    try:
        # The workflow travels with the run, so the cluster needs no prior setup.
        payload = {}
        for name in ("main.nf", "nextflow.config", "qc.py"):
            payload[name] = HERE / name
        payload["run.config"] = write_run_config(run)

        params = {
            "sample": run["sample"]["name"],
            "reads1": run["sample"]["r1"],
            "reads2": run["sample"]["r2"] or None,
            "pipeline": run["pipeline_id"],
            "outdir": "results",
        }

        # Reference files are local uploads, so they go up with the run and
        # are referenced by their staged name.
        for kind, flag in (("reference", "reference"), ("primers", "primers"),
                           ("adapters", "adapters"), ("panel", "refs_fasta")):
            local = run["refs"].get(kind)
            if local and Path(local).exists():
                staged = f"ref_{kind}_{Path(local).name}"
                payload[staged] = Path(local)
                params[flag] = staged

        log(f"Staging {len(payload)} files to the cluster\n")
        remote = c.stage(run_id, payload)

        for folder in ("modules", "bin", "assets"):
            src = HERE / folder
            if src.is_dir():
                c.push(src, f"{remote}/")

        script = hpc.submit_script(c, run_id, params, profile)
        script_local = d / "submit.sh"
        script_local.write_text(script)
        c.push(script_local, f"{remote}/submit.sh")

        log(f"Launching on {c.host}\n\n")
        pid = c.launch(remote)

        with RUNS_LOCK:
            run["remote_dir"] = remote
            run["remote_pid"] = pid
            save_run(run)

        offset = 0
        code = None
        while True:
            time.sleep(5)
            with RUNS_LOCK:
                cancelling = run.get("status") == "cancelling"
            if cancelling:
                c.cancel(remote, pid)
                code = -1
                break
            try:
                chunk = c.tail(remote, offset)
                if chunk:
                    offset += len(chunk.encode("utf-8", "replace"))
                    log(chunk)
                state, rc = c.status(remote, pid)
            except hpc.HPCError as exc:
                log(f"\n[cluster] lost contact: {exc}\n")
                # A dropped connection is not a failed job; keep trying.
                continue
            if state != "running":
                code = rc if rc is not None else 1
                break

        if code == 0:
            log("\nRetrieving results\n")
            got = c.pull(f"{remote}/results", str(d / "output_tmp"))
            tmp = d / "output_tmp"
            out = d / "output"
            if got and tmp.exists():
                if out.exists():
                    shutil.rmtree(out, ignore_errors=True)
                inner = tmp / "results"
                shutil.move(str(inner if inner.exists() else tmp), str(out))
                shutil.rmtree(tmp, ignore_errors=True)
                log("Results retrieved\n")
            else:
                log("Could not retrieve the results folder from the cluster.\n")

    except hpc.HPCError as exc:
        log(f"\n[cluster] {exc}\n")
        code = 1
    except Exception as exc:  # noqa: BLE001
        log(f"\n[runner] {exc}\n")
        code = 1

    consensus, report = find_outputs(run) if code == 0 else (None, None)

    with RUNS_LOCK:
        run["exit_code"] = code
        run["finished_at"] = now_iso()
        if run.get("status") == "cancelling":
            run["status"] = "cancelled"
        else:
            run["status"] = "complete" if code == 0 else "failed"
        run["consensus"] = consensus
        run["qc"] = report
        save_run(run)

    schedule()


def schedule():
    with RUNS_LOCK:
        active = sum(1 for r in RUNS.values()
                     if r.get("status") in ("running", "cancelling"))
        starts = []
        while QUEUE and active < MAX_CONCURRENT:
            starts.append(QUEUE.pop(0))
            active += 1
    for rid in starts:
        target = execute_remote if RUNS[rid].get("location") == "hpc" else execute
        threading.Thread(target=target, args=(rid,), daemon=True).start()


# ---------------------------------------------------------------- API

# Bumped whenever behaviour changes, so "am I running the new build?"
# is answerable from the page and from a run log instead of guessed.
VERSION = "1.1.6"
BUILD_DATE = "2026-11-10"

app = FastAPI(title="HIV pipeline runner", version=VERSION)


class StartRequest(BaseModel):
    pipeline_id: str
    sample_id: str
    threads: int | None = None
    reference: str | None = None
    primers: str | None = None
    adapters: str | None = None
    panel: str | None = None
    location: str = "local"
    memory_gb: float | None = None


class HPCPassword(BaseModel):
    password: str


class HPCConfig(BaseModel):
    host: str = ""
    user: str = ""
    port: int = 22
    key: str = ""
    auth: str = "key"
    remote_dir: str = ""
    data_dir: str = ""
    scheduler: str = "slurm"
    queue: str = ""
    account: str = ""
    container: str = "singularity"
    modules: list[str] = []
    nextflow: str = "nextflow"
    profile: str = ""


@app.get("/api/config")
def api_config():
    return {
        "data_dir": str(DATA_DIR), "runs_dir": str(RUNS_DIR),
        "ref_dir": str(REF_DIR), "work_dir": str(WORK_DIR),
        "profile": PROFILE, "threads": THREADS,
        "max_concurrent": MAX_CONCURRENT,
        "nextflow": NEXTFLOW,
        "version": VERSION, "build_date": BUILD_DATE,
        "default_reference": default_reference(),
        "ref_kinds": REF_KINDS,
    }


@app.get("/api/samples")
def api_samples(location: str = "local"):
    if location == "hpc":
        try:
            return {"samples": discover_remote_samples(), "location": "hpc"}
        except hpc.HPCError as exc:
            raise HTTPException(502, str(exc))
    return {"samples": discover_samples(), "location": "local"}


@app.get("/api/hpc")
def api_hpc_get():
    cfg = dict(HPC_CONFIG)
    c = cluster()
    cfg["configured"] = c.configured
    cfg["needs_password"] = c.needs_password
    cfg["password_held"] = hpc.has_password(c.destination) if c.host else False
    return {"hpc": cfg}


@app.put("/api/hpc")
def api_hpc_put(cfg: HPCConfig):
    global HPC_CONFIG
    old = cluster()
    HPC_CONFIG = cfg.model_dump()
    # The password is deliberately absent from this file. It lives in memory
    # for the life of the process and is re-entered after a restart.
    save_hpc(HPC_CONFIG)
    old.disconnect()
    c = cluster()
    return {"hpc": HPC_CONFIG, "configured": c.configured,
            "needs_password": c.needs_password}


@app.post("/api/hpc/password")
def api_hpc_password(body: HPCPassword):
    c = cluster()
    if not c.host:
        raise HTTPException(400, "Set the cluster host first.")
    if not body.password:
        hpc.clear_password(c.destination)
        return {"password_held": False}
    hpc.set_password(c.destination, body.password)
    return {"password_held": True}


@app.delete("/api/hpc/password")
def api_hpc_forget():
    c = cluster()
    if c.host:
        hpc.clear_password(c.destination)
    return {"password_held": False}


@app.post("/api/hpc/test")
def api_hpc_test():
    c = cluster()
    if not c.configured:
        raise HTTPException(400, "Set at least a host and a working directory.")
    try:
        return c.probe()
    except hpc.HPCError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/pipelines")
def api_pipelines():
    out = []
    for p in PIPELINES:
        spec = resources.PIPELINE_MEMORY.get(p["id"], {})
        out.append({**p, "memory": spec})
    return {"pipelines": out}


@app.get("/api/resources")
def api_resources(location: str = "local", memory_gb: float | None = None):
    budget = memory_gb or resources.budget_gb(location)
    return {
        "machine": resources.summary(location),
        "budget_gb": budget,
        "plan": resources.plan(budget, None, location),
        "assessment": {p["id"]: resources.assess(p["id"], budget)
                       for p in PIPELINES},
    }


@app.get("/api/capabilities")
def api_capabilities():
    """What this machine can actually do, and what each gap costs."""
    import platform

    def tool(name, args=None):
        path = shutil.which(name)
        if not path:
            return {"present": False, "detail": ""}
        try:
            p = subprocess.run(args or [name, "--version"],
                               capture_output=True, text=True, timeout=20)
            first = (p.stdout or p.stderr).strip().splitlines()
            return {"present": True, "detail": first[0][:80] if first else path}
        except Exception:  # noqa: BLE001
            return {"present": True, "detail": path}

    docker = tool("docker", ["docker", "--version"])
    if docker["present"]:
        try:
            p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=25)
            running = p.returncode == 0
        except Exception:  # noqa: BLE001
            running = False
        docker["running"] = running
        if not running:
            docker["detail"] = "Installed, but the daemon is not running."
    else:
        docker["running"] = False

    java = tool("java", ["java", "-version"])
    nf = shutil.which("nextflow") or (
        str(HERE / "nextflow") if (HERE / "nextflow").exists() else "")

    mem = resources.summary()
    checks = [
        {"name": "Browse samples and references", "ok": True,
         "detail": "Built in — nothing to install."},
        {"name": "Structural check on a consensus", "ok": True,
         "detail": "Built in — nothing to install."},
        {"name": "Run on an HPC cluster", "ok": True,
         "detail": "Built in. Needs only an account on the cluster."},
        {"name": "Run pipelines on this computer", "ok": bool(docker["running"]),
         "detail": docker["detail"] if docker["present"]
                   else "Needs Docker Desktop. The pipelines are Linux tools "
                        "and cannot run on Windows without it.",
         "optional": True},
        {"name": "Nextflow", "ok": bool(nf),
         "detail": nf or "curl -s https://get.nextflow.io | bash",
         "optional": True},
        {"name": "Java (Nextflow runs on the JVM)", "ok": java["present"],
         "detail": java["detail"] or "sudo apt install default-jre-headless",
         "optional": True},
    ]
    return {
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "memory": mem,
        "checks": checks,
        "can_run_locally": bool(docker["running"]) and bool(nf),
    }


@app.get("/api/references")
def api_references():
    return {"references": list_references()}


def store_reference(kind, filename, data):
    """Validate and save one reference file.

    Shared by the web endpoint and the desktop GUI so there is exactly one
    place that decides what counts as an acceptable reference file.
    """
    if kind not in REF_KINDS:
        raise HTTPException(400, f"Unknown kind '{kind}'.")
    name = SAFE_NAME.sub("_", Path(filename or "upload.fasta").name)
    if not FASTA_SUFFIX.search(name):
        raise HTTPException(400, "Upload a FASTA file (.fasta, .fa, .fas or .fna).")
    if not data.lstrip().startswith(b">"):
        raise HTTPException(400, "That file does not start with a FASTA header.")

    dest = REF_DIR / kind / name
    dest.write_bytes(data)
    return {"name": name, "path": str(dest), "kind": kind,
            "sequences": data.count(b">"),
            "size_kb": round(len(data) / 1024, 1)}


@app.post("/api/references")
async def api_upload_reference(kind: str = Form(...), file: UploadFile = File(...)):
    return store_reference(kind, file.filename, await file.read())


@app.delete("/api/references")
def api_delete_reference(kind: str, name: str):
    if kind not in REF_KINDS:
        raise HTTPException(400, f"Unknown kind '{kind}'.")
    target = (REF_DIR / kind / SAFE_NAME.sub("_", name)).resolve()
    if REF_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "No such file.")
    target.unlink()
    return {"deleted": name}


@app.get("/api/runs")
def api_runs():
    with RUNS_LOCK:
        runs = sorted(RUNS.values(), key=lambda r: r.get("created_at", ""), reverse=True)
        return {"runs": runs[:60]}


@app.post("/api/runs")
def api_start(req: StartRequest):
    pipeline = next((p for p in PIPELINES if p["id"] == req.pipeline_id), None)
    if not pipeline:
        raise HTTPException(404, "No pipeline with that id.")

    location = "hpc" if req.location == "hpc" else "local"
    if location == "hpc":
        c = cluster()
        if not c.configured:
            raise HTTPException(400, "No cluster configured. Fill in the HPC "
                                     "settings and test the connection first.")
        if c.needs_password:
            raise HTTPException(400, "Enter the cluster password before starting "
                                     "a run.")
        try:
            pool = discover_remote_samples()
        except hpc.HPCError as exc:
            raise HTTPException(502, str(exc))
    else:
        pool = discover_samples()

    sample = next((s for s in pool if s["id"] == req.sample_id), None)
    if not sample:
        raise HTTPException(404, "That sample is no longer in the reads folder.")

    refs = {
        "reference": req.reference or default_reference(),
        "primers": req.primers,
        "adapters": req.adapters,
        "panel": req.panel,
    }
    for kind, value in refs.items():
        if value and not Path(value).is_file():
            raise HTTPException(400, f"{REF_KINDS[kind]} file is missing: {value}")

    # Caught here rather than on the cluster: the pipeline would fail too, but
    # only after staging, queueing and pulling a container, and the message
    # would arrive in a log the person has to go looking for.
    for kind in pipeline.get("requires", []):
        if not refs.get(kind):
            raise HTTPException(
                400,
                f"{pipeline['name']} needs a {REF_KINDS[kind].lower()}. "
                f"Upload one under References and pick it before starting.")

    budget = req.memory_gb or resources.budget_gb(location)

    # A local run cannot exceed what the machine has. Nextflow would refuse
    # it too, but several seconds later and with a less useful message.
    if location == "local":
        machine = resources.summary()
        ceiling = machine["docker_memory_gb"] or machine["host_memory_gb"]
        if ceiling and budget > ceiling:
            where = ("Docker" if machine["docker_memory_gb"] else "This computer")
            raise HTTPException(
                400,
                f"{budget:g} GB was asked for, but {where.lower()} has only "
                f"{ceiling} GB. Lower the memory setting, raise Docker's limit, "
                f"or run this on the cluster.")

    verdict = resources.assess(pipeline["id"], budget)
    if verdict["level"] == "blocked":
        raise HTTPException(400, verdict["message"])

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = {
        "id": run_id,
        "pipeline_id": pipeline["id"], "pipeline_name": pipeline["name"],
        "sample": sample, "refs": refs,
        "threads": int(req.threads or THREADS),
        "memory_gb": budget,
        "memory_note": verdict["message"] if verdict["level"] != "ok" else "",
        "location": location,
        "where": cluster().host if location == "hpc" else "this machine",
        "status": "queued", "created_at": now_iso(),
        "started_at": None, "finished_at": None,
        "exit_code": None, "pid": None, "consensus": None, "qc": None,
        "remote_dir": None, "remote_pid": None,
    }
    with RUNS_LOCK:
        RUNS[run_id] = run
        QUEUE.append(run_id)
        save_run(run)
    schedule()
    return run


@app.get("/api/runs/{run_id}/log")
def api_log(run_id: str, offset: int = 0):
    path = run_path(run_id) / "run.log"
    if not path.exists():
        return {"text": "", "offset": 0}
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return {"text": fh.read(), "offset": size}


@app.post("/api/runs/{run_id}/cancel")
def api_cancel(run_id: str):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run:
            raise HTTPException(404, "No run with that id.")
        if run["status"] == "queued" and run_id in QUEUE:
            QUEUE.remove(run_id)
            run["status"] = "cancelled"
            run["finished_at"] = now_iso()
            save_run(run)
            return run
        if run["status"] != "running":
            return run
        run["status"] = "cancelling"
        pid = run.get("pid")
        remote = run.get("location") == "hpc"
        save_run(run)

    # A remote run is stopped by its own polling loop, which sees the
    # cancelling state and calls scancel on the cluster.
    if not remote and pid:
        # Nextflow traps SIGTERM and shuts its tasks down in order.
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return RUNS[run_id]


@app.get("/api/runs/{run_id}/consensus")
def api_consensus(run_id: str):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run or not run.get("consensus"):
        raise HTTPException(404, "This run has no consensus file.")
    return FileResponse(run["consensus"],
                        filename=f"{run['sample']['name']}_{run['pipeline_id']}.fasta")


# ------------------------------------------------- interpretation
#
# Two layers, deliberately in this order. The offline table is free,
# instant, deterministic and sends nothing; the model is the fallback for
# what the table does not cover.


def _run_log_text(run_id):
    path = run_path(run_id) / "run.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/runs/{run_id}/diagnose")
def api_diagnose(run_id: str):
    """The offline check. No model, no network, no key."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "No run with that id.")
    log = _run_log_text(run_id)
    hits = diagnostics.diagnose(run, log, PROFILE)
    return {"findings": hits,
            "advice": "" if hits else diagnostics.no_match_advice(run)}


class AskRequest(BaseModel):
    question: str
    preview: bool = False


@app.get("/api/assistant")
def api_assistant_get():
    cfg = assistant.load_config(STATE_DIR)
    return {
        "config": cfg,
        "providers": {k: {"label": v["label"], "models": v["models"],
                          "needs_key": v["needs_key"],
                          "key_hint": v["key_hint"]}
                      for k, v in assistant.PROVIDERS.items()},
        "presets": [{"label": a, "prompt": b} for a, b in assistant.PRESETS],
        "key_source": assistant.key_source(STATE_DIR, cfg["provider"]),
    }


class AssistantConfig(BaseModel):
    provider: str = "anthropic"
    model: str = ""
    ollama_host: str = "http://localhost:11434"
    send_sequences: bool = False
    send_sample_names: bool = False
    send_paths: bool = False


@app.put("/api/assistant")
def api_assistant_put(cfg: AssistantConfig):
    if cfg.provider not in assistant.PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{cfg.provider}'.")
    assistant.save_config(STATE_DIR, cfg.model_dump())
    return {"config": assistant.load_config(STATE_DIR),
            "key_source": assistant.key_source(STATE_DIR, cfg.provider)}


class AssistantKey(BaseModel):
    provider: str
    key: str = ""


@app.post("/api/assistant/key")
def api_assistant_key(body: AssistantKey):
    if body.provider not in assistant.PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{body.provider}'.")
    assistant.save_key(STATE_DIR, body.provider, body.key)
    return {"key_source": assistant.key_source(STATE_DIR, body.provider)}


@app.post("/api/runs/{run_id}/ask")
def api_ask(run_id: str, body: AskRequest):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "No run with that id.")

    log = _run_log_text(run_id)
    cfg = assistant.load_config(STATE_DIR)
    context = assistant.build_context(run, log, cfg)
    report = diagnostics.summarise(run, log, PROFILE)
    if report:
        context += ("\n\n## Known issues already identified by the "
                    "application's own checks\n" + report)

    # The payload is shown before anything is sent, so nobody has to trust
    # a description of what leaves the machine.
    if body.preview:
        return {"preview": context}

    if not body.question.strip():
        raise HTTPException(400, "Ask something.")
    try:
        return {"answer": assistant.ask(STATE_DIR, body.question, context, cfg)}
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc))


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "static" / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

load_runs()
schedule()


if __name__ == "__main__":
    import uvicorn

    print(f"  reads     : {DATA_DIR}")
    print(f"  results   : {RUNS_DIR}")
    print(f"  references: {REF_DIR}")
    print(f"  profile   : {PROFILE}")
    print(f"  nextflow  : {NEXTFLOW}")
    print("  open      : http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
