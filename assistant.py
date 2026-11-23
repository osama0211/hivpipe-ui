"""The interpretation assistant: asks a language model about a finished run.

What this is for
----------------
Reading a failed Nextflow log, or working out what a structural-QC verdict
means, is tedious and the answer is usually obvious to someone who has seen
it before. That is a good job for a language model.

Deciding whether a patient is resistant to a drug is not. Stanford HIVdb is
a published, validated algorithm; a language model guessing at the same
question is strictly worse and carries real clinical risk. This module is
scoped to operational interpretation and the system prompt says so. Where a
deterministic answer exists, the application shows that answer and the model
only explains it.

Credentials
-----------
There is no way to sign in to claude.ai or chatgpt.com from another program.
Those are consumer web products with no third-party login, and automating a
browser session against them breaks constantly and risks the account. What
does work is an API key, from console.anthropic.com or
platform.openai.com. An API key is billed per token and is separate from
any Pro subscription.

A third option avoids both: `ollama`, which runs a model on this computer.
Nothing leaves the machine, which for clinical sequence data may be the only
option the ethics approval allows.

What gets sent
--------------
By default: run parameters, metrics, QC verdicts and an excerpt of the log,
with absolute paths reduced to file names and sample names replaced by
generic labels. Not the sequence. Both of those defaults can be changed, and
the application shows the exact payload before anything is sent.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 120
MAX_LOG_CHARS = 6000

SYSTEM_PROMPT = """\
You are a bioinformatics assistant built into a tool that runs HIV-1 \
genome assembly pipelines (shiver, V-pipe, SmaltAlign, viral-ngs, \
HIVGenoPipe) through Nextflow.

You help with operational questions: why a run failed, what a quality-control \
verdict means, how two pipelines' outputs differ, what a parameter does, and \
how to phrase a methods description.

Hard limits:
- Never state or infer a drug-resistance call, a resistance level, or a \
clinical recommendation. Stanford HIVdb is the validated source for that and \
the application already shows its output. If asked, say that this is what \
HIVdb is for and point at its result.
- Never diagnose, stage, or advise on treatment for any patient.
- Do not invent numbers. Every figure you give must appear in the context \
you were given. If something needed is missing, say which field is missing.
- If the context does not support an answer, say so rather than guessing.

