"""Tests for the metric primitives. These pass today.

If you change anything in evaluate.py's primitives, these are your safety net.
A metric that silently breaks is worse than no metric.
"""

from exception_desk.evaluate import (
    noise_rejection,
    over_merge_rate,
    over_split_rate,
    pairwise_scores,
)


def test_perfect_clustering_scores_one():
    true = {"a": "I1", "b": "I1", "c": "I2", "d": "I2"}
    pred = {"a": "P1", "b": "P1", "c": "P2", "d": "P2"}
    p, r, f1 = pairwise_scores(true, pred)
    assert (p, r, f1) == (1.0, 1.0, 1.0)


def test_everything_in_one_bucket_has_perfect_recall_poor_precision():
    """The degenerate 'merge it all' strategy. Recall 1, precision low.

    This is exactly why alert_reduction is never quoted on its own.
    """
    true = {"a": "I1", "b": "I1", "c": "I2", "d": "I2"}
    pred = {"a": "P1", "b": "P1", "c": "P1", "d": "P1"}
    p, r, f1 = pairwise_scores(true, pred)
    assert r == 1.0
    assert p < 0.5
    assert f1 < 1.0


def test_everything_separate_has_zero_recall():
    true = {"a": "I1", "b": "I1"}
    pred = {"a": "P1", "b": "P2"}
    p, r, f1 = pairwise_scores(true, pred)
    assert r == 0.0
    assert f1 == 0.0


def test_noise_excluded_from_pairwise():
    """Noise alerts must not influence clustering precision."""
    true = {"a": "I1", "b": "I1", "n": None}
    pred = {"a": "P1", "b": "P1", "n": "P1"}
    p, r, f1 = pairwise_scores(true, pred)
    assert (p, r, f1) == (1.0, 1.0, 1.0)


def test_over_merge_detects_two_incidents_in_one_bucket():
    true = {"a": "I1", "b": "I2"}
    pred = {"a": "P1", "b": "P1"}
    assert over_merge_rate(true, pred) == 1.0


def test_over_merge_zero_when_clean():
    true = {"a": "I1", "b": "I2"}
    pred = {"a": "P1", "b": "P2"}
    assert over_merge_rate(true, pred) == 0.0


def test_over_split_detects_one_incident_across_two_buckets():
    true = {"a": "I1", "b": "I1"}
    pred = {"a": "P1", "b": "P2"}
    assert over_split_rate(true, pred) == 1.0


def test_noise_rejection_counts_only_true_noise():
    true = {"a": "I1", "n1": None, "n2": None}
    pred = {"a": "P1", "n1": "P1"}          # n1 wrongly absorbed, n2 correctly dropped
    assert noise_rejection(true, pred) == 0.5


def test_noise_rejection_is_one_when_there_is_no_noise():
    true = {"a": "I1"}
    pred = {"a": "P1"}
    assert noise_rejection(true, pred) == 1.0
