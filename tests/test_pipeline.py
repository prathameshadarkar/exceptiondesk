"""Tests for the pipeline around the ranker: bursting, splitting, assembly.

These are deliberately separate from ``test_correlation.py``. That file is the
spec for ``rank_root_candidates`` and stays red until it is written; this file
covers everything else and must stay green, so a regression in the plumbing is
never hidden inside a wall of expected failures.

Every test here injects a ranker rather than using the real one, because the
question these ask is "did the right alerts end up together", which has nothing
to do with which service got named the cause.
"""

import pytest

from exception_desk.correlate import (
    AlertGroup,
    CorrelationConfig,
    RootCandidate,
    assemble_incidents,
    group_by_signature,
    split_by_topology,
    temporal_windows,
)
from exception_desk.evaluate import (
    load_scenario,
    over_merge_rate,
    over_split_rate,
    pairwise_scores,
    predicted_label_map,
    run_evaluation,
    score_scenario,
    true_label_map,
)
from exception_desk.topology import blast_radius, build_topology

DATA = "data"

ALL_KEYS = [
    "vendor_price_file_late",
    "fix_session_drop",
    "db_pool_exhaustion",
    "airflow_dag_cascade",
    "stale_fx_feed",
    "missed_corporate_action",
    "flapping_latency",
    "two_independent",
]


@pytest.fixture(scope="module")
def graph():
    return build_topology()


def widest_blast(burst, g, config=None):
    """Stand-in ranker: widest blast radius wins.

    Not the real heuristic and not meant to be — these tests are about the
    pipeline, and a ranker they inject is one that cannot silently change
    underneath them.
    """
    services = {grp.service for grp in burst}
    radii = {s: blast_radius(g, s) if s in g else 0 for s in services}
    top = max(radii.values()) or 1
    out = [RootCandidate(s, r / top, [f"{r} downstream"]) for s, r in radii.items()]
    out.sort(key=lambda c: (-c.score, c.service))
    return out


def run(key, g, config=None):
    alerts, truth = load_scenario(DATA, key)
    incidents = assemble_incidents(
        alerts["alerts"], g, config or CorrelationConfig(), ranker=widest_blast
    )
    return alerts, truth, incidents


def grp(service, first, last=None, count=1, key=None):
    return AlertGroup(
        key=key or f"{service}#{first}",
        service=service,
        alert_type="ERROR_RATE",
        entity=None,
        severity="P2",
        count=count,
        first_minute=first,
        last_minute=last if last is not None else first,
        alert_ids=[f"{service}-{first}-{n}" for n in range(count)],
    )


# --- step 2: temporal_windows --------------------------------------------


def test_burst_splits_on_a_long_gap():
    config = CorrelationConfig(burst_window_minutes=5.0)
    bursts = temporal_windows([grp("a", 0), grp("b", 2), grp("c", 30)], config)
    assert [len(b) for b in bursts] == [2, 1]


def test_burst_measures_the_gap_from_the_running_end_not_the_last_start():
    """A flap spanning the window must not close the burst underneath itself.

    ``a`` fires from minute 0 to minute 20. ``b`` arrives at 22. Measured from
    ``a``'s start that is a 22-minute gap and a new burst; measured from the
    burst's end it is 2 minutes and the same burst. The second reading is the
    correct one, and getting it wrong is how a long incident fragments.
    """
    config = CorrelationConfig(burst_window_minutes=5.0)
    bursts = temporal_windows([grp("a", 0, last=20, count=7), grp("b", 22)], config)
    assert len(bursts) == 1


def test_burst_of_nothing_is_no_bursts():
    assert temporal_windows([], CorrelationConfig()) == []


def test_bursts_preserve_every_group():
    _, _ = load_scenario(DATA, "flapping_latency")
    alerts, _ = load_scenario(DATA, "flapping_latency")
    groups = group_by_signature(alerts["alerts"], CorrelationConfig())
    bursts = temporal_windows(groups, CorrelationConfig())
    assert sum(len(b) for b in bursts) == len(groups)


# --- step 4a: the topological split --------------------------------------