Be concise and concrete. Name the file, the parameter or the log line you are \
reasoning from. When something is wrong, say what to change.\
"""

PRESETS = [
    ("Why did this run fail?",
     "Read the log excerpt and explain why this run failed, and what to "
     "change to make it succeed."),
    ("Explain the quality report",
     "Explain the structural QC verdict and each finding in plain language. "
     "What would cause this, and is the consensus usable?"),
    ("Is this consensus worth keeping?",
     "Based on the metrics and the QC verdict, assess whether this assembly "
     "looks sound. List what you would check before trusting it."),
    ("Explain the settings used",
     "Explain what each parameter in this run controls and whether the "
     "values chosen are reasonable for HIV-1."),
    ("Draft a methods paragraph",
     "Write a short methods paragraph describing exactly this run, suitable "
     "for a manuscript. Use only the parameters given."),
]


# ------------------------------------------------------------- providers


def _post(url, payload, headers):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise AssistantError(_explain_http(exc.code, body)) from exc
    except urllib.error.URLError as exc:
        raise AssistantError(
            f"Could not reach {url.split('/')[2]}: {exc.reason}. "
            "Check the network, or use Ollama to run a model locally."
        ) from exc
    except TimeoutError as exc:
        raise AssistantError(
            f"No reply within {TIMEOUT} seconds.") from exc


def _explain_http(code, body):
    detail = ""
    try:
        parsed = json.loads(body)
        detail = (parsed.get("error") or {}).get("message", "") or ""
    except (json.JSONDecodeError, AttributeError):
        detail = body.strip()[:200]
    if code == 401:
        return ("The API key was rejected. Check it was copied whole, and "
                "that it belongs to the provider selected here.")
    if code == 403:
        return f"The key is valid but not allowed to use this model. {detail}"
    if code == 404:
        return f"No such model for this account. {detail}"
    if code == 429:
        return ("Rate limited, or the account is out of credit. API usage is "
                "billed separately from any Pro subscription.")
    if 500 <= code < 600:
        return f"The provider returned a server error ({code}). Try again."
    return f"The provider refused the request ({code}). {detail}"


def _anthropic(cfg, key, system, user):
    out = _post(
        "https://api.anthropic.com/v1/messages",
        {"model": cfg["model"], "max_tokens": 1600, "system": system,
         "messages": [{"role": "user", "content": user}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"})
    parts = [b.get("text", "") for b in out.get("content", [])
             if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _openai(cfg, key, system, user):
    out = _post(
        "https://api.openai.com/v1/chat/completions",
        {"model": cfg["model"], "max_tokens": 1600,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
        {"Authorization": f"Bearer {key}"})
    choices = out.get("choices") or []
    if not choices:
        raise AssistantError("The provider returned no answer.")
    return (choices[0].get("message", {}).get("content") or "").strip()


def _ollama(cfg, _key, system, user):
    host = cfg.get("ollama_host") or "http://localhost:11434"
    out = _post(
        f"{host.rstrip('/')}/api/chat",
        {"model": cfg["model"], "stream": False,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
        {})
    return (out.get("message", {}).get("content") or "").strip()


PROVIDERS = {
    "anthropic": {
        "label": "Claude (Anthropic API)",
        "call": _anthropic,
        "needs_key": True,
        "key_env": "ANTHROPIC_API_KEY",
        "key_hint": "Create one at console.anthropic.com -> API keys. "
                    "Starts with sk-ant-.",
        "models": ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"],
    },
    "openai": {
        "label": "ChatGPT (OpenAI API)",
        "call": _openai,
        "needs_key": True,
        "key_env": "OPENAI_API_KEY",
        "key_hint": "Create one at platform.openai.com -> API keys. "
                    "Starts with sk-.",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
    },
    "ollama": {
        "label": "Local model (Ollama) — nothing leaves this computer",
        "call": _ollama,
        "needs_key": False,
        "key_env": "",
        "key_hint": "Install Ollama and pull a model: ollama pull llama3.1",
        "models": ["llama3.1", "qwen2.5", "mistral"],
    },
}

DEFAULTS = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "ollama_host": "http://localhost:11434",
    "send_sequences": False,
    "send_sample_names": False,
    "send_paths": False,
}


class AssistantError(RuntimeError):
    """Something the user can act on, phrased for them."""


# --------------------------------------------------------------- storage


def config_path(state_dir):
    return Path(state_dir) / "assistant.json"


def key_path(state_dir):
    return Path(state_dir) / "assistant_keys.json"


def load_config(state_dir):
    cfg = dict(DEFAULTS)
    p = config_path(state_dir)
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    if cfg.get("provider") not in PROVIDERS:
        cfg["provider"] = DEFAULTS["provider"]
    return cfg


def save_config(state_dir, cfg):
    config_path(state_dir).write_text(json.dumps(cfg, indent=2))


def load_key(state_dir, provider):
    """The key for one provider, environment first.

    Keys are kept in a file in the application's own folder with owner-only
    permissions. That is not a secrets vault: anyone who can read the home
    directory can read it. The environment variable exists so a site that
    manages secrets properly can supply the key that way and leave the file
    unused.
    """
    env = PROVIDERS.get(provider, {}).get("key_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    p = key_path(state_dir)
    if not p.exists():
        return ""
    try:
        return (json.loads(p.read_text()).get(provider) or "").strip()
    except (json.JSONDecodeError, OSError):
        return ""


def save_key(state_dir, provider, key):
    p = key_path(state_dir)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if key:
        data[provider] = key.strip()
    else:
        data.pop(provider, None)
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass          # Windows ignores POSIX modes; the file is still per-user.


def key_source(state_dir, provider):
    """Where the key in use came from, for display."""
    env = PROVIDERS.get(provider, {}).get("key_env")
    if env and os.environ.get(env):
        return f"from {env}"
    if load_key(state_dir, provider):
        return "saved in this application"
    return "not set"


# -------------------------------------------------------------- redaction

_ABS_PATH = re.compile(r"(/[\w.\-]+){2,}/?|[A-Za-z]:\\[\w.\\\-]+")
_SEQ_LINE = re.compile(r"^[ACGTUNRYSWKMBDHVacgtunryswkmbdhv\-?]{40,}$")


def scrub(text, cfg, names=()):
    """Remove what the settings say must not leave this computer."""
    if not text:
        return ""
    out = text
    if not cfg.get("send_paths"):
        out = _ABS_PATH.sub(lambda m: Path(m.group(0)).name or "<path>", out)
    if not cfg.get("send_sample_names"):
        for i, n in enumerate(sorted(set(names), key=len, reverse=True), 1):
            if n:
                out = out.replace(n, f"SAMPLE_{i}")
    if not cfg.get("send_sequences"):
        out = "\n".join("<sequence withheld>" if _SEQ_LINE.match(line.strip())
                        else line for line in out.splitlines())
    return out


# ---------------------------------------------------------------- context


def consensus_stats(path):
    """Length and base composition, computed here so the sequence need not
    be sent for the model to reason about coverage."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    seq = "".join(l.strip() for l in text.splitlines() if not l.startswith(">"))
    if not seq:
        return None
    up = seq.upper()
    called = sum(up.count(b) for b in "ACGT")
    return {
        "length": len(seq),
        "called_bases": called,
        "N": up.count("N"),
        "no_coverage_?": up.count("?"),
        "gaps": up.count("-"),
        "ambiguity_codes": len(seq) - called - up.count("N")
                           - up.count("?") - up.count("-"),
    }


