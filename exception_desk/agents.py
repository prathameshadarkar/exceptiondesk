"""The agent layer.

Four agents, each doing a job the desk actually does. Every one of them takes
the **output of the deterministic engine** as input and produces prose.

The boundary, restated because it is the whole argument of this project:

    The correlation engine decides WHAT happened and WHY.
    The agents decide HOW TO SAY IT.

No agent ranks root causes, scores candidates, or decides severity. If an agent
ever needs a fact, that fact comes from the context block built here -- which is
assembled from the engine's output, not from the model's memory. That is what
makes the output checkable: every claim in an agent's draft can be traced to a
line in the context it was given.

This also means the agents degrade honestly. Before ``assemble_incidents`` is
written they operate on signal groups instead of incidents, and they say so.

Cache vs live
-------------
``build_request`` is pure: agent + selection in, messages out. Both paths use
it. ``tools/generate_agent_cache.py`` calls the API offline and writes the
results into ``web/data/``; ``web/api/agent.js`` calls the same API at request
time. The page prefers the cache and falls back to live only when asked, so a
public link never depends on a key or a balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Model choice lives in providers.py, per tier, not here. Agents declare what
# KIND of work they are, and the provider adapter decides who serves it.
#
#   fast   — formatting/translation. A cheap model is correct, not a compromise.
#   reason — the judge, which grades free text against a checklist. Quality shows.
from .providers import MAX_OUTPUT as MAX_TOKENS  # noqa: F401  (kept for callers)


# --------------------------------------------------------------------------
# Context assembly -- the only source of facts an agent is allowed
# --------------------------------------------------------------------------


def build_context(
    scenario: dict,
    alerts: list[dict],
    groups: list[dict] | None = None,
    incidents: list[dict] | None = None,
) -> str:
    """Render the engine's findings as the factual brief an agent may use.

    Deliberately plain text rather than JSON: the agents are writing for humans
    and a human-shaped brief produces human-shaped prose. Deliberately complete
    about what is *missing* too -- if the correlation engine has not run, the
    context says so, and the agents are instructed not to invent a root cause.
    """
    lines: list[str] = []
    lines.append(f"SCENARIO: {scenario.get('title', scenario.get('key', 'unknown'))}")
    if scenario.get("summary"):
        lines.append(f"CONTEXT: {scenario['summary']}")
    lines.append("")

    sev_counts: dict[str, int] = {}
    for a in alerts:
        sev_counts[a["severity"]] = sev_counts.get(a["severity"], 0) + 1
    span = (min(a["ts_minute"] for a in alerts), max(a["ts_minute"] for a in alerts)) if alerts else (0, 0)
    lines.append(
        f"SELECTED: {len(alerts)} alerts across "
        f"{len({a['service'] for a in alerts})} services, "
        f"{_clock(span[0])}-{_clock(span[1])}, "
        + ", ".join(f"{k}:{v}" for k, v in sorted(sev_counts.items()))
    )
    lines.append("")

    if incidents:
        lines.append("CORRELATION ENGINE OUTPUT:")
        for inc in incidents:
            lines.append(
                f"  incident {inc['incident_id']}: root={inc['root']} "
                f"confidence={inc.get('confidence', 0):.0%} "
                f"severity={inc.get('severity', '?')} "
                f"({len(inc.get('alert_ids', []))} alerts, "
                f"{len(inc.get('affected_services', []))} services)"
            )
            for ev in inc.get("evidence", []):
                lines.append(f"    evidence: {ev}")
            for cand in (inc.get("candidates") or [])[:3]:
                reasons = "; ".join(cand.get("reasons", []))
                lines.append(
                    f"    candidate {cand['service']} score={cand.get('score', 0):.2f}"
                    + (f" ({reasons})" if reasons else "")
                )
        lines.append("")
    else:
        lines.append(
            "CORRELATION ENGINE OUTPUT: none. The root-cause engine has not run for "
            "this selection, so no root cause has been established. Do not assert one."
        )
        lines.append("")

    if groups:
        lines.append("SIGNAL GROUPS (repeated firings of one check collapsed):")
        for g in sorted(groups, key=lambda g: -g["count"])[:14]:
            flap = " FLAPPING" if g.get("flapping") else ""
            ent = f" [{g['entity']}]" if g.get("entity") else ""
            lines.append(
                f"  {g['service']} {g['alert_type']}{ent} x{g['count']} "
                f"{_clock(g['first_minute'])}-{_clock(g['last_minute'])}{flap}"
            )
        lines.append("")

    lines.append("ALERTS (up to 25 shown, earliest first):")
    for a in sorted(alerts, key=lambda a: a["ts_minute"])[:25]:
        lines.append(f"  {_clock(a['ts_minute'])} {a['severity']} {a['service']}: {a['message']}")
    if len(alerts) > 25:
        lines.append(f"  ... and {len(alerts) - 25} more")

    return "\n".join(lines)


def build_sim_context(shift: dict, state: dict) -> str:
    """Build the brief for an agent dispatched *during a shift*.

    The agent sees exactly what the analyst has established so far -- the
    scenario brief, the checks actually run and what they returned, the impact
    stages reached, and the elapsed clock. It does not see the answer key.

    That constraint is the point. An agent that could see the true root would
    produce a confident triage every time and prove nothing; an agent working
    from partial findings produces a triage whose quality tracks the analyst's
    own work, which is the honest simulation of the job.
    """
    lines: list[str] = []
    lines.append(f"SHIFT: {shift['title']} — opened {shift['opens_at']}")
    lines.append(f"SITUATION AS BRIEFED: {shift['brief']}")
    lines.append(f"ELAPSED: {state.get('elapsed', 0)} minutes of a "
                 f"{shift['budget_minutes']} minute window")
    lines.append("")

    findings = state.get("findings") or []
    if findings:
        lines.append("CHECKS RUN AND WHAT THEY RETURNED:")
        for f in findings:
            lines.append(f"  [{f.get('minute', '?')}m] {f['label']}")
            lines.append(f"      → {f['finding']}")
    else:
        lines.append("CHECKS RUN: none yet. Nothing has been established beyond the brief.")
    lines.append("")

    reached = state.get("impacts_reached") or []
    if reached:
        lines.append("IMPACT SO FAR:")
        for i in reached:
            lines.append(f"  [{i['at_minute']}m] {i['headline']} — {i['detail']}")
        lines.append("")

    declared = state.get("declared_root")
    if declared:
        lines.append(f"ANALYST'S WORKING ROOT CAUSE: {declared}")
    else:
        lines.append("ANALYST'S WORKING ROOT CAUSE: not yet declared.")
    lines.append("")

    alerts = state.get("alerts") or []
    if alerts:
        lines.append(f"ALERT BOARD ({len(alerts)} alerts, most recent first, 18 shown):")
        for a in alerts[:18]:
            lines.append(f"  {_clock(a['ts_minute'])} {a['severity']} {a['service']}: {a['message']}")

    return "\n".join(lines)


def build_judge_context(shift: dict, answers: dict, state: dict) -> str:
    """Build the brief the next-region agent grades a handover note against."""
    lines = [f"SHIFT: {shift['title']}", ""]
    lines.append("WHAT THE NEXT REGION NEEDS FROM THIS NOTE:")
    for item in answers.get("handover_must_cover", []):
        lines.append(f"  - {item}")
    lines.append("")
    lines.append(f"WHAT ACTUALLY HAPPENED: the root cause was {answers['true_root']}.")
    findings = state.get("findings") or []
    if findings:
        lines.append("Established during the shift:")
        for f in findings:
            lines.append(f"  - {f['finding']}")
    else:
        lines.append("The outgoing analyst established nothing beyond the initial brief.")
    return "\n".join(lines)


def _clock(minute: float) -> str:
    h, m = int(minute // 60) % 24, int(minute % 60)
    return f"{h:02d}:{m:02d}"


# --------------------------------------------------------------------------
# The agents
# --------------------------------------------------------------------------

_SHARED_RULES = """
Rules that apply to you absolutely:

