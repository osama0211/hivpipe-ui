#!/usr/bin/env bash
#
# Checks the installation without running a real pipeline.
#
#   ./selftest.sh
#
# Four things get tested: dependencies, the structural check on a genome that
# is known to be correct, the same check on one deliberately broken, and the
# whole Nextflow workflow using stub steps. Nothing here needs an assembly
# tool, a container, or more than a minute.

set -uo pipefail
cd "$(dirname "$0")"

pass=0; fail=0
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
note() { printf '        %s\n' "$1"; }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

REF=${HIVPIPE_TEST_REF:-refs/HXB2.fasta}

# ----------------------------------------------------------------- 1
head "Dependencies"

if command -v python3 >/dev/null; then
    ok "python3 ($(python3 --version 2>&1 | cut -d' ' -f2))"
else
    bad "python3 not found"; exit 1
fi

for mod in fastapi uvicorn yaml multipart; do
    if python3 -c "import $mod" 2>/dev/null; then
        ok "python module $mod"
    else
        bad "python module $mod missing — pip install -r requirements.txt"
    fi
done

if python3 -c "import paramiko" 2>/dev/null; then
    ok "python module paramiko (cluster password login)"
else
    note "paramiko missing — only needed for password login to a cluster"
fi

NF=$(command -v nextflow || echo ./nextflow)
if [ -x "$NF" ] || command -v nextflow >/dev/null; then
    NFVER=$("$NF" -version 2>&1 | awk '/^ *version/ {print $2; exit}')
    ok "nextflow ${NFVER:-present}"
    HAVE_NF=1
else
    bad "nextflow not found — curl -s https://get.nextflow.io | bash"
    HAVE_NF=0
fi

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
else
    note "docker not reachable — fine for this test, needed for real runs"
fi

if [ -f "$REF" ]; then
    ok "reference $REF ($(grep -vc '^>' "$REF" >/dev/null && python3 -c "
import sys; sys.path.insert(0,'.')
from qc import first_sequence
print(f'{len(first_sequence(\"$REF\")[1]):,} bp')"))"
else
    bad "no reference at $REF"; exit 1
fi

# ----------------------------------------------------------------- 2
head "Structural check — a correct genome should pass"

python3 bin/structural_qc.py --consensus "$REF" --reference "$REF" \
    --json "$OUT/clean.json" >/dev/null 2>&1
V=$(python3 -c "import json;print(json.load(open('$OUT/clean.json'))['verdict'])" 2>/dev/null)
if [ "$V" = "pass" ]; then
    ok "identical sequence reported as pass"
else
    bad "identical sequence reported as '$V', expected pass"
fi

# ----------------------------------------------------------------- 3
head "Structural check — a broken genome should fail"

python3 tests/make_rearrangement.py --input "$REF" --output "$OUT/broken.fasta" \
    --kind replacement --size 2000 --source 2300 --dest 6000 >/dev/null 2>&1

if [ -s "$OUT/broken.fasta" ]; then
    ok "built a test genome with a 2,000 bp block replaced"
else
    bad "could not build the test genome"
fi

python3 bin/structural_qc.py --consensus "$OUT/broken.fasta" --reference "$REF" \
    --json "$OUT/broken.json" --text "$OUT/broken.txt" >/dev/null 2>&1
V=$(python3 -c "import json;print(json.load(open('$OUT/broken.json'))['verdict'])" 2>/dev/null)
if [ "$V" = "fail" ] || [ "$V" = "warn" ]; then
    ok "rearrangement detected (verdict: $V)"
    python3 - "$OUT/broken.json" <<'PY'
import json, sys
for f in json.load(open(sys.argv[1]))["findings"][:3]:
    print(f"        - {f['kind']}: {f['detail']}")
PY
else
    bad "rearrangement NOT detected (verdict: $V) — the check is not working"
fi

