"""The eight seeded scenarios.

Each scenario is a real failure mode from a systematic trading operation, and
each one was chosen because it breaks a *different* naive approach to
correlation. If your engine handles all eight you have something defensible;
if it handles only the first two you have a time-clustering demo.

    1. vendor_price_file_late   -- the canonical morning storm
    2. fix_session_drop         -- trading-hours outage, narrow but severe
    3. db_pool_exhaustion       -- HARD: many services fail within seconds and
                                   look equally "root". Only topology resolves it.
    4. airflow_dag_cascade      -- scheduler fault; the root is infra, not data
    5. stale_fx_feed            -- false P&L spike; the loudest alert is a symptom
    6. missed_corporate_action  -- tiny blast radius, easily buried in noise
    7. flapping_latency         -- HARD: one service alerts repeatedly. Naive
                                   engines report seven incidents instead of one.
    8. two_independent          -- HARD: two unrelated incidents overlap in time.
                                   Naive engines merge them into one.

Scenarios 3, 7 and 8 are the ones worth building well. They are also the ones
worth talking about in an interview.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Fault semantics
# --------------------------------------------------------------------------

#: How a fault of a given kind tends to express itself, by tier. The simulator
#: intersects these preferences with what a node actually declares it can emit
#: (``Service.emits``), so a node never raises an alert type it has no business
#: raising.
FAULT_EXPRESSION: dict[str, dict[str, tuple[str, ...]]] = {
    "data_missing": {
        "vendor": ("JOB_LATE", "CONN_LOST"),
        "ingest": ("JOB_FAILED", "JOB_LATE"),
        "compute": ("STALE_DATA", "DQ_OUTLIER"),
        "risk": ("RISK_BREACH", "STALE_DATA"),
        "post_trade": ("STALE_DATA", "DQ_OUTLIER"),
        "reporting": ("JOB_LATE", "STALE_DATA"),
    },
    "data_wrong": {
        "vendor": ("STALE_DATA",),
        "ingest": ("DQ_OUTLIER", "ERROR_RATE"),
        "compute": ("DQ_OUTLIER", "STALE_DATA"),
        "risk": ("PNL_SPIKE", "RISK_BREACH"),
        "post_trade": ("DQ_OUTLIER", "RECON_BREAK"),
        "reporting": ("STALE_DATA",),
    },
    "connectivity": {
        "trading": ("CONN_LOST", "SEQ_GAP", "REJECT_RATE"),
        "compute": ("ERROR_RATE", "STALE_DATA"),
        "post_trade": ("RECON_BREAK", "QUEUE_DEPTH", "ERROR_RATE"),
        "risk": ("STALE_DATA",),
        "reporting": ("JOB_LATE",),
    },
    "saturation": {
        "infra": ("ERROR_RATE", "HIGH_LATENCY", "QUEUE_DEPTH", "DISK_PRESSURE"),
        "ingest": ("JOB_FAILED", "ERROR_RATE"),
        "compute": ("HIGH_LATENCY", "ERROR_RATE"),
        "risk": ("ERROR_RATE", "STALE_DATA"),
        "trading": ("HIGH_LATENCY", "ERROR_RATE", "QUEUE_DEPTH"),
        "post_trade": ("QUEUE_DEPTH", "ERROR_RATE"),
        "reporting": ("JOB_LATE",),
    },
    "scheduler": {
        "infra": ("JOB_FAILED", "ERROR_RATE"),
        "ingest": ("JOB_FAILED", "JOB_LATE"),
        "compute": ("STALE_DATA",),
        "risk": ("STALE_DATA",),
        "post_trade": ("JOB_LATE", "JOB_FAILED"),
        "reporting": ("JOB_LATE", "JOB_FAILED"),
    },
    "degradation": {
        "infra": ("HIGH_LATENCY", "QUEUE_DEPTH"),
        "compute": ("HIGH_LATENCY",),
        "trading": ("HIGH_LATENCY", "REJECT_RATE"),
        "post_trade": ("QUEUE_DEPTH",),
        "risk": ("HIGH_LATENCY",),
        "reporting": ("JOB_LATE",),
    },
}


@dataclass(frozen=True)
class Fault:
    """A single root cause injected into the topology.

    Attributes
    ----------
    root:
        Service where the fault originates. This is the answer the correlation
        engine is trying to recover.
    kind:
        Key into :data:`FAULT_EXPRESSION`.
    start_minute:
        Minutes after midnight (local desk time) when the fault begins.
    propagation_prob:
        Probability that a directly dependent service alerts. Decays by
        ``decay`` per additional hop.
    decay:
        Per-hop multiplier on ``propagation_prob``.
    hop_delay_s:
        Mean seconds added per hop before a downstream service notices.
    jitter_s:
        Uniform jitter applied to every alert timestamp.
    max_hops:
        Stop propagating past this many hops. ``None`` means no limit.
    repeats:
        Number of times each affected service re-alerts (flapping). 1 means a
        single alert per service.
    repeat_interval_s:
        Gap between repeated alerts from the same service.
    fanout:
        Maximum number of distinct alerts a single affected service raises.
        A failing service rarely trips exactly one check -- it trips the job
        monitor, then the error-rate monitor, then the queue-depth monitor, and
        data-quality checks fire once per affected instrument. Fanout is what
        turns a ten-alert trickle into the storm an operator actually faces,
        and it is the main lever on total volume.
    root_alert_delay_s:
        Seconds after ``start_minute`` before the root itself alerts. Some
        faults are detected downstream *first*, which is exactly why a
        "pick the earliest alert" heuristic fails.
    label:
        Human-readable incident label used in the UI.
    """

    root: str
    kind: str
    start_minute: int
    propagation_prob: float = 0.85
    decay: float = 0.82
    hop_delay_s: float = 45.0
    jitter_s: float = 12.0
    max_hops: int | None = None
    repeats: int = 1
    repeat_interval_s: float = 90.0
    fanout: int = 1
    root_alert_delay_s: float = 0.0
    label: str = ""


@dataclass(frozen=True)
class Scenario:
    """One simulated operational day (or window of one)."""

    key: str
    title: str
    summary: str
    faults: tuple[Fault, ...]
    window_start_minute: int
    window_end_minute: int
    noise_rate: float = 0.12
    teaches: str = ""
    seed: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# The eight
# --------------------------------------------------------------------------

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="vendor_price_file_late",
        title="Equity price file misses its window",
        summary=(
            "The primary equity price file does not land by 05:10. The price "
            "loader fails, pricing serves stale marks, risk limits evaluate "
            "against those marks and P&L flags a spike that is not real."
        ),
        faults=(
            Fault(
                root="vendor_px_equity",
                kind="data_missing",
                start_minute=310,
                propagation_prob=0.96,
                decay=0.93,
                hop_delay_s=110.0,
                fanout=9,
                label="Equity price file late",
            ),
        ),
        window_start_minute=290,
        window_end_minute=420,
        noise_rate=0.12,
        teaches="The baseline storm. Every engine should get this one.",
        seed=101,
        tags=("data", "morning", "baseline"),
    ),
    Scenario(
        key="fix_session_drop",
        title="FIX session to broker A drops mid-session",
        summary=(
            "The FIX gateway to executing broker A loses its session. Orders "
            "reject, the drop copy stops mirroring execution reports, fills go "
            "missing from trade capture and the blotter diverges from reality "
            "while portfolio managers are still trading."
        ),
        faults=(
            Fault(
                root="fix_gw_broker_a",
                kind="connectivity",
                start_minute=622,
                propagation_prob=0.88,
                decay=0.8,
                hop_delay_s=35.0,
                fanout=5,
                label="Broker A FIX session down",
            ),
        ),
        window_start_minute=600,
        window_end_minute=700,
        noise_rate=0.1,
        teaches="Narrow blast radius, high severity. Tests that severity is not confused with rootness.",
        seed=202,
        tags=("trading", "connectivity"),
    ),
    Scenario(
        key="db_pool_exhaustion",
        title="Primary database connection pool exhausted",
        summary=(
            "The primary database runs out of connections. Eight services fail "
            "within the same few seconds. Every one of them looks like a root "
            "cause on a timeline; only the dependency graph shows that they all "
            "sit on the same node."
        ),
        faults=(
            Fault(
                root="db_primary",
                kind="saturation",
                start_minute=548,
                propagation_prob=0.93,
                decay=0.9,
                hop_delay_s=4.0,      # near-simultaneous: this is the point
                jitter_s=3.0,
                fanout=4,
                label="DB connection pool exhausted",
            ),
        ),
        window_start_minute=530,
        window_end_minute=620,
        noise_rate=0.1,
        teaches=(
            "HARD. Propagation is nearly instantaneous, so temporal ordering "
            "carries almost no signal. Topology has to do the work."
        ),
        seed=303,
        tags=("infra", "hard", "saturation"),
    ),
    Scenario(
        key="airflow_dag_cascade",
        title="Scheduler fault stalls the morning DAG",
        summary=(
            "The Airflow scheduler stops dispatching. Every scheduled loader "
            "misses its window at once, so the symptoms look like four "
            "independent vendor problems rather than one infrastructure fault."
        ),
        faults=(
            Fault(
                root="scheduler_airflow",
                kind="scheduler",
                start_minute=268,
                propagation_prob=0.9,
                decay=0.88,
                hop_delay_s=60.0,
                fanout=4,
                label="Airflow scheduler not dispatching",
            ),
        ),
        window_start_minute=250,
        window_end_minute=400,
        noise_rate=0.12,
        teaches="Tests whether the engine prefers an infra root over several plausible data roots.",
        seed=404,
        tags=("infra", "scheduler", "morning"),
    ),
    Scenario(
        key="stale_fx_feed",
        title="FX rates go stale, P&L spikes on non-USD book",
        summary=(
            "The FX feed stops updating. Non-USD positions are marked at "
            "yesterday's rates. The loudest alert on the board is a P&L spike, "
            "which is three hops from the actual cause."
        ),
        faults=(
            Fault(
                root="vendor_fx",
                kind="data_wrong",
                start_minute=302,
                propagation_prob=0.85,
                decay=0.83,
                hop_delay_s=95.0,
                fanout=7,
                root_alert_delay_s=420.0,   # the vendor alert lands LAST
                label="FX rates stale",
            ),
        ),
        window_start_minute=290,
        window_end_minute=430,
        noise_rate=0.12,
        teaches=(
            "The root alerts *after* several of its symptoms. Kills any "
            "'earliest alert is the root' heuristic."
        ),
        seed=505,
        tags=("data", "hard", "morning"),
    ),
    Scenario(
        key="missed_corporate_action",
        title="Split not applied to a single name",
        summary=(
            "A 4-for-1 split is missing from the corporate actions file. One "
            "instrument shows an impossible overnight return. Two alerts total, "
            "sitting inside a day with ordinary background noise."
        ),
        faults=(
            Fault(
                root="vendor_corpactions",
                kind="data_wrong",
                start_minute=284,
                propagation_prob=0.55,
                decay=0.55,
                hop_delay_s=130.0,
                max_hops=3,
                fanout=1,
                label="Corporate action missing",
            ),
        ),
        window_start_minute=270,
        window_end_minute=430,
        noise_rate=0.30,          # deliberately noisy: the signal is small
        teaches="Tiny incident in a noisy window. Tests noise rejection, not clustering.",
        seed=606,
        tags=("data", "low-signal"),
    ),
    Scenario(
        key="flapping_latency",
        title="Message bus degrades and flaps",
        summary=(
            "The message bus degrades rather than fails. Latency crosses the "
            "threshold, recovers, and crosses again, seven times over forty "
            "minutes, while downstream services do the same."
        ),
        faults=(
            Fault(
                root="msg_bus",
                kind="degradation",
                start_minute=585,
                propagation_prob=0.8,
                decay=0.85,
                hop_delay_s=20.0,
                repeats=7,
                repeat_interval_s=330.0,
                fanout=2,
                label="Message bus latency flapping",
            ),
        ),
        window_start_minute=575,
        window_end_minute=680,
        noise_rate=0.1,
        teaches=(
            "HARD. One incident, many repeats. A naive engine reports seven "
            "incidents; a correct one reports one with a flap count."
        ),
        seed=707,
        tags=("infra", "hard", "flapping"),
    ),
    Scenario(
        key="two_independent",
        title="Two unrelated incidents overlap",
        summary=(
            "The locate file from the prime brokers is late while, in the same "
            "twenty minutes, the FIX gateway to broker B starts rejecting. The "
            "two share no dependency path and must not be merged."
        ),
        faults=(
            Fault(
                root="vendor_borrow",
                kind="data_missing",
                start_minute=340,
                propagation_prob=0.85,
                decay=0.8,
                hop_delay_s=70.0,
                fanout=4,
                label="Locate file late",
            ),
            Fault(
                root="fix_gw_broker_b",
                kind="connectivity",
                start_minute=347,
                propagation_prob=0.85,
                decay=0.8,
                hop_delay_s=40.0,
                fanout=4,
                label="Broker B rejecting orders",
            ),
        ),
        window_start_minute=325,
        window_end_minute=430,
        noise_rate=0.12,
        teaches=(
            "HARD. Overlapping in time, disjoint in topology. A time-window "
            "engine merges these and is wrong."
        ),
        seed=808,
        tags=("hard", "multi-incident"),
    ),
)


SCENARIO_INDEX: dict[str, Scenario] = {s.key: s for s in SCENARIOS}


def get_scenario(key: str) -> Scenario:
    """Look up a scenario by key."""
    try:
        return SCENARIO_INDEX[key]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(
            f"unknown scenario {key!r}; available: {sorted(SCENARIO_INDEX)}"
        ) from exc
