/* ===========================================================================
   Onboarding — guided tour and contextual first-use hints.
   ---------------------------------------------------------------------------
   Entirely local. No model call, no network, nothing to configure. Teaching
   someone how a button works is not a job for an LLM, and an onboarding flow
   that costs money per view is an onboarding flow nobody leaves switched on.

   Two mechanisms:

     Tour   — the five-step walkthrough on a new session. Spotlights a control,
              dims the rest, positions a card beside it, Back / Next / Skip,
              progress counter.
     hint() — one-off coach marks fired the first time a person reaches a piece
              of functionality. Dismissed once, never shown again this session.

   Steps can target controls that do not exist yet (the desk is not rendered
   while the picker is open). A step whose target is missing parks the tour;
   `Tour.resume()` — called from the app's render path — picks it up as soon as
   the control appears. That is why the tour can start at the scenario list and
   finish on the desk without the app having to sequence it.
   =========================================================================== */

window.Tour = (function () {
  'use strict';

  const SEEN_TOUR = 'ed.tour.done';
  const SEEN_HINT = 'ed.hint.';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const STEPS = [
    {
      // No target: a welcome card, centred. Says what the product is before it
      // starts pointing at controls, because "this is the scenario selector"
      // means nothing to someone who does not yet know what the app is for.
      key: 'welcome',
      welcome: true,
      title: 'Welcome to the operations desk',
      body: 'You are the operations analyst on a systematic trading desk. Your job is to '
          + 'watch the trading environment, work out what is actually broken when something '
          + 'goes wrong, and bring in specialised agents when they help. This takes about '
          + 'five minutes.',
      cta: 'Show me around',
    },
    {
      key: 'scenario',
      sel: '#shiftCards',
      title: 'Choose a scenario',
      body: 'Each card is a real operational failure mode — a late vendor file, a dropped '
          + 'broker session, a database saturating. Pick one to take the shift.',
      place: 'bottom',
      await: 'scenario-picked',
      awaitLabel: 'Your turn — pick a scenario below',
      awaitCta: 'Waiting for you',
      hold: true,
    },
    {
      // The workstation arrives cold, and until it is on the desk is inert. That
      // is deliberate — you take over a machine, you do not spawn into a session
      // — but it means the tutorial has to teach the power button rather than
      // assume it. Every later step depends on this one.
      key: 'power',
      sel: '#power',
      title: 'Power up the workstation',
      body: 'You have taken over a desk, not opened an app: the machine is off and the '
          + 'screens are dark. Press Power on, on the centre monitor, and the workstation '
          + 'comes up — alert bus, drop copy feed, runbooks.',
      place: 'right',
      avoid: '.monitor.tall .glass',
      await: 'workstation-powered',
      awaitLabel: 'Your turn — press Power on',
      awaitCta: 'Waiting for you',
      hold: true,
    },
    {
      key: 'screens',
      sel: '.monitor.tall .glass',
      title: 'Your workstation',
      body: 'Three screens: the alert board on the left, whatever you are working on in the '
          + 'centre, and the shift log on the right. The clock only moves when you act, so '
          + 'the decision is which minutes to spend — not how fast you can read.',
      place: 'bottom',
    },
    {
      // The second half of taking over the desk. Until the shift is accepted the
      // clock is stopped and the desk controls stay locked, so this step has to
      // be interactive too.
      key: 'accept',
      sel: '#accept',
      title: 'Read the brief, then take the shift',
      body: 'The centre screen has the incident as it was handed to you. Read it and accept '
          + 'the shift — that is the moment the clock starts and the desk unlocks. Nothing '
          + 'is running against you until you do.',
      place: 'right',
      avoid: '.monitor.tall .glass',
      await: 'shift-accepted',
      awaitLabel: 'Your turn — accept the shift',
      awaitCta: 'Waiting for you',
      hold: true,
      unblock: ['power'],
    },
    {
      key: 'alerts',
      sel: '#scrAlerts',
      title: 'Alerts and exceptions',
      body: 'This is how an operational problem shows up: not as one alert, but as a storm. '
          + 'Most of what lands here is a symptom of something upstream. Finding which one '
          + 'is the cause is the job.',
      place: 'right',
    },
    {
      key: 'agents',
      sel: '[data-agent]',
      title: 'Specialised agents',
      body: 'Different agents handle different parts of the operation — triage, broker '
          + 'escalation, briefing the portfolio managers. Click any agent to see what it '
          + 'reads and what it can investigate. Nothing runs until you say so.',
      place: 'top',
      await: 'agent-opened',
      awaitLabel: 'Your turn — click any agent on the desk',
      awaitCta: 'Waiting for you',
      hold: true,
      // The desk is inert until the machine is on AND the shift is accepted. If
      // we somehow arrive here before that, route back rather than point at a
      // button that cannot answer.
      unblock: ['power', 'accept'],
    },
    {
      key: 'desk',
      sel: '#keys',
      title: 'Start investigating',
      body: 'Run a diagnostic to establish a fact — each one costs minutes and some are dead '
          + 'ends by design. Answer the phones when they ring, declare the root cause on the '
          + 'dial when the evidence supports it, then write the handover. The Board chip at '
          + 'the top opens the data behind all of it.',
      place: 'top',
      last: true,
    },
  ];

  let idx = 0, active = false, parked = false, waiting = null, els = null;
  let escapeTimer = null, parkTimer = null, stranded = false;

  // Gates the person has already cleared, whether or not the tour was on that
  // step at the time. Someone who powers the machine on before we get round to
  // explaining it must not be shown a card pointing at a button that is gone.
  const satisfied = new Set();

  const keyIndex = (k) => STEPS.findIndex((s) => s.key === k);

  /** The ancestor that makes `el` unable to receive a click, or null.
   *  `.desk.locked` sets pointer-events:none, which is how the desk stays inert
   *  before the shift is live — a real state, not a bug, so the tutorial has to
   *  detect it rather than the app having to give it up. */
  function blockedBy(el) {
    if (!el) return null;
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (n.disabled) return n;
      const cs = getComputedStyle(n);
      if (cs.pointerEvents === 'none' || cs.visibility === 'hidden' || cs.display === 'none') return n;
    }
    return null;
  }

  /** For a step that cannot proceed, the index of the step that would unblock it
   *  — the first prerequisite still outstanding and reachable. -1 if none. */
  function router(step) {
    for (const k of (step && step.unblock) || []) {
      const j = keyIndex(k);
      if (j < 0) continue;
      const s = STEPS[j];
      if (s.await && satisfied.has(s.await)) continue;   // already cleared
      const el = target(j);
      if (visible(el) && !blockedBy(el)) return j;
    }
    return -1;
  }

  /* ------------------------------------------------------------ chrome */

  function ensure() {
    if (els) return els;
    const root = document.createElement('div');
    root.className = 'tour-root';
    root.innerHTML = `
      <div class="tour-veil" data-veil></div>
      <div class="tour-ring" data-ring></div>
      <div class="tour-card" role="dialog" aria-live="polite">
        <div class="tour-step" data-count></div>
        <h4 data-title></h4>
        <p data-body></p>
        <p class="tour-wait" data-wait></p>
        <button type="button" class="tour-escape" data-escape>Skip this step</button>
        <div class="tour-actions">
          <button type="button" class="ghost" data-skip>Skip tutorial</button>
          <span class="tour-gap"></span>
          <button type="button" data-back>Back</button>
          <button type="button" class="primary" data-next>Next</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    root.querySelector('[data-skip]').onclick = finish;
    root.querySelector('[data-back]').onclick = () => go(idx - 1);
    root.querySelector('[data-next]').onclick = () => go(idx + 1);
    root.querySelector('[data-veil]').onclick = finish;
    root.querySelector('[data-escape]').onclick = () => {
      waiting = null; stranded = false;
      for (let i = idx + 1; i < STEPS.length; i++) {
        const el = target(i);
        if (STEPS[i].welcome) return go(i);
        // Skipping onto another dead end is not an escape.
        if (visible(el) && !(STEPS[i].await && blockedBy(el))) return go(i);
      }
      finish();
    };
    els = {
      root,
      ring: root.querySelector('[data-ring]'),
      card: root.querySelector('.tour-card'),
      count: root.querySelector('[data-count]'),
      wait: root.querySelector('[data-wait]'),
      escape: root.querySelector('[data-escape]'),
      title: root.querySelector('[data-title]'),
      body: root.querySelector('[data-body]'),
      back: root.querySelector('[data-back]'),
      next: root.querySelector('[data-next]'),
    };
    return els;
  }

  /** Is there an earlier step we could actually return to? */
  function hasEarlierStep(i) {
    for (let j = i - 1; j >= 0; j--) {
      if (STEPS[j].await && satisfied.has(STEPS[j].await)) return false;
      if (STEPS[j].welcome || visible(target(j))) return true;
    }
    return false;
  }

  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && getComputedStyle(el).visibility !== 'hidden';
  };
  const target = (i) => document.querySelector(STEPS[i] && STEPS[i].sel);

  /* ------------------------------------------------------------ placement */

  function place(step, el) {
    const e = ensure();
    const r = el.getBoundingClientRect();
    const pad = 8;
    Object.assign(e.ring.style, {
      top: `${r.top - pad}px`, left: `${r.left - pad}px`,
      width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px`,
    });

    // Measure the card, then pick a side that keeps it on screen. Preference
    // from the step, but the viewport wins — a tooltip half off the edge is
    // worse than one on the "wrong" side.
    //
    // Measure the BOX, not the rect. The card animates its top/left, so parking
    // it at 0,0 to read its size (what this used to do) kicked off a 280ms slide
    // to the corner which the real position then had to slide back from — and
    // for that third of a second the card swept across whatever it was pointing
    // at, swallowing the click. offsetWidth/offsetHeight are the same numbers
    // without moving anything.
    const c = { width: e.card.offsetWidth, height: e.card.offsetHeight };
    const vw = innerWidth, vh = innerHeight, gap = 16;

    const options = {
      bottom: { top: r.bottom + gap, left: r.left + r.width / 2 - c.width / 2 },
      top:    { top: r.top - c.height - gap, left: r.left + r.width / 2 - c.width / 2 },
      right:  { top: r.top + r.height / 2 - c.height / 2, left: r.right + gap },
      left:   { top: r.top + r.height / 2 - c.height / 2, left: r.left - c.width - gap },
    };

    const fits = (o) =>
      o.top >= 8 && o.left >= 8 && o.top + c.height <= vh - 8 && o.left + c.width <= vw - 8;

    // A candidate that covers the spotlight is not a candidate. This was the
    // real cause of "I clicked the agent and nothing happened": the card sat on
    // top of the control it was telling you to press, so the click hit the card.
    const clears = (o) => {
      const pad = 6;
      return o.left > r.right + pad || o.left + c.width < r.left - pad
          || o.top  > r.bottom + pad || o.top + c.height < r.top - pad;
    };

    // Some steps point at a small control inside something the person has to
    // read — Accept the shift sits in the middle of the incident brief. Clearing
    // the button is not enough there, so a step can name a region to stay off,
    // and we add candidate positions measured from THAT rather than from the
    // button (no side of a small control clears the panel it sits in).
    const avoidEl = step.avoid && document.querySelector(step.avoid);
    const ar = avoidEl && avoidEl.getBoundingClientRect();
    if (ar) {
      options.avoidRight = { top: Math.max(8, ar.top), left: ar.right + gap };
      options.avoidLeft  = { top: Math.max(8, ar.top), left: ar.left - c.width - gap };
      options.avoidBelow = { top: ar.bottom + gap, left: ar.left + ar.width / 2 - c.width / 2 };
    }
    const clearsAvoid = (o) => !ar
      || o.left > ar.right + 6 || o.left + c.width < ar.left - 6
      || o.top  > ar.bottom + 6 || o.top + c.height < ar.top - 6;

    const order = [step.place, 'bottom', 'top', 'right', 'left'];
    let pick = null;
    if (ar) {
      for (const k of ['avoidRight', 'avoidLeft', 'avoidBelow', ...order]) {
        const o = options[k];
        if (o && fits(o) && clears(o) && clearsAvoid(o)) { pick = o; break; }
      }
    }
    // Keeping the control clickable always wins over keeping a panel readable.
    for (const k of order) {
      if (pick) break;
      const o = options[k];
      if (o && fits(o) && clears(o)) { pick = o; break; }
    }
    // Nothing clears on this screen: park the card against whichever edge is
    // furthest from the target, so the control stays reachable.
    if (!pick) {
      const below = r.bottom + gap + c.height <= vh - 8;
      const above = r.top - gap - c.height >= 8;
      if (below)      pick = { top: vh - c.height - 12, left: Math.max(12, Math.min(vw - c.width - 12, r.left)) };
      else if (above) pick = { top: 12, left: Math.max(12, Math.min(vw - c.width - 12, r.left)) };
      else            pick = { top: Math.max(12, vh - c.height - 12),
                               left: r.left > vw / 2 ? 12 : Math.max(12, vw - c.width - 12) };
    }

    pick.top = Math.min(Math.max(8, pick.top), vh - c.height - 8);
    pick.left = Math.min(Math.max(8, pick.left), vw - c.width - 8);

    // Final guarantee. Clamping above can push a cleared position back over the
    // target, so verify the result rather than trusting the arithmetic: if the
    // card still covers what it is pointing at, the control is unclickable and
    // the tutorial is broken. Slide it to whichever side has room.
    if (!clears(pick)) {
      const roomBelow = vh - r.bottom, roomAbove = r.top;
      if (roomBelow >= c.height + 20)      pick.top = Math.min(r.bottom + gap, vh - c.height - 8);
      else if (roomAbove >= c.height + 20) pick.top = Math.max(8, r.top - c.height - gap);
      else if (vw - r.right >= c.width + 20) { pick.left = r.right + gap; pick.top = Math.max(8, Math.min(pick.top, vh - c.height - 8)); }
      else if (r.left >= c.width + 20)     { pick.left = Math.max(8, r.left - c.width - gap); }
      else {
        // Genuinely no room beside it — go to the far corner and shrink.
        pick.left = r.left > vw / 2 ? 8 : vw - c.width - 8;
        pick.top = r.top > vh / 2 ? 8 : vh - c.height - 8;
      }
    }
    const moved = e.card.style.top !== `${pick.top}px` || e.card.style.left !== `${pick.left}px`;
    Object.assign(e.card.style, { top: `${pick.top}px`, left: `${pick.left}px`, visibility: '' });

    // The card slides between steps, and its path can pass over the control it
    // is about to point at. It takes no clicks until it has arrived.
    if (moved) {
      e.card.classList.add('moving');
      clearTimeout(moveTimer);
      moveTimer = setTimeout(() => e.card.classList.remove('moving'), 320);
    }
  }
  let moveTimer = null;

  /* ------------------------------------------------------------ flow */

  function go(i) {
    if (i < 0) return;
    if (i >= STEPS.length) return finish();

    // Going BACKWARDS to a step whose screen we have already left would park the
    // tutorial and make it vanish — it only ever resumes forwards. Walk back to
    // the nearest step that is actually on screen.
    //
    // But do not walk past a gate the person has already cleared. The scenario
    // picker is gone once a shift starts, so plain "nearest visible" sent Back
    // from step 3 all the way to the welcome card — which reads as the tutorial
    // resetting itself, not as going back one step. If there is no reachable
    // earlier step, stay put: Back is disabled below so it never looks broken.
    if (i < idx) {
      let j = i;
      while (j >= 0 && !(STEPS[j].welcome || visible(target(j)))) {
        if (STEPS[j].await && satisfied.has(STEPS[j].await)) { j = -1; break; }
        j--;
      }
      if (j < 0) return;
      i = j;
    }
    // A gate the person cleared on their own is not a gate, and its control is
    // usually gone by the time we look (the power button unmounts once the
    // machine is on). Move past it instead of pointing at nothing.
    const s = STEPS[i];
    if (s.await && satisfied.has(s.await) && !visible(target(i))) return go(i + 1);

    idx = i;
    const step = s;
    const e = ensure();
    stranded = false;
    clearTimeout(parkTimer);

    e.count.textContent = `${i + 1} of ${STEPS.length}`;
    e.title.textContent = step.title;
    e.body.textContent = step.body;
    // Disable Back when there is genuinely nowhere to go: step 1, or every
    // earlier step belongs to a screen we have left behind. An enabled button
    // that does nothing is the failure mode this whole pass is about.
    e.back.disabled = i === 0 || !hasEarlierStep(i);

    // A step can wait for the person to actually do the thing. Reading "this is
    // the scenario selector" teaches far less than picking one.
    waiting = step.await || null;
    clearTimeout(escapeTimer);
    e.next.classList.toggle('waiting', !!waiting);
    e.next.textContent = waiting
      ? (step.awaitCta || 'Your turn')
      : (step.cta || (step.last ? 'Finish' : 'Next'));
    e.next.disabled = !!waiting;
    e.wait.textContent = waiting ? step.awaitLabel || '' : '';
    e.wait.style.display = waiting ? '' : 'none';
    e.escape.style.display = 'none';
    if (waiting) {
      // Never strand someone who cannot find the control we are pointing at.
      escapeTimer = setTimeout(() => {
        if (active && waiting === step.await) e.escape.style.display = '';
      }, 6000);
    }

    if (step.welcome) {
      parked = false;
      e.root.classList.add('on', 'centred');
      e.ring.style.opacity = '0';
      Object.assign(e.card.style, { top: '', left: '', visibility: '' });
      return;
    }

    const el = target(i);
    if (!visible(el)) { park(i); return; }

    // The safeguard. A step that asks for a click must never be shown against a
    // control that cannot answer one: that is the state the tutorial used to sit
    // in forever at the agent step, waiting on a desk whose pointer events were
    // switched off. Route to whatever unblocks it, and if nothing does, say so
    // in the card instead of waiting silently.
    if (step.await && blockedBy(el)) {
      const j = router(step);
      if (j >= 0 && j !== i) return go(j);
      strand(step, 'That control is not active yet — the workstation still has to be '
                 + 'powered on and the shift accepted.');
      return;
    }

    parked = false;
    e.root.classList.remove('centred');
    e.root.classList.add('on');
    e.ring.style.opacity = '';

    el.scrollIntoView({ block: 'center', behavior: 'auto' });
    place(step, el);
    // Re-place on the next two frames: fonts, images and the desk's own render
    // can shift the target after the first measurement.
    requestAnimationFrame(() => { if (idx === i) place(step, el); });
    setTimeout(() => { if (idx === i) place(step, el); }, 240);
  }

  /** Advance a gated step. Called either directly or, preferably, by the
   *  application-state events below — so the tutorial reacts to what the app
   *  actually did rather than to a click it might not have seen. */
  function did(name) {
    if (!active || waiting !== name) return;
    waiting = null;
    clearTimeout(escapeTimer);
    setTimeout(() => go(idx + 1), 420);
  }

  // The app emits `ed:<state>` when its own state changes. The tutorial
  // subscribes; neither side needs to know about the other's internals, and a
  // missed click cannot desynchronise them because the event is fired from the
  // state transition, not from the click handler.
  ['scenario-picked', 'workstation-powered', 'shift-accepted',
   'agent-opened', 'check-run', 'root-declared']
    .forEach((name) => addEventListener('ed:' + name, () => { satisfied.add(name); did(name); }));

  /** Hide but stay armed: the step's control has not been rendered yet.
   *  Parking is normal — the desk does not exist while the picker is open — but
   *  it must never be permanent, so it comes with a watchdog. */
  function park(i) {
    parked = true;
    if (els) els.root.classList.remove('on');
    clearTimeout(parkTimer);
    parkTimer = setTimeout(() => {
      if (!active || !parked || idx !== i) return;
      const j = router(STEPS[i]);
      if (j >= 0 && j !== i) return go(j);
      strand(STEPS[i], 'That control is not on screen yet.');
    }, 7000);
  }

  /** Show the step's explanation with no gate and both exits open. The last
   *  resort: the person learns what the step was about and can keep moving. */
  function strand(step, why) {
    const e = ensure();
    parked = false; stranded = true; waiting = null;
    clearTimeout(escapeTimer); clearTimeout(parkTimer);
    e.title.textContent = step.title;
    e.body.textContent = step.body;
    e.wait.textContent = why + ' You can continue and come back to it.';
    e.wait.style.display = '';
    e.next.classList.remove('waiting');
    e.next.disabled = false;
    e.next.textContent = step.last ? 'Finish' : 'Next';
    e.escape.style.display = '';
    e.root.classList.add('on', 'centred');
    e.ring.style.opacity = '0';
    Object.assign(e.card.style, { top: '', left: '', visibility: '' });
  }

  function finish() {
    active = false; parked = false; stranded = false; waiting = null;
    clearTimeout(escapeTimer); clearTimeout(parkTimer);
    if (els) els.root.classList.remove('on');
    try { sessionStorage.setItem(SEEN_TOUR, '1'); } catch { /* private mode */ }
  }

  function start(force) {
    let done = false;
    try { done = sessionStorage.getItem(SEEN_TOUR) === '1'; } catch { /* ignore */ }
    if (done && !force) return;
    if (force) { try { sessionStorage.removeItem(SEEN_TOUR); } catch { /* ignore */ } }
    active = true; idx = 0; waiting = null; stranded = false;
    clearTimeout(parkTimer);
    clearHints();
    go(0);
  }

  /** Called from the app's render path. Advances a parked tour once its target
   *  exists, and keeps the spotlight aligned when the layout moves. */
  function resume() {
    if (!active) return;
    // A stranded step recovers the moment its control turns up.
    if (stranded) {
      const el = target(idx);
      if (visible(el) && !(STEPS[idx].await && blockedBy(el))) { stranded = false; go(idx); }
      return;
    }
    // A parked step is checked BEFORE the waiting guard. A gated step sets
    // `waiting` the moment it is entered, so a gate whose control has not been
    // rendered yet — Accept the shift, which only exists once the machine has
    // finished booting — would park with `waiting` set and then be refused by
    // that guard on every render afterwards. It never came back.
    if (parked) {
      for (let i = idx; i < STEPS.length; i++) {
        if (visible(target(i))) { go(i); return; }
        // Never skip forward past a step the person has to perform. Scanning for
        // "the next thing on screen" would sail past Power on and Accept — both
        // of whose controls are absent for a second or two — and land on a step
        // whose target only becomes clickable because of them.
        if (STEPS[i].hold && !(STEPS[i].await && satisfied.has(STEPS[i].await))) return;
      }
      return;
    }
    if (waiting) return;
    const el = target(idx);
    if (visible(el)) place(STEPS[idx], el);
    else park(idx);
  }

  /* ------------------------------------------------------------ one-off hints */

  //: At most one coach mark on screen. Two of these used to be able to fire
  //: within a second of each other — accepting the shift and running the first
  //: check — and they landed on top of one another, each covering the other's
  //: text and the controls underneath. A queue is overkill for a hint nobody
  //: asked for: the second one simply waits for the first to go.
  let openHint = null;
  let hintQueue = [];

  function hint(key, sel, text) {
    let seen = false;
    try { seen = sessionStorage.getItem(SEEN_HINT + key) === '1'; } catch { /* ignore */ }
    if (seen || active) return;
    if (!visible(document.querySelector(sel))) return;
    try { sessionStorage.setItem(SEEN_HINT + key, '1'); } catch { /* ignore */ }

    if (openHint) { hintQueue.push([sel, text]); return; }
    showHint(sel, text);
  }

  function showHint(sel, text) {
    const el = document.querySelector(sel);
    if (!visible(el)) return drainHints();

    const r = el.getBoundingClientRect();
    const tip = document.createElement('div');
    tip.className = 'tour-hint';
    tip.setAttribute('role', 'status');
    tip.innerHTML = `<p></p><button type="button">Got it</button>`;
    tip.querySelector('p').textContent = text;
    document.body.appendChild(tip);
    openHint = tip;

    // Place it clear of what it is describing — and clear of the whole control
    // cluster it sits in. Clearing only the target is not enough: every hint on
    // this product points at something inside the desk, where the controls are
    // packed tight, so "just below the clock" lands on top of the phones. The
    // panel the target belongs to is what has to be avoided.
    // Avoiding the target's own panel is not enough — a hint about the centre
    // screen, placed neatly below that monitor, lands squarely on the desk. So
    // score candidate positions by how many *interactive* things they would
    // cover, and take the first that covers none. Anything a person might need
    // to click during the nine seconds this is on screen counts.
    const panel = el.closest('#desk, .monitor, .panel') || el;
    const pr = panel.getBoundingClientRect();
    const c = tip.getBoundingClientRect();
    const gap = 12;
    const pad = 6;

    const obstacles = [...document.querySelectorAll(
      '#keys button, #phones button, .dial, .notepad, .monitor .screen, .panel button'
    )].map((n) => n.getBoundingClientRect())
      .filter((q) => q.width > 4 && q.bottom > 0 && q.top < innerHeight);

    const overlaps = (o, q) => !(o.left > q.right + pad || o.left + c.width < q.left - pad
      || o.top > q.bottom + pad || o.top + c.height < q.top - pad);
    const cost = (o) => obstacles.reduce((n, q) => n + (overlaps(o, q) ? 1 : 0), 0);
    const fits = (o) => o.top >= 8 && o.left >= 8
      && o.top + c.height <= innerHeight - 8 && o.left + c.width <= innerWidth - 8;

    const nearX = Math.max(10, Math.min(r.left, innerWidth - c.width - 10));
    const options = [
      { top: pr.top - c.height - gap, left: nearX },
      { top: pr.bottom + gap,         left: nearX },
      { top: Math.max(10, pr.top),    left: pr.right + gap },
      { top: Math.max(10, pr.top),    left: pr.left - c.width - gap },
      { top: 10,                       left: innerWidth - c.width - 10 },
      { top: 10,                       left: 10 },
      { top: innerHeight - c.height - 10, left: innerWidth - c.width - 10 },
      { top: innerHeight - c.height - 10, left: 10 },
    ].filter(fits);

    const pick = options.find((o) => cost(o) === 0)
      || options.sort((a, z) => cost(a) - cost(z))[0]
      || { top: 10, left: nearX };

    tip.style.top = `${Math.round(Math.min(Math.max(8, pick.top), innerHeight - c.height - 8))}px`;
    tip.style.left = `${Math.round(Math.min(Math.max(8, pick.left), innerWidth - c.width - 8))}px`;
    requestAnimationFrame(() => tip.classList.add('on'));

    let done = false;
    const close = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      tip.classList.remove('on');
      setTimeout(() => { tip.remove(); if (openHint === tip) openHint = null; drainHints(); }, 220);
    };
    tip.querySelector('button').onclick = close;
    const timer = setTimeout(close, 9000);
  }

  function drainHints() {
    if (openHint || !hintQueue.length) return;
    const [sel, text] = hintQueue.shift();
    showHint(sel, text);
  }

  /** Clear any coach mark before the tour takes over the screen. */
  function clearHints() {
    hintQueue = [];
    if (openHint) { const t = openHint; openHint = null; t.remove(); }
  }

  addEventListener('resize', () => resume());
  addEventListener('keydown', (e) => {
    if (!active) return;
    if (e.key === 'Escape') { e.preventDefault(); finish(); }
    if (e.key === 'ArrowRight' && !waiting) go(idx + 1);
    if (e.key === 'ArrowLeft') go(idx - 1);
  });

  return { start, resume, finish, hint, did, clearHints,
           get active() { return active; } };
})();
