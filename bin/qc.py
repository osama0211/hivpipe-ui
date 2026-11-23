"""Structural QC for assembled viral consensus sequences.

Detects translocations, duplications and large missing blocks by checking
whether the consensus is colinear with a reference. This catches a failure
class that genome-fraction and mismatch metrics do not: a consensus can be
full-length, gap-free and still have its genome in the wrong order.

No truth set required. Viral genome organisation is fixed, so an out-of-order
consensus is detectably wrong without knowing the sample's true sequence.
"""

from __future__ import annotations

IUPAC = {
    "A": {"A"}, "C": {"C"}, "G": {"G"}, "T": {"T"},
    "R": {"A", "G"}, "Y": {"C", "T"}, "S": {"G", "C"}, "W": {"A", "T"},
    "K": {"G", "T"}, "M": {"A", "C"}, "B": {"C", "G", "T"},
    "D": {"A", "G", "T"}, "H": {"A", "C", "T"}, "V": {"A", "C", "G"},
}


def read_fasta(path):
    """Minimal FASTA reader. Returns {name: sequence}."""
    seqs, name, buf = {}, None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if name is not None:
                    seqs[name] = "".join(buf)
                name, buf = line[1:].strip(), []
            else:
                buf.append(line)
    if name is not None:
        seqs[name] = "".join(buf)
    return seqs


def first_sequence(path):
    seqs = read_fasta(path)
    if not seqs:
        return "", ""
    name = next(iter(seqs))
    return name, seqs[name].upper()


def _ungapped(seq):
    return seq.replace("-", "").replace("?", "")


def composition(seq):
    """Base composition summary for a consensus."""
    s = seq.upper()
    total = len(s)
    acgt = sum(s.count(b) for b in "ACGT")
    return {
        "length": total,
        "called": acgt,
        "n_count": s.count("N"),
        "gap_count": s.count("-") + s.count("?"),
        "ambiguous": total - acgt - s.count("N") - s.count("-") - s.count("?"),
        "called_fraction": round(acgt / total, 4) if total else 0.0,
    }


def _locate(chunk, qry):
    """Find a window in the query, tolerating mismatches.

    Exact lookup first; failing that, try each half. A window carrying a
    single SNP still places correctly, which keeps ordinary sequence
    divergence from being reported as missing sequence.
    """
    hits, start = [], 0
    while True:
        j = qry.find(chunk, start)
        if j < 0:
            break
        hits.append(j)
        start = j + 1
    if hits:
        return hits, True

    half = len(chunk) // 2
    left = qry.find(chunk[:half])
    if left >= 0:
        return [left], False
    right = qry.find(chunk[half:])
    if right >= 0:
        return [max(right - half, 0)], False
    return [], False


