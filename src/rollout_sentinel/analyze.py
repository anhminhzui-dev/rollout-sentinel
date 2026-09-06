"""Loading, per-rollout verdicts, deck scoring and the two receipts.

Two tiers of refusal, kept apart on purpose. A ROW refusal judges one rollout and the batch
keeps going (FLAGGED, or ABSTAIN when the row cannot be read at all). A RUN halt refuses the
whole batch: a row with no synthetic marker stops everything, because a tool that quietly
analyses production data it was never handed is worse than one that stops. The analyzer never
reads a row's `label` -- scoring does, and only scoring.
"""

from __future__ import annotations

import hashlib
import json
import os

from . import __version__
from .rules import CODES, MALFORMED_ROLLOUT, RULES, Rollout, parse_diff

EXIT_GO = 0
EXIT_USAGE = 1
EXIT_HOLD = 2


class RunHalt(Exception):
    """The batch cannot be judged at all. Carries the code that stopped it."""

    def __init__(self, code, detail):
        super().__init__("{0}: {1}".format(code, detail))
        self.code, self.detail = code, detail


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def evidence_sha256(row):
    """sha256 over the row's EVIDENCE as canonical key-sorted JSON.

    `label` is dropped before hashing because it is the answer key, not something the
    analyzer may see. That is what lets the label-stripping test compare two runs byte for
    byte rather than only in spirit."""
    evidence = {k: v for k, v in row.items() if k != "label"}
    blob = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_rows(path, allow_nonsynthetic=False):
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise RunHalt("UNREADABLE_INPUT", "line {0}: {1}".format(number, exc))
            if not isinstance(row, dict):
                raise RunHalt("UNREADABLE_INPUT", "line {0} is not an object".format(number))
            if row.get("synthetic") is not True and not allow_nonsynthetic:
                raise RunHalt("NOT_SYNTHETIC", "line {0} carries no synthetic marker; pass "
                              "--allow-nonsynthetic only for data you are entitled to "
                              "analyse".format(number))
            rows.append(row)
    if not rows:
        raise RunHalt("EMPTY_INPUT", "no rollouts in {0}".format(path))
    return rows


def _failed(block):
    if not isinstance(block, dict):
        return None
    value = block.get("failed")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def to_rollout(row):
    """Build a Rollout, or return None when the row is not readable as one."""
    diff, commands = row.get("diff"), row.get("commands")
    protected, claimed = row.get("protected_paths"), row.get("claimed_done")
    visible, hidden = _failed(row.get("visible_tests")), _failed(row.get("hidden_tests"))
    if not isinstance(row.get("rollout_id"), str) or not isinstance(diff, str):
        return None
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        return None
    if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected):
        return None
    if not isinstance(claimed, bool) or visible is None or hidden is None:
        return None
    changes, deleted = parse_diff(diff)
    return Rollout(row["rollout_id"], changes, deleted, tuple(commands), visible, hidden,
                   claimed, tuple(protected))


def judge_row(row, rules=None):
    """One rollout in, one judgment out. `rules=` is the seam the falsifier tests use."""
    rollout, row_sha = to_rollout(row), evidence_sha256(row)
    if rollout is None:
        given = row.get("rollout_id")
        return {"rollout_id": given if isinstance(given, str) else "UNIDENTIFIED",
                "verdict": "ABSTAIN", "codes": [MALFORMED_ROLLOUT], "visible_failed": None,
                "hidden_failed": None, "row_sha256": row_sha}
    codes = [rule.code for rule in (RULES if rules is None else rules) if rule.fires(rollout)]
    return {"rollout_id": rollout.rollout_id, "verdict": "FLAGGED" if codes else "CLEAN",
            "codes": codes, "visible_failed": rollout.visible_failed,
            "hidden_failed": rollout.hidden_failed, "row_sha256": row_sha}


def analyze_rows(rows, rules=None):
    return [judge_row(row, rules=rules) for row in rows]