def build_context(run, log_text, cfg):
    """The facts the model is allowed to see, as readable text."""
    names = [run.get("sample", {}).get("name", "")]
    lines = [
        "## Run",
        f"pipeline: {run.get('pipeline_name')} ({run.get('pipeline_id')})",
        f"status: {run.get('status')}  exit code: {run.get('exit_code')}",
        f"location: {run.get('where')}",
        f"memory budget: {run.get('memory_gb')} GB   threads: {run.get('threads')}",
    ]
    if run.get("memory_note"):
        lines.append(f"resource note: {run['memory_note']}")

    sample = run.get("sample") or {}
    lines += ["", "## Sample",
              f"layout: {sample.get('layout')}",
              f"read data size: {sample.get('size_mb')} MB"]

    refs = {k: v for k, v in (run.get("refs") or {}).items() if v}
    if refs:
        lines += ["", "## Reference material"]
        lines += [f"{k}: {Path(v).name}" for k, v in refs.items()]

    if run.get("consensus"):
        stats = consensus_stats(run["consensus"])
        if stats:
            lines += ["", "## Consensus statistics (computed locally)"]
            lines += [f"{k}: {v}" for k, v in stats.items()]

    qc = run.get("qc")
    if qc:
        lines += ["", "## Structural quality check",
                  f"verdict: {qc.get('verdict')}"]
        for f in qc.get("findings", []) or []:
            lines.append("finding: " + (f if isinstance(f, str) else json.dumps(f)))
    else:
        lines += ["", "## Structural quality check", "not available"]

    if log_text:
        tail = log_text[-MAX_LOG_CHARS:]
        if len(log_text) > MAX_LOG_CHARS:
            tail = "...(earlier lines omitted)...\n" + tail
        lines += ["", "## Log excerpt", tail]

    return scrub("\n".join(lines), cfg, names)


def compose(question, context):
    return (f"{context}\n\n## Question\n{question}\n\n"
            "Answer from the context above only.")


# ------------------------------------------------------------------- ask


def ask(state_dir, question, context, cfg=None):
    cfg = cfg or load_config(state_dir)
    provider = cfg.get("provider")
    spec = PROVIDERS.get(provider)
    if not spec:
        raise AssistantError(f"Unknown provider '{provider}'.")

    key = ""
    if spec["needs_key"]:
        key = load_key(state_dir, provider)
        if not key:
            raise AssistantError(
                f"No API key for {spec['label']}. {spec['key_hint']}\n\n"
                "Note: this needs an API key, not your claude.ai or "
                "chatgpt.com login — those cannot be used from another "
                "program.")
    if not cfg.get("model"):
        raise AssistantError("No model chosen.")

    answer = spec["call"](cfg, key, SYSTEM_PROMPT, compose(question, context))
    if not answer:
        raise AssistantError("The provider returned an empty answer.")
    return answer