- Every factual claim must trace to the BRIEF below. If the brief does not say
  it, you do not know it.
- If the brief says no root cause has been established, do not name one. Say
  what is observed and what needs checking. An operator acting on a confident
  guess is worse off than one acting on an honest uncertainty.
- Never invent instrument names, counterparties, ticket numbers, times, people
  or dollar amounts.
- No preamble, no sign-off pleasantries, no "I hope this helps". Desk
  communication is dense and starts with the thing that matters.
- If you are genuinely unsure, say so in one clause and move on.
""".strip()


@dataclass(frozen=True)
class Agent:
    """One agent: a role, a system prompt, and the shape of its output."""

    id: str
    name: str
    blurb: str
    button: str
    system: str
    user_template: Callable[[str, str], str]
    prefix: str = ""
    #: "context" = brief only; "question" = free text + brief; "note" = a written
    #: note graded against the brief.
    mode: str = "context"
    #: Which model tier this agent's work deserves. See providers.py.
    tier: str = "fast"
    #: One line under the name in the briefing card.
    role: str = ""
    #: 3-5 things THIS agent can do. Specific to its job -- a generic list would
    #: defeat the point of having specialised agents at all.
    can_do: tuple[str, ...] = ()
    #: What it deliberately will not do. Stating the boundary is what separates
    #: this from a chatbot, and it is the thing worth showing a reviewer.
    wont_do: str = ""
    #: Label on the primary button.
    cta: str = "Run agent"
    #: Which systems / data this agent actually reads. Concrete, not "your data".
    understands: tuple[str, ...] = ()
    #: Things a person could reasonably ask it. Shown as examples in the modal.
    examples: tuple[str, ...] = ()
    #: One line: what happens the moment you activate it.
    on_activate: str = ""


def _triage_user(context: str, question: str) -> str:
    return (
        "Triage this. What is it, what should be checked first, and what is the "
        "blast radius on trading?\n\nBRIEF:\n" + context
    )


def _broker_user(context: str, question: str) -> str:
    return (
        "Draft the escalation to the executing broker's middle office. Subject "
        "line, then body. Be specific about what we see and what we are asking "
        "them to confirm or resend.\n\nBRIEF:\n" + context
    )


def _pm_user(context: str, question: str) -> str:
    return (
        "Write the note that goes to the portfolio managers. They do not care "
        "about infrastructure; they care whether their positions and fills are "
        "correct, and whether they can trade.\n\nBRIEF:\n" + context
    )


def _handover_user(context: str, question: str) -> str:
    return (
        "Write the end-of-shift handover note for the next region.\n\nBRIEF:\n" + context
    )


def _ask_user(context: str, question: str) -> str:
    return f"QUESTION: {question}\n\nBRIEF:\n{context}"


AGENTS: tuple[Agent, ...] = (
    Agent(
        id="triage",
        name="Triage",
        blurb="What is it, what to check first, what it touches",
        button="Triage this",
        system=f"""You are a trading operations analyst doing first-line triage on a