def test_disjoint_services_split_even_when_simultaneous(graph):
    """The whole of two_independent, in miniature."""
    burst = [grp("vendor_borrow", 0), grp("svc_locate", 1),
             grp("fix_gw_broker_b", 1), grp("svc_ems", 2)]
    parts = split_by_topology(burst, graph)
    assert len(parts) == 2
    sides = sorted(sorted({g.service for g in p}) for p in parts)
    assert sides == [["fix_gw_broker_b", "svc_ems"], ["svc_locate", "vendor_borrow"]]


def test_split_links_through_a_silent_intermediary(graph):
    """ingest_px_loader and svc_risk are linked by svc_pricing, which is quiet.

    An induced subgraph on the alerting services would find no edge here and
    split them. Reachability walks through the silent node, which is the reason
    the split is written as a relation rather than a subgraph.
    """
    assert not graph.has_edge("svc_risk", "ingest_px_loader")
    parts = split_by_topology([grp("ingest_px_loader", 0), grp("svc_risk", 1)], graph)
    assert len(parts) == 1


def test_split_does_not_link_through_a_shared_ancestor(graph):
    """svc_locate and svc_blotter are both under db_primary. That is not a link.

    If sharing an ancestor were enough, this topology would collapse into one
    component and two_independent would merge.
    """
    from exception_desk.topology import upstream_of
    assert "db_primary" in upstream_of(graph, "svc_blotter")
    parts = split_by_topology([grp("svc_locate", 0), grp("svc_blotter", 1)], graph)
    assert len(parts) == 2


def test_split_keeps_unknown_services_out_of_everything(graph):
    parts = split_by_topology([grp("vendor_borrow", 0), grp("not_a_service", 1)], graph)
    assert len(parts) == 2


# --- step 4b: assembly ----------------------------------------------------


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_alert_is_accounted_for(key, graph):
    """No alert may be duplicated across incidents. Dropped is fine; that is noise."""
    alerts, _, incidents = run(key, graph)
    seen = [aid for inc in incidents for aid in inc.alert_ids]
    assert len(seen) == len(set(seen)), "an alert appears in more than one incident"
    known = {a["alert_id"] for a in alerts["alerts"]}
    assert set(seen) <= known, "an incident cites an alert that does not exist"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_clustering_is_clean_regardless_of_which_root_is_named(key, graph):
    """Grouping quality is the pipeline's job and must not depend on the ranker."""
    _, truth, incidents = run(key, graph)
    t, p = true_label_map(truth), predicted_label_map(incidents)
    _, _, f1 = pairwise_scores(t, p)
    assert f1 >= 0.80, f"{key}: pairwise F1 {f1:.3f}"
    assert over_merge_rate(t, p) == 0.0, f"{key}: merged two incidents into one"


def test_flapping_collapses_to_one_incident(graph):
    _, truth, incidents = run("flapping_latency", graph)
    assert len(incidents) == 1
    assert over_split_rate(true_label_map(truth), predicted_label_map(incidents)) == 0.0


def test_two_independent_stay_two(graph):
    _, _, incidents = run("two_independent", graph)
    assert len(incidents) == 2


def test_small_real_incident_survives_the_noise_threshold(graph):
    """missed_corporate_action is four alerts and genuine. It must not be binned."""
    _, truth, incidents = run("missed_corporate_action", graph)
    assert len(incidents) == 1
    covered = set(incidents[0].alert_ids)
    members = {r["alert_id"] for r in truth["alerts"] if r["incident_id"]}
    assert members <= covered, "part of a real incident was dropped as noise"


def test_isolated_noise_pairs_do_not_become_incidents(graph):
    """Two adjacent background alerts are not an incident.

    vendor_refdata and svc_security_master fire 3.4 minutes apart in
    vendor_price_file_late and are topologically adjacent. At
    min_groups_for_incident=2 they manufactured a second incident out of noise.
    """
    _, _, incidents = run("vendor_price_file_late", graph)
    assert len(incidents) == 1
    assert "vendor_refdata" not in incidents[0].affected_services


