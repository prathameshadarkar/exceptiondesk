# Decisions

One line per tuning change: what moved, what it bought, what it cost. Three or
four honest entries here are worth more than a clean commit history, because
what is being assessed is how you decide, not that you decided well the first
time.

Format: **what changed** — why, measured on what, what was given up.

---

## Pipeline (steps 1, 2, 4, 5)

**`burst_window_minutes` 4.0 → 6.0** — `missed_corporate_action` has a 4.11
minute gap between `ingest_corpaction_proc` erroring and `svc_risk` breaching.
At 4.0 the burst closed inside the incident, the orphaned tail fell below the
noise threshold, and the engine missed a real incident by a six-second margin
on an arbitrary constant. Cost: `two_independent`'s two incidents are 4.35
minutes apart, so they now share a burst and the topological split has to
separate them. That is the scenario's actual purpose, so the cost is really a
benefit — at 4.0 the engine was passing that test by luck.

**`min_groups_for_incident` 2 → 3** — at 2, any two background alerts that
happen to be topologically adjacent manufacture an incident. `vendor_refdata`
and `svc_security_master` fire 3.4 minutes apart in `vendor_price_file_late`
and are one hop apart in the graph; the engine reported them as a second
incident. Raising the bar was only safe *after* the burst-window fix, which
took `missed_corporate_action` from 2 groups to 4. Cost: a genuine two-alert
incident would now be dropped. This threshold is the noise-rejection / recall
trade-off in one number and is the first thing to move if that requirement
ever appears.

**Topological split by reachability, not by induced subgraph** — the service
linking two symptoms frequently does not alert. `svc_pricing` sits between the
price loader and the P&L spike and is often healthy by its own monitoring.
An induced subgraph on alerting services finds no edge and over-splits; the
reachability relation walks through the silent node. Cost: the relation is
O(n²) in burst size, which is irrelevant at this scale and would not be on a
real estate.

**No linking through a shared ancestor** — almost everything here is eventually
downstream of `db_primary`, so "shares an ancestor" collapses the topology into
one component and merges `two_independent` specifically. A common ancestor is
evidence of a common cause only when it is itself implicated, and if it is, it
alerts and joins the component on its own. Cost: two genuine symptoms of a
silent common cause are reported as two incidents. Acceptable — over-merging
hides a whole problem from the operator, over-splitting only annoys them.

**Prune each incident to what its root can explain** — measured on
`flapping_latency`, which runs fifty minutes and collects background alerts
inside its own window:

| | pairwise F1 | noise rejection |
|---|---|---|
| off, correct root | 1.000 | 0.45 |
| on, correct root | 1.000 | **1.00** |
| on, wrong root | 0.869 | 0.91 |

Free when the ranking is right, expensive when it is wrong. Cost, and it is a
real one: clustering quality now depends on ranking quality — a wrong root
shreds its own incident instead of merely mislabelling it. Kept because an
incident is a cause plus what it explains, and a root that explains none of its
symptoms should look broken rather than quietly plausible. Guard rail: pruning
never runs if it would take the incident below `min_groups_for_incident`.

**Confidence floor 0.55, ceiling 0.99** — a near-tie is a reading to check, not
a coin flip, so it does not go to zero. Nothing gets 1.00 because this is a
heuristic over a model of the estate, not the estate.

---

## Ranking (step 3)

_Yours. One line per weight change: what you moved, which scenario moved with
it, what you gave up._

Baselines to beat, from `make rank`:

| ranker | root@1 | fails on |
|---|---|---|
| `earliest` | 0.62 | `stale_fx_feed`, `db_pool_exhaustion`, `flapping_latency` |
| `blast` | 0.88 | `flapping_latency` |

Ceiling with a correct root everywhere, measured with an oracle: macro F1
1.000, root@1 1.00, noise rejection 1.00.

**Open, and the interesting one:** the three specified signals cannot separate
`flapping_latency`. `msg_bus` and `fix_gw_broker_b` have identical upstream
coverage (0.69) and identical blast radius (10), so the tiebreak falls to
earliness and a single background alert on `fix_gw_broker_b` wins it by 0.013.
Lowering `w_earliest` converts a narrow loss into a tie. The distinguishing
fact — `msg_bus` fired 12 times across the window, `fix_gw_broker_b` once — is
not read by any signal in the spec. Resolve this by extending the design, not
by tuning; then write down what you added and what it cost.

- **`w_...` X → Y** —
