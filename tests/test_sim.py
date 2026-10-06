"""Tests for the shift simulator's scoring. These pass today.

The browser scores a run offline so the debrief works on a static deploy, but
this Python implementation is the reference: if the two ever disagree, this one
is right. Testing it here means the scoring rules are pinned somewhere a test
can reach them.
"""

import pytest

from exception_desk.sim import SHIFTS, SHIFT_INDEX, score_run, shift_answers, shift_manifest


@pytest.fixture
def shift():
    return SHIFT_INDEX["db_pool_exhaustion"]


def _run(**over):
    base = {"elapsed_minutes": 20, "declared_root": None, "declared_at": None,
            "checks_run": [], "actions_taken": [], "agents_used": []}
    base.update(over)
    return base


# --- shape ---------------------------------------------------------------


@pytest.mark.parametrize("s", SHIFTS, ids=lambda s: s.key)
def test_every_shift_has_a_decisive_check(s):
    assert any(c.decisive for c in s.checks), f"{s.key} has no decisive check to find"


@pytest.mark.parametrize("s", SHIFTS, ids=lambda s: s.key)
def test_true_root_is_never_only_a_decoy(s):
    assert s.true_root not in s.decoys


@pytest.mark.parametrize("s", SHIFTS, ids=lambda s: s.key)
def test_candidates_include_the_answer_and_plausible_wrong_ones(s):
    cands = shift_manifest()
    entry = next(c for c in cands if c["key"] == s.key)
    assert s.true_root in entry["candidates"]
    assert len(entry["candidates"]) >= 3, "a one-option choice is not a decision"


@pytest.mark.parametrize("s", SHIFTS, ids=lambda s: s.key)
def test_impacts_are_ordered_and_inside_the_budget(s):
    minutes = [i.at_minute for i in s.impacts]
    assert minutes == sorted(minutes)
    assert max(minutes) <= s.budget_minutes


@pytest.mark.parametrize("s", SHIFTS, ids=lambda s: s.key)
def test_cheapest_path_to_the_answer_fits_the_budget(s):
    """A shift you cannot win is a shift nobody plays twice."""
    decisive_cost = sum(c.cost_minutes for c in s.checks if c.decisive)
    required_cost = 2 * len(s.required)
    assert decisive_cost + required_cost + 1 < s.budget_minutes


def test_answer_key_covers_every_check():
    ans = shift_answers()
    for s in SHIFTS:
        keys = set(ans[s.key]["findings"])
        assert keys == {c.id for c in s.checks}


def test_manifest_does_not_leak_the_answer():
    """The playable manifest must not contain findings or the true root."""
    blob = repr(shift_manifest())
    for s in SHIFTS:
        assert f"'true_root': '{s.true_root}'" not in blob
        for c in s.checks:
            assert c.finding not in blob


# --- scoring -------------------------------------------------------------


def test_perfect_run_scores_high(shift):
    run = _run(
        declared_root="db_primary", declared_at=6,
        checks_run=[c.id for c in shift.checks if c.decisive],
        actions_taken=[{"id": r.id, "minute": 3} for r in shift.required],
    )
    res = score_run(shift, run)
    assert res["points"] >= 85
    assert res["grade"] == "Clean shift"


def test_wrong_root_loses_the_whole_category(shift):
    run = _run(declared_root="svc_oms", declared_at=5)
    res = score_run(shift, run)
    root = next(l for l in res["lines"] if l["label"] == "Root cause")
    assert root["points"] == 0
    assert "symptom" in root["note"]


def test_no_declaration_scores_zero_on_root(shift):
    res = score_run(shift, _run())
    assert next(l for l in res["lines"] if l["label"] == "Root cause")["points"] == 0


def test_speed_matters(shift):
    fast = score_run(shift, _run(declared_root="db_primary", declared_at=3))
    slow = score_run(shift, _run(declared_root="db_primary", declared_at=27))
    assert fast["points"] > slow["points"]


def test_late_action_scores_partial_not_zero(shift):
    late = score_run(shift, _run(
        declared_root="db_primary", declared_at=5,
        actions_taken=[{"id": "notify_pm", "minute": 25}]))
    missed = score_run(shift, _run(declared_root="db_primary", declared_at=5))
    lo = next(l for l in late["lines"] if l["label"] == "Required actions")
    mo = next(l for l in missed["lines"] if l["label"] == "Required actions")
    assert lo["points"] > mo["points"]
    assert "late" in lo["note"]


def test_red_herrings_cost_diagnostic_points(shift):
    clean = score_run(shift, _run(checks_run=["conn_pool"]))
    messy = score_run(shift, _run(checks_run=["conn_pool", "timeline", "restart_oms"]))
    c = next(l for l in clean["lines"] if l["label"] == "Diagnostic efficiency")
    m = next(l for l in messy["lines"] if l["label"] == "Diagnostic efficiency")
    assert m["points"] < c["points"]
    assert "dead end" in m["note"]


def test_score_never_leaves_the_scale(shift):
    for run in (_run(), _run(checks_run=[c.id for c in shift.checks]),
                _run(declared_root="db_primary", declared_at=0,
                     checks_run=[c.id for c in shift.checks],
                     actions_taken=[{"id": r.id, "minute": 1} for r in shift.required])):
        res = score_run(shift, run)
        assert 0 <= res["points"] <= 100
        for line in res["lines"]:
            assert 0 <= line["points"] <= line["of"]
