/* ===========================================================================
   Arrival sequence — builds and runs the office scene, then dissolves into the
   app that is already rendered underneath it.
   ---------------------------------------------------------------------------
   Contract with the app: this module owns #intro and nothing else. It never
   touches app state, never blocks boot, and if anything in here throws, the
   overlay is removed and the app is exactly where it would have been. An intro
   that can break the product is not worth having.

   Timing is one table. Five beats, ~5.4s total, skippable at any point by
   button, click, Escape or any key.
   =========================================================================== */

(function () {
  'use strict';

  const SEEN_KEY = 'ed.intro.seen';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const small = matchMedia('(max-width: 760px)').matches;

  // Camera beats, by name. Reduced motion collapses to a brief look at the scene.
  //
  //   s0open  the doors part and the analyst steps through
  //   s1      they walk onto the floor; the camera carries you in behind them
  //   s2      they reach the desk; the screens wake as they arrive
  //   sit     they take the chair and the chair tucks in
  //   s3      they settle toward the monitors and the centre screen takes the frame
  //
  // `sit` is additive — s2 stays on until s3 — so nothing that already keyed off
  // s2 (the monitor bloom, the caption, the neighbouring screens) changes.
  const BEATS = reduced
    ? [[1, 's0open'], [160, 's1'], [340, 's2'], [560, 'sit'], [1120, 's3'], [1420, 'end']]
    : [[200, 's0open'], [1000, 's1'], [2150, 's2'], [3300, 'sit'], [4350, 's3'], [5400, 'end']];

  // Monitor wake order and delay. Left to right, but the analyst's main screen
  // (index 1) comes up first, the way a machine that was asleep does.
  const WAKE = small ? [[0, 90], [1, 0], [2, 180], [3, 270]]
                     : [[1, 0], [2, 220], [0, 430], [3, 620]];

  let timers = [];
  const at = (ms, fn) => timers.push(setTimeout(fn, ms));

  /* ------------------------------------------------------------ mock data */
  // Deterministic: the same office every load, so screenshots and demos match.
  let seed = 20260924;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const px = (n, d = 2) => n.toFixed(d);

  const SYMS = ['ESZ6', 'NQZ6', 'EURUSD', 'USDJPY', 'CLF7', 'GCG7', 'AAPL', 'MSFT'];

  function tickerRow() {
    return SYMS.slice(0, 4).map((s) => {
      const up = rnd() > 0.45;
      return `<span><b>${s}</b> <span class="${up ? 'u' : 'd'}">${up ? '▲' : '▼'}${px(rnd() * 0.9)}%</span></span>`;
    }).join('');
  }

  function quotesPane() {
    const rows = SYMS.slice(0, 7).map((s) =>
      `<div class="mrow"><span>${s}</span><span class="v">${px(80 + rnd() * 4200, 2)}</span></div>`).join('');
    return `<div class="tick">${tickerRow()}</div><div class="lbl">Watchlist</div>${rows}`;
  }

  function chartPane() {
    // A restrained line + area. One series, no axis furniture at this size.
    const n = 44, pts = [];
    let v = 52;
    for (let i = 0; i < n; i++) { v += (rnd() - 0.48) * 5.2; pts.push(Math.max(8, Math.min(92, v))); }
    const w = 100, h = 62;
    const d = pts.map((p, i) => `${(i / (n - 1)) * w},${h - (p / 100) * h}`).join(' ');
    return `<div class="lbl">EG Composite · intraday</div>
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:62px">
        <polyline points="0,${h} ${d} ${w},${h}" fill="rgba(110,168,232,.13)" stroke="none"/>
        <polyline points="${d}" fill="none" stroke="#6ea8e8" stroke-width="1.1"
                  vector-effect="non-scaling-stroke"/>
      </svg>
      <div class="lbl" style="margin-top:6px">Positions</div>
      ${['EQ_STAT_ARB', 'FUT_TREND', 'FX_CARRY'].map((s) =>
        `<div class="mrow"><span>${s}</span><span class="v">${px(rnd() * 9 - 3, 2)}%</span></div>`).join('')}`;
  }

  function bookPane() {
    let rows = '';
    for (let i = 0; i < 7; i++) {
      const bw = 18 + rnd() * 78, aw = 18 + rnd() * 78;
      rows += `<span class="bid">${px(4412.25 - i * 0.25)}</span><span class="ask">${px(4412.75 + i * 0.25)}</span>
               <span class="bar b" style="width:${bw}%"></span><span class="bar a" style="width:${aw}%"></span>`;
    }
    return `<div class="lbl">ESZ6 · depth</div><div class="ladder">${rows}</div>`;
  }

  function blotterPane() {
    const rows = ['FILL', 'FILL', 'PART', 'FILL', 'ACK', 'FILL'].map((st, i) =>
      `<div class="mrow"><span>${['09:31', '09:31', '09:32', '09:33', '09:33', '09:34'][i]}
        ${SYMS[i % SYMS.length]}</span><span class="v">${st}</span></div>`).join('');
    return `<div class="lbl">Blotter</div>${rows}
      <div class="lbl" style="margin-top:6px">Jobs</div>
      <div class="mrow"><span>bod.refdata</span><span class="v">ok</span></div>
      <div class="mrow"><span>px.load</span><span class="v">ok</span></div>`;
  }

  const PANES = [quotesPane, chartPane, bookPane, blotterPane];

  /* ------------------------------------------------------------ build */

  function neighbours() {
    // Two receding rows of other desks. Silhouettes only: they establish that
    // this is a floor, not a bedroom, and then get out of the way.
    let out = '';
    for (let row = 0; row < 2; row++) {
      for (let i = 0; i < 5; i++) {
        // row 0 is nearer (bigger, lower); row 1 recedes up and shrinks
        const scale = row === 0 ? 1 : 0.68;
        const left = 3 + i * 18.7 + (row ? 9 : 0);
        const w = 12 * scale;
        out += `<div class="nbdesk" style="left:${left}%;width:${w}%;bottom:${row ? 54 : 2}%">
                  <div class="scr" style="width:78%;height:${18 * scale}px"></div>
                  <div class="top"></div>
                  <div class="chair" style="height:${9 * scale}px"></div></div>`;
      }
    }
    return out;
  }

  function build() {
    const el = document.createElement('div');
    el.id = 'intro';
    el.setAttribute('role', 'presentation');
    el.innerHTML = `
      <div class="stage">
        <div class="roombase"></div>
        <div class="office">
          <div class="backwall">
            <div class="glazing">${'<div class="pane"></div>'.repeat(5)}</div>
          </div>
          <div class="daylight"></div>
          <div class="ceiling"><i></i><i></i><i></i></div>
          <div class="floor"></div>
          <div class="neighbours">${neighbours()}</div>
          <div class="shafts"></div>
          <div class="deskglow"></div>
          <div class="vignette"></div>
        </div>

        <div class="workdesk">
          <div class="rig">
            ${PANES.map((fn) => `
              <div class="mon">
                <div class="case">
                  <div class="pane">
                    <div class="content">${fn()}</div>
                    <div class="wake"></div>
                    <div class="reflect"></div>
                  </div>
                </div>
                <div class="stand"></div><div class="base"></div>
              </div>`).join('')}
          </div>
          <div class="desksurface">
            <div class="mug2"></div><div class="keyboard"></div><div class="mouse"></div>
          </div>

          <!-- The analyst. Seen from behind for the whole shot: they walk away
               from the doors and toward the desk, so the camera never sees a face
               and there is nothing to pose. Drawn in the desk's own coordinate
               space so the seated position lands against the desk at any size. -->
          <div class="trader" aria-hidden="true">
            <div class="figure">
              <div class="legs"><i class="leg l"></i><i class="leg r"></i></div>
              <div class="torso"><i class="arm l"></i><i class="arm r"></i><i class="hem"></i></div>
              <div class="head"><i class="hair"></i></div>
            </div>
            <div class="tshadow"></div>
          </div>
          <!-- After the figure in paint order, because from behind the chair back
               is between you and their shoulders. -->
          <div class="chair" aria-hidden="true"><i class="back"></i><i class="post"></i></div>
        </div>
      </div>

      <div class="entrance" aria-hidden="true">
        <div class="jamb"></div>
        <div class="leaf l"></div>
        <div class="leaf r"></div>
        <div class="sign">Trading Floor · 31</div>
      </div>

      <div class="introcap" id="introcap">Ops — New York · 09:12</div>
      <div class="introbar">
        <button type="button" id="introSkip">Skip intro</button>
      </div>`;
    return el;
  }

  /* ------------------------------------------------------------ run */

  function finish(el) {
    timers.forEach(clearTimeout); timers = [];
    el.classList.add('gone');
    try { sessionStorage.setItem(SEEN_KEY, '1'); } catch { /* private mode: replay next time */ }
    setTimeout(() => el.remove(), 560);
    document.body.style.removeProperty('overflow');
    document.removeEventListener('keydown', onKey);
    window.introPending = false;
    window.dispatchEvent(new CustomEvent('intro:done'));
  }

  let current = null;
  const onKey = (e) => { if (current) { e.preventDefault(); finish(current); } };

  function run() {
    const el = build();
    current = el;
    document.body.appendChild(el);
    document.body.style.overflow = 'hidden';

    el.querySelector('#introSkip').addEventListener('click', (e) => {
      e.stopPropagation(); finish(el);
    });
    el.addEventListener('click', () => finish(el));
    document.addEventListener('keydown', onKey);

    const mons = [...el.querySelectorAll('.mon')];
    const cap = (s) => { const c = el.querySelector('#introcap'); if (c) c.textContent = s; };

    const beat = {
      // the doors part and the analyst steps through
      s0open: () => el.classList.add('s0open'),

      // onto the floor, the camera following them in
      s1: () => el.classList.add('s1'),

      // they reach the desk; screens wake one by one, each with a short bloom
      s2: () => {
        el.classList.remove('s1'); el.classList.add('s2');
        cap('Workstation ny-ops-04 · waking');
        WAKE.forEach(([i, delay]) => at(delay, () => {
          const m = mons[i];
          if (!m) return;
          m.classList.add('flash');
          setTimeout(() => { m.classList.remove('flash'); m.classList.add('on'); }, 110);
        }));
      },

      // they take the chair; it tucks in behind them
      sit: () => { el.classList.add('sit'); cap('ny-ops-04 · signed in'); },

      // the centre screen grows to fill the frame. The room stays lit; what
      // darkens the viewport is a monitor, which is the whole idea.
      s3: () => {
        el.classList.remove('s2'); el.classList.add('s3');
        const pane = mons[1] && mons[1].querySelector('.pane');
        if (!pane) return;
        const r = pane.getBoundingClientRect();
        const t = document.createElement('div');
        t.className = 'takeover';
        Object.assign(t.style, {
          top: `${r.top}px`, left: `${r.left}px`,
          width: `${r.width}px`, height: `${r.height}px`,
        });
        el.appendChild(t);
        // force layout so the transition runs from the monitor's actual rect
        void t.offsetWidth;
        Object.assign(t.style, {
          top: '0px', left: '0px', width: '100vw', height: '100vh', borderRadius: '0px',
        });
      },

      end: () => finish(el),
    };

    // Start the clock only once the scene has actually painted. The first paint
    // of this overlay is not cheap — a room, five window panes, two rows of
    // neighbouring desks and four live monitors — and beats scheduled before it
    // do not tick: the doors used to sit shut through most of their own opening
    // beat and then snap. Two frames is the browser telling us it is ready.
    const begin = () => BEATS.forEach(([ms, name]) => at(ms, () => { if (beat[name]) beat[name](); }));
    requestAnimationFrame(() => requestAnimationFrame(begin));
  }

  // Decided at parse time, not at DOMContentLoaded, and published synchronously.
  // The app's own boot is async (it fetches its data first), so if it looked for
  // #intro it could ask before this module had built it and start the tutorial
  // on top of the film. A flag that exists the moment this file is parsed is
  // something the app can trust whenever it happens to look.
  function willRun() {
    let seen = false;
    try { seen = sessionStorage.getItem(SEEN_KEY) === '1'; } catch { /* ignore */ }
    // Arriving from a Help link means they asked for the tutorial, not the film.
    return !(seen || location.hash === '#skip'
             || new URLSearchParams(location.search).has('tutorial'));
  }
  window.introPending = willRun();

  function boot() {
    if (!window.introPending) return;
    try { run(); } catch (err) {
      // An intro is decoration. If it fails, the app must still be usable — and
      // whatever was waiting on the handoff still has to be released.
      console.warn('intro failed, continuing to app:', err);
      const el = document.getElementById('intro');
      if (el) el.remove();
      document.body.style.removeProperty('overflow');
      window.introPending = false;
      window.dispatchEvent(new CustomEvent('intro:done'));
    }
  }

  // Exposed so the header can offer "replay".
  window.replayIntro = () => {
    try { sessionStorage.removeItem(SEEN_KEY); } catch { /* ignore */ }
    if (!document.getElementById('intro')) run();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else boot();
})();
