"""Copy generated scenario data into the deployable site folder.

The Python side writes to ``data/``, which is regenerable and gitignored. The
site reads from ``web/data/``, which IS committed, because Vercel deploys what
is in the repo and nothing runs Python at deploy time.

Keeping those two directories separate is deliberate: it means you can
regenerate and experiment freely without touching what is live, and a change to
the deployed demo is always an explicit `make site` plus a commit you can see in
a diff.

Ground truth is copied too. That is a choice worth being able to defend: this
is a portfolio project whose whole point is the measured scorecard, so a reader
should be able to check the answer key. If this were a real system the truth
files would never leave the evaluation environment.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Run from anywhere: `python3 tools/build_site_data.py` should not depend on cwd.
sys.path.insert(0, str(REPO))
SRC = REPO / "data"
DST = REPO / "web" / "data"


def _write_topology() -> None:
    """Export the dependency graph so the UI can draw it.

    Edge direction is preserved as ``{"from": dependent, "to": depends_on}``,
    matching the Python convention (``A -> B`` means A depends on B). The page
    must not silently flip it -- an arrow pointing the wrong way would make the
    root-cause story read backwards to anyone looking closely.
    """
    from exception_desk.topology import SERVICES, build_topology, blast_radius

    g = build_topology()
    payload = {
        "convention": "edge from->to means 'from depends on to'; to is UPSTREAM",
        "services": [
            {
                "name": s.name,
                "tier": s.tier,
                "criticality": s.criticality,
                "description": s.description,
                "blast_radius": blast_radius(g, s.name),
            }
            for s in SERVICES
        ],
        "edges": [{"from": a, "to": b} for a, b in g.edges()],
    }
    (DST / "topology.json").write_text(json.dumps(payload, indent=2))


def _write_sim() -> None:
    """Export the playable shifts, the answer key and the agent prompts.

    The answer key ships to the client because this is a single-player training
    simulator with no server-side session -- the findings and the debrief have
    to reach the browser eventually. Stated plainly in sim.shift_answers rather
    than pretended otherwise; a graded exam would need a real backend.
    """
    from exception_desk.sim import shift_manifest, shift_answers
    from exception_desk.agents import agents_manifest

    (DST / "shifts.json").write_text(json.dumps(shift_manifest(), indent=2))
    (DST / "shift_answers.json").write_text(json.dumps(shift_answers(), indent=2))
    (DST / "agents.json").write_text(json.dumps(agents_manifest(), indent=2))


def _write_incidents(scenarios: list[dict]) -> int:
    """Run the correlation engine if it exists yet, and export what it found.

    Written so `make site` works today: while ``assemble_incidents`` is still a
    stub it writes an empty result with a reason, and the page renders an
    honest empty state instead of breaking. The moment step 4 lands, the same
    command starts populating the incident panel with no further changes here.
    """
    from exception_desk.topology import build_topology

    g = build_topology()
    written = 0

    for meta in scenarios:
        key = meta["key"]
        payload = json.loads((SRC / f"{key}.alerts.json").read_text())
        out: dict = {"scenario": key, "incidents": [], "engine_ready": False, "groups": []}

        # Step 1 is implemented, so export the grouping even while step 4 is a
        # stub. It lets the page show "N alerts -> M signals" today, which is
        # the first half of the story and is genuinely working.
        try:
            from exception_desk.correlate import group_by_signature

            out["groups"] = [
                {"service": g.service, "alert_type": g.alert_type, "entity": g.entity,
                 "severity": g.severity, "count": g.count,
                 "first_minute": g.first_minute, "last_minute": g.last_minute,
                 "flapping": g.is_flapping, "alert_ids": g.alert_ids}
                for g in group_by_signature(payload["alerts"])
            ]
        except NotImplementedError:
            pass

        try:
            from exception_desk.correlate import assemble_incidents

            incidents = assemble_incidents(payload["alerts"], g)
            out["engine_ready"] = True
            out["incidents"] = [
                {
                    "incident_id": i.incident_id,
                    "root": i.root,
                    "confidence": round(i.confidence, 3),
                    "severity": i.severity,
                    "start_minute": i.start_minute,
                    "end_minute": i.end_minute,
                    "alert_ids": i.alert_ids,
                    "affected_services": i.affected_services,
                    "evidence": i.evidence,
                    "candidates": [
                        {"service": c.service, "score": round(c.score, 3),
                         "reasons": c.reasons}
                        for c in (i.candidates or [])
                    ],
                    "groups": [
                        {"service": grp.service, "alert_type": grp.alert_type,
                         "entity": grp.entity, "count": grp.count,
                         "first_minute": grp.first_minute,
                         "last_minute": grp.last_minute,
                         "flapping": grp.is_flapping}
                        for grp in (i.groups or [])
                    ],
                }
                for i in incidents
            ]
            written += 1
        except NotImplementedError as exc:
            out["reason"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface bugs, don't hide them
            out["reason"] = f"{type(exc).__name__}: {exc}"
        (DST / f"{key}.incidents.json").write_text(json.dumps(out, indent=2))

    return written


def build() -> dict:
    if not SRC.exists():
        raise SystemExit("no data/ directory -- run `make data` first")

    DST.mkdir(parents=True, exist_ok=True)
    for stale in DST.glob("*.json"):
        stale.unlink()

    from exception_desk.scenarios import SCENARIOS

    order = {s.key: i for i, s in enumerate(SCENARIOS)}
    files = sorted(SRC.glob("*.alerts.json"),
                   key=lambda f: order.get(f.name.removesuffix(".alerts.json"), 99))

    scenarios = []
    for alerts_file in files:
        key = alerts_file.name.removesuffix(".alerts.json")
        payload = json.loads(alerts_file.read_text())
        truth_file = SRC / f"{key}.truth.json"

        shutil.copy2(alerts_file, DST / alerts_file.name)
        if truth_file.exists():
            shutil.copy2(truth_file, DST / truth_file.name)

        scenarios.append(
            {
                "key": key,
                "title": payload.get("title", key),
                "summary": payload.get("summary", ""),
                "window": payload.get("window"),
                "n_alerts": len(payload.get("alerts", [])),
                "alerts_file": f"data/{alerts_file.name}",
                "truth_file": f"data/{truth_file.name}" if truth_file.exists() else None,
            }
        )

    scorecard = SRC / "scorecard.json"
    if scorecard.exists():
        shutil.copy2(scorecard, DST / "scorecard.json")

    _write_topology()
    _write_sim()
    n_correlated = _write_incidents(scenarios)

    manifest = {
        "generated_by": "make site",
        "synthetic": True,
        "has_scorecard": scorecard.exists(),
        "n_correlated": n_correlated,
        "scenarios": scenarios,
    }
    (DST / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    m = build()
    total = sum(s["n_alerts"] for s in m["scenarios"])
    print(f"wrote {len(m['scenarios'])} scenarios ({total} alerts) to web/data/")
    print(f"topology: {len(json.loads((DST / 'topology.json').read_text())['services'])} services")
    if m["n_correlated"]:
        print(f"incidents: correlated {m['n_correlated']}/{len(m['scenarios'])} scenarios")
    else:
        print("incidents: engine not implemented yet -- panel will show its empty state")
    if not m["has_scorecard"]:
        print("scorecard: none yet -- run `make eval` once the harness is written")
