"""Dev loop for `rank_root_candidates` -- the one function left to write.

The rest of the pipeline is done, so the only variable in the engine's accuracy
is how you score rootness. This harness holds everything else fixed and runs
whichever ranker you point it at across all eight scenarios, so the feedback
loop on that one function is two seconds instead of a full test run.

    python3 tools/rank_lab.py                 # compare the two baselines
    python3 tools/rank_lab.py --mine          # add your implementation
    python3 tools/rank_lab.py --mine --only stale_fx_feed --explain
    python3 tools/rank_lab.py --only flapping_latency --why   # the raw signals

Two baselines ship with it, and both are wrong on purpose:

``earliest``
    Whichever service alerted first is the root. This is the instinct every
    operator has and most naive correlation engines encode. ``stale_fx_feed``
    exists specifically to punish it: the vendor's own staleness check is
    slower than the downstream P&L check, so the root alerts seven minutes
    after its own symptoms.

``blast``
    Whichever service has the widest blast radius in the graph is the root.
    A pure structural prior with no reference to what actually alerted, so it
    will happily name ``db_primary`` for an incident ``db_primary`` had nothing
    to do with -- it is downstream of nothing and upstream of sixteen services,
    so it wins every burst it is anywhere near.

Your job is to beat both, and the interesting part of the interview answer is
*which* signal fixed *which* scenario. Run this before and after each weight
change and keep the numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exception_desk.correlate import AlertGroup, CorrelationConfig, RootCandidate  # noqa: E402
from exception_desk.evaluate import run_evaluation  # noqa: E402
from exception_desk.topology import blast_radius, downstream_of  # noqa: E402


# --------------------------------------------------------------------------
# Baselines -- deliberately naive, and useful precisely because they are
# --------------------------------------------------------------------------


def rank_earliest(burst: list[AlertGroup], graph, config=None) -> list[RootCandidate]:
    """The naive instinct: the first thing to alert is the cause."""
    first_seen: dict[str, float] = {}
    for g in burst:
        first_seen[g.service] = min(first_seen.get(g.service, 1e9), g.first_minute)

    # Tie-break on the name, not on dict order. Without it this baseline is
    # flaky: several services share a first-seen minute, the winner changes
    # between runs, and the number you are tuning against moves underneath you.
    ordered = sorted(first_seen.items(), key=lambda kv: (kv[1], kv[0]))
    span = (ordered[-1][1] - ordered[0][1]) or 1.0
    return [
        RootCandidate(
            service=svc,
            score=1.0 - (minute - ordered[0][1]) / span,
            reasons=[f"first alerted at minute {minute:.0f}"],
        )
        for svc, minute in ordered
    ]


def rank_blast(burst: list[AlertGroup], graph, config=None) -> list[RootCandidate]:
    """Pure structural prior: the widest blast radius wins."""
    services = {g.service for g in burst}
    radii = {s: blast_radius(graph, s) if s in graph else 0 for s in services}
    widest = max(radii.values()) or 1
    out = [
        RootCandidate(
            service=svc,
            score=r / widest,
            reasons=[f"{r} services downstream in the topology"],
        )
        for svc, r in radii.items()
    ]
    # Deterministic tie-break. msg_bus and fix_gw_broker_b have identical blast
    # radius in this topology, so without it flapping_latency flips between
    # right and wrong from run to run.
    out.sort(key=lambda c: (-c.score, c.service))
    return out


BASELINES = {"earliest": rank_earliest, "blast": rank_blast}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

COLS = ("scenario", "inc", "F1", "merge", "split", "root@1", "noise", "reduce")


def _row(s) -> str:
    return (
        f"{s.scenario:<26} {s.n_predicted_incidents}/{s.n_true_incidents:<4} "
        f"{s.pairwise_f1:<6.3f} {s.over_merge_rate:<6.2f} {s.over_split_rate:<6.2f} "
        f"{s.root_top1:<7.2f} {s.noise_rejection:<6.2f} {s.alert_reduction:>5.1f}x"
    )


def report(label: str, card, explain: bool = False) -> None:
    print(f"\n=== {label}")
    print(
        f"{'scenario':<26} {'inc':<5} {'F1':<6} {'merge':<6} {'split':<6} "
        f"{'root@1':<7} {'noise':<6} {'reduce':>6}"
    )
    print("-" * 78)
    for s in card.scenarios:
        print(_row(s))
        if explain:
            for note in s.notes:
                print(f"{'':<26} · {note}")
    print("-" * 78)
    print(
        f"{'macro':<26} {card.total_incidents:<5} {card.macro_f1:<6.3f} "
        f"{'':<6} {'':<6} {card.macro_root_top1:<7.2f} "
        f"{card.macro_noise_rejection:<6.2f} "
        f"{card.total_alerts / max(1, card.total_incidents):>5.1f}x"
    )


def why(key: str, config: CorrelationConfig) -> None:
    """Print the raw signals per candidate, per burst, for one scenario.

    Weight tuning without this is guesswork. With it you can see whether a
    scenario is losing because a weight is wrong or because the three signals
    genuinely cannot separate the top two -- which is a different problem and
    needs a different answer.
    """
    from exception_desk.correlate import (
        group_by_signature, split_by_topology, temporal_windows,
    )
    from exception_desk.evaluate import load_scenario
    from exception_desk.topology import build_topology

    graph = build_topology()
    alerts, truth = load_scenario("data", key)
    roots = {i["true_root"] for i in truth["incidents"]}
    print(f"\n=== signals: {key}   (true roots: {', '.join(sorted(roots))})")

    groups = group_by_signature(alerts["alerts"], config)
    for burst in temporal_windows(groups, config):
        for comp in split_by_topology(burst, graph):
            if len(comp) < config.min_groups_for_incident:
                continue
            services = {g.service for g in comp}
            first: dict[str, float] = {}
            fired: dict[str, int] = {}
            for g in comp:
                first[g.service] = min(first.get(g.service, 1e9), g.first_minute)
                fired[g.service] = fired.get(g.service, 0) + g.count
            lo, hi = min(first.values()), max(first.values())

            rows = []
            for s in services:
                others = services - {s}
                cover = len(downstream_of(graph, s) & others) / max(1, len(others))
                early = 1.0 if hi <= lo else 1 - (first[s] - lo) / (hi - lo)
                radius = blast_radius(graph, s) if s in graph else 0
                rows.append([s, cover, early, radius, fired[s]])
            widest = max(r[3] for r in rows) or 1
            for r in rows:
                r.append(config.w_upstream * r[1] + config.w_earliest * r[2]
                         + config.w_blast_radius * (r[3] / widest))
            rows.sort(key=lambda r: -r[5])

            print(f"\n  burst: {len(services)} services, minutes {lo:.0f}-{hi:.0f}")
            print(f"  {'service':<24}{'cover':>7}{'early':>7}{'blast':>7}"
                  f"{'fired':>7}{'score':>8}")
            for s, cover, early, radius, n, score in rows[:10]:
                mark = "  <-- TRUE ROOT" if s in roots else ""
                print(f"  {s:<24}{cover:>7.2f}{early:>7.2f}{radius:>7d}"
                      f"{n:>7d}{score:>8.3f}{mark}")
            if len(rows) > 1:
                gap = rows[0][5] - rows[1][5]
                print(f"  margin between top two: {gap:.3f}"
                      f"{'   ← near tie' if gap < 0.05 else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mine", action="store_true",
                    help="also run correlate.rank_root_candidates")
    ap.add_argument("--only", help="restrict to one scenario key")
    ap.add_argument("--explain", action="store_true",
                    help="print the per-scenario notes under each row")
    ap.add_argument("--baseline", choices=[*BASELINES, "none"], default=None,
                    help="run only this baseline")
    ap.add_argument("--why", action="store_true",
                    help="print the raw signals per candidate (needs --only)")
    args = ap.parse_args()

    if args.why:
        if not args.only:
            ap.error("--why needs --only <scenario>")
        why(args.only, CorrelationConfig())
        return 0

    keys = [args.only] if args.only else None
    picked = BASELINES if args.baseline in (None,) else (
        {} if args.baseline == "none" else {args.baseline: BASELINES[args.baseline]}
    )

    for name, fn in picked.items():
        report(f"baseline: {name}", run_evaluation("data", CorrelationConfig(), fn, keys),
               args.explain)

    if args.mine:
        from exception_desk.correlate import rank_root_candidates
        try:
            card = run_evaluation("data", CorrelationConfig(), rank_root_candidates, keys)
        except NotImplementedError:
            print("\n=== yours: rank_root_candidates is still a stub.")
            print("    See docs/BUILD.md step 3 and the docstring in correlate.py.")
            return 1
        report("yours: rank_root_candidates", card, args.explain)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
