#!/usr/bin/env python3
"""Inject a known rearrangement into a genome, for testing and calibration.

Two uses:

1. Test fixture. Produce a consensus that is wrong in a way you control, then
   confirm the structural check finds it. selftest.sh does this.

2. Calibration. Sweep the size of the injected block and measure where a
   downstream tool stops noticing. Resistance algorithms discard a gene when
   its alignment discordance crosses a threshold — HIVdb uses 40% — so there
   is a block size below which a rearranged consensus is still accepted and
   interpreted. Finding that boundary is the point of the sweep.

Examples
--------
    # one 2 kb block moved from 5000 to 1500
    make_rearrangement.py --input HXB2.fasta --output broken.fasta \\
        --kind translocation --size 2000 --source 5000 --dest 1500

    # a size sweep, one file per size
    for bp in 50 100 200 400 800 1600 3200; do
        make_rearrangement.py --input HXB2.fasta \\
            --output sweep/rearranged_${bp}.fasta \\
            --kind translocation --size $bp --source 2600 --dest 7000
    done
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qc import read_fasta  # noqa: E402


def write_fasta(path, name, seq, width=70):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f">{name}\n")
        for i in range(0, len(seq), width):
            fh.write(seq[i:i + width] + "\n")


def translocate(seq, source, size, dest):
    """Cut a block out and paste it elsewhere."""
    block = seq[source:source + size]
    without = seq[:source] + seq[source + size:]
    dest = min(dest, len(without))
    return without[:dest] + block + without[dest:]


def duplicate(seq, source, size, dest):
    """Copy a block elsewhere, leaving the original in place."""
    block = seq[source:source + size]
    dest = min(dest, len(seq))
    return seq[:dest] + block + seq[dest:]


def delete(seq, source, size, _dest=None):
    return seq[:source] + seq[source + size:]


def replace(seq, source, size, dest):
    """Overwrite a block with sequence copied from elsewhere.

    This is the shape seen in real misassemblies: a region is lost and the
    space is filled by a duplicate of somewhere else, so the genome keeps its
    length and looks complete.
    """
    donor = seq[dest:dest + size]
    if len(donor) < size:
        donor = (donor * (size // max(len(donor), 1) + 1))[:size]
    return seq[:source] + donor + seq[source + size:]


KINDS = {
    "translocation": translocate,
    "duplication": duplicate,
    "deletion": delete,
    "replacement": replace,
}


def main():
    ap = argparse.ArgumentParser(
        description="Inject a known rearrangement into a genome.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--input", required=True, help="source FASTA")
    ap.add_argument("--output", required=True, help="where to write the result")
    ap.add_argument("--kind", choices=sorted(KINDS), default="translocation")
    ap.add_argument("--size", type=int, required=True, help="block size in bp")
    ap.add_argument("--source", type=int, help="block start; random if omitted")
    ap.add_argument("--dest", type=int, help="destination; random if omitted")
    ap.add_argument("--seed", type=int, default=0, help="for reproducible positions")
    ap.add_argument("--name", help="sequence name for the output")
    args = ap.parse_args()

    seqs = read_fasta(args.input)
    if not seqs:
        sys.exit(f"No sequence in {args.input}")
    original_name = next(iter(seqs))
    seq = seqs[original_name].upper().replace("-", "")

    if args.size >= len(seq) // 2:
        sys.exit(f"--size {args.size} is too large for a {len(seq)} bp genome")

    rng = random.Random(args.seed)
    source = args.source if args.source is not None else rng.randint(0, len(seq) - args.size)
    dest = args.dest if args.dest is not None else rng.randint(0, len(seq) - args.size)

    if not 0 <= source <= len(seq) - args.size:
        sys.exit(f"--source {source} is outside the genome")

    mutated = KINDS[args.kind](seq, source, args.size, dest)
    name = args.name or f"{original_name}_{args.kind}_{args.size}bp"
    write_fasta(args.output, name, mutated)

    print(f"{args.kind}: {args.size} bp at {source}"
          f"{f' -> {dest}' if args.kind != 'deletion' else ''}")
    print(f"  input  {len(seq):,} bp   {args.input}")
    print(f"  output {len(mutated):,} bp   {args.output}")


if __name__ == "__main__":
    main()
