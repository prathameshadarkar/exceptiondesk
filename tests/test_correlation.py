"""The target spec. These FAIL until you implement correlate.py.

That is the point: they are the definition of done. Work them in order --
signature grouping first, then the topology split, then the thresholds.

The numbers below are deliberately achievable, not heroic. If you blow past
them, tighten them and say so in the README; if you cannot reach one, write
down *why* in the notes rather than lowering it quietly. An interviewer will
be far more impressed by "noise rejection sits at 0.71 because
missed_corporate_action is a two-alert incident and I chose recall over
precision there" than by a row of green checkmarks.
"""

import pytest

from exception_desk.correlate import CorrelationConfig, assemble_incidents, group_by_signature
from exception_desk.evaluate import (
    load_scenario,
    over_merge_rate,
    over_split_rate,
    pairwise_scores,
    predicted_label_map,
    true_label_map,
)
from exception_desk.topology import build_topology

DATA = "data"


@pytest.fixture(scope="module")
def graph():
    return build_topology()


def _run(key, graph, config=None):
    alerts, truth = load_scenario(DATA, key)
    incidents = assemble_incidents(alerts["alerts"], graph, config or CorrelationConfig())
    return alerts, truth, incidents


# --- step 1 ---------------------------------------------------------------


def test_signature_grouping_collapses_flaps(graph):
    """msg_bus flaps seven times; that is one signal, not seven."""
    alerts, _ = load_scenario(DATA, "flapping_latency")
    groups = group_by_signature(alerts["alerts"], CorrelationConfig())
    assert sum(g.count for g in groups) == len(alerts["alerts"]), (
        "every alert must land in exactly one group"
    )
    assert any(g.is_flapping for g in groups), "no flapping group detected"


def test_grouping_does_not_merge_different_services(graph):
    alerts, _ = load_scenario(DATA, "db_pool_exhaustion")
    groups = group_by_signature(alerts["alerts"], CorrelationConfig())
    for g in groups:
        services = {
            a["service"] for a in alerts["alerts"] if a["alert_id"] in g.alert_ids
        }
        assert services == {g.service}


# --- the three hard scenarios --------------------------------------------


def test_flapping_is_one_incident(graph):
    """Over-split check. Seven flaps, one problem, one page."""
    _, truth, incidents = _run("flapping_latency", graph)
    assert len(incidents) == 1, f"expected 1 incident, got {len(incidents)}"
    assert over_split_rate(true_label_map(truth), predicted_label_map(incidents)) == 0.0


def test_two_independent_stay_separate(graph):
    """Over-merge check. Disjoint in topology, overlapping in time."""
    _, truth, incidents = _run("two_independent", graph)
    assert len(incidents) == 2, f"expected 2 incidents, got {len(incidents)}"
    assert over_merge_rate(true_label_map(truth), predicted_label_map(incidents)) == 0.0
    roots = {i.root for i in incidents}
    assert roots == {"vendor_borrow", "fix_gw_broker_b"}


def test_db_pool_root_found_despite_simultaneous_failures(graph):
    """Topology check. Timestamps carry almost no signal here."""
    _, _, incidents = _run("db_pool_exhaustion", graph)
    assert incidents, "no incident detected"
    biggest = max(incidents, key=lambda i: i.alert_count)
    assert biggest.root == "db_primary"


def test_stale_fx_root_found_despite_late_root_alert(graph):
    """Earliness check. The root alerts after several of its own symptoms."""
    _, _, incidents = _run("stale_fx_feed", graph)
    assert incidents
    biggest = max(incidents, key=lambda i: i.alert_count)
    assert biggest.root == "vendor_fx"


def test_airflow_root_preferred_over_data_symptoms(graph):
    """Four loaders fail at once; the scheduler underneath them is the cause."""
    _, _, incidents = _run("airflow_dag_cascade", graph)
    assert incidents
    biggest = max(incidents, key=lambda i: i.alert_count)
    assert biggest.root == "scheduler_airflow"


# --- quality bars ---------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "vendor_price_file_late",
        "fix_session_drop",
        "db_pool_exhaustion",
        "airflow_dag_cascade",
        "stale_fx_feed",
        "flapping_latency",
        "two_independent",
    ],
)
def test_clustering_f1_above_bar(key, graph):
    _, truth, incidents = _run(key, graph)
    _, _, f1 = pairwise_scores(true_label_map(truth), predicted_label_map(incidents))
    assert f1 >= 0.80, f"{key}: pairwise F1 {f1:.3f} below 0.80"


def test_every_incident_carries_evidence(graph):
    """No unexplained incidents. If you cannot say why, do not assert it."""
    for key in ("db_pool_exhaustion", "two_independent", "stale_fx_feed"):
        _, _, incidents = _run(key, graph)
        for inc in incidents:
            assert inc.evidence, f"{key}/{inc.incident_id} has no evidence"
            assert inc.candidates, f"{key}/{inc.incident_id} has no ranked candidates"
            for reason in (inc.candidates[0].reasons or []):
                assert isinstance(reason, str) and reason.strip()


def test_confidence_is_lower_when_candidates_are_close(graph):
    """A near-tie should be flagged for a human, not asserted at 99%."""
    _, _, incidents = _run("db_pool_exhaustion", graph)
    for inc in incidents:
        assert 0.0 <= inc.confidence <= 1.0