def test_incidents_are_time_ordered_and_uniquely_identified(graph):
    for key in ALL_KEYS:
        _, _, incidents = run(key, graph)
        starts = [i.start_minute for i in incidents]
        assert starts == sorted(starts), f"{key}: incidents out of time order"
        ids = [i.incident_id for i in incidents]
        assert len(ids) == len(set(ids)), f"{key}: duplicate incident ids"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_assembly_always_produces_evidence(key, graph):
    _, _, incidents = run(key, graph)
    for inc in incidents:
        assert inc.evidence and all(isinstance(e, str) and e.strip() for e in inc.evidence)
        assert inc.candidates
        assert inc.affected_services
        assert inc.start_minute <= inc.end_minute
        assert inc.severity in {"P1", "P2", "P3", "P4"}


# --- confidence -----------------------------------------------------------


def test_confidence_falls_as_the_top_two_converge(graph):
    """The margin is the point: a near-tie must not be asserted at 99%."""
    burst = [grp("db_primary", 0), grp("svc_pricing", 1), grp("svc_risk", 2)]

    def clear(b, g, c=None):
        return [RootCandidate("db_primary", 1.0, ["r"]), RootCandidate("svc_risk", 0.1, ["r"])]

    def tie(b, g, c=None):
        return [RootCandidate("db_primary", 1.0, ["r"]), RootCandidate("svc_risk", 0.99, ["r"])]

    alerts = [
        {"alert_id": aid, "service": g.service, "alert_type": "ERROR_RATE",
         "entity": None, "severity": "P2", "ts_minute": g.first_minute}
        for g in burst for aid in g.alert_ids
    ]
    hi = assemble_incidents(alerts, graph, ranker=clear)[0].confidence
    lo = assemble_incidents(alerts, graph, ranker=tie)[0].confidence
    assert hi > lo
    assert 0.0 <= lo <= hi <= 1.0
    assert lo < 0.65, "a 1% margin should read as 'needs a human'"


# --- step 5: the harness --------------------------------------------------


def test_score_scenario_fills_every_field(graph):
    s = score_scenario("db_pool_exhaustion", DATA, graph, CorrelationConfig(), widest_blast)
    assert s.scenario == "db_pool_exhaustion"
    assert s.n_alerts > 0 and s.n_predicted_incidents > 0
    for field in ("pairwise_precision", "pairwise_recall", "pairwise_f1",
                  "over_merge_rate", "over_split_rate", "root_top1",
                  "root_top3", "noise_rejection"):
        value = getattr(s, field)
        assert 0.0 <= value <= 1.0, f"{field} out of range: {value}"
    assert s.alert_reduction >= 1.0


def test_run_evaluation_covers_every_scenario_in_a_stable_order():
    card = run_evaluation(DATA, CorrelationConfig(), widest_blast)
    assert [s.scenario for s in card.scenarios] == ALL_KEYS
    assert 0.0 <= card.macro_f1 <= 1.0
    assert card.total_alerts > 0
    assert card.to_dict()["scenarios"]


def test_harness_notes_flag_a_wrong_root(graph):
    """The scorecard has to say where it is uncomfortable, not just score well."""
    def always_wrong(b, g, c=None):
        services = sorted({x.service for x in b})
        return [RootCandidate(s, 1.0 - i, ["r"]) for i, s in enumerate(reversed(services))]

    s = score_scenario("db_pool_exhaustion", DATA, graph, CorrelationConfig(), always_wrong)
    assert any("root called as" in n for n in s.notes)


# --- determinism ----------------------------------------------------------


def test_split_component_order_is_stable(graph):
    """nx.connected_components yields sets in an unstable order.

    Anything downstream — incident ids, which component prunes first — would
    inherit that. Caught in the wild: the blast-radius baseline flipped between
    right and wrong on flapping_latency across re-runs of an unchanged repo.
    """
    burst = [grp("vendor_borrow", 0), grp("svc_locate", 1),
             grp("fix_gw_broker_b", 1), grp("svc_ems", 2), grp("svc_oms", 2)]
    shapes = {
        tuple(tuple(sorted(g.service for g in part)) for part in split_by_topology(burst, graph))
        for _ in range(12)
    }
    assert len(shapes) == 1, "component order varies between runs"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_same_input_gives_the_same_incidents(key, graph):
    runs = []
    for _ in range(3):
        _, _, incidents = run(key, graph)
        runs.append([(i.incident_id, i.root, tuple(i.alert_ids)) for i in incidents])
    assert runs[0] == runs[1] == runs[2]