systematic trading desk. You have been handed a set of alerts and whatever the
correlation engine concluded about them.

Produce exactly three short sections:

ASSESSMENT - two or three sentences. What appears to be happening, and how
confident the evidence supports that.
FIRST CHECKS - three to five numbered checks, most diagnostic first. Each one
names the specific thing to look at, not a category.
TRADING IMPACT - one or two sentences. Can portfolio managers trade? Are
positions, marks or fills unreliable right now?

{_SHARED_RULES}""",
        user_template=_triage_user,
        prefix="Triage this. What is it, what should be checked first, and what is the blast radius on trading?\n\nBRIEF:\n",
        role="First-line assessment of what is breaking and what to look at next",
        can_do=(
            "Read the alerts on your board and say what the pattern suggests",
            "Rank the next three to five checks, most diagnostic first",
            "Tell you whether positions, marks or fills are currently trustworthy",
            "Say plainly when the evidence does not yet support a root cause",
        ),
        wont_do="It never names a root cause the evidence does not support, and it cannot see the answer key — its assessment is only as sharp as the checks you have run.",
        cta="Run triage",
        understands=(
            "Alert board (service, severity, message, timing)",
            "Findings from every diagnostic you have run this shift",
            "Impact stages already reached",
            "Dependency topology of the 30 platform services",
        ),
        examples=(
            "What is actually failing here, and what is downstream noise?",
            "Which check should I run next, and why that one first?",
            "Can the PMs trust the blotter right now?",
        ),
        on_activate="Costs 4 shift minutes and one model call. Writes an assessment, ranked next checks and a trading-impact line onto the centre monitor.",
    ),
    Agent(
        id="broker",
        name="Broker escalation",
        blurb="Draft the note to the executing broker's middle office",
        button="Draft broker escalation",
        system=f"""You draft escalations from a hedge fund's operations desk to an
executing broker's middle office. These are read by people under time pressure
who receive fifty of them a day.

Format:
SUBJECT: one line, specific enough to be searchable later.
Then the body: what we observe, over what window, on which sessions or accounts,
and precisely what we need from them - a resend, a confirmation, an amended
file, a callback. Close with the urgency and who to reach.

Keep it under 160 words. No pleasantries beyond a single opening clause.

