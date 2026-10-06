"""The shift simulator.

You are the operations analyst on shift. An incident opens, alerts land, and a
clock runs. Every move costs simulated minutes, and while minutes pass, damage
accrues: portfolio managers trade on stale marks, shorts go on without locates,
fills stay missing from the blotter.

The design argument, which is the thing worth defending in an interview:

**The scenario content IS the domain knowledge.** Anyone can build a timer with
buttons. What makes this hard to fake is knowing that a missing locate file
matters before the open and is merely annoying after it; that a P&L spike three
hops downstream of a stale FX feed is a symptom and chasing it wastes the
window; that a connection-pool exhaustion looks like eight independent failures
and is one. Each scenario below encodes a real failure mode, the checks that
actually distinguish it from its neighbours, and what it costs to be slow.

**Scoring is deterministic where it can be.** Time-to-root-cause, whether the
PMs were warned before the impact threshold, and whether the scenario's required
action was taken are all computed, not judged. Only the handover note is graded
by a model, because prose quality is the one thing a rule cannot measure -- and
even there the model is given an explicit checklist of what the next region
needed, so its judgement is checkable.

Checks are deterministic and their findings are fixed. The model is never asked
what a check returns; it is asked to write about what the checks found.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """A diagnostic the analyst can run. Deterministic: same input, same finding.

    ``decisive`` marks the check that actually distinguishes this failure mode
    from its plausible neighbours. Scoring rewards finding it, because on a real
    desk the skill is knowing which check to run first, not running all of them.
    """

    id: str
    label: str
    cost_minutes: int
    finding: str
    decisive: bool = False
    available_after: int = 0     # minutes into the shift
    red_herring: bool = False    # a reasonable check that does not narrow it down


@dataclass(frozen=True)
class ImpactStage:
    """What has gone wrong by a given minute if the analyst has not acted."""

    at_minute: int
    headline: str
    detail: str
    severity: str                # "warning" | "serious" | "critical"


@dataclass(frozen=True)
class RequiredAction:
    """Something the shift genuinely required, checked at scoring time."""

    id: str
    label: str
    by_minute: int | None
    why: str


@dataclass(frozen=True)
class Shift:
    """One playable scenario."""

    key: str
    title: str
    opens_at: str
    brief: str
    true_root: str
    budget_minutes: int
    checks: tuple[Check, ...]
    impacts: tuple[ImpactStage, ...]
    required: tuple[RequiredAction, ...]
    handover_must_cover: tuple[str, ...]
    decoys: tuple[str, ...] = field(default_factory=tuple)
    debrief: str = ""


# --------------------------------------------------------------------------
# The shifts
# --------------------------------------------------------------------------

SHIFTS: tuple[Shift, ...] = (
    Shift(
        key="vendor_price_file_late",
        title="Equity price file misses its window",
        opens_at="05:12 New York",
        brief=(
            "You picked up the shift twelve minutes ago. Pricing is flagging stale "
            "marks, risk has thrown a limit breach on EQ_STAT_ARB, and P&L is "
            "showing a 6-sigma move that nobody on the desk believes. The open is "
            "in two hours and eighteen minutes."
        ),
        true_root="vendor_px_equity",
        budget_minutes=45,
        checks=(
            Check("file_landed", "Check whether the vendor price file landed", 3,
                  "No equity price file in the vendor drop for today. Yesterday's is still "
                  "the newest. Expected 05:10, now 05:15.", decisive=True),
            Check("loader_logs", "Read the price loader logs", 4,
                  "Loader started on schedule, found no input file, exited non-zero at 05:11. "
                  "No parse errors — it never had anything to parse."),
            Check("pnl_detail", "Drill into the P&L spike", 6,
                  "The spike is entirely in instruments whose last mark is yesterday's close. "
                  "It is an artefact of stale pricing, not a real move.", red_herring=True),
            Check("vendor_status", "Check the vendor's status page and open a ticket", 5,
                  "Vendor acknowledges a delayed publication affecting all clients in the "
                  "region. Their ETA is 05:55 — forty minutes before our cut-off."),
            Check("risk_breach", "Investigate the risk limit breach", 5,
                  "Limit evaluated against stale marks. The breach is not real, but risk will "
                  "keep re-firing it every cycle until pricing is current.", red_herring=True),
            Check("fallback_source", "Check whether the secondary price source is usable", 4,
                  "Secondary source has today's closes for 94% of the book. Coverage gap is "
                  "in small-cap names held by one strategy."),
        ),
        impacts=(
            ImpactStage(12, "Risk is re-firing", "The false breach re-fires every cycle and "
                        "is training the desk to ignore risk alerts.", "warning"),
            ImpactStage(25, "PMs are looking at bad marks",
                        "Two portfolio managers have opened the blotter. Nobody has told them "
                        "the marks are yesterday's.", "serious"),
            ImpactStage(40, "Pre-open checks will fail",
                        "Without current marks the pre-open risk run cannot complete, and the "
                        "desk opens blind.", "critical"),
        ),
        required=(
            RequiredAction("notify_pm", "Tell the PMs the marks are stale", 25,
                           "They are about to size positions off yesterday's closes."),
            RequiredAction("vendor_ticket", "Open a ticket with the vendor", None,
                           "You need their ETA on the record before you can promise the desk anything."),
        ),
        handover_must_cover=(
            "the vendor file was late and whether it has landed by end of shift",
            "that the risk breach and P&L spike were artefacts, not real",
            "the vendor's ETA and the pre-open cut-off it has to beat",
            "whether the secondary source was used and which names it does not cover",
        ),
        decoys=("svc_pnl", "svc_risk", "svc_pricing"),
        debrief=(
            "The loudest alerts — the risk breach and the P&L spike — were three hops "
            "downstream of the cause and were both false. The decisive check was the "
            "cheapest one on the board: look at whether the file arrived."
        ),
    ),
    Shift(
        key="db_pool_exhaustion",
        title="Everything fails at once",
        opens_at="09:08 New York",
        brief=(
            "Eight services went red inside four seconds. Trade capture, positions, "
            "pricing, risk, the blotter, recon, the OMS and reporting are all "
            "erroring. Three portfolio managers are already asking what is going on. "
            "The market is open and they are trading."
        ),
        true_root="db_primary",
        budget_minutes=30,
        checks=(
            Check("conn_pool", "Check database connection pool utilisation", 3,
                  "Pool at 200/200, saturated since 09:07:51. Queue depth climbing. "
                  "A reporting job opened 140 connections and has not released them.",
                  decisive=True),
            Check("timeline", "Order the failures by first error timestamp", 4,
                  "All eight land within 3.8 seconds. Ordering is inside the noise — "
                  "you cannot tell cause from effect this way.", red_herring=True),
            Check("common_dep", "Look at what the failing services have in common", 3,
                  "All eight depend on db_primary. Nothing else is shared by more than "
                  "four of them."),
            Check("restart_oms", "Restart the OMS", 8,
                  "OMS comes back and immediately fails again on the same database "
                  "timeout. You have spent eight minutes and learned what you "
                  "already knew.", red_herring=True),
            Check("slow_query", "Pull the slow query log", 5,
                  "One analytics query against the trade table has been running 11 "
                  "minutes without a limit clause. It is holding the connections."),
            Check("fix_capacity", "Kill the offending session", 4,
                  "Connections released, pool drains to 40/200, services recover in "
                  "sequence over ninety seconds."),
        ),
        impacts=(
            ImpactStage(4, "Blotter is diverging", "Fills are arriving but not being "
                        "captured. What PMs see is already wrong.", "serious"),
            ImpactStage(10, "PMs are trading on a stale book",
                        "Position keeping is behind. Anyone sizing off the blotter is "
                        "sizing off a book that is minutes old and getting older.", "critical"),
            ImpactStage(20, "Risk is not evaluating",
                        "Pre-trade risk checks are failing open or failing closed depending "
                        "on the strategy. Neither is acceptable during the session.", "critical"),
        ),
        required=(
            RequiredAction("notify_pm", "Tell the PMs the book is unreliable", 10,
                           "They are trading. This is the one message that cannot wait."),
            RequiredAction("escalate_eng", "Escalate to engineering", 15,
                           "You can identify the cause but you are not the one who fixes it."),
        ),
        handover_must_cover=(
            "that a single database saturation caused all eight failures",
            "what was killed or restarted, and when services recovered",
            "which trades captured late and need reconciling against the broker",
            "that the unbounded reporting query will recur unless someone fixes it",
        ),
        decoys=("svc_oms", "svc_trade_capture", "svc_position", "svc_recon"),
        debrief=(
            "This is the scenario that punishes timeline-first thinking. Eight failures "
            "inside four seconds carry no ordering signal at all; the only thing that "
            "separates cause from symptom is what they share underneath."
        ),
    ),
    Shift(
        key="fix_session_drop",
        title="Broker A goes quiet",
        opens_at="10:22 New York",
        brief=(
            "The FIX session to executing broker A has stopped acknowledging. Orders "
            "are rejecting, the drop copy has gone silent, and one portfolio manager "
            "says a fill he watched execute is not on his blotter. He is mid-programme "
            "and wants to know whether to keep going."
        ),
        true_root="fix_gw_broker_a",
        budget_minutes=25,
        checks=(
            Check("session_state", "Check the FIX session state and sequence numbers", 2,
                  "Session logged out at 10:19:40. Our outbound sequence is 84,112; last "
                  "acknowledged by the broker is 84,067. Forty-five messages unconfirmed.",
                  decisive=True),
            Check("dropcopy", "Compare the drop copy against the OMS", 4,
                  "Drop copy stops at 10:19:38. Three executions are in the OMS with no "
                  "corresponding drop-copy confirmation."),
            Check("broker_call", "Call the broker's trading desk", 5,
                  "They confirm a gateway failover on their side at 10:19. They are up "
                  "on the secondary and can resend from sequence 84,067."),
            Check("venue_status", "Check exchange connectivity", 3,
                  "Exchange links are healthy. Every other broker session is fine. The "
                  "problem is specific to this counterparty.", red_herring=True),
            Check("reroute", "Reroute the working programme to broker B", 6,
                  "Broker B has capacity and the strategy is approved for it. The "
                  "in-flight order has to be cancelled and re-entered, not transferred."),
            Check("position_check", "Reconstruct true position from execution reports", 5,
                  "Three fills totalling 41,200 shares are confirmed executed but not "
                  "captured. The PM's blotter understates his position."),
        ),
        impacts=(
            ImpactStage(5, "The PM is flying blind",
                        "He is watching a blotter that is missing fills and deciding "
                        "whether to keep working the order.", "serious"),
            ImpactStage(12, "Unconfirmed messages are ageing",
                        "Forty-five unacknowledged messages. Every minute makes the resend "
                        "and the reconciliation messier.", "serious"),
            ImpactStage(20, "Risk of double execution",
                        "If the PM re-enters what he thinks is missing and the original "
                        "fills confirm, the position doubles.", "critical"),
        ),
        required=(
            RequiredAction("notify_pm", "Give the PM his true position", 8,
                           "He is deciding whether to re-enter. Wrong number, doubled position."),
            RequiredAction("broker_escalate", "Escalate to the broker for a resend", 12,
                           "Only they can replay the missing execution reports."),
        ),
        handover_must_cover=(
            "the session dropped, when, and at what sequence number",
            "the three fills that executed but were not captured, and whether they are booked",
            "whether the programme was rerouted and to whom",
            "that the recon against broker A will break tonight and why",
        ),
        decoys=("svc_dropcopy", "svc_oms", "svc_trade_capture"),
        debrief=(
            "The severity was obvious and the root was narrow — the risk here is not "
            "misdiagnosis, it is being slow to give the PM a number he can act on. "
            "A position he cannot trust is more dangerous than an outage he knows about."
        ),
    ),
    Shift(
        key="stale_fx_feed",
        title="A P&L spike that is not real",
        opens_at="05:48 New York",
        brief=(
            "P&L is showing an 8-sigma move on FX_CARRY and a 4-sigma on MULTI_ASSET. "
            "Nothing in the overnight news supports it. The head of the FX strategy has "
            "already messaged asking whether he lost money overnight."
        ),
        true_root="vendor_fx",
        budget_minutes=35,
        checks=(
            Check("fx_timestamps", "Check the age of the FX rates in use", 3,
                  "Every non-USD rate carries yesterday's 17:00 timestamp. The FX feed "
                  "has not updated since.", decisive=True),
            Check("pnl_attrib", "Attribute the P&L move", 5,
                  "The entire move is in non-USD instruments. USD-denominated positions "
                  "show nothing unusual."),
            Check("positions", "Verify positions are correct", 4,
                  "Positions reconcile cleanly against yesterday's close plus overnight "
                  "fills. The position keeping is fine.", red_herring=True),
            Check("fx_vendor", "Check the FX vendor connection", 4,
                  "Connection is up and the vendor believes it is publishing. Our consumer "
                  "has not committed a new message since 17:00 yesterday."),
            Check("consumer_lag", "Check the FX consumer's offset", 4,
                  "Consumer is stuck on a message it cannot parse — a new currency pair "
                  "was added to the feed and the schema rejects it. Everything behind it "
                  "is queued."),
            Check("equity_marks", "Check whether equity marks are also stale", 3,
                  "Equity pricing is current. This is confined to FX.", red_herring=True),
        ),
        impacts=(
            ImpactStage(10, "A PM believes he lost money",
                        "The FX strategy head is acting on a number that is an artefact.", "serious"),
            ImpactStage(22, "Risk is sizing off bad rates",
                        "Overnight risk limits for non-USD books are evaluated against "
                        "yesterday's rates.", "serious"),
            ImpactStage(30, "The official P&L run will use the bad rates",
                        "If this is not fixed before the EOD run, a wrong P&L goes out to "
                        "investors and has to be restated.", "critical"),
        ),
        required=(
            RequiredAction("notify_pm", "Tell the FX PM the move is an artefact", 12,
                           "He is making decisions — possibly de-risking — on a number that is wrong."),
            RequiredAction("escalate_eng", "Escalate the parse failure to engineering", 20,
                           "A schema change on the vendor side needs a code fix, not an ops fix."),
        ),
        handover_must_cover=(
            "the FX feed was stale and the P&L move was not real",
            "the cause was a schema rejection on a newly added pair",
            "whether rates were backfilled and from when",
            "that the EOD P&L run must not go out on stale rates",
        ),
        decoys=("svc_pnl", "svc_pricing", "ingest_fx_loader"),
        debrief=(
            "The root alerted after its own symptoms — the P&L check is faster than the "
            "vendor's own staleness check. Anyone who worked this by following the loudest "
            "or earliest alert spent the window in the wrong place."
        ),
    ),
    Shift(
        key="two_independent",
        title="Two problems, one window",
        opens_at="05:40 New York",
        brief=(
            "The locate file from the prime brokers has not loaded, and in the same "
            "twenty minutes broker B has started rejecting orders. It is tempting to "
            "assume these are related. The short book needs locates before the open."
        ),
        true_root="vendor_borrow",
        budget_minutes=40,
        checks=(
            Check("locate_file", "Check the locate file load", 3,
                  "No locate file from two of three prime brokers. The third delivered "
                  "on time and covers 60% of the intended short list.", decisive=True),
            Check("broker_b_rejects", "Check the broker B reject reason", 3,
                  "Rejects carry 'invalid account mapping' — a reference data problem on "
                  "their side, unrelated to locates. Separate incident.", decisive=True),
            Check("shared_path", "Check whether the two share any dependency", 4,
                  "They share nothing. Locates come through the borrow loader; broker B "
                  "rejects originate at their gateway. No common upstream."),
            Check("coverage", "Work out short coverage against the intended book", 5,
                  "Forty per cent of intended shorts are uncovered, concentrated in two "
                  "hard-to-borrow names."),
            Check("pb_call", "Call the prime brokers for the missing files", 6,
                  "One is re-sending now, ETA 15 minutes. The other had a batch failure "
                  "and cannot deliver before the open."),
            Check("single_locates", "Source single-name locates for the gap", 8,
                  "Both hard-to-borrow names are available at elevated rates from the "
                  "third PB. Needs sign-off on the borrow cost."),
        ),
        impacts=(
            ImpactStage(15, "Short orders will fail pre-trade checks",
                        "Uncovered names will be blocked at the open, and the strategy "
                        "will run half-dressed.", "serious"),
            ImpactStage(28, "Reg SHO exposure",
                        "If an uncovered short reaches the market the firm has a "
                        "compliance problem, not just a trading one.", "critical"),
            ImpactStage(35, "Broker B still rejecting",
                        "Working the locate problem alone means the second incident has "
                        "gone unworked for half an hour.", "serious"),
        ),
        required=(
            RequiredAction("declare_two", "Treat these as two separate incidents", 20,
                           "Merging them means one of them goes unworked."),
            RequiredAction("notify_pm", "Tell the PMs which names are uncovered", 25,
                           "They need to know what they cannot short before the open, not after."),
        ),
        handover_must_cover=(
            "that these were two unrelated incidents, not one",
            "which shorts ended up uncovered and what was sourced",
            "the borrow cost sign-off if single-name locates were taken",
            "that broker B's account mapping is still broken and who owns it",
        ),
        decoys=("fix_gw_broker_b", "svc_locate", "ingest_borrow_loader"),
        debrief=(
            "Two incidents overlapping in time and disjoint in topology. The failure mode "
            "here is not misdiagnosis, it is tunnel vision: working the louder problem and "
            "letting the quieter one run unattended for the whole shift."
        ),
    ),
)

SHIFT_INDEX = {s.key: s for s in SHIFTS}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score_run(shift: Shift, run: dict) -> dict:
    """Score a completed run. Deterministic except the handover, graded separately.

    ``run`` carries what the player did:
        elapsed_minutes, declared_root, declared_at, checks_run (ids),
        actions_taken (ids with minute), agents_used (ids)
    """
    out: dict = {"lines": [], "points": 0, "max": 100}

    # --- root cause: 40 --------------------------------------------------
    declared = run.get("declared_root")
    at = run.get("declared_at")
    if declared == shift.true_root and at is not None:
        speed = max(0.0, 1 - (at / shift.budget_minutes))
        pts = round(20 + 20 * speed)
        out["lines"].append({
            "label": "Root cause", "points": pts, "of": 40,
            "note": f"Correct ({shift.true_root}) at minute {at} of {shift.budget_minutes}.",
        })
    elif declared:
        out["lines"].append({
            "label": "Root cause", "points": 0, "of": 40,
            "note": f"Declared {declared}; the cause was {shift.true_root}."
                    + (" That was one of the symptoms." if declared in shift.decoys else ""),
        })
    else:
        out["lines"].append({"label": "Root cause", "points": 0, "of": 40,
                             "note": "Shift ended with no root cause declared."})
    out["points"] += out["lines"][-1]["points"]

    # --- required actions: 35 -------------------------------------------
    taken = {a["id"]: a.get("minute") for a in run.get("actions_taken", [])}
    per = 35 / max(1, len(shift.required))
    got = 0
    notes = []
    for req in shift.required:
        m = taken.get(req.id)
        if m is None:
            notes.append(f"missed: {req.label} — {req.why}")
        elif req.by_minute is not None and m > req.by_minute:
            got += per * 0.4
            notes.append(f"late ({m}m, needed by {req.by_minute}m): {req.label}")
        else:
            got += per
    out["lines"].append({"label": "Required actions", "points": round(got), "of": 35,
                         "note": "; ".join(notes) or "All taken in time."})
    out["points"] += round(got)

    # --- diagnostic efficiency: 25 ---------------------------------------
    ran = set(run.get("checks_run", []))
    decisive = {c.id for c in shift.checks if c.decisive}
    herrings = {c.id for c in shift.checks if c.red_herring}
    found = decisive & ran
    wasted = herrings & ran
    eff = 25 * (len(found) / max(1, len(decisive))) - 4 * len(wasted)
    eff = max(0, min(25, eff))
    out["lines"].append({
        "label": "Diagnostic efficiency", "points": round(eff), "of": 25,
        "note": f"{len(found)}/{len(decisive)} decisive checks run"
                + (f", {len(wasted)} dead ends" if wasted else ""),
    })
    out["points"] += round(eff)

    out["grade"] = (
        "Clean shift" if out["points"] >= 85 else
        "Solid" if out["points"] >= 70 else
        "Survivable" if out["points"] >= 50 else
        "The desk noticed"
    )
    out["debrief"] = shift.debrief
    return out


def shift_manifest() -> list[dict]:
    """Describe the playable shifts to the UI (without leaking the answer)."""
    return [
        {
            "key": s.key,
            "title": s.title,
            "opens_at": s.opens_at,
            "brief": s.brief,
            "budget_minutes": s.budget_minutes,
            "checks": [
                {"id": c.id, "label": c.label, "cost_minutes": c.cost_minutes,
                 "available_after": c.available_after}
                for c in s.checks
            ],
            "impacts": [
                {"at_minute": i.at_minute, "headline": i.headline,
                 "detail": i.detail, "severity": i.severity}
                for i in s.impacts
            ],
            "required": [
                {"id": r.id, "label": r.label, "by_minute": r.by_minute}
                for r in s.required
            ],
            "candidates": sorted({s.true_root, *s.decoys}),
        }
        for s in SHIFTS
    ]


def shift_answers() -> dict:
    """Findings, the true root and the handover checklist.

    Served to the page because it is a single-player training simulator with no
    server -- the answers have to reach the client eventually. A graded exam
    would keep these behind an API; this is a teaching tool, and the debrief is
    the point of it. Worth saying out loud rather than pretending otherwise.
    """
    return {
        s.key: {
            "true_root": s.true_root,
            "findings": {c.id: c.finding for c in s.checks},
            "decisive": [c.id for c in s.checks if c.decisive],
            "red_herrings": [c.id for c in s.checks if c.red_herring],
            "handover_must_cover": list(s.handover_must_cover),
            "debrief": s.debrief,
        }
        for s in SHIFTS
    }
