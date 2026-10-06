"""Correlation engine -- YOUR HALF.

This is the part interviewers will drill, so it is the part you write. The
simulator, the topology and the metrics are done; the interfaces below are
fixed so the tests and the evaluation harness already know how to call you.

Read `docs/BUILD.md` before starting. Short version of the design:

    raw alerts
        -> group_by_signature()   collapse repeats/flaps into alert groups
        -> temporal_windows()     segment the stream into candidate bursts
        -> rank_root_candidates() score each service in a burst for rootness
        -> assemble_incidents()   emit incidents with evidence

Three rules that decide whether this is credible:

1. **No LLM in this file.** Classification and ranking are deterministic and
   inspectable. The language model writes prose about the output of this
   module; it never decides what the root cause is. If you can't explain a
   ranking by pointing at a number, an interviewer will find that out.

2. **Every incident carries evidence.** `Incident.evidence` is a list of plain
   strings a human can check ("db_primary is upstream of 7 of the 9 alerting
   services"). If you cannot generate the evidence string, the heuristic is
   not ready.

3. **Never read ground truth here.** It lives in a separate file for exactly
   this reason. Importing it in this module is cheating and the tests will
   not catch it -- only you will know.

The three scenarios that will break a naive implementation:

    db_pool_exhaustion  -- alerts arrive within seconds of each other, so
                           timestamps carry almost no ordering signal. The
                           graph has to resolve rootness.
    flapping_latency    -- one incident, seven repeats per service. If you
                           skip signature grouping you will report seven
                           incidents.
    two_independent     -- two incidents overlapping in time with disjoint
                           dependency paths. If you segment on time alone you
                           will merge them.

Start with `group_by_signature`. Get `flapping_latency` down to one incident
before you touch anything else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations

import networkx as nx

from exception_desk.topology import blast_radius, downstream_of, upstream_of


# --------------------------------------------------------------------------
# Types the rest of the project already depends on -- do not rename
# --------------------------------------------------------------------------


@dataclass
class AlertGroup:
    """A set of alerts treated as one signal.

    Repeated alerts from the same service for the same check are one thing
    happening, not N things. `count` is how many raw alerts collapsed in, and
    `first_minute` / `last_minute` bound the flap window.
    """

    key: str
    service: str
    alert_type: str
    entity: str | None
    severity: str
    count: int
    first_minute: float
    last_minute: float
    alert_ids: list[str] = field(default_factory=list)

    @property
    def is_flapping(self) -> bool:
        """More than two firings spread over more than five minutes."""
        return self.count > 2 and (self.last_minute - self.first_minute) > 5.0


@dataclass
class RootCandidate:
    """A service scored for how likely it is to be the root of a burst."""

    service: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Incident:
    """What the desk actually works: one cause, many symptoms."""

    incident_id: str
    root: str
    confidence: float
    severity: str
    start_minute: float
    end_minute: float
    alert_ids: list[str]
    affected_services: list[str]
    groups: list[AlertGroup] = field(default_factory=list)
    candidates: list[RootCandidate] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def alert_count(self) -> int:
        return len(self.alert_ids)


@dataclass
class CorrelationConfig:
    """Tunables. Defaults are a starting point, not an answer.

    Tune these against the evaluation harness, not by eyeballing the UI --
    and record what you changed and why. "I tuned the burst window from 4
    minutes to 90 seconds because db_pool_exhaustion was absorbing unrelated
    noise" is an interview answer. "It looked better" is not.
    """

    signature_window_minutes: float = 12.0

    #: Gap that closes a burst. Raised from 4.0 after measuring: in
    #: ``missed_corporate_action`` the gap between the loader erroring and risk
    #: breaching is 4.11 minutes, so a 4-minute window cut that incident in half
    #: and the orphaned tail was then dropped as noise — the engine missed a real
    #: incident because of a six-second margin on an arbitrary constant.
    #:
    #: Widening it also makes ``two_independent`` harder, which is the point:
    #: at 4.0 those two incidents were 4.35 minutes apart and time separated
    #: them by luck. At 6.0 they share a burst and the topology split has to
    #: earn the separation, which is what that scenario exists to test.
    burst_window_minutes: float = 6.0

    min_group_size: int = 1
    #: Weights for the three rootness signals. They should be defensible.
    w_upstream: float = 0.5
    w_earliest: float = 0.2
    w_blast_radius: float = 0.3

    #: An incident with fewer than this many groups is treated as noise.
    #: Raised from 2 once the burst window above stopped truncating
    #: ``missed_corporate_action`` (4 groups, comfortably clear of the bar).
    #: At 2, any two background alerts that happen to be topologically adjacent
    #: — ``vendor_refdata`` and ``svc_security_master``, 3.4 minutes apart in
    #: ``vendor_price_file_late`` — manufacture an incident out of nothing.
    #: This threshold IS the noise-rejection / recall trade-off; it is the first
    #: number to move if the engine ever has to find two-alert incidents.
    min_groups_for_incident: int = 3

    #: Keep only the groups the chosen root can actually explain — see
    #: :func:`_prune_to_root_reachable`. Measured on ``flapping_latency``, the
    #: scenario that has background alerts landing inside a fifty-minute window:
    #:
    #:     off, correct root : F1 1.000, noise rejection 0.45
    #:     on,  correct root : F1 1.000, noise rejection 1.00
    #:     on,  wrong root   : F1 0.869, noise rejection 0.91
    #:
    #: So it is free when the ranking is right and expensive when it is wrong,
    #: which is the behaviour worth having: a root that explains none of its
    #: own symptoms should look broken rather than quietly plausible.
    prune_to_root_reachable: bool = True


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

#: Lower number = more severe. P1 pages someone at 3am.
SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def _worst_severity(a: str, b: str) -> str:
    """Return the more severe of two severity labels."""
    return a if SEVERITY_ORDER.get(a, 9) <= SEVERITY_ORDER.get(b, 9) else b


# --------------------------------------------------------------------------
# Step 1 -- signature grouping  [WORKED EXAMPLE -- steps 2-4 follow this shape]
# --------------------------------------------------------------------------


def group_by_signature(
    alerts: list[dict],
    config: CorrelationConfig | None = None,
) -> list[AlertGroup]:
    """Collapse repeated firings of the same check into single signals.

    Two alerts share a signature when they have the same ``service``,
    ``alert_type`` and ``entity``. Alerts sharing a signature that fall within
    ``config.signature_window_minutes`` of the previous firing belong to the
    same group; a longer gap starts a new group, because a check that fires,
    clears for an hour and fires again is genuinely two events.

    Parameters
    ----------
    alerts:
        Raw alert dicts as written by the simulator, sorted by ``ts_minute``.
    config:
        Tunables; defaults used when ``None``.

    Returns
    -------
    list[AlertGroup]
        Sorted by ``first_minute``. Every input alert appears in exactly one
        group -- assert this, it is the easiest bug to ship.

    Notes
    -----
    This single function is the difference between reporting one incident and
    seven for ``flapping_latency``.

    Implementation notes, because the choices here are the interesting part:

    * The window is measured from the group's **last** firing, not its first.
      Measuring from the first splits any flap that outlasts the window --
      which is precisely the case this function exists to handle.
    * Sorting defensively rather than trusting the caller. The simulator does
      emit in time order, but a one-line sort removes a whole class of bug
      that would otherwise surface as a mysterious accuracy drop.
    * A group inherits the *worst* severity it ever saw. A check that flaps
      P3, P3, P1 is a P1 problem; taking the first or last severity would
      under-report it.
    """
    config = config or CorrelationConfig()

    # Defensive: the contract says sorted, but a silent ordering bug here would
    # show up three steps later as bad clustering and take an hour to find.
    ordered = sorted(alerts, key=lambda a: float(a["ts_minute"]))

    open_groups: dict[tuple[str, str, str | None], AlertGroup] = {}
    groups: list[AlertGroup] = []
    seq = 0

    for alert in ordered:
        signature = (alert["service"], alert["alert_type"], alert.get("entity"))
        minute = float(alert["ts_minute"])
        current = open_groups.get(signature)

        # Extend the open group if this firing is close enough to the LAST one.
        if current is not None and (minute - current.last_minute) <= config.signature_window_minutes:
            current.count += 1
            current.alert_ids.append(alert["alert_id"])
            current.last_minute = minute
            current.severity = _worst_severity(current.severity, alert["severity"])
            continue

        # Otherwise this is a new event: the check cleared and fired again.
        seq += 1
        service, alert_type, entity = signature
        group = AlertGroup(
            key=f"{service}|{alert_type}|{entity or '-'}#{seq}",
            service=service,
            alert_type=alert_type,
            entity=entity,
            severity=alert["severity"],
            count=1,
            first_minute=minute,
            last_minute=minute,
            alert_ids=[alert["alert_id"]],
        )
        groups.append(group)
        open_groups[signature] = group

    grouped = sum(g.count for g in groups)
    if grouped != len(alerts):  # pragma: no cover - invariant, not a branch
        raise AssertionError(
            f"grouping lost or duplicated alerts: {grouped} grouped vs {len(alerts)} input"
        )

    groups.sort(key=lambda g: g.first_minute)
    return groups


# --------------------------------------------------------------------------
# Step 2 -- temporal segmentation
# --------------------------------------------------------------------------


def temporal_windows(
    groups: list[AlertGroup],
    config: CorrelationConfig | None = None,
) -> list[list[AlertGroup]]:
    """Segment groups into candidate bursts by time.

    A burst ends when the gap to the next group exceeds
    ``config.burst_window_minutes``.

    This is deliberately crude, and it is *supposed* to be. Time alone cannot
    separate ``two_independent``; the topology step does that. Resist the urge
    to make this clever -- keep the cleverness where it can be explained.

    Returns
    -------
    list[list[AlertGroup]]
        Each inner list is one candidate burst, in time order.

    Notes
    -----
    The gap is measured against the burst's running **end** -- the latest
    ``last_minute`` seen so far -- not against the previous group's start. A
    flapping group can span twenty minutes on its own; measuring from its start
    would close the burst underneath it and strand every group that arrived
    while it was still firing.

    Nothing here looks at the topology, and that is deliberate. Time is a weak
    signal that is cheap and easy to explain; the dependency graph does the work
    that actually requires judgement, one step later. Keeping the two separate
    is what makes it possible to say which one caused a given mistake.
    """
    config = config or CorrelationConfig()
    if not groups:
        return []

    ordered = sorted(groups, key=lambda g: g.first_minute)
    bursts: list[list[AlertGroup]] = [[ordered[0]]]
    burst_end = ordered[0].last_minute

    for group in ordered[1:]:
        if (group.first_minute - burst_end) > config.burst_window_minutes:
            bursts.append([group])
            burst_end = group.last_minute
        else:
            bursts[-1].append(group)
            burst_end = max(burst_end, group.last_minute)

    return bursts


# --------------------------------------------------------------------------
# Step 3 -- rootness ranking (the core)
# --------------------------------------------------------------------------


def rank_root_candidates(
    burst: list[AlertGroup],
    graph: nx.DiGraph,
    config: CorrelationConfig | None = None,
) -> list[RootCandidate]:
    """Score every alerting service in a burst for how root-like it is.

    Three signals, combined with the weights in ``config``:

    **Upstream coverage** (``w_upstream``) -- of the other services alerting in
    this burst, what fraction are downstream of this one? Use
    :func:`exception_desk.topology.downstream_of`. This is the signal that
    resolves ``db_pool_exhaustion``, where eight services fail within seconds
    and only one of them sits under the rest.

    **Earliness** (``w_earliest``) -- normalised inverse of first-seen time
    within the burst. Weight it *low*. In ``stale_fx_feed`` the root alerts
    seven minutes after its own symptoms, so an engine that trusts timestamps
    gets that scenario wrong and cannot be talked out of it.

    **Blast radius** (``w_blast_radius``) -- total downstream count in the
    graph, normalised across candidates. A prior on structural importance,
    independent of what actually alerted.

    Every candidate must come back with ``reasons``: human-readable strings
    naming the numbers that produced the score. Those strings end up on screen
    and in the LLM's summary prompt, and they are what makes the ranking
    defensible rather than magic.

    Returns
    -------
    list[RootCandidate]
        Sorted by ``score`` descending, with a **deterministic tie-break** --
        sort on ``(-score, service)``, never on ``score`` alone. Services come
        out of a set, so a bare sort leaves ties to dict ordering and the same
        input ranks differently between runs. This is not hypothetical: in
        ``flapping_latency`` the top two are separated by 0.013 and the naive
        blast-radius baseline flipped between right and wrong on re-runs until
        its tie-break was pinned. Never empty for a non-empty burst.

    Notes
    -----
    Think about whether a service that did *not* alert can still be the root.
    It can -- the intermediate node's own check can miss while everything under
    it screams. Decide whether you support that, and be ready to say why.

    ------------------------------------------------------------------------
    THE SHAPE OF IT
    ------------------------------------------------------------------------
    Everything around this function is finished, so this is the only thing
    standing between the repo and a working scorecard. Concretely:

        services   = {g.service for g in burst}
        first_seen = earliest first_minute per service
        for each candidate service:
            coverage = |downstream_of(graph, svc) & (services - {svc})|
                       / max(1, len(services) - 1)
            earliness = 1 - (first_seen[svc] - min) / (max - min)   # guard /0
            radius    = blast_radius(graph, svc) / max over candidates
            score     = w_upstream*coverage + w_earliest*earliness
                        + w_blast_radius*radius
        sort by score descending, tie-break on something stable (service name)

    Return ``RootCandidate(service, score, reasons)`` for every candidate, not
    just the winner: ``root_top3`` reads the ranked list, ``_confidence()``
    reads the margin between the top two, and the UI shows the runner-up.

    ------------------------------------------------------------------------
    HOW TO KNOW IT WORKS
    ------------------------------------------------------------------------
        python3 tools/rank_lab.py --mine --explain

    Two baselines run alongside yours. Both are wrong on purpose and both are
    useful as a floor:

        earliest  root@1 = 0.62   the naive instinct; stale_fx_feed kills it
        blast     root@1 = 0.88   pure structural prior; flapping_latency
                                  kills it, because msg_bus has a narrower
                                  blast radius than fix_gw_broker_b, which is
                                  merely *noisy* during the same window

    With a correct root on every scenario the ceiling is macro F1 1.000,
    root@1 1.00, noise rejection 1.00 -- measured, not assumed. Anything less
    is your ranking, not the pipeline.

    ------------------------------------------------------------------------
    THE TRAPS, AND WHICH SCENARIO CATCHES EACH
    ------------------------------------------------------------------------
    * ``stale_fx_feed``     -- the root alerts 7 minutes AFTER its own
                               symptoms. If ``w_earliest`` is doing real work
                               here you will get this wrong and no amount of
                               tuning the other two will save it.
    * ``db_pool_exhaustion``-- 8 services inside 4 seconds. Earliness is noise
                               at this resolution; only coverage separates
                               ``db_primary`` from the seven under it.
    * ``flapping_latency``  -- the culprit is not the widest node in the graph.
                               Coverage over *what actually alerted* is what
                               distinguishes ``msg_bus`` here; blast radius
                               alone picks the wrong one.
    * ``two_independent``   -- already split before you see it. Each component
                               arrives separately, so do not try to handle
                               multiple roots in one burst.

    ------------------------------------------------------------------------
    NON-NEGOTIABLE
    ------------------------------------------------------------------------
    Every candidate carries ``reasons``: strings naming the numbers that
    produced the score, e.g. ``"upstream of 7 of 9 alerting services"``,
    ``"16 services downstream in the topology"``. They go on screen, they go
    into the model's prompt, and they are the difference between a ranking you
    can defend in a room and one you cannot. If you cannot write the string,
    the heuristic is not ready.

    And record what you changed. ``w_earliest`` moved from X to Y because
    ``stale_fx_feed`` did Z is an interview answer; "it looked better" is not.
    """
    raise NotImplementedError("you write this -- see docs/BUILD.md step 3")


# --------------------------------------------------------------------------
# Step 4 -- assembly
# --------------------------------------------------------------------------


#: Signature of a rootness ranker, so alternatives can be swapped in without
#: touching the pipeline. ``tools/rank_lab.py`` uses this to compare a candidate
#: implementation against a deliberately naive baseline.
Ranker = Callable[[list[AlertGroup], nx.DiGraph, "CorrelationConfig"], list[RootCandidate]]


def assemble_incidents(
    alerts: list[dict],
    graph: nx.DiGraph,
    config: CorrelationConfig | None = None,
    *,
    ranker: Ranker | None = None,
) -> list[Incident]:
    """Full pipeline: raw alerts in, incidents out.

    Steps 1-3, then:

    * **Split bursts that are topologically disjoint.** After ranking, check
      whether the burst's services form more than one weakly-connected
      component once you project them onto the dependency graph. If they do,
      it is more than one incident. This is the whole of ``two_independent``,
      and it is the step most implementations skip.
    * **Drop weak bursts as noise.** A burst with fewer than
      ``config.min_groups_for_incident`` groups is background, not an incident.
      Be careful: ``missed_corporate_action`` is a *real* incident with a very
      small footprint, so an aggressive threshold buys noise rejection at the
      cost of missing it. That trade-off is a good thing to have measured.
    * **Set confidence** from the margin between the top two root candidates.
      A burst where two services score nearly the same is one you want flagged
      for a human, not asserted at 99%.

    Returns
    -------
    list[Incident]
        Sorted by ``start_minute``. Alerts judged to be noise appear in no
        incident.
    """
    config = config or CorrelationConfig()
    rank = ranker or rank_root_candidates

    groups = group_by_signature(alerts, config)
    incidents: list[Incident] = []

    for burst in temporal_windows(groups, config):
        for component in split_by_topology(burst, graph):
            # Background chatter: a lone group with nothing causally attached to
            # it is one machine having a bad minute, not an incident. This is the
            # only place noise is rejected, and the threshold is the whole of the
            # noise-rejection / recall trade-off.
            if len(component) < config.min_groups_for_incident:
                continue

            candidates = rank(component, graph, config)
            if not candidates:
                continue

            members = _prune_to_root_reachable(component, candidates[0].service, graph, config)
            incidents.append(_build_incident(members, candidates, graph))

    incidents.sort(key=lambda i: i.start_minute)
    for n, inc in enumerate(incidents, start=1):
        inc.incident_id = f"INC{n:03d}"
    return incidents


# --------------------------------------------------------------------------
# Step 4 helpers
# --------------------------------------------------------------------------


def split_by_topology(
    burst: list[AlertGroup],
    graph: nx.DiGraph,
) -> list[list[AlertGroup]]:
    """Split one time-burst into topologically connected components.

    Two alerting services belong to the same incident only when one of them can
    actually cause the other -- that is, when a directed dependency path runs
    between them in either direction. Group the burst by that relation and take
    the connected components.

    This is the entirety of ``two_independent``: a late locate file and a broken
    FIX session, seven minutes apart, are adjacent in time and unreachable from
    each other in the graph.

    Notes
    -----
    Two decisions worth being able to defend:

    * **Relation, not induced subgraph.** Taking the subgraph induced on the
      alerting services and finding its components would over-split constantly,
      because the service that actually links two symptoms frequently does not
      alert itself -- ``svc_pricing`` sits between the loader and the P&L spike
      and may be perfectly healthy from its own monitoring's point of view. The
      reachability relation walks through silent intermediaries; the induced
      subgraph cannot.
    * **No linking through a shared ancestor.** ``svc_locate`` and
      ``svc_blotter`` are both, eventually, downstream of ``db_primary``.
      Treating "shares an ancestor" as a link would merge almost everything in
      this topology into one component, and would merge ``two_independent``
      specifically. A common ancestor is only evidence of a common cause when
      that ancestor is itself implicated, and if it is, it alerts and joins the
      component on its own.
    """
    if len(burst) <= 1:
        return [list(burst)] if burst else []

    services = {g.service for g in burst}
    # Reachability once per service, not once per pair.
    reach: dict[str, set[str]] = {}
    for svc in services:
        if svc in graph:
            reach[svc] = downstream_of(graph, svc) | upstream_of(graph, svc)
        else:  # a service the topology does not know: it links to nothing
            reach[svc] = set()

    related = nx.Graph()
    related.add_nodes_from(services)
    for a, b in combinations(sorted(services), 2):
        if b in reach[a] or a in reach[b]:
            related.add_edge(a, b)

    # Sorted, because nx.connected_components yields sets in an order that is
    # not stable across runs. Everything downstream of here -- incident ids,
    # which component gets pruned first -- would inherit that instability.
    components = sorted(
        (sorted(c) for c in nx.connected_components(related)),
        key=lambda c: (min(g.first_minute for g in burst if g.service in set(c)), c[0]),
    )
    if len(components) <= 1:
        return [list(burst)]

    by_service: dict[str, int] = {}
    for idx, comp in enumerate(components):
        for svc in comp:
            by_service[svc] = idx

    out: list[list[AlertGroup]] = [[] for _ in components]
    for group in burst:
        out[by_service[group.service]].append(group)
    return [c for c in out if c]


def _prune_to_root_reachable(
    component: list[AlertGroup],
    root: str,
    graph: nx.DiGraph,
    config: CorrelationConfig,
) -> list[AlertGroup]:
    """Keep only the groups the chosen root can actually explain.

    Connectivity puts a group in the component; *reachability from the root*
    decides whether it stays. A group whose service is neither the root nor
    downstream of it is something the root cannot have caused, so asserting it
    as a symptom would be a claim the graph does not support.

    Why this exists, concretely: ``flapping_latency`` runs for fifty minutes,
    and background alerts land inside that window on services that are connected
    to *some* member of the component without being caused by ``msg_bus`` at all
    — ``ingest_px_loader`` is upstream of ``svc_blotter``, which is a genuine
    symptom, so connectivity alone drags it in. Reachability evicts it.

    The trade-off, which is real and worth stating: this makes clustering
    quality depend on ranking quality. A wrong root now shreds its own incident
    rather than merely mislabelling it. That is deliberate — an incident is a
    cause plus what it explains, and a root that explains none of its members
    should look broken rather than quietly plausible.

    Guard rails: pruning never runs if it would empty the incident or take it
    below ``min_groups_for_incident``. In those cases the unpruned component is
    kept, because losing an incident is worse than carrying a stray alert.
    """
    if not config.prune_to_root_reachable:
        return component

    explainable = downstream_of(graph, root) | {root} if root in graph else {root}
    kept = [g for g in component if g.service in explainable]

    if len(kept) < max(1, config.min_groups_for_incident):
        return component
    return kept


def _build_incident(
    component: list[AlertGroup],
    candidates: list[RootCandidate],
    graph: nx.DiGraph,
) -> Incident:
    """Turn a ranked component into the record the desk works.

    ``incident_id`` is assigned by the caller once everything is time-ordered,
    so that ids read in the order an operator met them.
    """
    alert_ids = [aid for g in component for aid in g.alert_ids]
    services = sorted({g.service for g in component})
    severity = "P4"
    for g in component:
        severity = _worst_severity(severity, g.severity)

    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    confidence = _confidence(candidates)

    # Evidence is not decoration. It is the reason this module is allowed to
    # assert a root cause at all: every line below is a number a human can go
    # and check, and none of them came from a language model.
    evidence = [
        f"{len(alert_ids)} alerts from {len(services)} services collapsed into one incident",
        f"root candidate: {top.service} (score {top.score:.2f})",
    ]
    evidence.extend(top.reasons)
    if runner_up is not None:
        evidence.append(
            f"runner-up {runner_up.service} scored {runner_up.score:.2f} "
            f"— confidence {confidence:.0%} reflects that margin"
        )
    else:
        evidence.append("no competing candidate in this component")

    flapping = [g for g in component if g.is_flapping]
    if flapping:
        worst = max(flapping, key=lambda g: g.count)
        evidence.append(
            f"{worst.service} flapped {worst.count}× over "
            f"{worst.last_minute - worst.first_minute:.0f} minutes — collapsed to one signal"
        )

    if top.service in graph and top.service not in services:
        evidence.append(
            f"{top.service} did not alert itself; it is implicated by sitting "
            f"upstream of {len(services)} services that did"
        )

    return Incident(
        incident_id="INC000",          # replaced by the caller after sorting
        root=top.service,
        confidence=confidence,
        severity=severity,
        start_minute=min(g.first_minute for g in component),
        end_minute=max(g.last_minute for g in component),
        alert_ids=alert_ids,
        affected_services=services,
        groups=list(component),
        candidates=candidates,
        evidence=evidence,
    )


def _confidence(candidates: list[RootCandidate]) -> float:
    """Confidence from the margin between the top two candidates.

    A burst where two services score within a few percent of each other is one
    an operator should look at, not one to assert at 99%. The floor of 0.55
    says "this is the better of two close readings"; the ceiling of 0.99 refuses
    to ever claim certainty, because the engine is a heuristic over a graph and
    the graph is a model of the estate, not the estate.

    A single candidate gets 0.80: there is nothing competing with it, but there
    is also nothing corroborating it.
    """
    if not candidates:
        return 0.0
    if len(candidates) == 1:
        return 0.80

    top, second = candidates[0].score, candidates[1].score
    if top <= 0:
        return 0.55
    margin = max(0.0, (top - second) / top)
    return round(min(0.99, 0.55 + 0.44 * margin), 3)


# --------------------------------------------------------------------------
# Convenience wrapper used by the UI exporter -- already written
# --------------------------------------------------------------------------


def correlate_scenario(alerts_payload: dict, graph: nx.DiGraph,
                       config: CorrelationConfig | None = None) -> list[Incident]:
    """Run the engine over a scenario payload loaded from ``data/*.alerts.json``."""
    return assemble_incidents(alerts_payload["alerts"], graph, config)