{_SHARED_RULES}""",
        user_template=_broker_user,
        prefix="Draft the escalation to the executing broker's middle office. Subject line, then body. Be specific about what we see and what we are asking them to confirm or resend.\n\nBRIEF:\n",
        role="Drafts the escalation to an executing broker's middle office",
        can_do=(
            "Write a searchable subject line and a tight factual body",
            "State what we observe, over what window, on which session or account",
            "Ask for the specific thing we need — a resend, an amended file, a callback",
            "Keep it under 160 words, because the person reading it has fifty today",
        ),
        wont_do="It invents nothing — no ticket numbers, counterparties, quantities or times that are not already established by your checks.",
        cta="Draft escalation",
        understands=(
            "Session and connectivity findings",
            "Execution and fill discrepancies you have established",
            "Which counterparty and account the incident touches",
        ),
        examples=(
            "Draft the resend request for the missing execution reports.",
            "Write the note asking them to confirm the quantity break.",
            "Escalate the sequence gap with the window and message counts.",
        ),
        on_activate="Costs 4 shift minutes and one model call. Produces a subject line and a body under 160 words, ready to send.",
    ),
    Agent(
        id="pm",
        name="PM brief",
        blurb="Plain English for the portfolio managers",
        button="Brief the PMs",
        system=f"""You write short operational notes to portfolio managers at a
multi-manager systematic fund. They are smart, busy, and do not care about
infrastructure. They care about three things and in this order: is what I am
looking at correct, can I trade, and when will it be fixed.

Four sentences maximum. Lead with the answer to "does this affect me". Name the
affected strategies or instruments only if the brief names them. If you do not
know the ETA, say the ETA is not yet known rather than guessing.

{_SHARED_RULES}""",
        user_template=_pm_user,
        prefix="Write the note that goes to the portfolio managers. They do not care about infrastructure; they care whether their positions and fills are correct, and whether they can trade.\n\nBRIEF:\n",
        role="Translates an operational problem into what a portfolio manager needs",
        can_do=(
            "Answer the only question a PM has first: does this affect me",
            "Say whether the blotter, marks and positions can be trusted right now",
            "Name affected strategies or instruments when the evidence names them",
            "Say the ETA is unknown rather than guessing one",
        ),
        wont_do="No infrastructure detail. A PM does not need to know which service failed, only whether they can trade.",
        cta="Brief the PMs",
        understands=(
            "Position, pricing and blotter reliability from your findings",
            "Which strategies or instruments the evidence names",
            "Whether an ETA has been established",
        ),
        examples=(
            "Tell the desk their marks are stale and what not to trust.",
            "Explain why the P&L move is an artefact, not a loss.",
            "Give the PM his true position after the missing fills.",
        ),
        on_activate="Costs 4 shift minutes and one model call. Produces four sentences a portfolio manager can act on.",
    ),
    Agent(
        id="handover",
        name="Shift handover",
        blurb="The four-part note for the next region",
        button="Write handover note",
        system=f"""You write the end-of-shift handover note on a follow-the-sun
trading operations desk covering New York, London and Singapore. The person
reading it has no context and may have to act within five minutes of reading.

Use exactly these four headed sections, in this order, each a short bullet list:

RESOLVED - closed during this shift, with the outcome.
WATCH ITEMS - open and still moving. Say what to watch and what "bad" looks like.
FOR THE NEXT REGION - things that require action in their hours, with the deadline.
REMINDERS - scheduled events, cut-offs, or context they will otherwise miss.

If a section is genuinely empty, write "None." rather than padding it. Write for
someone who is not you and was not here.

{_SHARED_RULES}""",
        user_template=_handover_user,
        prefix="Write the end-of-shift handover note for the next region.\n\nBRIEF:\n",
        role="Writes the end-of-shift note in the four-part desk structure",
        can_do=(
            "Draft resolved / watch items / for the next region / reminders",
            "Carry forward what your checks actually established, with times",
            "Flag what is still moving and what 'bad' would look like",
            "Write for someone with no context who must act within five minutes",
        ),
        wont_do="It drafts; you edit. The note you send is yours, and the next region grades what you sent, not what it suggested.",
        cta="Draft handover",
        understands=(
            "Everything established this shift, with timings",
            "Impacts reached and actions taken",
            "What the incoming region will need to act on",
        ),
        examples=(
            "Draft the note covering what I resolved and what is still open.",
            "Carry forward the watch items with what 'bad' looks like.",
            "Flag the cut-off the next region has to beat.",
        ),
        on_activate="Free — no shift time. One model call. Drafts into the handover pad, which you then edit before sending.",
    ),
    Agent(
        id="judge",
        name="Next region",
        blurb="The incoming desk reads your handover and says what it cannot act on",
        button="Send handover",
        system=f"""You are the operations analyst in the next region, picking up a
