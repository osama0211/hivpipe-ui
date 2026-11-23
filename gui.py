"""HIV pipeline runner — native desktop window.

    python gui.py

This is the same application as the web interface, with the web part taken
out. It imports `app` and calls its functions directly, so **nothing listens
on a port**: no uvicorn, no localhost, no browser, and nothing for a firewall
or a corporate proxy to interfere with. The validation, the Nextflow command
and the run bookkeeping are the same code the web interface uses, so the two
cannot drift apart.

Everything slow — scanning a cluster for samples, probing SSH, starting a run
— happens on a worker thread. Tk is not thread-safe, so those threads never
touch a widget: they put their result on a queue that the main loop drains.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fastapi import HTTPException

import app
import assistant
import diagnostics
import hpc
import resources

POLL_MS = 1200          # how often the run list and the open log refresh
DRAIN_MS = 120          # how often results from worker threads are collected


def local_time(iso):
    """A run's timestamp as the person's own clock shows it.

    Runs are stored in UTC, which is right for the file and wrong for a
    window someone is watching: a run they started a minute ago should not
    appear to have started five hours ago.
    """
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return iso.replace("T", " ").replace("+00:00", "")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%d %b %H:%M")


def human_error(exc):
    """The message to show the user for an exception from app or hpc."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, hpc.HPCError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class Runner(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("HIV pipeline runner")
        self.geometry("1120x840")
        self.minsize(940, 680)

        self.results = queue.Queue()
        self.samples = []
        self.refs = {}
        self.ref_rows = {}
        self.open_run = None
        self.log_offset = 0
        self.busy = 0

        self.closing = False
        self._jobs = []

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._jobs.append(self.after(DRAIN_MS, self._drain))
        self._jobs.append(self.after(POLL_MS, self._tick))

        self.refresh_references()
        self.rescan_samples()
        self.update_resources()
        self.refresh_runs()

    # ------------------------------------------------------------ chrome

    def _style(self):
        s = ttk.Style(self)
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Head.TLabel", font=("TkDefaultFont", 13, "bold"))
        s.configure("Quiet.TLabel", foreground="#5c6672")
        s.configure("Tiny.TLabel", foreground="#78828f", font=("TkDefaultFont", 8))
        s.configure("Good.TLabel", foreground="#1b7a45")
        s.configure("Warn.TLabel", foreground="#8a5a00")
        s.configure("Bad.TLabel", foreground="#b3261e")
        s.configure("Go.TButton", font=("TkDefaultFont", 10, "bold"))

    def _build(self):
        bar = ttk.Frame(self, padding=(12, 10, 12, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="HIV pipeline runner", style="Head.TLabel").pack(side="left")
        ttk.Label(bar, text="  five pipelines, one window", style="Quiet.TLabel"
                  ).pack(side="left")
        self.status = ttk.Label(bar, text="", style="Quiet.TLabel")
        self.status.pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tab_run = ttk.Frame(nb, padding=10)
        self.tab_refs = ttk.Frame(nb, padding=10)
        self.tab_ai = ttk.Frame(nb, padding=10)
        self.tab_hpc = ttk.Frame(nb, padding=10)
        self.tab_machine = ttk.Frame(nb, padding=10)
        nb.add(self.tab_run, text="  Run  ")
        nb.add(self.tab_refs, text="  References  ")
        nb.add(self.tab_ai, text="  Interpret  ")
        nb.add(self.tab_hpc, text="  Cluster  ")
        nb.add(self.tab_machine, text="  This machine  ")
        nb.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.nb = nb

        self._build_run()
        self._build_refs()
        self._build_ai()
        self._build_hpc()
        self._build_machine()

    def _tab_changed(self, _evt):
        tab = self.nb.tab(self.nb.select(), "text").strip()
        if tab == "This machine":
            self.refresh_machine()
        elif tab == "Cluster":
            self.load_hpc_fields()
        elif tab == "References":
            self.refresh_references()
        elif tab == "Interpret":
            self.load_ai_fields()

    def set_status(self, text):
        self.status.configure(text=text)

    # ----------------------------------------------------------- run tab

    def _build_run(self):
        t = self.tab_run
        t.columnconfigure(0, weight=3, uniform="col")
        t.columnconfigure(1, weight=2, uniform="col")
        t.rowconfigure(2, weight=1)

        # --- pipeline ---------------------------------------------------
        pf = ttk.LabelFrame(t, text="Pipeline", padding=8)
        pf.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.pipeline = tk.StringVar(value=app.PIPELINES[0]["id"])
        for p in app.PIPELINES:
            row = ttk.Frame(pf)
            row.pack(fill="x", anchor="w")
            ttk.Radiobutton(row, text=p["name"], value=p["id"],
                            variable=self.pipeline,
                            command=self.update_resources).pack(side="left")
            ttk.Label(row, text="  " + p["blurb"], style="Tiny.TLabel"
                      ).pack(side="left")

        # --- resources --------------------------------------------------
        rf = ttk.LabelFrame(t, text="Resources", padding=8)
        rf.grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        grid = ttk.Frame(rf)
        grid.pack(fill="x")
        ttk.Label(grid, text="Memory (GB)").grid(row=0, column=0, sticky="w")
        self.memory = tk.IntVar(value=resources.budget_gb())
        ttk.Spinbox(grid, from_=2, to=512, textvariable=self.memory, width=7,
                    command=self.update_resources).grid(row=0, column=1, padx=(6, 16))
        ttk.Label(grid, text="Threads").grid(row=0, column=2, sticky="w")
        self.threads = tk.IntVar(value=app.THREADS)
        ttk.Spinbox(grid, from_=1, to=256, textvariable=self.threads, width=7
                    ).grid(row=0, column=3, padx=6)
        self.machine_line = ttk.Label(rf, text="", style="Tiny.TLabel")
        self.machine_line.pack(anchor="w", pady=(8, 0))
        self.verdict = ttk.Label(rf, text="", wraplength=380, justify="left")
        self.verdict.pack(anchor="w", pady=(4, 0))

        # --- samples ----------------------------------------------------
        sf = ttk.LabelFrame(t, text="Samples", padding=8)
        sf.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(0, 8))
        head = ttk.Frame(sf)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="Read from").pack(side="left")
        self.location = tk.StringVar(value="This computer")
        loc = ttk.Combobox(head, textvariable=self.location, width=16,
                           state="readonly",
                           values=["This computer", "Cluster"])
        loc.pack(side="left", padx=6)
        loc.bind("<<ComboboxSelected>>", lambda e: (self.rescan_samples(),
                                                    self.update_resources()))
        ttk.Button(head, text="Rescan", command=self.rescan_samples).pack(side="left")

        cols = ("name", "layout", "size", "folder")
        self.sample_tree = ttk.Treeview(sf, columns=cols, show="headings", height=9)
        for c, w, txt in (("name", 200, "Sample"), ("layout", 100, "Layout"),
                          ("size", 80, "Size MB"), ("folder", 140, "Folder")):
            self.sample_tree.heading(c, text=txt)
            self.sample_tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(sf, orient="vertical", command=self.sample_tree.yview)
        self.sample_tree.configure(yscrollcommand=sb.set)
        self.sample_tree.pack(side="top", fill="both", expand=True)
        sb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.sample_tree.bind("<<TreeviewSelect>>",
                              lambda e: self.update_resources())

        # Where the reads are read from, and a way straight to it. Without
        # this the first question anyone asks is where to put the files.
        foot = ttk.Frame(sf)
        foot.pack(side="top", fill="x", pady=(6, 0))
        ttk.Button(foot, text="Open reads folder",
                   command=lambda: self.open_folder(app.DATA_DIR)
                   ).pack(side="left")
        ttk.Label(foot, text="  " + str(app.DATA_DIR), style="Tiny.TLabel",
                  wraplength=430, justify="left").pack(side="left")

        # --- references + start -----------------------------------------
        right = ttk.Frame(t)
        right.grid(row=1, column=1, sticky="nsew")
        xf = ttk.LabelFrame(right, text="Reference material", padding=8)
        xf.pack(fill="x")
        self.ref_vars = {}
        for i, (kind, label) in enumerate(app.REF_KINDS.items()):
            ttk.Label(xf, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            box = ttk.Combobox(xf, textvariable=var, width=30, state="readonly")
            box.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=2)
            self.ref_vars[kind] = (var, box)
        xf.columnconfigure(1, weight=1)
        ttk.Label(xf, text="Set these on the References tab.",
                  style="Tiny.TLabel").grid(row=len(app.REF_KINDS), column=0,
                                            columnspan=2, sticky="w", pady=(6, 0))

        go = ttk.Frame(right, padding=(0, 10, 0, 0))
        go.pack(fill="x")
        self.start_btn = ttk.Button(go, text="Start run", style="Go.TButton",
                                    command=self.start_run)
        self.start_btn.pack(fill="x", ipady=4)
        self.profile_line = ttk.Label(
            go, text=f"Profile {app.PROFILE} · {app.NEXTFLOW}",
            style="Tiny.TLabel", wraplength=380, justify="left")
        self.profile_line.pack(anchor="w", pady=(6, 0))

        # --- runs + log --------------------------------------------------
        bottom = ttk.Frame(t)
        bottom.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        t.rowconfigure(3, weight=2)
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)

        rh = ttk.Frame(bottom)
        rh.grid(row=0, column=0, sticky="ew")
        ttk.Label(rh, text="Runs", style="Head.TLabel").pack(side="left")
        ttk.Button(rh, text="Cancel", command=self.cancel_run).pack(side="right")
        self.save_btn = ttk.Button(rh, text="Save consensus…",
                                   command=self.save_consensus, state="disabled")
        self.save_btn.pack(side="right", padx=6)

        pane = ttk.PanedWindow(bottom, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        rwrap = ttk.Frame(pane)
        rcols = ("when", "pipeline", "sample", "where", "status")
        self.run_tree = ttk.Treeview(rwrap, columns=rcols, show="headings", height=7)
        # Widths are chosen so the status column survives at the default sash
        # position; a clipped status is the one column that must never clip.
        for c, w, txt in (("when", 104, "Started"), ("pipeline", 78, "Pipeline"),
                          ("sample", 104, "Sample"), ("where", 80, "Where"),
                          ("status", 74, "Status")):
            self.run_tree.heading(c, text=txt)
            self.run_tree.column(c, width=w, minwidth=60, anchor="w")
        rsb = ttk.Scrollbar(rwrap, orient="vertical", command=self.run_tree.yview)
        self.run_tree.configure(yscrollcommand=rsb.set)
        self.run_tree.pack(side="left", fill="both", expand=True)
        rsb.pack(side="left", fill="y")
        self.run_tree.bind("<<TreeviewSelect>>", self.on_run_selected)
        pane.add(rwrap, weight=2)

        lwrap = ttk.Frame(pane)
        self.log = tk.Text(lwrap, wrap="word", height=10, state="disabled",
                           background="#0d1117", foreground="#d7dde5",
                           insertbackground="#d7dde5", relief="flat",
                           font=("TkFixedFont", 9), padx=8, pady=6)
        lsb = ttk.Scrollbar(lwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")
        pane.add(lwrap, weight=3)

    # ---------------------------------------------------- references tab

    def _build_refs(self):
        t = self.tab_refs
        top = ttk.LabelFrame(t, text="Add a file", padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Kind").pack(side="left")
        self.up_kind = tk.StringVar(value=list(app.REF_KINDS.values())[0])
        ttk.Combobox(top, textvariable=self.up_kind, state="readonly", width=20,
                     values=list(app.REF_KINDS.values())).pack(side="left", padx=8)
        ttk.Button(top, text="Choose a FASTA file…",
                   command=self.add_reference).pack(side="left")
        ttk.Label(top, text="   Copied into the references folder.",
                  style="Tiny.TLabel").pack(side="left")

        body = ttk.LabelFrame(t, text="Stored files", padding=10)
        body.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("kind", "name", "size")
        self.ref_tree = ttk.Treeview(body, columns=cols, show="headings")
        for c, w, txt in (("kind", 170, "Kind"), ("name", 420, "File"),
                          ("size", 100, "Size kB")):
            self.ref_tree.heading(c, text=txt)
            self.ref_tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.ref_tree.yview)
        self.ref_tree.configure(yscrollcommand=sb.set)
        self.ref_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        foot = ttk.Frame(t, padding=(0, 8, 0, 0))
        foot.pack(fill="x")
        ttk.Button(foot, text="Delete selected",
                   command=self.delete_reference).pack(side="left")
        self.ref_dir_line = ttk.Label(foot, text=f"  {app.REF_DIR}",
                                      style="Tiny.TLabel")
        self.ref_dir_line.pack(side="left")

    # ----------------------------------------------------- interpret tab

    def _build_ai(self):
        t = self.tab_ai
        t.columnconfigure(0, weight=1)
        t.rowconfigure(2, weight=1)

        setup = ttk.LabelFrame(t, text="Model", padding=8)
        setup.grid(row=0, column=0, sticky="ew")
        setup.columnconfigure(1, weight=1)

        ttk.Label(setup, text="Provider").grid(row=0, column=0, sticky="w")
        self.ai_provider = tk.StringVar()
        prov = ttk.Combobox(
            setup, textvariable=self.ai_provider, state="readonly", width=46,
            values=[assistant.PROVIDERS[p]["label"] for p in assistant.PROVIDERS])
        prov.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=2)
        prov.bind("<<ComboboxSelected>>", lambda e: self.on_provider_changed())

        ttk.Label(setup, text="Model").grid(row=1, column=0, sticky="w")
        self.ai_model = tk.StringVar()
        self.ai_model_box = ttk.Combobox(setup, textvariable=self.ai_model,
                                         width=46)
        self.ai_model_box.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(setup, text="API key").grid(row=2, column=0, sticky="w")
        keyrow = ttk.Frame(setup)
        keyrow.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2)
        self.ai_key = tk.StringVar()
        self.ai_key_entry = ttk.Entry(keyrow, textvariable=self.ai_key,
                                      show="•", width=38)
        self.ai_key_entry.pack(side="left")
        ttk.Button(keyrow, text="Save", command=self.save_ai_key).pack(
            side="left", padx=4)
        ttk.Button(keyrow, text="Forget", command=self.forget_ai_key).pack(
            side="left")
        self.ai_key_state = ttk.Label(keyrow, text="", style="Tiny.TLabel")
        self.ai_key_state.pack(side="left", padx=6)

        self.ai_hint = ttk.Label(setup, text="", style="Tiny.TLabel",
                                 wraplength=760, justify="left")
        self.ai_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(setup, wraplength=760, justify="left", style="Tiny.TLabel",
                  text="This needs an API key, not your claude.ai or "
                       "chatgpt.com password — those logins cannot be "
                       "used from another program. API usage is billed per "
                       "token, separately from any Pro subscription. Ollama "
                       "runs a model on this computer and sends nothing."
                  ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))

        priv = ttk.LabelFrame(t, text="What may leave this computer", padding=8)
        priv.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.ai_send_names = tk.BooleanVar()
        self.ai_send_paths = tk.BooleanVar()
        self.ai_send_seq = tk.BooleanVar()
        for col, (var, text) in enumerate((
                (self.ai_send_names, "Sample names"),
                (self.ai_send_paths, "File paths"),
                (self.ai_send_seq, "Sequence data"))):
            ttk.Checkbutton(priv, text=text, variable=var,
                            command=self.save_ai_config
                            ).grid(row=0, column=col, sticky="w", padx=(0, 18))
        ttk.Label(priv, style="Tiny.TLabel", wraplength=760, justify="left",
                  text="All three are off by default. Metrics, settings and "
                       "quality verdicts are always sent; consensus "
                       "statistics are computed here so the sequence itself "
                       "need not be. Use “Show what will be sent” to "
                       "see the exact text first."
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        work = ttk.Frame(t)
        work.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        work.columnconfigure(0, weight=1)
        work.rowconfigure(2, weight=1)

        bar = ttk.Frame(work)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="About the run selected on the Run tab:"
                  ).pack(side="left")
        self.ai_run_label = ttk.Label(bar, text="none selected",
                                      style="Quiet.TLabel")
        self.ai_run_label.pack(side="left", padx=6)
        ttk.Button(bar, text="Show what will be sent",
                   command=self.preview_payload).pack(side="right")

        presets = ttk.Frame(work)
        presets.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(presets, text="Check offline",
                   command=self.show_diagnosis).pack(side="left", padx=(0, 12))
        ttk.Label(presets, text="ask a model:", style="Quiet.TLabel"
                  ).pack(side="left", padx=(0, 6))
        for label, prompt in assistant.PRESETS:
            ttk.Button(presets, text=label,
                       command=(lambda p=prompt: self.ask_ai(p))
                       ).pack(side="left", padx=(0, 5))

        ansbox = ttk.Frame(work)
        ansbox.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.ai_answer = tk.Text(ansbox, wrap="word", height=14,
                                 state="disabled", relief="flat",
                                 padx=10, pady=8)
        asb = ttk.Scrollbar(ansbox, orient="vertical",
                            command=self.ai_answer.yview)
        self.ai_answer.configure(yscrollcommand=asb.set)
        self.ai_answer.pack(side="left", fill="both", expand=True)
        asb.pack(side="left", fill="y")

        askrow = ttk.Frame(work)
        askrow.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.ai_question = tk.StringVar()
        entry = ttk.Entry(askrow, textvariable=self.ai_question)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.ask_ai(self.ai_question.get()))
        self.ai_ask_btn = ttk.Button(askrow, text="Ask",
                                     command=lambda: self.ask_ai(
                                         self.ai_question.get()))
        self.ai_ask_btn.pack(side="left", padx=(6, 0))

        ttk.Label(t, style="Tiny.TLabel", wraplength=900, justify="left",
                  text="The assistant does not give drug-resistance or "
                       "clinical interpretations. Stanford HIVdb is the "
                       "validated source for those, and its output is shown "
                       "on the Run tab. Treat everything here as a reading "
                       "aid, not a result."
                  ).grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _provider_id(self):
        label = self.ai_provider.get()
        for pid, spec in assistant.PROVIDERS.items():
            if spec["label"] == label:
                return pid
        return assistant.DEFAULTS["provider"]

    def load_ai_fields(self):
        cfg = assistant.load_config(app.STATE_DIR)
        self.ai_cfg = cfg
        spec = assistant.PROVIDERS[cfg["provider"]]
        self.ai_provider.set(spec["label"])
        self.ai_model_box["values"] = spec["models"]
        self.ai_model.set(cfg.get("model") or spec["models"][0])
        self.ai_send_names.set(bool(cfg.get("send_sample_names")))
        self.ai_send_paths.set(bool(cfg.get("send_paths")))
        self.ai_send_seq.set(bool(cfg.get("send_sequences")))
        self.ai_hint.configure(text=spec["key_hint"])
        self._refresh_key_state()
        self._refresh_ai_run_label()
        self.show_diagnosis()

    def _refresh_key_state(self):
        pid = self._provider_id()
        spec = assistant.PROVIDERS[pid]
        if not spec["needs_key"]:
            self.ai_key_entry.configure(state="disabled")
            self.ai_key_state.configure(text="no key needed")
            return
        self.ai_key_entry.configure(state="normal")
        self.ai_key_state.configure(
            text=assistant.key_source(app.STATE_DIR, pid))

    def _refresh_ai_run_label(self):
        with app.RUNS_LOCK:
            run = app.RUNS.get(self.open_run or "")
        if not run:
            self.ai_run_label.configure(text="none selected")
        else:
            self.ai_run_label.configure(
                text=f"{run['pipeline_name']} on {run['sample']['name']} "
                     f"— {run['status']}")

    def on_provider_changed(self):
        pid = self._provider_id()
        spec = assistant.PROVIDERS[pid]
        self.ai_model_box["values"] = spec["models"]
        if self.ai_model.get() not in spec["models"]:
            self.ai_model.set(spec["models"][0])
        self.ai_hint.configure(text=spec["key_hint"])
        self._refresh_key_state()
        self.save_ai_config()

    def save_ai_config(self):
        cfg = assistant.load_config(app.STATE_DIR)
        cfg.update({
            "provider": self._provider_id(),
            "model": self.ai_model.get().strip(),
            "send_sample_names": bool(self.ai_send_names.get()),
            "send_paths": bool(self.ai_send_paths.get()),
            "send_sequences": bool(self.ai_send_seq.get()),
        })
        assistant.save_config(app.STATE_DIR, cfg)
        self.ai_cfg = cfg

    def save_ai_key(self):
        self.save_ai_config()
        assistant.save_key(app.STATE_DIR, self._provider_id(),
                           self.ai_key.get())
        self.ai_key.set("")
        self._refresh_key_state()
        self.set_status("API key saved")

    def forget_ai_key(self):
        assistant.save_key(app.STATE_DIR, self._provider_id(), "")
        self.ai_key.set("")
        self._refresh_key_state()

    def _ai_context(self):
        with app.RUNS_LOCK:
            run = app.RUNS.get(self.open_run or "")
        if not run:
            return None, None
        log = ""
        path = app.run_path(run["id"]) / "run.log"
        if path.exists():
            log = path.read_text(encoding="utf-8", errors="replace")
        cfg = assistant.load_config(app.STATE_DIR)
        context = assistant.build_context(run, log, cfg)
        # Hand the model what the table already concluded, so it builds on
        # that instead of contradicting a deterministic answer.
        report = diagnostics.summarise(run, log, app.PROFILE)
        if report:
            context += ("\n\n## Known issues already identified by the "
                        "application's own checks\n" + report)
        return run, context

    def show_diagnosis(self):
        """The offline check. Runs first, costs nothing, sends nothing."""
        with app.RUNS_LOCK:
            run = app.RUNS.get(self.open_run or "")
        if not run:
            self._show_answer(
                "Open a run on the Run tab and this will check it against "
                "the built-in table of known failures — no model, no "
                "network, no key needed.")
            return
        log = ""
        path = app.run_path(run["id"]) / "run.log"
        if path.exists():
            log = path.read_text(encoding="utf-8", errors="replace")
        report = diagnostics.summarise(run, log, app.PROFILE)
        header = ("Checked against the built-in table. Nothing was sent "
                  "anywhere.\n" + "─" * 66 + "\n\n")
        self._show_answer(header + (report or
                                    diagnostics.no_match_advice(run)))

    def preview_payload(self):
        run, context = self._ai_context()
        if not run:
            messagebox.showinfo("Nothing selected",
                                "Open a run on the Run tab first.")
            return
        self._show_answer("This is the entire text that would be sent, "
                          "with nothing added:\n\n" + context)

    def ask_ai(self, question):
        question = (question or "").strip()
        if not question:
            return
        run, context = self._ai_context()
        if not run:
            messagebox.showinfo("Nothing selected",
                                "Open a run on the Run tab first, so there "
                                "is something to interpret.")
            return
        self.save_ai_config()
        self.ai_ask_btn.configure(state="disabled")
        self._show_answer("Thinking…")
        cfg = assistant.load_config(app.STATE_DIR)
        self._bg(lambda: assistant.ask(app.STATE_DIR, question, context, cfg),
                 self._ai_answered, "Asking the model…")

    def _ai_answered(self, ok, value):
        self.ai_ask_btn.configure(state="normal")
        if not ok:
            self._show_answer("Could not get an answer.\n\n"
                              + human_error(value))
            return
        self._show_answer(value)
        self.ai_question.set("")

    def _show_answer(self, text):
        self.ai_answer.configure(state="normal")
        self.ai_answer.delete("1.0", "end")
        self.ai_answer.insert("1.0", text)
        self.ai_answer.configure(state="disabled")

    # ------------------------------------------------------- cluster tab

    def _build_hpc(self):
        t = self.tab_hpc
        t.columnconfigure(0, weight=1, uniform="h")
        t.columnconfigure(1, weight=1, uniform="h")

        conn = ttk.LabelFrame(t, text="Connection", padding=10)
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        sched = ttk.LabelFrame(t, text="Scheduler", padding=10)
        sched.grid(row=0, column=1, sticky="nsew")

        self.h = {}

        def field(parent, key, label, row, show=None, width=26):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            e = ttk.Entry(parent, textvariable=var, width=width, show=show)
            e.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
            parent.columnconfigure(1, weight=1)
            self.h[key] = var
            return e

        field(conn, "host", "Host", 0)
        field(conn, "user", "User", 1)
        field(conn, "port", "Port", 2)
        ttk.Label(conn, text="Authentication").grid(row=3, column=0, sticky="w", pady=3)
        self.h["auth"] = tk.StringVar(value="key")
        auth = ttk.Combobox(conn, textvariable=self.h["auth"], state="readonly",
                            width=24, values=["key", "password"])
        auth.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=3)
        auth.bind("<<ComboboxSelected>>", lambda e: self.auth_toggle())

        self.key_row = field(conn, "key", "Private key", 4)
        self.key_label = conn.grid_slaves(row=4, column=0)[0]

        self.pw_label = ttk.Label(conn, text="Password")
        self.pw_label.grid(row=5, column=0, sticky="w", pady=3)
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(conn, textvariable=self.pw_var, show="•",
                                  width=26)
        self.pw_entry.grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=3)
        pwbtns = ttk.Frame(conn)
        pwbtns.grid(row=6, column=1, sticky="w", padx=(8, 0))
        ttk.Button(pwbtns, text="Hold", command=self.hold_password).pack(side="left")
        ttk.Button(pwbtns, text="Forget", command=self.forget_password
                   ).pack(side="left", padx=4)
        self.pw_state = ttk.Label(pwbtns, text="not set", style="Tiny.TLabel")
        self.pw_state.pack(side="left", padx=6)
        ttk.Label(conn, text="Held in memory only, never written to disk.",
                  style="Tiny.TLabel").grid(row=7, column=0, columnspan=2,
                                            sticky="w")

        field(conn, "remote_dir", "Working directory", 8)
        field(conn, "data_dir", "Reads directory", 9)

        ttk.Label(sched, text="Scheduler").grid(row=0, column=0, sticky="w", pady=3)
        self.h["scheduler"] = tk.StringVar(value="slurm")
        ttk.Combobox(sched, textvariable=self.h["scheduler"], state="readonly",
                     width=24, values=["slurm", "none"]
                     ).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Label(sched, text="Containers").grid(row=1, column=0, sticky="w", pady=3)
        self.h["container"] = tk.StringVar(value="singularity")
        ttk.Combobox(sched, textvariable=self.h["container"], state="readonly",
                     width=24, values=["singularity", "docker"]
                     ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)
        sched.columnconfigure(1, weight=1)
        field(sched, "queue", "Queue", 2)
        field(sched, "account", "Account", 3)
        field(sched, "modules", "Modules (comma separated)", 4)
        field(sched, "nextflow", "Nextflow command", 5)

        btns = ttk.Frame(sched)
        btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Save", command=self.save_hpc).pack(side="left")
        ttk.Button(btns, text="Test connection", command=self.test_hpc
                   ).pack(side="left", padx=6)
        self.hpc_note = ttk.Label(t, text="", wraplength=900, justify="left")
        self.hpc_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def auth_toggle(self):
        password = self.h["auth"].get() == "password"
        for w in (self.pw_label, self.pw_entry):
            w.grid() if password else w.grid_remove()
        for w in (self.key_label, self.key_row):
            w.grid_remove() if password else w.grid()

    # -------------------------------------------------- this machine tab

    def open_folder(self, path):
        """Show a folder in the system file manager."""
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(path))             # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:                                   # noqa: BLE001
            # No file manager, or a sandbox that forbids it. The path is
            # still the answer the person wanted.
            messagebox.showinfo("Folder", f"The folder is:\n\n{path}")

    def _build_machine(self):
        t = self.tab_machine
        self.machine_head = ttk.Label(t, text="", style="Quiet.TLabel")
        self.machine_head.pack(anchor="w")

        paths = ttk.LabelFrame(t, text="Folders this application uses",
                               padding=10)
        paths.pack(fill="x", pady=(8, 0))
        for i, (label, path, hint) in enumerate((
                ("Reads", app.DATA_DIR, "put your FASTQ files here"),
                ("Results", app.RUNS_DIR, "one folder per run"),
                ("References", app.REF_DIR, "uploaded panels and genomes"),
                ("Nextflow work", app.WORK_DIR,
                 "intermediates; safe to delete between projects"))):
            ttk.Button(paths, text="Open", width=6,
                       command=(lambda p=path: self.open_folder(p))
                       ).grid(row=i, column=0, pady=1)
            ttk.Label(paths, text=label).grid(row=i, column=1, sticky="w",
                                              padx=(8, 8))
            ttk.Label(paths, text=str(path), style="Tiny.TLabel"
                      ).grid(row=i, column=2, sticky="w")
            ttk.Label(paths, text=hint, style="Tiny.TLabel"
                      ).grid(row=i, column=3, sticky="w", padx=(12, 0))
        box = ttk.LabelFrame(t, text="What this machine can do", padding=10)
        box.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("ok", "what", "detail")
        self.cap_tree = ttk.Treeview(box, columns=cols, show="headings")
        for c, w, txt in (("ok", 60, ""), ("what", 260, "Capability"),
                          ("detail", 620, "Detail")):
            self.cap_tree.heading(c, text=txt)
            self.cap_tree.column(c, width=w, anchor="w")
        self.cap_tree.pack(fill="both", expand=True)
        ttk.Button(t, text="Re-check", command=self.refresh_machine
                   ).pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------- threading plumbing

    def _bg(self, fn, then, what=""):
        """Run fn() off the main thread; call then(ok, value) back on it."""
        self.busy += 1
        if what:
            self.set_status(what)

        def work():
            try:
                self.results.put((then, True, fn()))
            except Exception as exc:                        # noqa: BLE001
                self.results.put((then, False, exc))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        if self.closing:
            return
        while True:
            try:
                then, ok, value = self.results.get_nowait()
            except queue.Empty:
                break
            self.busy = max(0, self.busy - 1)
            if self.busy == 0:
                self.set_status("")
            try:
                then(ok, value)
            except Exception as exc:                        # noqa: BLE001
                messagebox.showerror("Interface error", human_error(exc))
        self._jobs.append(self.after(DRAIN_MS, self._drain))

    # -------------------------------------------------------- data loads

    def rescan_samples(self):
        remote = self.location.get() == "Cluster"
        self.sample_tree.delete(*self.sample_tree.get_children())
        self.sample_tree.insert("", "end", values=("Scanning…", "", "", ""))
        fn = app.discover_remote_samples if remote else app.discover_samples
        self._bg(fn, self._samples_loaded, "Scanning for samples…")

    def _samples_loaded(self, ok, value):
        self.sample_tree.delete(*self.sample_tree.get_children())
        if not ok:
            self.samples = []
            self.sample_tree.insert("", "end",
                                    values=(human_error(value), "", "", ""))
            return
        self.samples = value
        if not self.samples:
            where = ("the cluster's reads directory"
                     if self.location.get() == "Cluster"
                     else "the reads folder below")
            self.sample_tree.insert(
                "", "end",
                values=(f"No FASTQ files in {where}", "", "", ""))
            return
        for s in self.samples:
            self.sample_tree.insert(
                "", "end", iid=s["id"],
                values=(s["name"], s["layout"], s["size_mb"],
                        "" if s["folder"] in (".", "") else s["folder"]))

    def refresh_references(self):
        self._bg(app.list_references, self._refs_loaded)

    def _refs_loaded(self, ok, value):
        if not ok:
            messagebox.showerror("References", human_error(value))
            return
        self.refs = value
        for kind, (var, box) in self.ref_vars.items():
            names = [f["name"] for f in self.refs.get(kind, [])]
            optional = kind != "reference"
            box["values"] = (["(none)"] if optional else []) + names
            if var.get() not in box["values"]:
                var.set(names[0] if names else ("(none)" if optional else ""))
        # Row ids are generated rather than built from the kind and the file
        # name. Tcl stores ids as strings and truncates at a NUL, so a
        # composite id silently collapsed every file of one kind into a
        # single row; anything derived from a name also has to survive
        # whatever characters that name contains.
        self.ref_tree.delete(*self.ref_tree.get_children())
        self.ref_rows = {}
        for kind, label in app.REF_KINDS.items():
            for f in self.refs.get(kind, []):
                iid = f"row{len(self.ref_rows)}"
                self.ref_rows[iid] = (kind, f["name"])
                self.ref_tree.insert("", "end", iid=iid,
                                     values=(label, f["name"], f["size_kb"]))

    def update_resources(self):
        where = "hpc" if self.location.get() == "Cluster" else "local"
        try:
            budget = int(self.memory.get())
        except (tk.TclError, ValueError):
            return
        m = resources.summary(where)
        line = f"{m['host_memory_gb'] or '?'} GB RAM, {m['cpus']} cores"
        if m["docker_memory_gb"]:
            line += f" · Docker has {m['docker_memory_gb']} GB"
            if m["docker_is_limit"]:
                line += " (Docker is the limit)"
        self.machine_line.configure(text=line)

        v = resources.assess(self.pipeline.get(), budget)
        style = {"ok": "Good.TLabel", "tight": "Warn.TLabel",
                 "blocked": "Bad.TLabel"}[v["level"]]
        self.verdict.configure(text=v["message"], style=style)
        self.start_btn.configure(
            state="disabled" if v["level"] == "blocked" else "normal")

    def refresh_runs(self):
        with app.RUNS_LOCK:
            runs = sorted(app.RUNS.values(),
                          key=lambda r: r.get("created_at", ""), reverse=True)[:60]
        keep = set()
        for r in runs:
            when = local_time(r.get("started_at") or r.get("created_at"))
            values = (when, r["pipeline_name"], r["sample"]["name"],
                      r.get("where", ""), r["status"])
            keep.add(r["id"])
            if self.run_tree.exists(r["id"]):
                self.run_tree.item(r["id"], values=values)
            else:
                self.run_tree.insert("", "end", iid=r["id"], values=values)
        for iid in self.run_tree.get_children():
            if iid not in keep:
                self.run_tree.delete(iid)

    def refresh_machine(self):
        self._bg(app.api_capabilities, self._machine_loaded, "Checking…")

    def _machine_loaded(self, ok, value):
        if not ok:
            messagebox.showerror("This machine", human_error(value))
            return
        self.machine_head.configure(
            text=f"{value['platform']} · Python {value['python']}")
        self.cap_tree.delete(*self.cap_tree.get_children())
        for c in value["checks"]:
            self.cap_tree.insert("", "end",
                                 values=("yes" if c["ok"] else "no",
                                         c["name"], c["detail"]))

    # -------------------------------------------------------- run actions

    def selected_sample(self):
        sel = self.sample_tree.selection()
        if not sel:
            return None
        return next((s for s in self.samples if s["id"] == sel[0]), None)

    def chosen_ref(self, kind):
        var, _ = self.ref_vars[kind]
        name = var.get()
        if not name or name == "(none)":
            return None
        for f in self.refs.get(kind, []):
            if f["name"] == name:
                return f["path"]
        return None

    def start_run(self):
        sample = self.selected_sample()
        if not sample:
            messagebox.showinfo("Start run", "Select a sample first.")
            return
        req = app.StartRequest(
            pipeline_id=self.pipeline.get(),
            sample_id=sample["id"],
            threads=int(self.threads.get()),
            memory_gb=float(self.memory.get()),
            location="hpc" if self.location.get() == "Cluster" else "local",
            reference=self.chosen_ref("reference"),
            primers=self.chosen_ref("primers"),
            adapters=self.chosen_ref("adapters"),
            panel=self.chosen_ref("panel"),
        )
        self.start_btn.configure(state="disabled")
        self._bg(lambda: app.api_start(req), self._run_started, "Starting…")

    def _run_started(self, ok, value):
        # Re-enable through the verdict, not unconditionally: the budget may
        # still be one the chosen pipeline cannot start on, and a failed
        # start should not hand back a button that is about to fail again.
        self.update_resources()
        if not ok:
            messagebox.showerror("Could not start", human_error(value))
            return
        self.refresh_runs()
        if self.run_tree.exists(value["id"]):
            self.run_tree.selection_set(value["id"])

    def cancel_run(self):
        sel = self.run_tree.selection()
        if not sel:
            return
        self._bg(lambda: app.api_cancel(sel[0]), lambda ok, v: self.refresh_runs())

    def on_run_selected(self, _evt=None):
        sel = self.run_tree.selection()
        if not sel:
            return
        self.open_run = sel[0]
        self.log_offset = 0
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._poll_log()
        self._update_save_button()
        if hasattr(self, 'ai_run_label'):
            self._refresh_ai_run_label()

    def _update_save_button(self):
        with app.RUNS_LOCK:
            run = app.RUNS.get(self.open_run or "")
        has = bool(run and run.get("consensus"))
        self.save_btn.configure(state="normal" if has else "disabled")

    def save_consensus(self):
        with app.RUNS_LOCK:
            run = app.RUNS.get(self.open_run or "")
        if not run or not run.get("consensus"):
            return
        src = Path(run["consensus"])
        dest = filedialog.asksaveasfilename(
            title="Save consensus",
            initialfile=f"{run['sample']['name']}_{run['pipeline_id']}.fasta",
            defaultextension=".fasta",
            filetypes=[("FASTA", "*.fasta *.fa"), ("All files", "*.*")])
        if not dest:
            return
        try:
            shutil.copyfile(src, dest)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.set_status(f"Saved {Path(dest).name}")

    def _poll_log(self):
        if not self.open_run:
            return
        path = app.run_path(self.open_run) / "run.log"
        if not path.exists():
            return
        size = path.stat().st_size
        if size < self.log_offset:
            self.log_offset = 0
        if size == self.log_offset:
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.log_offset)
            text = fh.read()
        self.log_offset = size
        at_bottom = self.log.yview()[1] > 0.99
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    # --------------------------------------------------- reference actions

    def add_reference(self):
        label = self.up_kind.get()
        kind = next(k for k, v in app.REF_KINDS.items() if v == label)
        path = filedialog.askopenfilename(
            title=f"Choose a {label.lower()} FASTA file",
            filetypes=[("FASTA", "*.fasta *.fa *.fas *.fna"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            saved = app.store_reference(kind, Path(path).name, data)
        except (HTTPException, OSError) as exc:
            messagebox.showerror("Could not add that file", human_error(exc))
            return
        self.refresh_references()
        self.set_status(f"Added {saved['name']} ({saved['sequences']} sequences)")

    def delete_reference(self):
        sel = self.ref_tree.selection()
        if not sel:
            return
        kind, name = self.ref_rows.get(sel[0], (None, None))
        if not kind:
            return
        if not messagebox.askyesno("Delete", f"Delete {name}?"):
            return
        try:
            app.api_delete_reference(kind, name)
        except HTTPException as exc:
            messagebox.showerror("Delete failed", human_error(exc))
            return
        self.refresh_references()

    # ----------------------------------------------------- cluster actions

    def load_hpc_fields(self):
        cfg = app.HPC_CONFIG
        for key in ("host", "user", "key", "remote_dir", "data_dir",
                    "queue", "account"):
            self.h[key].set(str(cfg.get(key) or ""))
        self.h["port"].set(str(cfg.get("port") or 22))
        self.h["nextflow"].set(str(cfg.get("nextflow") or "nextflow"))
        self.h["auth"].set(cfg.get("auth") or "key")
        self.h["scheduler"].set(cfg.get("scheduler") or "slurm")
        self.h["container"].set(cfg.get("container") or "singularity")
        self.h["modules"].set(", ".join(cfg.get("modules") or []))
        self.auth_toggle()
        c = app.cluster()
        held = hpc.has_password(c.destination) if c.host else False
        self.pw_state.configure(text="held in memory" if held else "not set")

    def hpc_body(self):
        try:
            port = int(self.h["port"].get() or 22)
        except ValueError:
            port = 22
        return app.HPCConfig(
            host=self.h["host"].get().strip(), user=self.h["user"].get().strip(),
            port=port, key=self.h["key"].get().strip(),
            auth=self.h["auth"].get(),
            remote_dir=self.h["remote_dir"].get().strip(),
            data_dir=self.h["data_dir"].get().strip(),
            scheduler=self.h["scheduler"].get(),
            queue=self.h["queue"].get().strip(),
            account=self.h["account"].get().strip(),
            container=self.h["container"].get(),
            modules=[m.strip() for m in self.h["modules"].get().split(",")
                     if m.strip()],
            nextflow=self.h["nextflow"].get().strip() or "nextflow",
            profile="")

    def _push_typed_password(self):
        """Hold whatever is in the password box before saving or testing.

        Without this, typing a password and pressing Test ignored it, and
        the probe answered "enter the cluster password" — which is maddening
        when you just did. Pressing Hold first was never discoverable.
        """
        typed = self.pw_var.get()
        if not typed or self.h["auth"].get() != "password":
            return
        c = app.cluster()
        if not c.host:
            return
        hpc.set_password(c.destination, typed)
        self.pw_var.set("")
        self.pw_state.configure(text="held in memory")

    def save_hpc(self):
        try:
            app.api_hpc_put(self.hpc_body())
            self._push_typed_password()
        except HTTPException as exc:
            self.hpc_note.configure(text=human_error(exc), style="Bad.TLabel")
            return
        self.hpc_note.configure(text="Saved.", style="Good.TLabel")

    def test_hpc(self):
        try:
            app.api_hpc_put(self.hpc_body())
            self._push_typed_password()
        except HTTPException as exc:
            self.hpc_note.configure(text=human_error(exc), style="Bad.TLabel")
            return
        self.hpc_note.configure(text="Connecting…", style="Quiet.TLabel")
        self._bg(app.api_hpc_test, self._hpc_tested, "Testing the cluster…")

    def _hpc_tested(self, ok, value):
        if not ok:
            self.hpc_note.configure(text=human_error(value), style="Bad.TLabel")
            return
        # The probe returns {ok, host, checks:[{name, ok, detail}]}. Joining
        # the dict printed the checks list as its repr and lost the detail,
        # which is the only part that says why a test failed.
        checks = value.get("checks") or []
        lines = [("Connected to " if value.get("ok") else "Could not connect to ")
                 + str(value.get("host", ""))]
        for c in checks:
            mark = "✓" if c.get("ok") else "✗"
            detail = c.get("detail") or ""
            lines.append(f"  {mark} {c.get('name', '')}"
                         + (f" — {detail}" if detail else ""))
        self.hpc_note.configure(
            text="\n".join(lines),
            style="Good.TLabel" if value.get("ok") else "Bad.TLabel")

    def hold_password(self):
        c = app.cluster()
        if not c.host:
            self.hpc_note.configure(text="Set the host first.", style="Bad.TLabel")
            return
        hpc.set_password(c.destination, self.pw_var.get())
        self.pw_var.set("")
        self.pw_state.configure(text="held in memory")

    def forget_password(self):
        c = app.cluster()
        if c.host:
            hpc.clear_password(c.destination)
        self.pw_state.configure(text="not set")

    # ------------------------------------------------------------- timer

    def _tick(self):
        if self.closing:
            return
        self.refresh_runs()
        self._poll_log()
        self._update_save_button()
        self._jobs.append(self.after(POLL_MS, self._tick))

    def on_close(self):
        """Closing the window does not stop a run, so say so before it does.

        Nextflow is started in its own session and keeps going once this
        process exits; the run then shows as interrupted on the next launch
        even though the pipeline may still be writing output. Better to let
        the person decide than to discover that afterwards.
        """
        with app.RUNS_LOCK:
            active = [r for r in app.RUNS.values()
                      if r.get("status") in ("running", "queued", "cancelling")]
        if active:
            names = ", ".join(sorted({r["sample"]["name"] for r in active})[:3])
            more = "" if len(active) <= 3 else f" and {len(active) - 3} more"
            if not messagebox.askyesno(
                    "Close while running?",
                    f"{len(active)} run(s) are still going ({names}{more}).\n\n"
                    "Closing this window does not stop them — they keep "
                    "running in the background, and their results still "
                    "arrive in the results folder. Reopen the application to "
                    "watch them again.\n\nClose anyway?"):
                return

        # Cancel the repeating timers before tearing the interpreter down.
        # A callback that fires after destroy() has no interpreter to run
        # in, and Tk prints an "invalid command name" traceback that reads
        # like a crash on an otherwise clean exit.
        self.closing = True
        for job in self._jobs:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        self._jobs.clear()
        self.destroy()


def main():
    try:
        ui = Runner()
    except tk.TclError as exc:
        raise SystemExit(
            "No display is available, so the window cannot open.\n"
            f"  {exc}\n"
            "On a headless server or plain WSL without WSLg, run the web\n"
            "interface instead:  python app.py")
    ui.mainloop()


if __name__ == "__main__":
    main()