# exit code contract, used when scripting over many samples
python3 bin/structural_qc.py --consensus "$OUT/broken.fasta" --reference "$REF" \
    --fail-on fail >/dev/null 2>&1
if [ $? -ne 0 ]; then
    ok "--fail-on fail returns non-zero, so batch scripts can branch on it"
else
    bad "--fail-on fail returned zero on a broken genome"
fi

# ----------------------------------------------------------------- 4
head "Nextflow workflow — stub run"

if [ "$HAVE_NF" = "1" ]; then
    LOG="$OUT/nf.log"
    NXF_DISABLE_CHECK_LATEST=true "$NF" -log "$OUT/nextflow.log" run main.nf \
        -profile stub -stub-run -ansi-log false \
        -work-dir "$OUT/work" \
        --sample TESTSAMPLE \
        --reads1 demo_data/DRR030147_1.fastq.gz \
        --reads2 demo_data/DRR030147_2.fastq.gz \
        --pipeline shiver \
        --reference "$REF" \
        --outdir "$OUT/results" > "$LOG" 2>&1

    if [ $? -eq 0 ]; then
        ok "workflow completed"
    else
        bad "workflow failed — see below"
        tail -15 "$LOG" | sed 's/^/        /'
    fi

    [ -f "$OUT/results/shiver/TESTSAMPLE_consensus.fasta" ] \
        && ok "consensus published to the results folder" \
        || bad "no consensus in the results folder"

    [ -f "$OUT/results/qc/TESTSAMPLE_qc.json" ] \
        && ok "QC report published alongside it" \
        || bad "no QC report in the results folder"

    # every pipeline path should at least wire up
    for P in vpipe smaltalign viralngs hivgenopipe; do
        NXF_DISABLE_CHECK_LATEST=true "$NF" -log "$OUT/nextflow.log" run main.nf \
            -profile stub -stub-run -ansi-log false -work-dir "$OUT/work_$P" \
            --sample T_$P --reads1 demo_data/SAMPLE_A_1.fastq.gz \
            --reads2 demo_data/SAMPLE_A_2.fastq.gz \
            --pipeline "$P" --reference "$REF" \
            --outdir "$OUT/res_$P" > "$OUT/nf_$P.log" 2>&1 \
            && ok "$P path runs" \
            || { bad "$P path failed"; tail -8 "$OUT/nf_$P.log" | sed 's/^/        /'; }
    done
else
    note "skipped — nextflow is not installed"
fi

# ----------------------------------------------------------------- 5
head "Memory planning"

python3 - <<'PY'
import sys
sys.path.insert(0, '.')
import resources as r

host = r.host_memory_gb()
docker = r.docker_memory_gb()
print(f"        host RAM {host or '?'} GB, docker {docker or 'not running'}, "
      f"{r.host_cpus()} cores")
print(f"        suggested budget: {r.budget_gb()} GB")

ok = True
# A budget must never hand a label more than the whole budget.
for gb in (2, 4, 8, 16, 64):
    plan = r.plan(gb)
    for label, v in plan.items():
        if v["memory_gb"] > max(gb, 2):
            print(f"  FAIL  {label} asks {v['memory_gb']} GB from a {gb} GB budget")
            ok = False
# viral-ngs must be refused on a small budget and allowed on a large one.
if r.assess("viralngs", 4)["level"] != "blocked":
    print("  FAIL  viral-ngs not blocked at 4 GB"); ok = False
if r.assess("viralngs", 32)["level"] != "ok":
    print("  FAIL  viral-ngs not allowed at 32 GB"); ok = False
if "withLabel: assembly" not in r.render_config(8):
    print("  FAIL  generated config missing process labels"); ok = False
sys.exit(0 if ok else 1)
PY
if [ $? -eq 0 ]; then
    ok "memory plans stay within budget and gate pipelines correctly"
else
    bad "memory planning produced an inconsistent plan"
fi

# ----------------------------------------------------------------- 6
head "Cluster (optional)"