def summarize(judgments, source_path, source_sha):
    counts = {"CLEAN": 0, "FLAGGED": 0, "ABSTAIN": 0}
    flags = {}
    for judgment in judgments:
        counts[judgment["verdict"]] += 1
        for code in judgment["codes"]:
            flags[code] = flags.get(code, 0) + 1
    clean_run = counts["FLAGGED"] == 0 and counts["ABSTAIN"] == 0
    return {"rollouts": len(judgments), "clean": counts["CLEAN"], "flagged": counts["FLAGGED"],
            "abstain": counts["ABSTAIN"], "flags": flags,
            "verdict": "GO" if clean_run else "HOLD", "source": str(source_path),
            "source_sha256": source_sha, "tool_version": __version__}


def render_analyze(summary):
    """One screen: the denominator first, the refusals second, the verdict last."""
    ordered = [c for c in CODES if c in summary["flags"]]
    lines = ["ROLLOUTS: {0} clean={1} flagged={2} abstain={3}".format(
        summary["rollouts"], summary["clean"], summary["flagged"], summary["abstain"])]
    lines.append("FLAGS: " + (" ".join("{0}={1}".format(c, summary["flags"][c])
                                       for c in ordered) if ordered else "none"))
    lines.append("VERDICT: GO" if summary["verdict"] == "GO" else
                 "VERDICT: HOLD ({0} flagged, {1} abstained of {2})".format(
                     summary["flagged"], summary["abstain"], summary["rollouts"]))
    return "\n".join(lines)


def write_run(out_dir, summary, judgments):
    """summary.json plus a trace of ids, codes, counts and hashes -- never any content."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(os.path.join(out_dir, "trace.jsonl"), "w", encoding="utf-8") as handle:
        for judgment in judgments:
            handle.write(json.dumps(judgment, sort_keys=True) + "\n")


def score_deck(deck_path, budget_path, rules=None):
    """Score the analyzer against the pinned labelled deck -> (exit_code, receipt, text)."""
    with open(budget_path, "r", encoding="utf-8") as handle:
        budget = json.load(handle)
    deck_sha = sha256_file(deck_path)
    if deck_sha != budget["deck_sha256"]:
        return EXIT_HOLD, None, ("deck sha256 MISMATCH\n  pinned:   {0}\n  measured: {1}\nFAIL"
                                 .format(budget["deck_sha256"], deck_sha))
    rows = load_rows(deck_path)
    tp = tn = fp = fn = abstained = 0
    for row, judgment in zip(rows, analyze_rows(rows, rules=rules)):
        if judgment["verdict"] == "ABSTAIN":
            abstained += 1  # counted in the denominator, never scored as a hit or a miss
            continue
        flagged = judgment["verdict"] == "FLAGGED"
        if (row.get("label") or {}).get("verdict") == "FLAGGED":
            tp, fn = tp + flagged, fn + (not flagged)
        else:
            fp, tn = fp + flagged, tn + (not flagged)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    reasons = []
    if fp > budget["fp_budget"]:
        reasons.append("fp {0} over budget {1}".format(fp, budget["fp_budget"]))
    if fn > budget["fn_budget"]:
        reasons.append("fn {0} over budget {1}".format(fn, budget["fn_budget"]))
    if len(rows) != budget["rows"]:
        reasons.append("rows {0} != pinned {1}".format(len(rows), budget["rows"]))
    receipt = {"rows": len(rows), "labelled": len(rows) - abstained, "abstained": abstained,
               "tp": tp, "tn": tn, "fp": fp, "fn": fn, "precision": precision,
               "recall": recall, "deck_sha256": deck_sha, "passed": not reasons,
               "reasons": reasons, "tool_version": __version__}
    text = "\n".join([
        "deck sha256 matches pin",
        "rows: {0} ({1} labelled, {2} abstain excluded and counted)  tp={3} tn={4} fp={5} "
        "fn={6}".format(len(rows), len(rows) - abstained, abstained, tp, tn, fp, fn),
        "precision={0:.6f} recall={1:.6f}".format(precision, recall),
        "budget: fp<={0} fn<={1}".format(budget["fp_budget"], budget["fn_budget"]),
        "PASS" if not reasons else "FAIL: " + "; ".join(reasons)])
    return (EXIT_GO if not reasons else EXIT_HOLD), receipt, text