def structural_check(reference, query, window=60, step=20,
                     min_windows=5, missing_min_bp=300, max_findings=12):
    """Compare query against reference for order and completeness.

    Walks windows along the reference and locates each in the query. A block
    of windows sitting at an offset far from the dominant one is a
    translocation; windows found more than once are duplicated; runs of
    windows absent entirely are missing blocks.

    Returns a dict with `verdict` in {pass, warn, fail} plus findings.
    """
    ref = _ungapped(reference.upper())
    qry = _ungapped(query.upper())

    findings = []
    if len(ref) < window * 2 or len(qry) < window * 2:
        return {
            "verdict": "warn",
            "summary": "Sequence too short for a structural check.",
            "findings": [],
            "windows_placed": 0,
            "windows_total": 0,
        }

    placed, offsets, absent = [], {}, []
    total = 0
    for i in range(0, len(ref) - window, step):
        chunk = ref[i:i + window]
        if chunk.count("N") > window // 10:
            continue
        total += 1
        hits, exact = _locate(chunk, qry)
        if not hits:
            absent.append(i)
            continue
        placed.append((i, hits if exact else []))
        for j in hits:
            offsets.setdefault(round((j - i) / 1000), []).append(i)

    if not placed:
        return {
            "verdict": "fail",
            "summary": "No part of the consensus matches the reference.",
            "findings": [{
                "kind": "unmatched",
                "detail": "The consensus shares no exact window with the reference. "
                          "Check that the right reference was selected.",
            }],
            "windows_placed": 0,
            "windows_total": total,
        }

    # The offset regime holding most windows is the correct placement.
    main = max(offsets, key=lambda k: len(offsets[k]))
    genome_kb = len(ref) / 1000.0

    for regime, positions in sorted(offsets.items()):
        if regime == main or len(positions) < min_windows:
            continue
        # Terminal repeats (HIV LTRs) shift by nearly the whole genome.
        if abs(regime - main) > genome_kb * 0.85:
            continue
        lo, hi = min(positions), max(positions) + window
        findings.append({
            "kind": "translocation",
            "start": lo,
            "end": hi,
            "length": hi - lo,
            "offset_kb": regime - main,
            "detail": f"{hi - lo:,} bp from reference position {lo:,}-{hi:,} "
                      f"sits about {regime - main:+d} kb away from where it belongs.",
        })

    # A window legitimately appears twice when the genome has terminal
    # repeats — HIV's 5' and 3' LTRs are identical. Those copies sit about
    # one genome apart, so only closer repeats indicate a real duplication.
    ltr_span = len(ref) * 0.85
    duplicated = []
    for i, hits in placed:
        if len(hits) < 2:
            continue
        close = [(a, b) for idx, a in enumerate(hits) for b in hits[idx + 1:]
                 if abs(b - a) < ltr_span]
        if close:
            duplicated.append(i)

    if len(duplicated) >= min_windows:
        lo, hi = min(duplicated), max(duplicated) + window
        findings.append({
            "kind": "duplication",
            "start": lo,
            "end": hi,
            "length": hi - lo,
            "detail": f"{len(duplicated)} windows between reference position "
                      f"{lo:,} and {hi:,} appear more than once in the consensus, "
                      f"too close together to be terminal repeats.",
        })

    runs = []
    for i in absent:
        if runs and i - runs[-1][1] <= step * 3:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    missing = []
    for lo, hi in runs:
        span = hi + window - lo
        if span >= missing_min_bp:
            missing.append({
                "kind": "missing",
                "start": lo,
                "end": hi + window,
                "length": span,
                "detail": f"{span:,} bp spanning reference position "
                          f"{lo:,}-{hi + window:,} is absent from the consensus.",
            })
    missing.sort(key=lambda f: f["length"], reverse=True)
    shown = missing[:max(0, max_findings - len(findings))]
    findings.extend(shown)
    hidden = len(missing) - len(shown)
    if hidden > 0:
        findings.append({
            "kind": "missing",
            "length": 0,
            "detail": f"{hidden} further gap(s) below the largest ones listed here.",
        })

    severe = {"translocation", "duplication"}
    if any(f["kind"] in severe for f in findings):
        verdict = "fail"
    elif any(f["kind"] == "missing" and f["length"] > 500 for f in findings):
        verdict = "warn"
    elif findings:
        verdict = "warn"
    else:
        verdict = "pass"

    if verdict == "pass":
        summary = "Consensus is colinear with the reference. No rearrangement found."
    elif verdict == "warn":
        summary = f"{len(findings)} issue(s) worth checking before you use this consensus."
    else:
        summary = ("Structural rearrangement detected. Do not submit this consensus "
                   "for resistance interpretation without inspecting it.")

    return {
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "windows_placed": len(placed),
        "windows_total": total,
    }


def run_qc(consensus_path, reference_path):
    """Full QC report for one consensus file."""
    cname, cseq = first_sequence(consensus_path)
    if not cseq:
        return {"verdict": "fail", "summary": "Consensus file is empty.",
                "findings": [], "composition": None}

    report = {"sequence_name": cname, "composition": composition(cseq)}

    if reference_path:
        _, rseq = first_sequence(reference_path)
        if rseq:
            report.update(structural_check(rseq, cseq))
            return report

    report.update({
        "verdict": "warn",
        "summary": "No reference available, so only composition was checked.",
        "findings": [],
    })
    return report
