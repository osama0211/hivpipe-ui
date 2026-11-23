#!/usr/bin/env python3
"""Command-line front end for the structural check.

Called by the Nextflow STRUCTURAL_QC process, and usable on its own:

    structural_qc.py --consensus cons.fasta --reference HXB2.fasta
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from qc import run_qc
except ImportError:  # inside the container qc.py sits alongside this script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from qc import run_qc


def as_text(report):
    lines = [
        f"verdict : {report['verdict']}",
        f"summary : {report['summary']}",
    ]
    comp = report.get("composition")
    if comp:
        lines.append(
            f"length  : {comp['length']:,} bp "
            f"({comp['called']:,} called, {comp['n_count']:,} N, "
            f"{comp['gap_count']:,} gap)"
        )
    placed = report.get("windows_placed")
    if placed is not None:
        lines.append(f"windows : {placed} of {report.get('windows_total', 0)} placed")
    if report.get("findings"):
        lines.append("findings:")
        for f in report["findings"]:
            lines.append(f"  - [{f['kind']}] {f['detail']}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Structural check on a viral consensus.")
    ap.add_argument("--consensus", required=True, help="assembled consensus FASTA")
    ap.add_argument("--reference", help="reference FASTA; composition only without it")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--text", help="write a readable summary here")
    ap.add_argument("--fail-on", choices=["never", "fail", "warn"], default="never",
                    help="exit non-zero when the verdict is this bad (default: never)")
    args = ap.parse_args()

    report = run_qc(args.consensus, args.reference)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
    text = as_text(report)
    if args.text:
        with open(args.text, "w") as fh:
            fh.write(text)
    sys.stdout.write(text)

    if args.fail_on == "fail" and report["verdict"] == "fail":
        return 1
    if args.fail_on == "warn" and report["verdict"] in ("warn", "fail"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
