"""Talking to an HPC cluster over SSH.

Two transports, chosen by how the cluster authenticates:

Key or agent (default) shells out to the system ssh/scp binaries, so
~/.ssh/config applies — ProxyJump through a bastion, ssh-agent, hardware keys
and host aliases behave exactly as they do when you type `ssh cluster`.
Connections are multiplexed over a ControlMaster socket, so polling a running
job costs one round trip rather than a full handshake.

Password uses paramiko, because the system ssh client deliberately refuses to
read a password from anything but a terminal. The password is held in memory
for the life of the process and never written to disk.

Only `run`, `push` and `pull` differ between the two. Everything built on
them — probing, listing samples, launching and watching jobs — is shared.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import stat
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import paramiko
except ImportError:  # password auth is unavailable without it
    paramiko = None

CONTROL_DIR = Path(tempfile.gettempdir()) / "hivpipe-ssh"
CONTROL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

# Passwords live here and nowhere else: in memory, keyed by destination, for
# the life of this process. They are never written to hpc.yaml or any log.
_PASSWORDS = {}
_CLIENTS = {}


def set_password(destination, password):
    _PASSWORDS[destination] = password
    client = _CLIENTS.pop(destination, None)
    if client:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def has_password(destination):
    return bool(_PASSWORDS.get(destination))


def clear_password(destination):
    _PASSWORDS.pop(destination, None)
    client = _CLIENTS.pop(destination, None)
    if client:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


class HPCError(RuntimeError):
    """A remote operation failed. The message is meant for the user."""


class Cluster:
    """One SSH destination, with the paths a run needs on it."""

    def __init__(self, config):
        self.host = (config.get("host") or "").strip()
        self.user = (config.get("user") or "").strip()
        self.port = int(config.get("port") or 22)
        self.key = (config.get("key") or "").strip()
        self.auth = config.get("auth") or "key"
        self.remote_dir = (config.get("remote_dir") or "").strip()
        self.data_dir = (config.get("data_dir") or "").strip()
        self.scheduler = config.get("scheduler") or "slurm"
        self.queue = (config.get("queue") or "").strip()
        self.account = (config.get("account") or "").strip()
        self.container = config.get("container") or "singularity"
        self.modules = config.get("modules") or []
        self.nextflow = (config.get("nextflow") or "nextflow").strip()
        self.timeout = int(config.get("timeout") or 30)

    # ------------------------------------------------------------ plumbing

    @property
    def destination(self):
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def configured(self):
        return bool(self.host and self.remote_dir)

    @property
    def uses_password(self):
        return self.auth == "password"

    @property
    def needs_password(self):
        """True when password auth is selected but none has been entered."""
        return self.uses_password and not has_password(self.destination)

    # ---------------------------------------------------- paramiko transport

    def _client(self):
        """A connected paramiko client, reused across calls."""
        if paramiko is None:
            raise HPCError("Password login needs the paramiko package. "
                           "Install it with: pip install paramiko")
        existing = _CLIENTS.get(self.destination)
        if existing is not None:
            transport = existing.get_transport()
            if transport is not None and transport.is_active():
                return existing
            _CLIENTS.pop(self.destination, None)

        password = _PASSWORDS.get(self.destination)
        if not password:
            raise HPCError("No password entered for this cluster yet.")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # A cluster is usually reached by hostname on a trusted network and
        # the user has no way to vet a fingerprint here, so unknown hosts are
        # accepted rather than failing. Known hosts are still checked above.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user or None,
                password=password,
                timeout=self.timeout,
                banner_timeout=max(self.timeout, 30),
                auth_timeout=max(self.timeout, 30),
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:  # noqa: BLE001 — paramiko raises many types
            raise HPCError(_explain_paramiko(exc, self.host))
        _CLIENTS[self.destination] = client
        return client

    def _run_paramiko(self, command, timeout, check, detach):
        client = self._client()
        if detach:
            command = f"({command}) >/dev/null 2>&1"
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        except Exception as exc:  # noqa: BLE001
            _CLIENTS.pop(self.destination, None)
            raise HPCError(f"The connection to {self.host} dropped: {exc}")
        if check and rc != 0:
            raise HPCError(err.strip() or f"Remote command exited with status {rc}.")
        return rc, out, err

    def _sftp(self):
        try:
            return self._client().open_sftp()
        except HPCError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HPCError(f"Could not open a file transfer channel: {exc}")

    def _push_paramiko(self, local, remote):
        sftp = self._sftp()
        try:
            local = Path(local)
            if local.is_dir():
                self._put_tree(sftp, local, remote.rstrip("/") + "/" + local.name)
            else:
                sftp.put(str(local), remote)
        finally:
            sftp.close()

    def _put_tree(self, sftp, local_dir, remote_dir):
        try:
            sftp.mkdir(remote_dir)
        except IOError:
            pass  # already there
        for item in sorted(Path(local_dir).iterdir()):
            target = remote_dir + "/" + item.name
            if item.is_dir():
                self._put_tree(sftp, item, target)
            elif item.is_file():
                sftp.put(str(item), target)

    def _pull_paramiko(self, remote, local):
        sftp = self._sftp()
        try:
            try:
                info = sftp.stat(remote)
            except IOError:
                return False
            if stat.S_ISDIR(info.st_mode):
                self._get_tree(sftp, remote, Path(local) / posixpath.basename(remote))
            else:
                Path(local).parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote, str(local))
            return True
        except Exception:  # noqa: BLE001 — a missing result is not an error
            return False
        finally:
            sftp.close()

    def _get_tree(self, sftp, remote_dir, local_dir):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for entry in sftp.listdir_attr(remote_dir):
            rpath = remote_dir + "/" + entry.filename
            lpath = Path(local_dir) / entry.filename
            if stat.S_ISDIR(entry.st_mode):
                self._get_tree(sftp, rpath, lpath)
            else:
                sftp.get(rpath, str(lpath))

    def _control_path(self):
        tag = f"{self.user}_{self.host}_{self.port}".replace("/", "_")
        return str(CONTROL_DIR / f"cm_{tag}")

    def _ssh_base(self):
        args = [
            "ssh",
            "-o", "BatchMode=yes",                  # never prompt; fail instead
            "-o", f"ConnectTimeout={self.timeout}",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path()}",
            "-o", "ControlPersist=120",
            "-o", "ServerAliveInterval=30",
        ]
        if self.port != 22:
            args += ["-p", str(self.port)]
        if self.key:
            args += ["-i", os.path.expanduser(self.key)]
        return args

    def run(self, command, timeout=None, check=True, detach=False):
        """Run a shell command on the cluster. Returns (rc, stdout, stderr).

        With detach=True the remote command's own stdout and stderr are closed
        on the SSH channel. That matters for starting a background job: ssh
        waits for every descriptor on the channel to close, so a backgrounded
        process whose parent shell still holds the channel open will hang the
        connection even when the process itself is fully redirected.
        """
        if not self.host:
            raise HPCError("No cluster host configured.")
        if self.uses_password:
            return self._run_paramiko(command, timeout or max(self.timeout, 60),
                                      check, detach)
        if detach:
            command = f"({command}) >/dev/null 2>&1"
        argv = self._ssh_base() + ["-n", self.destination, command]
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout or max(self.timeout, 60))
        except subprocess.TimeoutExpired:
            raise HPCError(f"{self.host} did not respond within "
                           f"{timeout or self.timeout}s.")
        except FileNotFoundError:
            raise HPCError("The ssh command was not found on this machine.")
        if check and p.returncode != 0:
            raise HPCError(_explain(p.returncode, p.stderr, self.host))
        return p.returncode, p.stdout, p.stderr

    def push(self, local, remote):
        """Copy a local file or directory to the cluster."""
        if self.uses_password:
            return self._push_paramiko(local, remote)
        local = str(local)
        argv = ["scp", "-q",
                "-o", "BatchMode=yes",
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path()}",
                "-o", "ControlPersist=120"]
        if self.port != 22:
            argv += ["-P", str(self.port)]
        if self.key:
            argv += ["-i", os.path.expanduser(self.key)]
        if Path(local).is_dir():
            argv.append("-r")
        argv += [local, f"{self.destination}:{remote}"]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise HPCError(f"Could not copy {Path(local).name} to the cluster: "
                           f"{p.stderr.strip() or 'scp failed'}")

    def pull(self, remote, local):
        """Copy a remote file or directory back. Returns False if absent."""
        if self.uses_password:
            Path(local).mkdir(parents=True, exist_ok=True)
            return self._pull_paramiko(remote, local)
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        argv = ["scp", "-q", "-r",
                "-o", "BatchMode=yes",
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path()}",
                "-o", "ControlPersist=120"]
        if self.port != 22:
            argv += ["-P", str(self.port)]
        if self.key:
            argv += ["-i", os.path.expanduser(self.key)]
        argv += [f"{self.destination}:{remote}", str(local)]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        return p.returncode == 0

    def disconnect(self):
        """Close any open connection. Best effort; failures are ignored."""
        client = _CLIENTS.pop(self.destination, None)
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            subprocess.run(self._ssh_base() + ["-O", "exit", self.destination],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ checks

    def probe(self):
        """Check the connection and report what is present on the cluster."""
        report = {"ok": False, "host": self.host, "checks": []}

        def add(name, ok, detail=""):
            report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

        if not self.host:
            add("Host", False, "No hostname set.")
            return report
        if not self.remote_dir:
            add("Working directory", False, "No remote working directory set.")
            return report
        if self.needs_password:
            add("Password", False, "Enter the cluster password above, then test again.")
            return report
        if self.uses_password and paramiko is None:
            add("Password login", False,
                "Needs the paramiko package: pip install paramiko")
            return report

        t0 = time.time()
        try:
            _, out, _ = self.run("hostname; echo __OK__", timeout=self.timeout)
        except HPCError as exc:
            add("SSH connection", False, str(exc))
            return report
        if "__OK__" not in out:
            add("SSH connection", False, "Connected, but the shell returned nothing.")
            return report
        remote_name = out.splitlines()[0].strip() if out.strip() else self.host
        add("SSH connection", True, f"{remote_name} in {time.time() - t0:.1f}s")

        rc, _, _ = self.run(f"mkdir -p {shlex.quote(self.remote_dir)} && "
                            f"test -w {shlex.quote(self.remote_dir)}", check=False)
        add("Working directory", rc == 0,
            self.remote_dir if rc == 0 else f"{self.remote_dir} is not writable.")

        if self.data_dir:
            rc, out, _ = self.run(
                f"test -d {shlex.quote(self.data_dir)} && "
                f"find {shlex.quote(self.data_dir)} -maxdepth 3 "
                f"\\( -name '*.fastq*' -o -name '*.fq*' \\) | wc -l", check=False)
            n = out.strip() or "0"
            add("Reads folder", rc == 0,
                f"{n} FASTQ files in {self.data_dir}" if rc == 0
                else f"{self.data_dir} not found.")

        prelude = self._module_prelude()

        # If modules were named, confirm the module system actually answers.
        # A load that silently does nothing is the worst kind of failure:
        # everything reports fine and the run dies later for an unrelated
        # looking reason.
        # Always reported, not only when modules are listed: when the module
        # system is unreachable the most useful thing the probe can say is
        # exactly that, and that is also the case where nobody has managed
        # to put anything in the Modules box yet.
        rc, out, _ = self.run(
            f"{prelude}type module >/dev/null 2>&1 && echo HAVE; "
            f'echo "PATHLEN=${{#MODULEPATH}}"; '
            f"module -t avail 2>/dev/null | grep -c . || true", check=False)
        blob = out or ""
        has_module = "HAVE" in blob
        pathlen = 0
        for line in blob.splitlines():
            if line.startswith("PATHLEN="):
                pathlen = int(line.split("=", 1)[1] or 0)
        if not has_module:
            add("Module system", False,
                "No `module` command over SSH, so anything in the Modules "
                "box is ignored. Ask the administrators which file defines "
                "it for non-login shells.")
        elif pathlen == 0:
            add("Module system", False,
                "`module` works but MODULEPATH is empty, so it can see no "
                "modules at all. The file that sets MODULEPATH is not being "
                "sourced for non-login shells.")
        else:
            add("Module system", True,
                f"available, {len(self.modules)} module(s) requested"
                if self.modules else
                "available, but no modules are listed in the box below")

        # Java before Nextflow: Nextflow runs on the JVM, and the usual
        # cluster failure is a java on PATH that is too old. Checking only
        # that `nextflow` exists reported a ready cluster and then died
        # minutes into the run.
        rc, out, err = self.run(f"{prelude}java -version 2>&1", check=False)
        blob = (out or "") + (err or "")
        major = _java_major(blob)
        if rc != 0 or major is None:
            add("Java", False,
                "No java on PATH. Nextflow needs Java 17 or later. "
                + self._java_module_hint(prelude))
        elif major < 17:
            add("Java", False,
                f"Java {major} is too old; Nextflow needs 17 or later. "
                + self._java_module_hint(prelude))
        elif major > 26:
            add("Java", False,
                f"Java {major} is newer than Nextflow supports (up to 26). "
                + self._java_module_hint(prelude))
        else:
            add("Java", True, f"Java {major}")

        rc, out, _ = self.run(f"{prelude}command -v {shlex.quote(self.nextflow)} "
                              f"|| command -v nextflow", check=False)
        if rc != 0:
            add("Nextflow", False,
                "Not on PATH. Add a module, or set a full path.")
        else:
            where = out.strip()
            # Presence is not capability: this is the call that actually
            # starts the JVM, which is what the run will do.
            vrc, vout, verr = self.run(
                f"{prelude}{shlex.quote(self.nextflow)} -version 2>&1",
                check=False)
            vblob = ((vout or "") + (verr or "")).strip()
            if vrc == 0:
                version = next((l.strip() for l in vblob.splitlines()
                                if "version" in l.lower()), where)
                add("Nextflow", True, version[:80])
            else:
                first = next((l.strip() for l in vblob.splitlines() if l.strip()),
                             "it exited non-zero")
                add("Nextflow", False,
                    f"Found at {where}, but it will not start: {first[:120]}")

        if self.scheduler == "slurm":
            rc, out, _ = self.run(f"{prelude}command -v sbatch", check=False)
            add("Slurm", rc == 0, out.strip() if rc == 0 else "sbatch not found.")
            if rc == 0:
                # A partition that does not exist is only discovered when
                # sbatch rejects the job, by which time the run has already
                # staged files and started Nextflow. Check it up front.
                prc, pout, _ = self.run(
                    f"{prelude}sinfo -h -o '%P' 2>/dev/null | tr -d '*' "
                    f"| sort -u", check=False)
                parts = [p.strip() for p in (pout or "").splitlines()
                         if p.strip()]
                if not parts:
                    add("Partition", True,
                        "Could not list partitions; leaving the box empty "
                        "lets the cluster choose.")
                elif not self.queue:
                    add("Partition", True,
                        "using the cluster default — available: "
                        + ", ".join(parts[:8]))
                elif self.queue in parts:
                    add("Partition", True, self.queue)
                else:
                    add("Partition", False,
                        f"'{self.queue}' is not a partition on this cluster. "
                        f"Available: {', '.join(parts[:8])}. Leave the box "
                        f"empty to use the default.")
        elif self.scheduler in ("pbs", "sge"):
            rc, out, _ = self.run(f"{prelude}command -v qsub", check=False)
            add(self.scheduler.upper(), rc == 0,
                out.strip() if rc == 0 else "qsub not found.")

        if self.container in ("singularity", "apptainer"):
            rc, out, _ = self.run(
                f"{prelude}command -v {self.container} || command -v apptainer "
                f"|| command -v singularity", check=False)
            add("Container engine", rc == 0,
                out.strip() if rc == 0 else
                f"{self.container} not found. Load a module, or use 'none'.")

        report["ok"] = all(c["ok"] for c in report["checks"])
        return report

    def _module_prelude(self):
        """Commands to put a remote shell into the right environment.

        Always starts by initialising the module system. `ssh host 'cmd'`
        runs a non-login, non-interactive shell, and `module` is a shell
        function defined by a profile script that such a shell never
        sources. Without this, every `module load` silently does nothing —
        the load appears to succeed, the tool it was meant to provide is
        still missing, and the error arrives much later and somewhere else.
        """
        prelude = MODULE_INIT
        return prelude + "".join(
            f"module load {shlex.quote(m)} >/dev/null 2>&1; "
            for m in self.modules)

    def _java_module_hint(self, prelude=""):
        """Name the java modules this cluster offers, if it uses modules.

        "Install Java" is useless advice on a cluster nobody has root on.
        What the person needs is the module name to put in the Modules box,
        so the check goes and finds the candidates.
        """
        rc, out, _ = self.run(
            f"{prelude}module -t avail 2>&1 | grep -iE '^(java|jdk|openjdk)' "
            f"| head -30", check=False)
        names = []
        for line in (out or "").splitlines():
            name = line.strip()
            # `module -t avail` mixes real modules with directory headers
            # ("Java/", lines ending in ":") and alias annotations
            # ("Java/17(@Java/17.0.15)"). Only the bare names are usable.
            if not name or name.endswith(":") or name.endswith("/"):
                continue
            name = name.split("(")[0].strip()
            if "/" not in name:
                continue
            if name not in names:
                names.append(name)
        # Prefer versions Nextflow accepts, newest first, so the first
        # suggestion is one that will actually work.
        def usable(n):
            major = _java_major('version "' + n.split("/", 1)[1] + '"')
            return major is not None and 17 <= major <= 26
        names.sort(key=lambda n: (not usable(n), n))
        if names:
            good = [n for n in names if usable(n)]
            if good:
                return (f"Add {good[0]} to Modules on this tab"
                        + (f" (also available: {', '.join(names[1:5])})"
                           if len(names) > 1 else "") + ".")
            return ("These java modules exist but none is version 17-26: "
                    + ", ".join(names[:6]))
        return ("Ask the cluster's administrators which module provides "
                "Java 17, then add it to Modules on this tab.")

    # ------------------------------------------------------------ samples

    def samples(self):
        """List FASTQ files under the cluster's reads folder."""
        if not self.data_dir:
            return []
        rc, out, err = self.run(
            f"find {shlex.quote(self.data_dir)} -maxdepth 4 -type f "
            f"\\( -name '*.fastq' -o -name '*.fastq.gz' "
            f"-o -name '*.fq' -o -name '*.fq.gz' \\) "
            f"-printf '%s\\t%p\\n' 2>/dev/null | head -5000", check=False)
        if rc != 0 and not out.strip():
            raise HPCError(f"Could not list {self.data_dir}: "
                           f"{err.strip() or 'find failed'}")
        entries = []
        for line in out.splitlines():
            if "\t" not in line:
                continue
            size, path = line.split("\t", 1)
            try:
                entries.append((int(size), path.strip()))
            except ValueError:
                continue
        return entries

    # ------------------------------------------------------------ jobs

    def stage(self, run_id, files):
        """Create the run directory on the cluster and copy files into it.

        `files` maps a destination name to a local path.
        """
        remote = f"{self.remote_dir.rstrip('/')}/{run_id}"
        self.run(f"mkdir -p {shlex.quote(remote)}")
        for name, local in files.items():
            if local and Path(local).exists():
                self.push(local, f"{remote}/{name}")
        return remote

    def launch(self, remote_dir, script_name="submit.sh"):
        """Start the job script detached, so it outlives this SSH session.

        Done in two steps. The launch itself returns nothing — its streams are
        closed so ssh can return immediately — and the process id is written
        to a file, which a second call reads back.
        """
        quoted = shlex.quote(remote_dir)
        # No `echo $!` here: that would record setsid's pid, which dies
        # immediately. The script writes its own pid as its first act.
        self.run(
            f"cd {quoted} && rm -f launch.pid exit_code && "
            f"chmod +x {shlex.quote(script_name)} && "
            f"setsid ./{shlex.quote(script_name)} > run.log 2>&1 < /dev/null &",
            detach=True, timeout=60)

        # The pid file appears as soon as the shell forks, but give a slow
        # filesystem a moment to show it.
        for _ in range(10):
            rc, out, _ = self.run(f"cat {quoted}/launch.pid 2>/dev/null || true",
                                  check=False, timeout=30)
            pid = out.strip().splitlines()[-1] if out.strip() else ""
            if pid.isdigit():
                return int(pid)
            time.sleep(1)
        raise HPCError("The job was started but the cluster did not report a "
                       "process id. Check the run directory on the cluster.")

    def tail(self, remote_dir, offset=0):
        """Read new bytes from the remote run log."""
        path = f"{remote_dir.rstrip('/')}/run.log"
        rc, out, _ = self.run(
            f"test -f {shlex.quote(path)} && "
            f"tail -c +{int(offset) + 1} {shlex.quote(path)} || true",
            check=False, timeout=45)
        return out if rc == 0 else ""

    def alive(self, pid):
        rc, _, _ = self.run(f"kill -0 {int(pid)} 2>/dev/null && echo yes || echo no",
                            check=False, timeout=30)
        return rc == 0

    def status(self, remote_dir, pid):
        """Is the run still going? Returns running | complete | failed."""
        path = f"{remote_dir.rstrip('/')}/exit_code"
        rc, out, _ = self.run(
            f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; "
            f"elif kill -0 {int(pid)} 2>/dev/null; then echo RUNNING; "
            f"else echo GONE; fi", check=False, timeout=30)
        token = out.strip().splitlines()[-1] if out.strip() else "GONE"
        if token == "RUNNING":
            return "running", None
        if token == "GONE":
            # The process is not there and no exit code was written. That is
            # usually the gap between the script's last command and its
            # `echo $? > exit_code`, so look once more before condemning a
            # run that may have just succeeded.
            time.sleep(3)
            rc, out, _ = self.run(
                f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; "
                f"else echo GONE; fi", check=False, timeout=30)
            token = out.strip().splitlines()[-1] if out.strip() else "GONE"
            if token == "GONE":
                return "failed", None
        try:
            code = int(token)
        except ValueError:
            return "failed", None
        return ("complete" if code == 0 else "failed"), code

    def cancel(self, remote_dir, pid):
        """Stop the run and any scheduler jobs it started."""
        prelude = self._module_prelude()
        cmds = [f"kill -TERM -{int(pid)} 2>/dev/null || kill -TERM {int(pid)} 2>/dev/null || true"]
        if self.scheduler == "slurm":
            # Nextflow records its Slurm job ids in the trace file.
            cmds.append(
                f"{prelude}if [ -f {shlex.quote(remote_dir)}/results/_trace/trace.txt ]; then "
                f"awk 'NR>1 {{print $1}}' {shlex.quote(remote_dir)}/results/_trace/trace.txt "
                f"| grep -E '^[0-9]+$' | xargs -r scancel 2>/dev/null || true; fi")
        self.run(" ; ".join(cmds), check=False, timeout=60)


