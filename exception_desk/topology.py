"""Service dependency topology for a systematic trading operation.

The graph is the backbone of the whole project. Correlation without topology is
just time-clustering, and time-clustering cannot tell an upstream cause from a
downstream symptom -- see scenario `db_pool_exhaustion`, where eight services
fail inside the same four seconds and only the graph reveals which one matters.

Edge direction convention (important, and easy to get backwards):

    G.add_edge(A, B)  means  "A depends on B"

So B is UPSTREAM of A. A fault at B propagates to A. Propagation therefore
walks *incoming* edges (``G.predecessors``), and root-cause search walks
*outgoing* edges (``G.successors``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

# --------------------------------------------------------------------------
# Node taxonomy
# --------------------------------------------------------------------------

TIERS = (
    "vendor",      # external data providers
    "ingest",      # loaders that pull vendor data into the platform
    "infra",       # database, message bus, scheduler -- everything sits on these
    "compute",     # pricing, security master, positions
    "risk",        # risk engine, P&L
    "trading",     # OMS/EMS, FIX gateways, drop copy, trade capture
    "post_trade",  # blotter, recon, locates
    "reporting",   # client and internal reporting
)

# Alert classes an operator would actually see on a monitoring stack.
ALERT_TYPES = (
    "JOB_FAILED",
    "JOB_LATE",
    "STALE_DATA",
    "CONN_LOST",
    "SEQ_GAP",
    "HIGH_LATENCY",
    "ERROR_RATE",
    "QUEUE_DEPTH",
    "RISK_BREACH",
    "PNL_SPIKE",
    "RECON_BREAK",
    "REJECT_RATE",
    "DISK_PRESSURE",
    "DQ_OUTLIER",
)


@dataclass(frozen=True)
class Service:
    """A node in the topology.

    Attributes
    ----------
    name:
        Stable identifier used everywhere else (alerts, ground truth, the UI).
    tier:
        One of :data:`TIERS`. Used for layout and for tier-aware heuristics.
    emits:
        Alert types this service is capable of raising. The simulator only ever
        emits an alert type a node actually declares, which keeps generated
        data structurally honest.
    criticality:
        1 (highest) to 4. Drives alert severity and, in the UI, sort order.
    sla_minute:
        Minutes after midnight by which this service is expected to have
        completed its scheduled run, or ``None`` for services that are always
        on. Only batch/ingest nodes carry one.
    """

    name: str
    tier: str
    emits: tuple[str, ...]
    criticality: int = 3
    sla_minute: int | None = None
    description: str = ""


# --------------------------------------------------------------------------
# The services
# --------------------------------------------------------------------------

SERVICES: tuple[Service, ...] = (
    # --- vendor feeds ------------------------------------------------------
    Service("vendor_px_equity", "vendor", ("JOB_LATE", "STALE_DATA", "CONN_LOST"),
            criticality=1, sla_minute=310,
            description="Equity price and returns file from primary market data vendor"),
    Service("vendor_px_futures", "vendor", ("JOB_LATE", "STALE_DATA", "CONN_LOST"),
            criticality=2, sla_minute=310,
            description="Futures settlement price file"),
    Service("vendor_fx", "vendor", ("JOB_LATE", "STALE_DATA", "CONN_LOST"),
            criticality=1, sla_minute=300,
            description="FX spot and forward rates"),
    Service("vendor_corpactions", "vendor", ("JOB_LATE", "STALE_DATA"),
            criticality=2, sla_minute=280,
            description="Corporate actions: splits, dividends, spin-offs"),
    Service("vendor_refdata", "vendor", ("JOB_LATE", "STALE_DATA"),
            criticality=2, sla_minute=270,
            description="Security reference data and identifier mappings"),
    Service("vendor_borrow", "vendor", ("JOB_LATE", "STALE_DATA"),
            criticality=2, sla_minute=330,
            description="Prime broker locate availability and borrow rate file"),

    # --- infrastructure ----------------------------------------------------
    Service("db_primary", "infra", ("ERROR_RATE", "HIGH_LATENCY", "DISK_PRESSURE", "QUEUE_DEPTH"),
            criticality=1,
            description="Primary transactional database"),
    Service("db_replica", "infra", ("HIGH_LATENCY", "STALE_DATA", "DISK_PRESSURE"),
            criticality=3,
            description="Read replica serving reporting and research"),
    Service("msg_bus", "infra", ("QUEUE_DEPTH", "HIGH_LATENCY", "ERROR_RATE"),
            criticality=1,
            description="Internal message bus carrying fills, positions and alerts"),
    Service("scheduler_airflow", "infra", ("JOB_FAILED", "JOB_LATE", "ERROR_RATE"),
            criticality=1,
            description="Airflow scheduler running BOD/EOD DAGs"),

    # --- ingest ------------------------------------------------------------
    Service("ingest_px_loader", "ingest", ("JOB_FAILED", "JOB_LATE", "ERROR_RATE"),
            criticality=1, sla_minute=320,
            description="Loads vendor price files into the pricing store"),
    Service("ingest_fx_loader", "ingest", ("JOB_FAILED", "JOB_LATE", "ERROR_RATE"),
            criticality=1, sla_minute=315,
            description="Loads FX rates"),
    Service("ingest_corpaction_proc", "ingest", ("JOB_FAILED", "ERROR_RATE", "DQ_OUTLIER"),
            criticality=2, sla_minute=295,
            description="Applies corporate actions to positions and historical prices"),
    Service("ingest_refdata_loader", "ingest", ("JOB_FAILED", "JOB_LATE", "ERROR_RATE"),
            criticality=2, sla_minute=285,
            description="Loads security master reference data"),
    Service("ingest_borrow_loader", "ingest", ("JOB_FAILED", "JOB_LATE"),
            criticality=2, sla_minute=340,
            description="Loads the daily locate file from prime brokers"),

    # --- compute -----------------------------------------------------------
    Service("svc_security_master", "compute", ("ERROR_RATE", "STALE_DATA", "DQ_OUTLIER"),
            criticality=1,
            description="Canonical security identifiers and instrument terms"),
    Service("svc_pricing", "compute", ("STALE_DATA", "ERROR_RATE", "DQ_OUTLIER", "HIGH_LATENCY"),
            criticality=1,
            description="Serves marks to risk, P&L and the blotter"),
    Service("svc_position", "compute", ("ERROR_RATE", "STALE_DATA", "HIGH_LATENCY"),
            criticality=1,
            description="Real-time position keeping across strategies"),

    # --- risk and P&L ------------------------------------------------------
    Service("svc_risk", "risk", ("RISK_BREACH", "STALE_DATA", "ERROR_RATE"),
            criticality=1,
            description="Pre- and post-trade risk limits per strategy"),
    Service("svc_pnl", "risk", ("PNL_SPIKE", "STALE_DATA", "ERROR_RATE"),
            criticality=1,
            description="Intraday and end-of-day P&L attribution"),

    # --- trading -----------------------------------------------------------
    Service("fix_gw_broker_a", "trading", ("CONN_LOST", "SEQ_GAP", "REJECT_RATE", "HIGH_LATENCY"),
            criticality=1,
            description="FIX gateway to executing broker A"),
    Service("fix_gw_broker_b", "trading", ("CONN_LOST", "SEQ_GAP", "REJECT_RATE", "HIGH_LATENCY"),
            criticality=1,
            description="FIX gateway to executing broker B"),
    Service("svc_dropcopy", "trading", ("SEQ_GAP", "CONN_LOST", "STALE_DATA"),
            criticality=1,
            description="Drop copy session mirroring execution reports to ops and books"),
    Service("svc_ems", "trading", ("ERROR_RATE", "HIGH_LATENCY", "REJECT_RATE"),
            criticality=1,
            description="Execution management: algos, venue routing"),
    Service("svc_oms", "trading", ("ERROR_RATE", "HIGH_LATENCY", "REJECT_RATE", "QUEUE_DEPTH"),
            criticality=1,
            description="Order management and book of record"),

    # --- post trade --------------------------------------------------------
    Service("svc_trade_capture", "post_trade", ("JOB_FAILED", "ERROR_RATE", "QUEUE_DEPTH"),
            criticality=1,
            description="Captures fills into the trade database"),
    Service("svc_blotter", "post_trade", ("STALE_DATA", "ERROR_RATE", "DQ_OUTLIER"),
            criticality=1,
            description="The blotter portfolio managers actually look at"),
    Service("svc_recon", "post_trade", ("RECON_BREAK", "JOB_FAILED", "JOB_LATE"),
            criticality=1, sla_minute=1050,
            description="Reconciles internal trades against executing broker files"),
    Service("svc_locate", "post_trade", ("JOB_FAILED", "STALE_DATA"),
            criticality=2, sla_minute=345,
            description="Locate coverage checks for short sales (Reg SHO)"),

    # --- reporting ---------------------------------------------------------
    Service("svc_reporting", "reporting", ("JOB_FAILED", "JOB_LATE", "STALE_DATA"),
            criticality=3, sla_minute=1140,
            description="Internal and investor reporting pack"),
)


# --------------------------------------------------------------------------
# The dependencies: (dependent, depends_on)
# --------------------------------------------------------------------------

DEPENDENCIES: tuple[tuple[str, str], ...] = (
    # ingest depends on its vendor feed and on the scheduler
    ("ingest_px_loader", "vendor_px_equity"),
    ("ingest_px_loader", "vendor_px_futures"),
    ("ingest_px_loader", "scheduler_airflow"),
    ("ingest_px_loader", "db_primary"),
    ("ingest_fx_loader", "vendor_fx"),
    ("ingest_fx_loader", "scheduler_airflow"),
    ("ingest_fx_loader", "db_primary"),
    ("ingest_corpaction_proc", "vendor_corpactions"),
    ("ingest_corpaction_proc", "scheduler_airflow"),
    ("ingest_corpaction_proc", "db_primary"),
    ("ingest_refdata_loader", "vendor_refdata"),
    ("ingest_refdata_loader", "scheduler_airflow"),
    ("ingest_refdata_loader", "db_primary"),
    ("ingest_borrow_loader", "vendor_borrow"),
    ("ingest_borrow_loader", "scheduler_airflow"),

    # compute
    ("svc_security_master", "ingest_refdata_loader"),
    ("svc_security_master", "db_primary"),
    ("svc_pricing", "ingest_px_loader"),
    ("svc_pricing", "ingest_fx_loader"),
    ("svc_pricing", "ingest_corpaction_proc"),
    ("svc_pricing", "svc_security_master"),
    ("svc_pricing", "db_primary"),
    ("svc_position", "svc_trade_capture"),
    ("svc_position", "svc_security_master"),
    ("svc_position", "db_primary"),
    ("svc_position", "msg_bus"),

    # risk and P&L sit on pricing and positions
    ("svc_risk", "svc_pricing"),
    ("svc_risk", "svc_position"),
    ("svc_risk", "msg_bus"),
    ("svc_pnl", "svc_pricing"),
    ("svc_pnl", "svc_position"),
    ("svc_pnl", "db_primary"),

    # trading
    ("svc_ems", "fix_gw_broker_a"),
    ("svc_ems", "fix_gw_broker_b"),
    ("svc_ems", "msg_bus"),
    ("svc_oms", "svc_ems"),
    ("svc_oms", "svc_risk"),
    ("svc_oms", "svc_security_master"),
    ("svc_oms", "db_primary"),
    ("svc_dropcopy", "fix_gw_broker_a"),
    ("svc_dropcopy", "fix_gw_broker_b"),
    ("svc_dropcopy", "msg_bus"),

    # post trade
    ("svc_trade_capture", "svc_dropcopy"),
    ("svc_trade_capture", "msg_bus"),
    ("svc_trade_capture", "db_primary"),
    ("svc_blotter", "svc_trade_capture"),
    ("svc_blotter", "svc_pricing"),
    ("svc_blotter", "svc_position"),
    ("svc_recon", "svc_trade_capture"),
    ("svc_recon", "scheduler_airflow"),
    ("svc_recon", "db_primary"),
    ("svc_locate", "ingest_borrow_loader"),
    ("svc_locate", "svc_security_master"),

    # reporting
    ("svc_reporting", "svc_pnl"),
    ("svc_reporting", "svc_position"),
    ("svc_reporting", "db_replica"),
    ("svc_reporting", "scheduler_airflow"),
    ("db_replica", "db_primary"),
)


# --------------------------------------------------------------------------
# Graph construction and helpers
# --------------------------------------------------------------------------

_SERVICE_INDEX: dict[str, Service] = {s.name: s for s in SERVICES}


def get_service(name: str) -> Service:
    """Return the :class:`Service` record for ``name``."""
    try:
        return _SERVICE_INDEX[name]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"unknown service {name!r}") from exc


def build_topology() -> nx.DiGraph:
    """Build the dependency graph.

    Returns a :class:`networkx.DiGraph` where an edge ``A -> B`` means
    "A depends on B". Node attributes mirror the :class:`Service` fields.
    """
    g = nx.DiGraph()
    for svc in SERVICES:
        g.add_node(
            svc.name,
            tier=svc.tier,
            emits=svc.emits,
            criticality=svc.criticality,
            sla_minute=svc.sla_minute,
            description=svc.description,
        )
    for dependent, depends_on in DEPENDENCIES:
        if dependent not in g or depends_on not in g:
            raise ValueError(f"dependency references unknown service: {dependent} -> {depends_on}")
        g.add_edge(dependent, depends_on)

    if not nx.is_directed_acyclic_graph(g):
        cycle = nx.find_cycle(g)
        raise ValueError(f"topology must be acyclic; found cycle {cycle}")
    return g


def downstream_of(g: nx.DiGraph, node: str) -> set[str]:
    """Every service that would be affected by a fault at ``node``.

    Walks *incoming* edges, because ``A -> B`` reads "A depends on B".
    """
    return nx.ancestors(g, node)


def upstream_of(g: nx.DiGraph, node: str) -> set[str]:
    """Every service ``node`` itself depends on, transitively."""
    return nx.descendants(g, node)


def blast_radius(g: nx.DiGraph, node: str) -> int:
    """Number of services downstream of ``node``. Used to rank root candidates."""
    return len(downstream_of(g, node))


def depth_from_node(g: nx.DiGraph, root: str) -> dict[str, int]:
    """Hop count from ``root`` to each downstream service.

    Returns ``{service: hops}`` including ``root`` itself at 0. Services not
    downstream of ``root`` are omitted.
    """
    reverse = g.reverse(copy=False)
    return dict(nx.single_source_shortest_path_length(reverse, root))


@dataclass
class TopologyStats:
    """Summary used in the README and the UI's topology panel."""

    n_services: int
    n_dependencies: int
    tiers: dict[str, int] = field(default_factory=dict)
    widest_blast_radius: tuple[str, int] = ("", 0)


def describe(g: nx.DiGraph) -> TopologyStats:
    """Compute headline numbers about the graph."""
    tiers: dict[str, int] = {}
    for _, data in g.nodes(data=True):
        tiers[data["tier"]] = tiers.get(data["tier"], 0) + 1
    widest = max(((n, blast_radius(g, n)) for n in g.nodes), key=lambda kv: kv[1])
    return TopologyStats(
        n_services=g.number_of_nodes(),
        n_dependencies=g.number_of_edges(),
        tiers=tiers,
        widest_blast_radius=widest,
    )


if __name__ == "__main__":  # pragma: no cover
    graph = build_topology()
    stats = describe(graph)
    print(f"services      : {stats.n_services}")
    print(f"dependencies  : {stats.n_dependencies}")
    print(f"tiers         : {stats.tiers}")
    print(f"widest blast  : {stats.widest_blast_radius[0]} "
          f"({stats.widest_blast_radius[1]} services downstream)")
