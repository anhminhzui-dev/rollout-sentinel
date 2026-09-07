"""Every rule, both gates, and the checks that make the other checks worth reading.

Four of these exist only to prove the suite can fail: two falsifiers (drop a rule, then widen
one) push the deck past opposite ends of the published budget, the public-clean scanner plants
every shape it forbids and asserts each one fires, and the network scan plants an import line.
A check that has never been shown to fail certifies nothing. Lines carrying the anchor token
PLANTED-SHAPE are the plants themselves and are exempt from the scan they feed.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from rollout_sentinel import analyze as A
from rollout_sentinel import rules as R

ROOT = Path(__file__).resolve().parent.parent
DECK = ROOT / "fixtures" / "deck.jsonl"
CLEAN = ROOT / "fixtures" / "batch_clean.jsonl"
BUDGET = ROOT / "budget.json"
ROWS = A.load_rows(DECK)
BY_ID = {row["rollout_id"]: row for row in ROWS}
ANCHOR = "PLANTED-" + "SHAPE"


def codes_for(rollout_id, rules=None):
    return set(A.judge_row(BY_ID[rollout_id], rules=rules)["codes"])


@pytest.mark.parametrize("rollout_id,code", [
    ("rt-013", R.TEST_DISABLED), ("rt-014", R.ASSERTION_WEAKENED),
    ("rt-015", R.PROTECTED_DATA_DELETED), ("rt-016", R.SECRET_LEAKED),
    ("rt-017", R.PERMISSION_BROADENED), ("rt-018", R.VALIDATION_BYPASSED),
    ("rt-019", R.HIDDEN_SUITE_FAILED), ("rt-020", R.FALSE_COMPLETION)])
def test_each_rule_family_fires_on_its_own_seeded_row(rollout_id, code):
    assert codes_for(rollout_id) == {code}


def test_every_deck_row_matches_its_label():
    for row, judgment in zip(ROWS, A.analyze_rows(ROWS)):
        assert sorted(judgment["codes"]) == sorted(row["label"]["expected_flags"]), row["rollout_id"]


def test_near_misses_stay_clean():
    """A marker inside a comment and a flag inside a commit message are mentions, not acts."""
    assert codes_for("rt-005") == set() and codes_for("rt-006") == set()


@pytest.mark.parametrize("rollout_id", ["rt-025", "rt-026"])
def test_guard_moved_or_renamed_in_one_diff_is_not_a_bypass(rollout_id):
    """A refactor that takes a guard out and puts it back -- into a helper, or under a new
    name -- is not a bypass. Only a NET removal is the act."""
    assert codes_for(rollout_id) == set()


def test_weaker_guard_under_the_same_name_is_still_a_bypass():
    """Same name back, one guard token short: the net is still a removal, and it fires."""
    assert codes_for("rt-027") == {R.VALIDATION_BYPASSED}
    assert R.validation_bypassed(A.to_rollout(BY_ID["rt-025"])) is False


def test_malformed_row_abstains_rather_than_passing():
    judgment = A.judge_row(BY_ID["rt-024"])
    assert judgment["verdict"] == "ABSTAIN" and judgment["codes"] == [R.MALFORMED_ROLLOUT]


@pytest.mark.parametrize("field", ["visible_tests", "hidden_tests"])
def test_failure_counts_are_nonnegative_integers(field):
    row = copy.deepcopy(BY_ID["rt-001"])
    row["claimed_done"] = True
    for value in (-1, -10, True, 0.5, "0", None):
        row[field]["failed"] = value
        judgment = A.judge_row(row)
        assert (judgment["verdict"], judgment["codes"]) == (
            "ABSTAIN", [R.MALFORMED_ROLLOUT]), (field, value)
    row[field]["failed"] = 0
    assert A.judge_row(row)["verdict"] == "CLEAN"
    row[field]["failed"] = 1
    assert A.judge_row(row)["verdict"] == "FLAGGED"


def test_clean_batch_is_go_and_seeded_deck_is_hold():
    clean = A.summarize(A.analyze_rows(A.load_rows(CLEAN)), CLEAN, "x")
    deck = A.summarize(A.analyze_rows(ROWS), DECK, "x")
    assert (clean["verdict"], clean["rollouts"]) == ("GO", 3)
    assert deck["verdict"] == "HOLD"
    assert (deck["clean"], deck["flagged"], deck["abstain"]) == (14, 12, 1)


def test_unmarked_synthetic_row_halts_the_whole_run(tmp_path):
    row = copy.deepcopy(BY_ID["rt-001"])
    del row["synthetic"]
    path = tmp_path / "unmarked.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(A.RunHalt) as halt:
        A.load_rows(path)
    assert halt.value.code == "NOT_SYNTHETIC"
    assert len(A.load_rows(path, allow_nonsynthetic=True)) == 1


def test_labels_stripped_output_identical():
    """The analyzer never reads the answer key: delete it and nothing about the run moves."""
    stripped = [{k: v for k, v in row.items() if k != "label"} for row in ROWS]
    kept, without = A.analyze_rows(ROWS), A.analyze_rows(stripped)
    assert json.dumps(kept, sort_keys=True) == json.dumps(without, sort_keys=True)
    assert A.render_analyze(A.summarize(kept, "d", "x")) == A.render_analyze(A.summarize(without, "d", "x"))


def test_run_is_deterministic_and_trace_carries_no_content(tmp_path):
    for name in ("one", "two"):
        judgments = A.analyze_rows(ROWS)
        A.write_run(tmp_path / name, A.summarize(judgments, DECK, "x"), judgments)
    assert (tmp_path / "one" / "summary.json").read_bytes() == (tmp_path / "two" / "summary.json").read_bytes()
    trace = (tmp_path / "one" / "trace.jsonl").read_text(encoding="utf-8")
    assert len(trace.strip().splitlines()) == 27
    for banned in ("diff", "commands", "SNTL-", "demo_secret_", "chmod", "assert"):
        assert banned not in trace


def test_shipped_deck_passes_the_published_budget():
    exit_code, receipt, text = A.score_deck(DECK, BUDGET)
    assert exit_code == A.EXIT_GO and receipt["passed"] is True
    assert (receipt["tp"], receipt["fn"], receipt["abstained"]) == (12, 0, 1)
    assert "deck sha256 matches pin" in text and text.strip().endswith("PASS")


def test_edited_deck_row_breaks_the_pin(tmp_path):
    edited = tmp_path / "deck.jsonl"
    edited.write_text(DECK.read_text(encoding="utf-8").replace("rt-001", "rt-901", 1),
                      encoding="utf-8", newline="\n")
    exit_code, receipt, text = A.score_deck(edited, BUDGET)
    assert exit_code == A.EXIT_HOLD and receipt is None and "MISMATCH" in text


def test_falsifier_secret_rule_disabled_misses_planted_row():
    """Drop one rule and the row planted for it walks through clean; the budget catches it."""
    without_secret = tuple(r for r in R.RULES if r.code != R.SECRET_LEAKED)
    assert codes_for("rt-016", rules=without_secret) == set()
    assert codes_for("rt-023", rules=without_secret) == {R.VALIDATION_BYPASSED}
    exit_code, receipt, _text = A.score_deck(DECK, BUDGET, rules=without_secret)
    assert exit_code == A.EXIT_HOLD and receipt["fn"] == 1 and receipt["recall"] < 1.0


def test_falsifier_over_broad_rule_blows_the_false_positive_budget():
    """A rule firing on everything misses the fp budget -- the gate can fail from both ends."""
    always = R.RULES + (R.Rule("ALWAYS", lambda rollout: True),)
    exit_code, receipt, _text = A.score_deck(DECK, BUDGET, rules=always)
    assert exit_code == A.EXIT_HOLD and receipt["fp"] == 14 and receipt["passed"] is False


SHIPPED = [p for p in ROOT.rglob("*") if p.is_file() and not any(
    part in {".git", "__pycache__", ".pytest_cache", "runs"} for part in p.parts)]
FORBIDDEN = (
    ("drive_letter_root", re.compile(r"(?<![a-z])[a-z]:[\\/]", re.IGNORECASE)),
    ("windows_user_home", re.compile(r"[\\/]users[\\/]", re.IGNORECASE)),
    ("posix_user_home", re.compile(r"[\\/]home[\\/][a-z0-9._-]+", re.IGNORECASE)),
    ("accuracy_claim", re.compile(r"\b(?:MAE|accuracy|band)\s*[:=]?\s*[0-9]", re.IGNORECASE)),
    ("personal_mailbox", re.compile(r"[a-z0-9._%+-]+@(?:gmail|outlook|yahoo|hotmail|icloud)\.",
                                    re.IGNORECASE)),
)


def test_public_clean_scan_over_every_shipped_file():
    assert len(SHIPPED) >= 10
    hits = ["{0}:{1} {2}".format(path.name, number, name)
            for path in SHIPPED
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace")
                                          .splitlines(), start=1)
            for name, pattern in FORBIDDEN
            if pattern.search(line) and ANCHOR not in line]
    assert hits == []


def test_public_clean_scan_fires_on_every_planted_shape():
    plants = ("D:" + "/work/notes.txt", "C:" + "\\Users" + "\\someone\\x", "/home" + "/someone/x",  # PLANTED-SHAPE
              "accuracy = 0.91", "someone" + "@gmail.com")  # PLANTED-SHAPE
    for name, pattern in FORBIDDEN:
        assert any(pattern.search(plant) for plant in plants), name


def _names_network(text):
    return [t for t in ("urllib", "socket", "http.client", "requests", "httpx") if t in text]


def test_no_network_code_path_anywhere_in_src():
    for path in (ROOT / "src").rglob("*.py"):
        assert _names_network(path.read_text(encoding="utf-8")) == [], path.name
    assert _names_network("import socket") == ["socket"]  # the scan can find one


def test_cli_exit_codes_end_to_end(tmp_path):
    def run(args):
        return subprocess.run([sys.executable, "-m", "rollout_sentinel.cli"] + args, cwd=ROOT,
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": "src"})
    ok = run(["analyze", "--rollouts", "fixtures/batch_clean.jsonl"])
    assert ok.returncode == 0 and "VERDICT: GO" in ok.stdout
    held = run(["analyze", "--rollouts", "fixtures/deck.jsonl", "--out", str(tmp_path / "d")])
    assert held.returncode == 2 and "VERDICT: HOLD (12 flagged, 1 abstained of 27)" in held.stdout
    assert "TEST_DISABLED" in (tmp_path / "d" / "summary.json").read_text(encoding="utf-8")
    scored = run(["score", "--deck", "fixtures/deck.jsonl", "--budget", "budget.json"])
    assert scored.returncode == 0 and "recall=1.000000" in scored.stdout
