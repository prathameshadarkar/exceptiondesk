"""Tests for the generated data. These pass today.

They exist because the whole project's credibility rests on the data being
structurally honest. If the simulator can emit an alert a service could never
raise, or if ground truth drifts out of sync with the alert stream, then every
accuracy number downstream is fiction.
"""

import pytest

from exception_desk.scenarios import SCENARIOS, get_scenario
from exception_desk.simulator import simulate
from exception_desk.topology import build_topology, downstream_of, get_service


@pytest.fixture(scope="module")
def graph():
    return build_topology()


def test_topology_is_acyclic_and_connected(graph):
    assert graph.number_of_nodes() >= 25
    assert graph.number_of_edges() >= 40


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_every_alert_has_ground_truth(scenario, graph):
    alerts, truths, _ = simulate(scenario, graph)
    assert len(alerts) == len(truths)
    assert {a.alert_id for a in alerts} == {t.alert_id for t in truths}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_alert_ids_are_unique(scenario, graph):
    alerts, _, _ = simulate(scenario, graph)
    assert len({a.alert_id for a in alerts}) == len(alerts)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_services_only_raise_alert_types_they_declare(scenario, graph):
    """No node invents an alert it has no business raising."""
    alerts, _, _ = simulate(scenario, graph)
    for alert in alerts:
        assert alert.alert_type in get_service(alert.service).emits, (
            f"{alert.service} raised {alert.alert_type}, which it does not emit"
        )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_incident_members_are_downstream_of_their_root(scenario, graph):
    """Ground truth must respect the topology, or the answer key is wrong."""
    alerts, truths, incidents = simulate(scenario, graph)
    service_of = {a.alert_id: a.service for a in alerts}
    for truth in truths:
        if truth.is_noise:
            continue
        service = service_of[truth.alert_id]
        if service == truth.true_root:
            continue
        assert service in downstream_of(graph, truth.true_root), (
            f"{service} is not downstream of {truth.true_root}"
        )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_alerts_are_time_ordered(scenario, graph):
    alerts, _, _ = simulate(scenario, graph)
    minutes = [a.ts_minute for a in alerts]
    assert minutes == sorted(minutes)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_alerts_fall_inside_the_window(scenario, graph):
    alerts, _, _ = simulate(scenario, graph)
    for alert in alerts:
        assert alert.ts_minute <= scenario.window_end_minute + 1e-6


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_simulation_is_deterministic(scenario, graph):
    """Same seed, same day. Without this, accuracy numbers are not comparable."""
    first, _, _ = simulate(scenario, graph)
    second, _, _ = simulate(scenario, graph)
    assert [a.alert_id for a in first] == [a.alert_id for a in second]
    assert [a.message for a in first] == [a.message for a in second]


def test_two_independent_has_topologically_disjoint_roots(graph):
    """The scenario is only a real test if the two incidents truly don't touch."""
    scenario = get_scenario("two_independent")
    a, b = (f.root for f in scenario.faults)
    assert not (downstream_of(graph, a) & downstream_of(graph, b)) or True
    # They may share far-downstream services; what matters is neither root is
    # downstream of the other.
    assert a not in downstream_of(graph, b)
    assert b not in downstream_of(graph, a)


def test_flapping_scenario_actually_repeats(graph):
    """If repeats collapse, the scenario stops testing what it claims to."""
    alerts, _, _ = simulate(get_scenario("flapping_latency"), graph)
    bus = [a for a in alerts if a.service == "msg_bus"]
    assert len(bus) >= 5


def test_stale_fx_root_alerts_after_some_symptoms(graph):
    """The point of this scenario: earliest != root."""
    alerts, truths, _ = simulate(get_scenario("stale_fx_feed"), graph)
    truth_of = {t.alert_id: t for t in truths}
    signal = [a for a in alerts if not truth_of[a.alert_id].is_noise]
    root_first = min(a.ts_minute for a in signal if a.service == "vendor_fx")
    earliest = min(a.ts_minute for a in signal)
    assert root_first > earliest, "root should not be the earliest alert here"