if [ -f hpc.yaml ] && grep -q '^host: *[^ ]' hpc.yaml 2>/dev/null; then
    HOST=$(awk '/^host:/{print $2}' hpc.yaml)
    USERN=$(awk '/^user:/{print $2}' hpc.yaml)
    AUTH=$(awk '/^auth:/{print $2}' hpc.yaml)
    DEST=${USERN:+$USERN@}$HOST
    if [ "${AUTH:-key}" = "password" ]; then
        note "cluster $DEST uses password login — enter it in the HPC panel"
        python3 -c "import paramiko" 2>/dev/null \
            && ok "paramiko present for password login" \
            || bad "password login needs paramiko: pip install paramiko"
    elif ssh -o BatchMode=yes -o ConnectTimeout=10 "$DEST" true 2>/dev/null; then
        ok "ssh $DEST works without a prompt"
    else
        bad "ssh $DEST failed — fix that in your terminal first"
        note "or switch the cluster to password login in the HPC panel"
    fi
else
    note "no cluster configured — set one up in the HPC panel if you want one"
fi

# ----------------------------------------------------------------- 7
head "Offline diagnosis"
python3 - > "$OUT/diag.txt" 2>&1 <<'DIAGPY'
import sys, os
sys.path.insert(0, os.getcwd())
import diagnostics as D

bad = []
cases = [
    ("exit status 137\nKilled", {"exit_code": 137}, "oom"),
    ("unknown flag: --profile", {}, "compose-v1"),
    ("Cannot connect to the Docker daemon", {}, "docker-daemon-down"),
    ("Unexpected input: 'switch' @ line 44", {}, "nf-strict-syntax"),
    ("Process requirement exceeds available memory", {},
     "nf-memory-request"),
    ("Permission denied (publickey,password).", {}, "ssh-auth"),
    ("No space left on device", {}, "disk-full"),
    ("gzip: stdin: unexpected end of file", {}, "bad-fastq"),
    ("", {"status": "complete",
          "qc": {"verdict": "fail", "findings": ["absent block 2180 bp"]}},
     "qc-structural-fail"),
]
for log, run, expect in cases:
    hits = D.diagnose(dict(run, status=run.get("status", "failed")), log)
    top = hits[0]["id"] if hits else None
    if top != expect:
        bad.append(f"{expect}: got {top}")

# A clean run must stay silent. A table that always finds something is
# worse than no table, because people stop reading it.
clean = D.diagnose({"status": "complete", "exit_code": 0,
                    "qc": {"verdict": "pass", "findings": []}},
                   "[SUCCESS] completed=6 failed=0")
if clean:
    bad.append(f"clean run flagged {[h['id'] for h in clean]}")

# Every rule must be able to fire, and must say what to do about it.
for rule in D.RULES:
    if not rule.get("log") and not rule.get("check") \
            and rule.get("exit_code") is None:
        bad.append(f"rule {rule['id']} can never match")
    if not rule.get("fix"):
        bad.append(f"rule {rule['id']} has no fix")

print("RULES:%d" % len(D.RULES))
print("PROBLEMS:" + ("; ".join(bad) if bad else "none"))
DIAGPY
DIAGRC=$?
if [ "$DIAGRC" -ne 0 ]; then
    bad "the diagnosis module raised an error"
    tail -4 "$OUT/diag.txt" | sed 's/^/        /'
elif grep -q "^PROBLEMS:none" "$OUT/diag.txt"; then
    ok "$(grep "^RULES:" "$OUT/diag.txt" | cut -d: -f2) rules: known failures matched, clean run silent"
else
    bad "diagnosis checks failed"
    grep "^PROBLEMS:" "$OUT/diag.txt" | sed 's/^/        /'
fi

head "Interpretation assistant"
AIOUT="$OUT/assistant.txt"
python3 - "$OUT" > "$AIOUT" 2>&1 <<'PY'
import json, os, sys, threading, time
sys.path.insert(0, os.getcwd())
from http.server import BaseHTTPRequestHandler, HTTPServer
import assistant