def _explain_paramiko(exc, host):
    """Turn a paramiko failure into something a person can act on."""
    name = type(exc).__name__
    text = str(exc).strip()
    low = text.lower()
    if name == "AuthenticationException" or "authentication failed" in low:
        return (f"{host} rejected the password. If your cluster also asks for a "
                f"code or token, password login alone will not work — set up an "
                f"SSH key instead.")
    if name == "BadHostKeyException":
        return f"The host key for {host} does not match the one in known_hosts."
    if "timed out" in low or name == "timeout":
        return f"{host} did not answer. Check the VPN, or whether a bastion host is needed."
    if "name or service not known" in low or "getaddrinfo" in low:
        return f"Could not resolve {host}. Check the hostname."
    if "connection refused" in low:
        return f"{host} refused the connection. Check the port."
    if "no existing session" in low:
        return (f"{host} closed the connection during login. If it requires a "
                f"second factor, use an SSH key instead.")
    return text or f"Could not connect to {host} ({name})."


def _explain(code, stderr, host):
    """Turn an ssh failure into something a person can act on."""
    err = (stderr or "").strip()
    low = err.lower()
    if "permission denied" in low:
        return (f"{host} refused the key. Check that `ssh {host}` works in your "
                f"terminal, and that the key is loaded in ssh-agent.")
    if "could not resolve" in low or "name or service not known" in low:
        return f"Could not resolve {host}. Check the hostname, or add it to ~/.ssh/config."
    if "connection refused" in low:
        return f"{host} refused the connection. Check the port, and that you are on the VPN."
    if "connection timed out" in low or "operation timed out" in low:
        return f"{host} did not answer. Check the VPN, or whether a bastion host is needed."
    if "host key verification failed" in low:
        return (f"The host key for {host} is not trusted yet. Run `ssh {host}` once "
                f"in your terminal and accept it.")
    if "batch mode" in low or "publickey" in low:
        return (f"{host} wants a password. This tool only uses key authentication — "
                f"set up an SSH key, or load one with ssh-agent.")
    return err or f"ssh exited with status {code}."


