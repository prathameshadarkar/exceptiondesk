"""Evaluation metrics.

The metric primitives here are written and tested, because a subtly wrong
metric produces confident numbers that mean nothing, and you would never find
out. The *harness* -- wiring the engine across all eight scenarios and
producing the scorecard -- is yours to write (see :func:`run_evaluation`).

What gets measured, and why each one is here:

``pairwise_f1``
    Clustering quality without needing to match cluster labels. For every pair
    of non-noise alerts, the truth says "same incident or not" and so does the
    engine; precision and recall follow. Chosen over adjusted Rand index
    because precision and recall decompose into the two failure modes that
    actually matter on a desk, and you can say which one you are trading away.

``over_merge_rate``
    Fraction of predicted incidents containing alerts from more than one true
    incident. This is the ``two_independent`` failure: two unrelated problems
    presented to the operator as one, so the second one goes unworked.

``over_split_rate``
    Fraction of true incidents spread across more than one predicted incident.
    This is the ``flapping_latency`` failure: seven pages for one problem.

``root_top_k``
    Of the true incidents the engine found at all, how often is the real root
    its first (or top-3) guess. The number an operator actually feels.

``noise_rejection``
    Fraction of genuine background alerts correctly left out of every
    incident. Cheap to game by rejecting everything, which is why it is
    reported next to recall and never alone.

``alert_reduction``
    Raw alerts divided by incidents surfaced. The headline number -- and the
    least meaningful one on its own, since collapsing everything into a single
    incident scores beautifully and helps nobody. Always quote it with
    ``pairwise_f1``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path


# --------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------


@dataclass
class ScenarioScore:
    """Metrics for a single scenario."""

    scenario: str
    n_alerts: int
    n_true_incidents: int
    n_predicted_incidents: int
    pairwise_precision: float
    pairwise_recall: float
    pairwise_f1: float
    over_merge_rate: float
    over_split_rate: float
    root_top1: float
    root_top3: float
    noise_rejection: float
    alert_reduction: float
    notes: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    """The whole run. This is what the UI's scorecard tab renders."""

    scenarios: list[ScenarioScore]

    @property
    def macro_f1(self) -> float:
        return _mean([s.pairwise_f1 for s in self.scenarios])

    @property
    def macro_root_top1(self) -> float:
        return _mean([s.root_top1 for s in self.scenarios])

    @property
    def macro_noise_rejection(self) -> float:
        return _mean([s.noise_rejection for s in self.scenarios])

    @property
    def total_alerts(self) -> int:
        return sum(s.n_alerts for s in self.scenarios)

    @property
    def total_incidents(self) -> int:
        return sum(s.n_predicted_incidents for s in self.scenarios)

    def to_dict(self) -> dict:
        return {
            "macro_f1": round(self.macro_f1, 4),
            "macro_root_top1": round(self.macro_root_top1, 4),
            "macro_noise_rejection": round(self.macro_noise_rejection, 4),
            "total_alerts": self.total_alerts,
            "total_incidents": self.total_incidents,
            "scenarios": [vars(s) for s in self.scenarios],
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# --------------------------------------------------------------------------
# Metric primitives -- written and tested
# --------------------------------------------------------------------------


def pairwise_scores(
    true_labels: dict[str, str | None],
    pred_labels: dict[str, str | None],
) -> tuple[float, float, float]:
    """Pairwise precision, recall and F1 over co-membership.

    Only alerts that are genuinely part of an incident (``true_labels[a]`` is
    not ``None``) take part, so noise handling is scored separately by
    :func:`noise_rejection` rather than being smuggled in here.

    A pair counts as a true positive when truth and prediction agree the two
    alerts belong together; a false positive when only the prediction says so;
    a false negative when only the truth does.
    """
    ids = [a for a, lab in true_labels.items() if lab is not None]
    tp = fp = fn = 0
    for a, b in combinations(ids, 2):
        same_true = true_labels[a] == true_labels[b]
        pa, pb = pred_labels.get(a), pred_labels.get(b)
        same_pred = pa is not None and pa == pb
        if same_true and same_pred:
            tp += 1
        elif same_pred and not same_true:
            fp += 1
        elif same_true and not same_pred:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def over_merge_rate(
    true_labels: dict[str, str | None],
    pred_labels: dict[str, str | None],
) -> float:
    """Fraction of predicted incidents mixing more than one true incident."""
    buckets: dict[str, set[str]] = {}
    for alert_id, pred in pred_labels.items():
        if pred is None:
            continue
        truth = true_labels.get(alert_id)
        if truth is None:
            continue
        buckets.setdefault(pred, set()).add(truth)
    if not buckets:
        return 0.0
    merged = sum(1 for members in buckets.values() if len(members) > 1)
    return merged / len(buckets)


def over_split_rate(
    true_labels: dict[str, str | None],
    pred_labels: dict[str, str | None],
) -> float:
    """Fraction of true incidents spread across more than one prediction."""
    buckets: dict[str, set[str]] = {}
    for alert_id, truth in true_labels.items():
        if truth is None:
            continue
        pred = pred_labels.get(alert_id)
        if pred is None:
            continue
        buckets.setdefault(truth, set()).add(pred)
    if not buckets:
        return 0.0
    split = sum(1 for members in buckets.values() if len(members) > 1)
    return split / len(buckets)


def root_accuracy(
    true_incidents: list[dict],
    predicted: list,
    true_labels: dict[str, str | None],
    k: int = 3,
) -> tuple[float, float]:
    """Top-1 and top-k root accuracy.

    Each true incident is matched to the predicted incident sharing the most
    alerts with it. An unmatched true incident scores zero on both -- missing
    an incident is worse than guessing its root wrong, and the metric should
    say so.

    ``predicted`` holds :class:`~exception_desk.correlate.Incident` objects;
    only ``.root``, ``.alert_ids`` and ``.candidates`` are read here.
    """
    if not true_incidents:
        return 0.0, 0.0

    pred_sets = [(p, set(p.alert_ids)) for p in predicted]
    top1_hits = topk_hits = 0

    for inc in true_incidents:
        members = {
            a for a, lab in true_labels.items() if lab == inc["incident_id"]
        }
        best, best_overlap = None, 0
        for p, ids in pred_sets:
            overlap = len(members & ids)
            if overlap > best_overlap:
                best, best_overlap = p, overlap
        if best is None:
            continue
        if best.root == inc["true_root"]:
            top1_hits += 1
        ranked = [c.service for c in (best.candidates or [])][:k] or [best.root]
        if inc["true_root"] in ranked:
            topk_hits += 1

    n = len(true_incidents)
    return top1_hits / n, topk_hits / n


def noise_rejection(
    true_labels: dict[str, str | None],
    pred_labels: dict[str, str | None],
) -> float:
    """Fraction of true background alerts correctly left out of all incidents."""
    noise_ids = [a for a, lab in true_labels.items() if lab is None]
    if not noise_ids:
        return 1.0
    rejected = sum(1 for a in noise_ids if pred_labels.get(a) is None)
    return rejected / len(noise_ids)


# --------------------------------------------------------------------------
# Loading helpers -- written
# --------------------------------------------------------------------------


def load_scenario(data_dir: Path | str, key: str) -> tuple[dict, dict]:
    """Return ``(alerts_payload, truth_payload)`` for one scenario."""
    d = Path(data_dir)
    alerts = json.loads((d / f"{key}.alerts.json").read_text())
    truth = json.loads((d / f"{key}.truth.json").read_text())
    return alerts, truth


def true_label_map(truth_payload: dict) -> dict[str, str | None]:
    """``{alert_id: incident_id or None}`` from a truth payload."""
    return {row["alert_id"]: row["incident_id"] for row in truth_payload["alerts"]}


def predicted_label_map(incidents: list) -> dict[str, str]:
    """``{alert_id: incident_id}`` from engine output. Unlisted alerts are noise."""
    out: dict[str, str] = {}
    for inc in incidents:
        for alert_id in inc.alert_ids:
            out[alert_id] = inc.incident_id
    return out


# --------------------------------------------------------------------------
# The harness -- YOUR HALF
# --------------------------------------------------------------------------


def score_scenario(key: str, data_dir: Path | str, graph, config=None,
                   ranker=None) -> ScenarioScore:
    """Run the engine over one scenario and compute every metric above.

    Steps:

    1. :func:`load_scenario`
    2. :func:`exception_desk.correlate.assemble_incidents` on the alerts
    3. :func:`true_label_map` / :func:`predicted_label_map`
    4. Feed those into the primitives and fill a :class:`ScenarioScore`

    Keep it boring. The primitives are tested; this function's only job is not
    to mix up which map goes where. Add a ``notes`` entry whenever a metric
    lands somewhere surprising -- future you will want to know what you saw.
    """
    from exception_desk.correlate import assemble_incidents

    alerts_payload, truth_payload = load_scenario(data_dir, key)
    raw = alerts_payload["alerts"]
    incidents = assemble_incidents(raw, graph, config, **(
        {"ranker": ranker} if ranker is not None else {}
    ))

    truth = true_label_map(truth_payload)
    pred = predicted_label_map(incidents)

    precision, recall, f1 = pairwise_scores(truth, pred)
    top1, top3 = root_accuracy(truth_payload["incidents"], incidents, truth)

    score = ScenarioScore(
        scenario=key,
        n_alerts=len(raw),
        n_true_incidents=len(truth_payload["incidents"]),
        n_predicted_incidents=len(incidents),
        pairwise_precision=round(precision, 4),
        pairwise_recall=round(recall, 4),
        pairwise_f1=round(f1, 4),
        over_merge_rate=round(over_merge_rate(truth, pred), 4),
        over_split_rate=round(over_split_rate(truth, pred), 4),
        root_top1=round(top1, 4),
        root_top3=round(top3, 4),
        noise_rejection=round(noise_rejection(truth, pred), 4),
        alert_reduction=round(len(raw) / len(incidents), 2) if incidents else 0.0,
        notes=_notes_for(key, truth_payload, incidents, precision, recall),
    )
    return score


def _notes_for(
    key: str,
    truth_payload: dict,
    incidents: list,
    precision: float,
    recall: float,
) -> list[str]:
    """Flag the things a human should look at before believing the row.

    A scorecard that only reports numbers invites you to read the good ones and
    skip the rest. These notes are the engine saying where it is uncomfortable.
    """
    notes: list[str] = []
    n_true = len(truth_payload["incidents"])
    n_pred = len(incidents)

    if n_pred != n_true:
        notes.append(
            f"predicted {n_pred} incidents against {n_true} in truth"
        )
    if precision < recall - 0.05:
        notes.append("precision below recall: merging things that do not belong together")
    elif recall < precision - 0.05:
        notes.append("recall below precision: splitting one problem across several incidents")

    missed = _missed_incidents(truth_payload, incidents)
    for inc in missed:
        notes.append(
            f"missed {inc['incident_id']} entirely "
            f"({len(inc['member_alert_ids'])} alerts, root {inc['true_root']})"
        )

    wrong = _wrong_roots(truth_payload, incidents)
    for true_root, called in wrong:
        notes.append(f"root called as {called}, truth is {true_root}")

    low = [i for i in incidents if i.confidence < 0.65]
    if low:
        notes.append(f"{len(low)} of {n_pred} incidents flagged low-confidence for a human")
    return notes


def _missed_incidents(truth_payload: dict, incidents: list) -> list[dict]:
    """True incidents with no predicted incident sharing any alert."""
    covered = {aid for inc in incidents for aid in inc.alert_ids}
    return [
        inc for inc in truth_payload["incidents"]
        if not (set(inc["member_alert_ids"]) & covered)
    ]


def _wrong_roots(truth_payload: dict, incidents: list) -> list[tuple[str, str]]:
    """``(true_root, predicted_root)`` for each matched incident we got wrong."""
    out: list[tuple[str, str]] = []
    pred_sets = [(p, set(p.alert_ids)) for p in incidents]
    for inc in truth_payload["incidents"]:
        members = set(inc["member_alert_ids"])
        best, best_overlap = None, 0
        for p, ids in pred_sets:
            overlap = len(members & ids)
            if overlap > best_overlap:
                best, best_overlap = p, overlap
        if best is not None and best.root != inc["true_root"]:
            out.append((inc["true_root"], best.root))
    return out


def run_evaluation(data_dir: Path | str = "data", config=None,
                   ranker=None, keys: list[str] | None = None) -> Scorecard:
    """Score every scenario and return the card the UI renders.

    Write the per-scenario numbers to ``data/scorecard.json`` as well -- the
    web app reads that file directly, and having it in git means you can see
    a regression in a diff.

    Scenario order comes from ``scenarios.SCENARIOS`` rather than from globbing
    the directory, so the scorecard's rows are stable across machines and a diff
    shows a changed number instead of a reordered file.
    """
    from exception_desk.scenarios import SCENARIOS
    from exception_desk.topology import build_topology

    graph = build_topology()
    order = keys if keys is not None else [s.key for s in SCENARIOS]

    scores: list[ScenarioScore] = []
    for key in order:
        if not (Path(data_dir) / f"{key}.alerts.json").exists():
            continue
        scores.append(score_scenario(key, data_dir, graph, config, ranker))
    return Scorecard(scenarios=scores)
