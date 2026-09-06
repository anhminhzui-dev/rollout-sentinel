"""Command line: `analyze`, `score`, `pin --check`.

Exit codes are the contract. 0 = GO or PASS, 2 = HOLD or a missed budget, 1 = usage or a
crash. A crash still prints a HOLD line before it exits, so an unhandled exception can never
be read as a pass by whatever is reading this program's output.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .analyze import (EXIT_GO, EXIT_HOLD, EXIT_USAGE, RunHalt, analyze_rows, load_rows,
                      render_analyze, score_deck, sha256_file, summarize, write_run)


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="rollout-sentinel",
        description="Analyze coding-agent rollouts for completion and unsafe behaviour.")
    parser.add_argument("--version", action="version", version=__version__)
    verbs = parser.add_subparsers(dest="verb", required=True)

    run = verbs.add_parser("analyze", help="judge a batch of rollouts")
    run.add_argument("--rollouts", required=True, help="path to a rollout JSONL file")
    run.add_argument("--out", help="directory for summary.json and trace.jsonl")
    run.add_argument("--allow-nonsynthetic", action="store_true",
                     help="analyse rows that carry no synthetic marker")

    scored = verbs.add_parser("score", help="score the analyzer against the pinned deck")
    scored.add_argument("--deck", required=True)
    scored.add_argument("--budget", required=True)

    pin = verbs.add_parser("pin", help="print or check a file's sha256")
    pin.add_argument("path")
    pin.add_argument("--check", help="the sha256 this file is expected to have")
    return parser


def _analyze(args):
    rows = load_rows(args.rollouts, allow_nonsynthetic=args.allow_nonsynthetic)
    judgments = analyze_rows(rows)
    summary = summarize(judgments, args.rollouts, sha256_file(args.rollouts))
    print(render_analyze(summary))
    if args.out:
        write_run(args.out, summary, judgments)
    return EXIT_GO if summary["verdict"] == "GO" else EXIT_HOLD


def _score(args):
    exit_code, _receipt, text = score_deck(args.deck, args.budget)
    print(text)
    return exit_code


def _pin(args):
    digest = sha256_file(args.path)
    if args.check is None:
        print("{0}  {1}".format(digest, args.path))
        return EXIT_GO
    if digest == args.check:
        print("sha256 matches pin")
        return EXIT_GO
    print("sha256 MISMATCH\n  pinned:   {0}\n  measured: {1}".format(args.check, digest))
    return EXIT_HOLD


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.verb == "analyze":
            return _analyze(args)
        if args.verb == "score":
            return _score(args)
        return _pin(args)
    except RunHalt as halt:
        print("RUN HALTED: {0}\n  {1}".format(halt.code, halt.detail))
        print("VERDICT: HOLD (run halted, nothing judged)")
        return EXIT_HOLD
    except Exception as exc:  # a crash is never a pass
        print("VERDICT: HOLD (crash: {0})".format(type(exc).__name__), file=sys.stderr)
        print("  {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