# Stamped into every run log, so a log identifies the build that produced
# it. Keep in step with app.VERSION.
VERSION = "1.1.7"
BUILD_DATE = "2026-11-10"

# Make `module` available in a shell that never sourced a login profile.
# Covers Environment Modules and Lmod in the places distributions put them;
# if none is readable the block is a no-op and `module` stays undefined,
# which the probe reports rather than hiding.
MODULE_INIT = (
    # /etc/profile first, not last. It sources everything in
    # /etc/profile.d in order, which matters because clusters commonly
    # split the module system across two files: one defining the `module`
    # function and another setting MODULEPATH. Sourcing only the first
    # leaves a `module` command that works but can see no modules — so
    # `module avail` is empty and `module load` fails, which looks exactly
    # like "this cluster has no Java".
    'if ! type module >/dev/null 2>&1 || [ -z "${MODULEPATH:-}" ]; then '
    '{ [ -r /etc/profile ] && . /etc/profile; } >/dev/null 2>&1 || true; fi; '
    # Only if that did not produce a `module` command, guess at init files.
    'if ! type module >/dev/null 2>&1; then '
    'for __mi in "${MODULESHOME:-}/init/bash" "${LMOD_PKG:-}/init/bash" '
    '/etc/profile.d/modules.sh /etc/profile.d/lmod.sh '
    '/etc/profile.d/z00_lmod.sh /usr/share/lmod/lmod/init/bash '
    '/usr/share/Modules/init/bash /opt/ohpc/admin/lmod/lmod/init/bash '
    '/opt/apps/lmod/lmod/init/bash; do '
    '[ -r "$__mi" ] && . "$__mi" && break; done; fi; '
    # And if a module command exists but still has nowhere to look, find
    # the file that sets MODULEPATH.
    'if type module >/dev/null 2>&1 && [ -z "${MODULEPATH:-}" ]; then '
    'for __mp in /etc/profile.d/00-modulepath.sh '
    '/etc/profile.d/z01_modulepath.sh /etc/profile.d/modulepath.sh '
    '"${MODULESHOME:-}/init/.modulespath"; do '
    '{ [ -r "$__mp" ] && . "$__mp"; } >/dev/null 2>&1 && break; done; fi; '
)

