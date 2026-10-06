"""Fault propagation simulator.

Takes a :class:`~exception_desk.scenarios.Scenario`, walks each injected fault
downstream through the dependency graph, and emits the alert stream an
operator would have seen -- plus a separate ground-truth file recording which
incident every alert really belonged to.

The two files are written separately **on purpose**. If ground truth lived on
the alert records, it would be far too easy to accidentally read it inside the
correlation engine and score a perfect result that means nothing. Keeping them
apart is the difference between an evaluation and a demo.

Determinism: every scenario carries a seed, and the simulator seeds a local
``random.Random`` from it. The same scenario always produces the same alerts,
so your accuracy numbers are reproducible and a regression is a real signal.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import networkx as nx

from .scenarios import FAULT_EXPRESSION, Fault, Scenario, SCENARIOS
from .topology import build_topology, depth_from_node, get_service

# Alerts are stamped onto a fixed nominal date so timestamps are comparable
# across scenarios and stable across runs.
NOMINAL_DATE = datetime(2026, 3, 12, tzinfo=timezone.utc)

SEVERITY_BY_CRITICALITY = {1: "P1", 2: "P2", 3: "P3", 4: "P4"}

#: Alert types that always page, regardless of how critical the service is.
ALWAYS_PAGE = {"RISK_BREACH", "CONN_LOST", "SEQ_GAP", "RECON_BREAK"}

MESSAGE_TEMPLATES: dict[str, str] = {
    "JOB_FAILED": "{service}: scheduled run failed ({detail})",
    "JOB_LATE": "{service}: expected completion missed by {minutes_late}m ({detail})",
    "STALE_DATA": "{service}: serving data older than threshold ({detail})",
    "CONN_LOST": "{service}: session disconnected ({detail})",
    "SEQ_GAP": "{service}: sequence gap detected, {gap} messages missing",
    "HIGH_LATENCY": "{service}: p99 latency {latency}ms over threshold",
    "ERROR_RATE": "{service}: error rate {rate}% over threshold",
    "QUEUE_DEPTH": "{service}: queue depth {depth} over threshold",
    "RISK_BREACH": "{service}: limit breach on {strategy} ({detail})",
    "PNL_SPIKE": "{service}: intraday P&L move {sigma}s on {strategy}",
    "RECON_BREAK": "{service}: {count} unmatched trades against executing broker",
    "REJECT_RATE": "{service}: order reject rate {rate}% over threshold",
    "DISK_PRESSURE": "{service}: volume {pct}% full",
    "DQ_OUTLIER": "{service}: {count} instruments outside expected return band",
}

STRATEGIES = ("EQ_STAT_ARB", "EQ_MKT_NEUTRAL", "FUT_TREND", "FX_CARRY", "CREDIT_RV", "MULTI_ASSET")

INSTRUMENTS = (
    "AAPL.O", "MSFT.O", "NVDA.O", "JPM.N", "XOM.N", "TSLA.O", "BRKb.N", "UNH.N",
    "ESZ6", "NQZ6", "CLF7", "GCG7", "EURUSD", "USDJPY", "GBPUSD", "USDCHF",
)

BROKERS = ("BROKER_A", "BROKER_B", "BROKER_C")

#: Alert types that are raised once per affected entity rather than once per
#: service. A stale pricing service does not raise one STALE_DATA alert -- it
#: raises one per instrument, which is exactly why storms get loud.
ENTITY_SCOPED = {
    "STALE_DATA": INSTRUMENTS,
    "DQ_OUTLIER": INSTRUMENTS,
    "RISK_BREACH": STRATEGIES,
    "PNL_SPIKE": STRATEGIES,
    "RECON_BREAK": BROKERS,
}


@dataclass(frozen=True)
class Alert:
    """One row on the alert board. Contains no ground truth."""

    alert_id: str
    ts: str
    ts_minute: float
    service: str
    tier: str
    alert_type: str
    severity: str
    message: str
    host: str
    entity: str | None = None   # instrument / strategy / broker, when scoped


@dataclass(frozen=True)
class GroundTruth:
    """The answer key for a single alert."""

    alert_id: str
    incident_id: str | None   # None means background noise
    true_root: str | None
    hops_from_root: int | None
    is_noise: bool


@dataclass(frozen=True)
class IncidentTruth:
    """The answer key for a whole incident."""

    incident_id: str
    true_root: str
    label: str
    kind: str
    start_minute: int
    member_alert_ids: tuple[str, ...]
    affected_services: tuple[str, ...]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _minute_to_ts(minute: float) -> str:
    return (NOMINAL_DATE + timedelta(minutes=float(minute))).isoformat()


def _host_for(service: str, rng: random.Random) -> str:
    return f"{service.replace('_', '-')}-{rng.randint(1, 4):02d}.ny.eglp.internal"


def _severity(service: str, alert_type: str) -> str:
    """Severity from service criticality AND what kind of alert it is.

    Criticality alone put ~70% of a storm at P1, which makes P1 meaningless --
    on a real desk P1 is reserved for the handful of things that justify waking
    someone. A paging-class alert (:data:`ALWAYS_PAGE`) escalates one step; a
    routine threshold crossing on the same service does not.
    """
    svc = get_service(service)
    level = svc.criticality + 1
    if alert_type in ALWAYS_PAGE:
        level -= 1
    level = max(1, min(4, level))
    return SEVERITY_BY_CRITICALITY[level]


def _choose_alert_type(service: str, kind: str, rng: random.Random) -> str:
    """Pick an alert type this service can plausibly raise for this fault kind.

    Intersects the fault's expression for the service's tier with the alert
    types the service declares. Falls back to the service's own list if the
    intersection is empty, so we never invent an alert a node cannot raise.
    """
    svc = get_service(service)
    preferred = FAULT_EXPRESSION.get(kind, {}).get(svc.tier, ())
    candidates = [a for a in preferred if a in svc.emits]
    if not candidates:
        candidates = list(svc.emits)
    return rng.choice(candidates)


def _entity_for(alert_type: str, rng: random.Random) -> str | None:
    """Pick the instrument / strategy / broker an entity-scoped alert refers to."""
    pool = ENTITY_SCOPED.get(alert_type)
    return rng.choice(pool) if pool else None


def _render_message(
    service: str,
    alert_type: str,
    rng: random.Random,
    detail: str,
    entity: str | None = None,
) -> str:
    template = MESSAGE_TEMPLATES[alert_type]
    # If the entity IS a strategy, fold it into the message rather than
    # tacking it on the end, so the text reads the way a real alert would.
    strategy = entity if entity in STRATEGIES else rng.choice(STRATEGIES)
    suffix = f" [{entity}]" if entity and entity not in STRATEGIES else ""
    rendered = template.format(
        service=service,
        detail=detail,
        strategy=strategy,
        minutes_late=rng.randint(3, 48),
        gap=rng.randint(2, 900),
        latency=rng.choice([420, 680, 940, 1500, 2300]),
        rate=round(rng.uniform(3.5, 41.0), 1),
        depth=rng.choice([5_000, 12_400, 38_000, 91_000]),
        sigma=round(rng.uniform(3.2, 9.8), 1),
        count=rng.randint(1, 64),
        pct=rng.randint(88, 99),
    )
    return rendered + suffix


# --------------------------------------------------------------------------
# Propagation
# --------------------------------------------------------------------------


def propagate(
    g: nx.DiGraph,
    fault: Fault,
    rng: random.Random,
) -> list[tuple[str, int, float]]:
    """Walk a fault downstream and decide who alerts, when.

    Returns a list of ``(service, hops_from_root, minute)`` triples, including
    the root itself at hop 0.

    The walk is breadth-first over *dependents*. At each hop the probability of
    a service alerting is ``propagation_prob * decay ** (hops - 1)``, so the
    storm thins out with distance the way a real one does. A service that does
    not alert still lets the fault pass through it -- in practice a downstream
    system often notices a problem that the service in between failed to flag.
    """
    depths = depth_from_node(g, fault.root)
    emitted: list[tuple[str, int, float]] = []

    # The root itself. Note root_alert_delay_s: for some faults the root is
    # detected last, which is the entire point of `stale_fx_feed`.
    root_minute = fault.start_minute + fault.root_alert_delay_s / 60.0
    root_minute += rng.uniform(-fault.jitter_s, fault.jitter_s) / 60.0
    emitted.append((fault.root, 0, root_minute))

    for service, hops in sorted(depths.items(), key=lambda kv: kv[1]):
        if hops == 0:
            continue
        if fault.max_hops is not None and hops > fault.max_hops:
            continue
        prob = fault.propagation_prob * (fault.decay ** (hops - 1))
        if rng.random() > prob:
            continue
        minute = fault.start_minute + (fault.hop_delay_s * hops) / 60.0
        minute += rng.uniform(-fault.jitter_s, fault.jitter_s) / 60.0
        emitted.append((service, hops, minute))

    return emitted


def _noise_alerts(
    g: nx.DiGraph,
    scenario: Scenario,
    n_signal: int,
    rng: random.Random,
    start_index: int,
) -> list[Alert]:
    """Unrelated background alerts.

    Real boards are never quiet. Noise is drawn from services not involved in
    any injected fault where possible, at uniformly random times in the window.
    """
    n_noise = max(1, round(n_signal * scenario.noise_rate / (1 - scenario.noise_rate)))
    involved: set[str] = set()
    for fault in scenario.faults:
        involved.add(fault.root)
        involved |= set(depth_from_node(g, fault.root))
    candidates = [n for n in g.nodes if n not in involved] or list(g.nodes)

    out: list[Alert] = []
    for i in range(n_noise):
        service = rng.choice(candidates)
        svc = get_service(service)
        alert_type = rng.choice(list(svc.emits))
        entity = _entity_for(alert_type, rng)
        minute = rng.uniform(scenario.window_start_minute, scenario.window_end_minute)
        out.append(
            Alert(
                alert_id=f"{scenario.key}-A{start_index + i:04d}",
                ts=_minute_to_ts(minute),
                ts_minute=round(minute, 3),
                service=service,
                tier=svc.tier,
                alert_type=alert_type,
                severity=_severity(service, alert_type),
                message=_render_message(
                    service, alert_type, rng, "routine threshold crossing", entity
                ),
                host=_host_for(service, rng),
                entity=entity,
            )
        )
    return out


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------


def simulate(
    scenario: Scenario,
    g: nx.DiGraph | None = None,
) -> tuple[list[Alert], list[GroundTruth], list[IncidentTruth]]:
    """Run one scenario.

    Returns ``(alerts, ground_truth, incidents)``. ``alerts`` is what the
    correlation engine is allowed to see. The other two are the answer key.
    """
    g = g if g is not None else build_topology()
    rng = random.Random(scenario.seed)

    alerts: list[Alert] = []
    truths: list[GroundTruth] = []
    incidents: list[IncidentTruth] = []
    counter = 0

    for i, fault in enumerate(scenario.faults, start=1):
        incident_id = f"{scenario.key}-INC{i}"
        members: list[str] = []
        affected: list[str] = []

        for service, hops, minute in propagate(g, fault, rng):
            affected.append(service)
            for r in range(fault.repeats):
                m0 = minute + (r * fault.repeat_interval_s) / 60.0
                if m0 > scenario.window_end_minute:
                    break
                # One failing service trips several checks, and entity-scoped
                # checks trip once per instrument or strategy.
                n_here = rng.randint(1, max(1, fault.fanout))
                seen_here: set[tuple[str, str | None]] = set()
                for _ in range(n_here):
                    # Distinct checks, not the same check repeated: a duplicate
                    # (type, entity) pair in one cycle reads as a data bug.
                    for _attempt in range(6):
                        alert_type = _choose_alert_type(service, fault.kind, rng)
                        entity = _entity_for(alert_type, rng)
                        if (alert_type, entity) not in seen_here:
                            break
                    else:
                        continue
                    seen_here.add((alert_type, entity))
                    m = m0 + rng.uniform(0, 25) / 60.0
                    if m > scenario.window_end_minute:
                        continue
                    alert_id = f"{scenario.key}-A{counter:04d}"
                    counter += 1
                    alerts.append(
                        Alert(
                            alert_id=alert_id,
                            ts=_minute_to_ts(m),
                            ts_minute=round(m, 3),
                            service=service,
                            tier=get_service(service).tier,
                            alert_type=alert_type,
                            severity=_severity(service, alert_type),
                            message=_render_message(
                                service, alert_type, rng, fault.label, entity
                            ),
                            host=_host_for(service, rng),
                            entity=entity,
                        )
                    )
                    truths.append(
                        GroundTruth(
                            alert_id=alert_id,
                            incident_id=incident_id,
                            true_root=fault.root,
                            hops_from_root=hops,
                            is_noise=False,
                        )
                    )
                    members.append(alert_id)

        incidents.append(
            IncidentTruth(
                incident_id=incident_id,
                true_root=fault.root,
                label=fault.label,
                kind=fault.kind,
                start_minute=fault.start_minute,
                member_alert_ids=tuple(members),
                affected_services=tuple(dict.fromkeys(affected)),
            )
        )

    noise = _noise_alerts(g, scenario, len(alerts), rng, counter)
    for alert in noise:
        alerts.append(alert)
        truths.append(
            GroundTruth(
                alert_id=alert.alert_id,
                incident_id=None,
                true_root=None,
                hops_from_root=None,
                is_noise=True,
            )
        )

    alerts.sort(key=lambda a: a.ts_minute)
    order = {a.alert_id: i for i, a in enumerate(alerts)}
    truths.sort(key=lambda t: order[t.alert_id])
    return alerts, truths, incidents


def write_scenario(scenario: Scenario, out_dir: Path, g: nx.DiGraph | None = None) -> dict:
    """Simulate one scenario and write alerts / ground truth to ``out_dir``."""
    alerts, truths, incidents = simulate(scenario, g)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / f"{scenario.key}.alerts.json").write_text(
        json.dumps(
            {
                "scenario": scenario.key,
                "title": scenario.title,
                "summary": scenario.summary,
                "window": [scenario.window_start_minute, scenario.window_end_minute],
                "alerts": [asdict(a) for a in alerts],
            },
            indent=2,
        )
    )
    (out_dir / f"{scenario.key}.truth.json").write_text(
        json.dumps(
            {
                "scenario": scenario.key,
                "teaches": scenario.teaches,
                "incidents": [asdict(i) for i in incidents],
                "alerts": [asdict(t) for t in truths],
            },
            indent=2,
        )
    )
    n_noise = sum(1 for t in truths if t.is_noise)
    return {
        "scenario": scenario.key,
        "alerts": len(alerts),
        "incidents": len(incidents),
        "noise": n_noise,
        "noise_pct": round(100 * n_noise / len(alerts), 1),
        "services_touched": len({a.service for a in alerts}),
    }


def generate_all(out_dir: Path | str = "data") -> list[dict]:
    """Generate every scenario. This is what ``make data`` runs."""
    g = build_topology()
    out = Path(out_dir)
    return [write_scenario(s, out, g) for s in SCENARIOS]


if __name__ == "__main__":  # pragma: no cover
    rows = generate_all()
    width = max(len(r["scenario"]) for r in rows)
    print(f"{'scenario'.ljust(width)}  alerts  incidents  noise  services")
    for r in rows:
        print(
            f"{r['scenario'].ljust(width)}  "
            f"{r['alerts']:>6}  {r['incidents']:>9}  "
            f"{str(r['noise']) + ' (' + str(r['noise_pct']) + '%)':>11}  "
            f"{r['services_touched']:>8}"
        )