state = sys.argv[1]
bad = []

# Nothing identifying may leave by default: not the path, not the sample
# name, not the sequence.
cfg = dict(assistant.DEFAULTS)
raw = ("/home/someone/study/PATIENT_9/reads.fastq.gz\nPATIENT_9 failed\n"
       + "ACGT" * 15 + "\n")
out = assistant.scrub(raw, cfg, names=["PATIENT_9"])
for leak in ("/home/someone", "PATIENT_9", "ACGT" * 15):
    if leak in out:
        bad.append(f"default settings leaked {leak[:20]}")
# ...and opting in must genuinely restore it, or the control is a lie.
opted = assistant.scrub(raw, dict(cfg, send_paths=True,
                                  send_sample_names=True,
                                  send_sequences=True), names=["PATIENT_9"])
if "PATIENT_9" not in opted or "/home/someone" not in opted:
    bad.append("opting in did not restore the real values")

# The key file must not be world readable.
assistant.save_key(state, "anthropic", "sk-ant-selftest")
if assistant.load_key(state, "anthropic") != "sk-ant-selftest":
    bad.append("key did not round-trip")
if os.name == "posix":
    mode = oct(os.stat(assistant.key_path(state)).st_mode)[-3:]
    if mode != "600":
        bad.append(f"key file mode {mode}, expected 600")
assistant.save_key(state, "anthropic", "")

# A missing key must explain that a login is not the same as an API key.
try:
    assistant.ask(state, "q", "ctx", dict(cfg, provider="anthropic"))
    bad.append("asked with no key without complaining")
except assistant.AssistantError as exc:
    if "login" not in str(exc):
        bad.append("no-key message does not mention the login caveat")