_JAVA_VERSION = re.compile(r'version "?(\d+)(?:\.(\d+))?')


def _java_major(text):
    """The major version from `java -version` output, or None.

    Handles both shapes: `version "1.8.0_392"` (where the major number is
    the second field) and `version "17.0.9"` (where it is the first).
    """
    m = _JAVA_VERSION.search(text or "")
    if not m:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2):
        return int(m.group(2))
    return first


def submit_script(cluster, run_id, params, profile):
    """The script that runs on the login node and drives Nextflow.

    Nextflow submits each process to the scheduler; this wrapper just keeps it
    alive and records the exit code so the app can tell when it finished.
    """
    lines = [
        "#!/usr/bin/env bash",
        "",
        # The script records its own pid. The launcher cannot: it backgrounds
        # `setsid`, which forks and exits at once, so `$!` there is a process
        # that is already gone — and the poller then reads a dead pid and
        # calls a perfectly healthy run failed.
        'cd "$(dirname "$0")" || exit 1',
        "echo $$ > launch.pid",
        "",
    ]

    # Module setup runs before `set -u`, deliberately: this script is started
    # by a non-login shell, so `module` must be brought in first or every
    # load is a silent no-op, and profile scripts routinely reference unset
    # variables, which would abort the run under `set -u`.
    lines += ["# Make `module` available in a non-login shell.",
              MODULE_INIT.replace("; ", "\n").rstrip(), ""]

    # Then the user's own login files. /etc/profile above is the system's; it
    # does not add ~/.local/bin, a conda install under $HOME, or anything else
    # the person put on their own PATH — and tools installed without root,
    # Nextflow among them, usually live exactly there. A login shell reads
    # these; this script is not one, so it reads them itself.
    #
    # Still before `set -u`, and with output discarded: dotfiles reference
    # unset variables and print banners, and neither should reach the run log
    # or stop the run. An interactive-only guard in a dotfile may skip it
    # anyway, which is what the PATH report below is for.
    lines += [
        "# Pick up the user's own PATH (~/.local/bin, conda, custom installs).",
        "# A login shell reads the first of these three, so stop at the first.",
        'for __rc in "$HOME/.bash_profile" "$HOME/.bash_login" '
        '"$HOME/.profile"; do',
        '    if [ -r "$__rc" ]; then',
        '        { . "$__rc"; } >/dev/null 2>&1 || true',
        "        break",
        "    fi",
        "done",
        "# And .bashrc, where conda init and most PATH edits actually land. It",
        "# is normally sourced by .bash_profile; sourced twice is harmless,",
        "# never sourced is the failure that brought us here.",
        '{ [ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"; } >/dev/null 2>&1 || true',
        "",
    ]
    for mod in cluster.modules:
        lines.append(f"module load {shlex.quote(mod)} || true")

    # Always reported, whether or not modules were requested. A run log that
    # states the build, the modules and the Java it actually got answers in
    # one line what otherwise takes several rounds of guessing.
    mods = ", ".join(cluster.modules) if cluster.modules else "(none)"
    lines += [
        "",
        f'echo "# runner {VERSION} ({BUILD_DATE})"',
        f'echo "# modules requested: {mods}"',
        'if type module >/dev/null 2>&1; then echo "# module: available"; '
        'else echo "# module: NOT AVAILABLE - module loads did nothing"; fi',
        'echo "# java: $(java -version 2>&1 | head -1)"',
        '__jv=$(java -version 2>&1 | head -1 | sed -E \'s/.*version "?([0-9]+)'
        '(\\.([0-9]+))?.*/\\1 \\3/\')',
        '__maj=$(echo "$__jv" | awk \'{print ($1==1 ? $2 : $1)}\')',
        'if [ -z "$__maj" ] || [ "$__maj" -lt 17 ] 2>/dev/null; then',
        '    echo ""',
        '    echo "ERROR: Java ${__maj:-not found} is too old. Nextflow needs '
        '17 or later."',
        '    echo "Add a Java module to the Modules box on the Cluster tab. '
        'Candidates:"',
        # Same filtering the probe does: drop the bare directory entry and
        # the "(@alias)" annotations, and put versions Nextflow accepts
        # first, so the first line offered is one that will actually work.
        '    module -t avail 2>&1 | grep -iE \'^(java|jdk|openjdk)/\' '
        '| sed -E \'s/\\(@.*\\)//; s/[[:space:]]+$//\' '
        '| grep -vE \'/$\' | sort -u '
        '| awk -F/ \'{v=$2; sub(/^1\\./,"",v); split(v,a,"."); '
        'print (a[1]>=17 && a[1]<=26 ? 0 : 1), $0}\' '
        '| sort -n | cut -d" " -f2- | head -6 | sed "s/^/    /"',
        '    echo 127 > exit_code',
        '    exit 127',
        'fi',
        "",
    ]

    # Nextflow itself, checked the same way and for the same reason: a bare
    # "nextflow: command not found" on line 50 says nothing about where the
    # script looked, and the answer is almost always that it lives somewhere
    # a non-login shell never sees. Report the resolved path on every run so
    # a log says what was used, and when it is missing, say where to look.
    nf = shlex.quote(cluster.nextflow or "nextflow")
    lines += [
        f'echo "# nextflow: $(command -v {nf} 2>/dev/null '
        f'|| echo "NOT FOUND on PATH")"',
        f'if ! command -v {nf} >/dev/null 2>&1; then',
        '    echo ""',
        f'    echo "ERROR: {cluster.nextflow or "nextflow"} is not on PATH in '
        'this job\'s shell."',
        '    echo "PATH was: $PATH"',
        '    echo ""',
        '    echo "Nextflow installed without root usually sits in one of '
        'these:"',
        # Looked up rather than guessed at, so the message names a real file
        # if one exists. The `find` is last and depth-capped: on a home
        # directory served over a network filesystem an unbounded search can
        # take minutes.
        '    { for __c in "$HOME/.local/bin/nextflow" "$HOME/bin/nextflow" '
        '"$HOME/nextflow" "$HOME/miniconda3/bin/nextflow" '
        '"$HOME/miniforge3/bin/nextflow" "$HOME/anaconda3/bin/nextflow" '
        '/opt/nextflow/nextflow /usr/local/bin/nextflow; do',
        '        [ -x "$__c" ] && echo "$__c"',
        '      done',
        '      find "$HOME" -maxdepth 4 -name nextflow -type f -perm -u+x '
        '2>/dev/null',
        '    } | sort -u | head -6 | sed "s/^/    found: /"',
        '    echo ""',
        '    echo "Put the full path in the Nextflow box on the Cluster tab,"',
        '    echo "or load a module that provides it. Candidates:"',
        '    module -t avail 2>&1 | grep -iE \'^nextflow\' '
        '| sed -E \'s/\\(@.*\\)//; s/[[:space:]]+$//\' '
        '| grep -vE \'/$\' | sort -u | head -4 | sed "s/^/    /"',
        '    echo 127 > exit_code',
        '    exit 127',
        'fi',
        "",
    ]

    lines += ["set -uo pipefail", ""]

    if cluster.queue:
        lines.append(f"export HIVPIPE_QUEUE={shlex.quote(cluster.queue)}")
    if cluster.account:
        lines.append(f"export HIVPIPE_ACCOUNT={shlex.quote(cluster.account)}")
    work_dir = cluster.remote_dir.rstrip("/") + "/" + run_id + "/work"
    lines += [
        "export NXF_ANSI_LOG=false",
        "export NXF_DISABLE_CHECK_LATEST=true",
        "export NXF_WORK=" + shlex.quote(work_dir),
        # Without this, Nextflow caches converted Singularity images inside
        # the per-run work directory, so every run re-downloads and
        # re-converts several GB of images. Pointing it at one directory
        # per user means the download happens once.
        "export NXF_SINGULARITY_CACHEDIR="
        + shlex.quote(posixpath.join(cluster.remote_dir, ".singularity-cache")),
        "mkdir -p \"$NXF_SINGULARITY_CACHEDIR\"",
        "",
    ]

    args = [shlex.quote(cluster.nextflow), "run", "main.nf",
            "-profile", shlex.quote(profile),
            "-c", "run.config",
            "-work-dir", "work",
            "-ansi-log", "false"]
    # The stub profile only turns containers off; selecting the stub blocks
    # themselves is a separate flag. Same rule as a local run.
    if profile == "stub":
        args.append("-stub-run")
    for key, value in params.items():
        if value not in (None, "", False):
            args += [f"--{key}", shlex.quote(str(value))]

    lines += [
        "cd \"$(dirname \"$0\")\"",
        "",
        " \\\n    ".join(args),
        "",
        "echo $? > exit_code",
        "",
    ]
    return "\n".join(lines)