shift. You were not here for any of it. You have five minutes before the desk
starts asking you questions, and the only thing you have is the handover note in
front of you.

You will be given the note and a checklist of what this shift actually needed to
pass on. Respond in three parts:

CAN ACT ON - what the note tells you clearly enough to act on immediately.
CANNOT ACT ON - each checklist item the note leaves you unable to act on, and
what specifically is missing. Be concrete: "says the file was late but not
whether it landed" beats "insufficient detail".
FIRST QUESTION - the one thing you would message the outgoing analyst to ask.

Be exacting but not unkind -- this is the feedback a good colleague gives. If
the note is genuinely complete, say so; do not manufacture criticism.

{_SHARED_RULES}""",
        user_template=lambda context, question: (
            f"HANDOVER NOTE AS WRITTEN:\n{question}\n\n"
            f"WHAT THIS SHIFT NEEDED TO PASS ON, AND WHAT HAPPENED:\n{context}"
        ),
        prefix="HANDOVER NOTE AS WRITTEN:\n",
        mode="note",
        # The only agent doing real evaluation rather than drafting.
        tier="reason",
        role="The incoming region reads your handover and says what it cannot act on",
        can_do=(
            "Check your note against what this shift actually needed to pass on",
            "Name precisely what is missing, not just that something is",
            "Separate what it can act on immediately from what it cannot",
            "Ask the one question it would send back to you",
        ),
        wont_do="This is the one agent doing real evaluation rather than drafting — it reads free text you wrote and grades it against a checklist, which is why it runs on the more capable model.",
        cta="Send handover",
        understands=(
            "The handover note exactly as you wrote it",
            "A checklist of what this shift needed to pass on",
            "What actually happened, including the true root cause",
        ),
        examples=(
            "Can the next region act on this, or will they have to ask?",
            "What did I leave out that they will need at 06:00?",
            "Is the watch list specific enough to be useful?",
        ),
        on_activate="Free — no shift time. One model call on the reasoning tier. Returns what it can act on, what it cannot, and the one question it would send back.",
    ),
    Agent(
        id="ask",
        name="Ask",
        blurb="A question against this selection",
        button="Ask",
        system=f"""You answer questions about a specific set of trading operations
alerts. Answer only from the brief. If the brief does not contain the answer,
say so plainly and say what would need to be checked to find out - do not
speculate to fill the gap.

Keep answers to a few sentences unless the question genuinely needs more.

{_SHARED_RULES}""",
        user_template=_ask_user,
        prefix="QUESTION: ",
        mode="question",
        tier="reason",
        role="Answers a specific question about this incident and nothing else",
        can_do=(
            "Explain what a particular alert or check result means here",
            "Work out the blast radius on a named strategy or service",
            "Say which systems are affected and which are merely downstream",
            "Tell you what would need checking to answer something it cannot",
        ),
        wont_do="It answers only from the brief in front of it. If the answer is not there, it says so and says what to check instead of filling the gap.",
        cta="Ask agent",
        understands=(
            "The incident brief and your findings",
            "The alert board as it stands",
            "Impacts reached so far",
        ),
        examples=(
            "What is the blast radius on EQ_STAT_ARB?",
            "Why would the drop copy go silent but the OMS stay up?",
            "Which of these alerts are downstream of the same service?",
        ),
        on_activate="Costs 4 shift minutes and one model call. Answers from the brief only; says so when the answer is not there.",
    ),
)

AGENT_INDEX = {a.id: a for a in AGENTS}


def build_request(agent_id: str, context: str, question: str = "") -> tuple[str, str, str]:
    """Return ``(system, user, tier)`` for an agent. Provider-agnostic on purpose.

    The cache generator and the web endpoint both call this and then hand the
    result to the provider adapter, so there is exactly one definition of what
    an agent is asked and exactly one place that knows an API shape.
    """
    agent = AGENT_INDEX[agent_id]
    return agent.system, agent.user_template(context, question), agent.tier


def agents_manifest() -> list[dict]:
    """Describe the agents to the UI."""
    return [
        {"id": a.id, "name": a.name, "blurb": a.blurb, "button": a.button,
         "system": a.system, "prefix": a.prefix, "mode": a.mode, "tier": a.tier,
         "role": a.role, "can_do": list(a.can_do), "wont_do": a.wont_do, "cta": a.cta,
         "understands": list(a.understands), "examples": list(a.examples),
         "on_activate": a.on_activate}
        for a in AGENTS
    ]