# Full round trip against a stand-in provider: the guardrails must be in
# the system prompt every time, not only when someone remembers.
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        H.body = json.loads(self.rfile.read(n))
        out = json.dumps({"message": {"content": "ok"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
host = f"http://127.0.0.1:{srv.server_address[1]}"
answer = assistant.ask(state, "why?", "## Run\nstatus: failed",
                       dict(cfg, provider="ollama", model="m",
                            ollama_host=host))
if answer != "ok":
    bad.append("provider answer not returned")
sysmsg = H.body["messages"][0]["content"]
if "Never state or infer a drug-resistance call" not in sysmsg:
    bad.append("the clinical guardrail is missing from the system prompt")
if "Do not invent numbers" not in sysmsg:
    bad.append("the no-invented-numbers rule is missing")
srv.shutdown()

print("PROBLEMS:" + ("; ".join(bad) if bad else "none"))
PY
AIRC=$?
if [ "$AIRC" -ne 0 ]; then
    bad "the assistant module raised an error"
    tail -4 "$AIOUT" | sed 's/^/        /'
elif grep -q "^PROBLEMS:none" "$AIOUT"; then
    ok "redaction, key storage and the guardrail prompt all hold"
else
    bad "assistant checks failed"
    grep "^PROBLEMS:" "$AIOUT" | sed 's/^/        /'
fi

head "Interpretation over the web API"
python3 - "$OUT" > "$OUT/webai.txt" 2>&1 <<'WEBPY'
import json, os, sys, threading, time, urllib.error, urllib.request
sys.path.insert(0, os.getcwd())

state = sys.argv[1]
runs = os.path.join(state, "webruns")
os.makedirs(os.path.join(runs, "r1"), exist_ok=True)
open(os.path.join(runs, "r1", "run.log"), "w").write(
    "Command exit status:\n  137\n  .command.sh: line 9: Killed\n")
open(os.path.join(runs, "r1", "run.json"), "w").write(json.dumps({
    "id": "r1", "pipeline_id": "shiver", "pipeline_name": "shiver",
    "sample": {"id": "PT_77", "name": "PT_77", "layout": "Paired-end",
               "size_mb": 10.0, "r1": "/x/a.fq.gz", "r2": "",
               "folder": "."},
    "refs": {}, "threads": 2, "memory_gb": 4, "location": "local",
    "where": "this machine", "status": "failed", "exit_code": 137,
    "created_at": "2026-01-01T00:00:00+00:00", "consensus": None,
    "qc": None}))

os.environ["HIVPIPE_RUNS_DIR"] = runs
os.environ.setdefault("HIVPIPE_PROFILE", "stub")
import uvicorn, app

port = 8921
srv = uvicorn.Server(uvicorn.Config(app.app, host='127.0.0.1',
                                    port=port, log_level='error'))
threading.Thread(target=srv.run, daemon=True).start()
B = f'http://127.0.0.1:{port}'
for _ in range(60):
    try:
        urllib.request.urlopen(B + '/api/config', timeout=1)
        break
    except Exception:
        time.sleep(0.5)

bad = []

def get(path):
    with urllib.request.urlopen(B + path, timeout=15) as r:
        return json.load(r)

def post(path, body):
    req = urllib.request.Request(
        B + path, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

# The offline layer must diagnose a known failure over HTTP.
d = get('/api/runs/r1/diagnose')
if not d['findings'] or 'memory' not in d['findings'][0]['title']:
    bad.append(f"offline diagnosis missed the OOM: {d}")

# The assistant must advertise its providers and presets.
a = get('/api/assistant')
if set(a['providers']) < {'anthropic', 'openai', 'ollama'}:
    bad.append('providers missing')
if not a['presets']:
    bad.append('no presets offered')

# The preview must show the payload, redacted, with the offline
# findings folded in so the model cannot contradict them.
code, prev = post('/api/runs/r1/ask', {'question': '', 'preview': True})
if code != 200:
    bad.append(f'preview failed: {code} {prev}')
else:
    text = prev.get('preview', '')
    if 'PT_77' in text:
        bad.append('preview leaked the sample name')
    if 'Known issues already identified' not in text:
        bad.append('offline findings not passed to the model')

# Asking without a key must explain the login/API-key difference.
code, err = post('/api/runs/r1/ask', {'question': 'why?'})
if code != 502 or 'login' not in str(err.get('detail', '')):
    bad.append(f'no-key path wrong: {code} {err}')

print('PROBLEMS:' + ('; '.join(bad) if bad else 'none'))
WEBPY
WEBRC=$?
if [ "$WEBRC" -ne 0 ]; then
    bad "the web interpretation endpoints raised an error"
    tail -4 "$OUT/webai.txt" | sed 's/^/        /'
elif grep -q "^PROBLEMS:none" "$OUT/webai.txt"; then
    ok "diagnose, assistant and preview endpoints behave"
else
    bad "web interpretation endpoints failed"
    grep "^PROBLEMS:" "$OUT/webai.txt" | sed 's/^/        /'
fi

head "Desktop window (optional)"
# Skipped wherever there is no Tk or no display — a headless server, or a
# WSL distribution without WSLg. The web interface does not need either.
# GUI_PYTHON lets this be checked against another interpreter, since on
# Linux tkinter is a separate package and may not be on the default one.
GUI_PY="${GUI_PYTHON:-python3}"
if ! "$GUI_PY" -c "import tkinter" 2>/dev/null; then
    note "skipped — $GUI_PY has no tkinter (Linux: apt install python3-tk)"
elif [ -z "${DISPLAY:-}" ] && ! command -v xvfb-run >/dev/null; then
    note "skipped — no display and no xvfb-run"
else
    RUN_GUI="$GUI_PY"
    [ -z "${DISPLAY:-}" ] && RUN_GUI="xvfb-run -a $GUI_PY"
    GUIOUT="$OUT/gui.txt"
    HIVPIPE_DATA_DIR="$OUT/gui_reads" HIVPIPE_RUNS_DIR="$OUT/gui_runs" \
    HIVPIPE_REFS_DIR="$OUT/gui_refs" HIVPIPE_WORK_DIR="$OUT/gui_work" \
    HIVPIPE_PROFILE=stub \
    $RUN_GUI - <<'PY' > "$GUIOUT" 2>&1
import os, sys, tempfile, time
sys.path.insert(0, os.getcwd())
# No modal may open: an unattended dialog would block for ever.
import tkinter.messagebox as mb, tkinter.filedialog as fd
mb.showinfo = mb.showerror = mb.showwarning = lambda *a, **k: None
mb.askyesno = lambda *a, **k: True

for d in ("gui_reads", "gui_refs"):
    os.makedirs(os.environ["HIVPIPE_" + ("DATA" if "reads" in d else "REFS")
                           + "_DIR"], exist_ok=True)
import gui, app

tmp = tempfile.mkdtemp()
paths = []
for n in ("panel_a", "panel_b"):
    p = os.path.join(tmp, n + ".fasta")
    open(p, "w").write(">s\nACGTACGTAC\n")
    paths.append(p)

ui = gui.Runner()
def pump(s):
    t0 = time.time()
    while time.time() - t0 < s:
        ui.update(); time.sleep(0.02)
pump(1.5)

problems = []

# Two files of one kind must give two distinct rows. A composite row id
# containing a NUL silently collapsed them into one.
ui.up_kind.set(app.REF_KINDS["panel"])
for p in paths:
    fd.askopenfilename = (lambda q: (lambda **k: q))(p)
    ui.add_reference(); pump(0.6)
rows = list(ui.ref_tree.get_children())
if len(rows) != 2:
    problems.append(f"reference rows: expected 2, got {len(rows)}")
if len(set(rows)) != len(rows):
    problems.append("reference row ids collide")

# Deleting must remove the row that was selected, not another one.
want = [i for i in rows if ui.ref_tree.item(i)["values"][1] == "panel_a.fasta"]
if want:
    ui.ref_tree.selection_set(want[0]); ui.delete_reference(); pump(0.6)
    left = [f["name"] for f in ui.refs.get("panel", [])]
    if left != ["panel_b.fasta"]:
        problems.append(f"delete removed the wrong file: {left}")

# A budget too small for the pipeline must keep Start disabled, including
# after a start attempt fails.
ui.memory.set(2); ui.update_resources(); pump(0.2)
ui._run_started(False, Exception("simulated")); pump(0.2)
if str(ui.start_btn.cget("state")) != "disabled":
    problems.append("Start enabled on a budget that cannot run the pipeline")
ui.memory.set(16); ui.update_resources(); pump(0.2)
if str(ui.start_btn.cget("state")) != "normal":
    problems.append("Start stuck disabled on an ample budget")

# Closing must cancel its timers, or Tk prints a traceback on exit.
ui.on_close()
time.sleep(0.8)

print("PROBLEMS:" + ("; ".join(problems) if problems else "none"))
PY
    if grep -q "^PROBLEMS:none" "$GUIOUT" 2>/dev/null; then
        ok "window opens, references and run gating behave"
    else
        bad "desktop window misbehaved"
        grep -E "PROBLEMS:|Error|Traceback" "$GUIOUT" | head -4 | sed 's/^/        /'
    fi
    if grep -q "invalid command name" "$GUIOUT" 2>/dev/null; then
        bad "Tk timers still fire after the window closes"
    else
        ok "closes without Tk errors"
    fi
fi

head "Result"
printf '  %d passed, %d failed\n\n' "$pass" "$fail"

if [ "$fail" -eq 0 ]; then
    echo "  Ready. Start the interface with:  python3 app.py"
    echo "  For real runs you also need the pipeline images:"
    echo "      docker compose --profile setup run --rm warm-images"
    exit 0
else
    echo "  Fix the failures above before running real data."
    exit 1
fi
