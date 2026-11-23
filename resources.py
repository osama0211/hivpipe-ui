"""Working out how much memory and CPU each pipeline may use.

A laptop is not a compute node. WSL2 in particular hands out half the host's
RAM by default, so an 16 GB machine often gives a container 8 GB, and an
assembler that asks for 16 GB simply never starts — Nextflow refuses the
process before any tool runs.

This module turns one number the user picks ("use at most N GB") into the
per-process limits Nextflow needs, and says plainly when a pipeline is being
squeezed below what it realistically needs.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# What each pipeline actually needs, in GB.
#
#   floor       below this it will not finish; the app blocks the run
#   workable    runs, but slowly or on small samples only
#   comfortable what the tool's own documentation assumes
PIPELINE_MEMORY = {
    "shiver":      {"floor": 3, "workable": 6,  "comfortable": 16,
                    "note": "IVA assembly is the memory-hungry step."},
    "vpipe":       {"floor": 2, "workable": 4,  "comfortable": 8,
                    "note": "Reference-based, so fairly modest."},
    "smaltalign":  {"floor": 2, "workable": 4,  "comfortable": 8,
                    "note": "Iterative mapping; memory scales with read count."},
    "viralngs":    {"floor": 6, "workable": 12, "comfortable": 32,
                    "note": "Trinity assembly is the heaviest step in any of these."},
    "hivgenopipe": {"floor": 3, "workable": 6,  "comfortable": 16,
                    "note": "De novo plus reference assembly over gag-pol."},
}

# Share of the budget each process class may take.
LABEL_SHARE = {
    "light": 0.25,
    "assembly": 0.85,
    "assembly_large": 1.0,
}
LABEL_MIN_GB = {"light": 1, "assembly": 2, "assembly_large": 2}


def host_memory_gb():
    """Total RAM visible to this process, in GB. None if it cannot be read."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * size / (1024 ** 3), 1)
    except (ValueError, OSError, AttributeError):
        pass
    try:  # Windows
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        s = Status()
        s.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return round(s.ullTotalPhys / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        return None


def host_cpus():
    return os.cpu_count() or 2


def docker_memory_gb():
    """How much RAM the Docker daemon will actually hand out, in GB.

    On Windows and macOS, Docker runs in a VM with its own limit, which is
    usually well below the host's RAM. That VM's limit is what matters, not
    what the laptop has.
    """
    if not shutil.which("docker"):
        return None
    try:
        p = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"],
                           capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip().isdigit():
            return round(int(p.stdout.strip()) / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        pass
    return None


def budget_gb(location="local"):
    """A sensible default memory budget for this machine, in GB."""
    if location == "hpc":
        return 32
    limits = [v for v in (docker_memory_gb(), host_memory_gb()) if v]
    if not limits:
        return 8
    # Leave the operating system room to breathe.
    usable = min(limits) - 2
    return max(2, int(usable))


def plan(total_gb, cpus=None, location="local"):
    """Per-label memory and CPU limits for a budget of total_gb."""
    total_gb = max(2, float(total_gb))
    cpus = int(cpus or host_cpus())
    # Asking for more cores than exist makes Nextflow refuse the process
    # before any tool runs. On a cluster the limit belongs to the node the
    # job lands on, not to this machine, so only cap local runs.
    if location != "hpc":
        cpus = min(cpus, host_cpus())
    out = {}
    for label, share in LABEL_SHARE.items():
        gb = max(LABEL_MIN_GB[label], int(total_gb * share))
        if label == "light":
            out[label] = {"memory_gb": min(gb, 4), "cpus": 1}
        else:
            out[label] = {"memory_gb": gb, "cpus": max(1, cpus)}
    return out


def assess(pipeline_id, total_gb):
    """Is this budget enough for this pipeline? Returns a verdict dict."""
    spec = PIPELINE_MEMORY.get(pipeline_id)
    if not spec:
        return {"level": "ok", "message": ""}
    usable = plan(total_gb)["assembly_large"]["memory_gb"] \
        if pipeline_id == "viralngs" else plan(total_gb)["assembly"]["memory_gb"]

    if usable < spec["floor"]:
        return {
            "level": "blocked",
            "message": (f"{usable} GB is below the {spec['floor']} GB this "
                        f"pipeline needs to start. {spec['note']}"),
        }
    if usable < spec["workable"]:
        return {
            "level": "tight",
            "message": (f"{usable} GB will be tight — {spec['workable']} GB is "
                        f"the realistic minimum and {spec['comfortable']} GB is "
                        f"what the tool assumes. Expect slow runs, and failures "
                        f"on large samples. {spec['note']}"),
        }
    if usable < spec["comfortable"]:
        return {
            "level": "ok",
            "message": (f"{usable} GB is workable. {spec['comfortable']} GB "
                        f"would be comfortable."),
        }
    return {"level": "ok", "message": f"{usable} GB is ample for this pipeline."}


def render_config(total_gb, cpus=None, location="local"):
    """A Nextflow config fragment pinning the limits for one run.

    Written per run and passed with -c, rather than baked into a profile:
    the limit is a property of the machine and the moment, not the workflow.
    """
    p = plan(total_gb, cpus, location)
    lines = [
        "// Generated per run by the HIV pipeline runner.",
        f"// Memory budget: {total_gb} GB total.",
        "",
        "process {",
    ]
    for label in ("light", "assembly", "assembly_large"):
        v = p[label]
        lines.append(f"    withLabel: {label} {{ "
                     f"cpus = {v['cpus']}; memory = {v['memory_gb']}.GB }}")
    lines += ["}", ""]
    return "\n".join(lines)


def summary(location="local"):
    """What this machine has, for the capability screen."""
    host = host_memory_gb()
    docker = docker_memory_gb()
    return {
        "host_memory_gb": host,
        "docker_memory_gb": docker,
        "cpus": host_cpus(),
        "suggested_gb": budget_gb(location),
        "docker_is_limit": bool(docker and host and docker < host - 1),
    }
