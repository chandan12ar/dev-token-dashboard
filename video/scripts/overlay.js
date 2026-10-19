/* Recording overlay: cursor, spotlight ring, captions, title/problem cards.
   Injected into the page by record.py. Exposes window.V.
   Optional per-app config, set BEFORE this file loads (record.py --config-js):
     window.V_CONFIG = {
       accent:   '#c2573a',                 // ring/label/ripple colour (falls back to the app's --acc)
       root:     'main section:not([hidden])',  // where card:/kpi:/text: targets are searched (default: body)
       ready:    '.kpi',                    // selector that proves the app has rendered real data
       resolvers:{ card: (want, root) => el, kpi: (want, root) => el, nav: (want) => el }  // app-specific target prefixes
     }
   Target syntax everywhere (spot/hover/click/scroll): CSS selector | text:<visible text> | any prefix in resolvers. */
(() => {
  if (window.V) return;
  const CFG = window.V_CONFIG || {};
  // Theme: defaults < extract_brand.py output (window.V_BRAND) < V_CONFIG.theme / V_CONFIG.accent
  const TH = Object.assign({ accent: '#c2573a', stageBg: '#0e1016', stageFg: '#f5f3f0', muted: '#b9bcc6', font: 'inherit' },
                           window.V_BRAND || {}, CFG.theme || {}, CFG.accent ? { accent: CFG.accent } : {});
  const ACC = TH.accent;
  const css = `
  #v-root{position:fixed;inset:0;z-index:2147483000;pointer-events:none;font-family:%FONT%;--v-acc:%ACC%;--v-bg:%BG%;--v-fg:%FG%;--v-muted:%MUTED%}
  #v-cursor{position:fixed;left:0;top:0;width:28px;height:28px;margin:-4px 0 0 -4px;z-index:2147483600;
    filter:drop-shadow(0 2px 3px rgba(0,0,0,.35));transition:transform .08s ease-out;pointer-events:none;opacity:0}
  #v-cursor.on{opacity:1}
  .v-rip{position:fixed;z-index:2147483500;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
    border:3px solid var(--v-acc);pointer-events:none;animation:v-rip .55s ease-out forwards}
  @keyframes v-rip{from{transform:scale(.4);opacity:.95}to{transform:scale(3.4);opacity:0}}
  #v-spot{position:fixed;z-index:2147483100;border-radius:16px;opacity:0;pointer-events:none;
    box-shadow:0 0 0 3px var(--v-acc),0 0 0 9999px rgba(12,14,20,.34),0 0 36px 4px color-mix(in srgb,var(--v-acc) 35%,transparent);
    transition:opacity .35s ease}
  #v-spot.on{opacity:1}
  #v-spot.pulse{animation:v-pulse 2.2s ease-in-out infinite}
  @keyframes v-pulse{50%{box-shadow:0 0 0 5px var(--v-acc),0 0 0 9999px rgba(12,14,20,.34),0 0 52px 10px color-mix(in srgb,var(--v-acc) 50%,transparent)}}
  #v-label{position:fixed;z-index:2147483200;padding:7px 14px;border-radius:999px;background:var(--v-acc);color:#fff;
    font-weight:650;font-size:15px;letter-spacing:.01em;box-shadow:0 6px 20px rgba(0,0,0,.28);opacity:0;
    transition:opacity .3s ease;white-space:nowrap}
  #v-label.on{opacity:1}
  #v-cap{font-family:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;position:fixed;left:50%;bottom:30px;transform:translate(-50%,14px);z-index:2147483400;max-width:1040px;width:max-content;
    padding:13px 26px;border-radius:16px;background:rgba(14,16,22,.82);backdrop-filter:blur(8px);color:#fff;
    font-size:21px;line-height:1.4;text-align:center;font-weight:500;opacity:0;transition:opacity .3s ease,transform .3s ease;
    box-shadow:0 10px 40px rgba(0,0,0,.35)}
  #v-cap.on{opacity:1;transform:translate(-50%,0)}
  #v-cap .vw{opacity:.62;transition:opacity .12s ease,background .12s ease;padding:1px 3px;border-radius:7px;margin:0 -1px}
  #v-cap .vw.vw-done{opacity:1}
  #v-cap .vw.vw-cur{opacity:1;background:var(--v-acc);color:#fff;box-shadow:0 2px 10px color-mix(in srgb,var(--v-acc) 55%,transparent)}
  .v-blur{filter:blur(7px)!important}
  #v-badge{position:fixed;top:14px;right:18px;z-index:2147483300;padding:5px 12px;border-radius:999px;
    background:rgba(14,16,22,.8);color:#fff;font-size:13px;font-weight:600;letter-spacing:.03em;opacity:0}
  #v-badge.on{opacity:1}
  #v-stage{position:fixed;inset:0;z-index:2147483300;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(1200px 700px at 78% 12%,color-mix(in srgb,var(--v-acc) 20%,transparent),transparent 60%),
               radial-gradient(900px 600px at 8% 95%,rgba(90,110,220,.14),transparent 60%),var(--v-bg);
    color:var(--v-fg);font-family:%FONT%;opacity:0;transition:opacity .7s ease;pointer-events:none}
  #v-stage.on{opacity:1}
  .vs-in{width:1180px;max-width:90vw;display:flex;flex-direction:column;gap:22px}
  .vs-in.center{align-items:center;text-align:center}
  .vs-eyebrow{font-size:17px;letter-spacing:.18em;text-transform:uppercase;color:color-mix(in srgb,var(--v-acc) 72%,#fff);font-weight:700}
  .vs-title{font-size:62px;line-height:1.08;font-weight:760;letter-spacing:-.02em;margin:0}
  .vs-title em{font-style:normal;color:color-mix(in srgb,var(--v-acc) 62%,#fff)}
  .vs-sub{font-size:27px;line-height:1.4;color:var(--v-muted);max-width:980px;margin:0}
  .vs-mark{width:96px;height:96px;border-radius:26px;background:linear-gradient(145deg,color-mix(in srgb,var(--v-acc) 88%,#fff),color-mix(in srgb,var(--v-acc) 80%,#000));display:grid;place-items:center;
    box-shadow:0 18px 50px color-mix(in srgb,var(--v-acc) 45%,transparent)}
  .vs-mark svg{width:52px;height:52px;color:#fff}
  .vs-list{display:flex;flex-direction:column;gap:12px;margin-top:6px}
  .vs-item{display:grid;grid-template-columns:270px 1fr;gap:22px;align-items:center;padding:15px 24px;border-radius:16px;
    background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09);opacity:0;transform:translateY(16px);
    transition:opacity .5s ease,transform .5s ease}
  .vs-item.on{opacity:1;transform:none}
  .vs-k{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:23px;color:color-mix(in srgb,var(--v-acc) 62%,#fff);font-weight:600}
  .vs-v{font-size:23px;color:color-mix(in srgb,var(--v-fg) 86%,transparent)}
  .vs-item.q{grid-template-columns:1fr}
  .vs-item.q .vs-v{font-size:27px;color:var(--v-fg)}
  .vs-chips{display:flex;gap:16px;flex-wrap:wrap;justify-content:center;margin-top:10px}
  .vs-chip{padding:13px 26px;border-radius:999px;font-size:25px;font-weight:650;background:rgba(255,255,255,.08);
    border:1px solid rgba(255,255,255,.14);opacity:0;transform:translateY(12px);transition:opacity .5s ease,transform .5s ease}
  .vs-chip.on{opacity:1;transform:none}
  .vs-cmd{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:27px;padding:16px 30px;border-radius:14px;
    background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);color:color-mix(in srgb,var(--v-acc) 40%,#fff)}
  .vs-url{font-size:25px;color:var(--v-muted)}
  `;
  const st = document.createElement('style'); st.textContent = css.replace(/%ACC%/g, ACC).replace(/%BG%/g, TH.stageBg).replace(/%FG%/g, TH.stageFg).replace(/%MUTED%/g, TH.muted).replace(/%FONT%/g, TH.font); document.head.appendChild(st);
  const root = document.createElement('div'); root.id = 'v-root';
  root.innerHTML = `
    <div id="v-spot"></div><div id="v-label"></div><div id="v-cap"></div><div id="v-badge"></div>
    <div id="v-stage"></div>`;
  document.body.appendChild(root);
  const cur = document.createElement('div'); cur.id = 'v-cursor';
  cur.innerHTML = `<svg viewBox="0 0 24 24" width="28" height="28"><path d="M4 2l15 10.2-6.4 1.3 3.6 6.9-2.9 1.5-3.6-7L4 18.5z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  document.body.appendChild(cur);
  const $ = id => document.getElementById(id);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

  addEventListener('mousemove', e => {
    cur.classList.add('on'); cur.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
  }, true);
  addEventListener('mousedown', e => {
    const r = document.createElement('div'); r.className = 'v-rip';
    r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
    document.body.appendChild(r); setTimeout(() => r.remove(), 700);
  }, true);

  function sec() { return (CFG.root && document.querySelector(CFG.root)) || document.body; }
  function find(t) {
    if (!t) return null;
    const m = /^([a-z]+):(.*)$/.exec(t);
    if (m && CFG.resolvers && CFG.resolvers[m[1]]) return CFG.resolvers[m[1]](m[2].trim(), sec());
    if (m && m[1] === 'text') {    // first element whose own visible text equals the target (case-insensitive)
      const want = m[2].trim().toLowerCase();
      for (const e of sec().querySelectorAll('h1,h2,h3,h4,label,button,a,th,td,span,p,li'))
        if (e.textContent.trim().toLowerCase() === want) return e;
      return null;
    }
    try { return document.querySelector(t); } catch (e) { return null; }
  }
  function rectOf(targets) {
    const arr = (Array.isArray(targets) ? targets : [targets]).map(find).filter(el => el && el.getClientRects().length);   // hidden elements have no box
    if (!arr.length) return null;
    let l = 1e9, tp = 1e9, r = -1e9, b = -1e9;
    for (const el of arr) { const q = el.getBoundingClientRect(); l = Math.min(l, q.left); tp = Math.min(tp, q.top); r = Math.max(r, q.right); b = Math.max(b, q.bottom); }
    return { l, t: tp, r, b };
  }

  let capTimers = [];
  const AUTO = [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b/g, /\bgh[pousr]_[A-Za-z0-9]{20,}\b/g,
                /\bAKIA[0-9A-Z]{16}\b/g, /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/g, /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b/g,
                /\b[a-f0-9]{32,}\b/gi, /\b(?:\d{1,3}\.){3}\d{1,3}\b/g];
  function blurSetup(cfg) {
    const sels = (cfg.selectors || []).filter(Boolean);
    if (sels.length) { const b = document.createElement('style'); b.textContent = sels.join(',') + '{filter:blur(7px)!important}'; document.head.appendChild(b); }
    if (!cfg.auto) return;
    let t = 0;
    const scan = () => {
      obs.disconnect();
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, { acceptNode: n =>
        n.parentElement && !n.parentElement.closest('#v-root,#v-stage,script,style,.v-blur') && AUTO.some(r => { r.lastIndex = 0; return r.test(n.nodeValue); })
          ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT });
      const nodes = []; while (w.nextNode()) nodes.push(w.currentNode);
      for (const n of nodes) {
        const frag = document.createDocumentFragment(); let txt = n.nodeValue, parts = [{ s: txt, hit: false }];
        for (const r of AUTO) parts = parts.flatMap(p => p.hit ? [p] : (() => { const o = []; let last = 0; r.lastIndex = 0; let m;
          while ((m = r.exec(p.s))) { o.push({ s: p.s.slice(last, m.index), hit: false }, { s: m[0], hit: true }); last = m.index + m[0].length; }
          o.push({ s: p.s.slice(last), hit: false }); return o; })());
        for (const p of parts) { if (!p.s) continue; if (p.hit) { const sp = document.createElement('span'); sp.className = 'v-blur'; sp.textContent = p.s; frag.appendChild(sp); } else frag.appendChild(document.createTextNode(p.s)); }
        n.parentNode.replaceChild(frag, n);
      }
      obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    };
    const obs = new MutationObserver(() => { clearTimeout(t); t = setTimeout(scan, 150); });
    scan();
  }

  let spotTargets = null, spotLabel = null, raf = 0;
  function track() {
    const sp = $('v-spot'), lb = $('v-label');
    if (spotTargets) {
      const q = rectOf(spotTargets);
      if (!q) { sp.classList.remove('on'); lb.classList.remove('on'); }   // target hidden (tab switched): no stray dimming
      if (q) {
        sp.classList.add('on'); lb.classList.toggle('on', !!spotLabel);
        const pad = 8;
        sp.style.left = (q.l - pad) + 'px'; sp.style.top = (q.t - pad) + 'px';
        sp.style.width = (q.r - q.l + pad * 2) + 'px'; sp.style.height = (q.b - q.t + pad * 2) + 'px';
        if (spotLabel) {
          lb.textContent = spotLabel;
          const lw = lb.offsetWidth;
          let ly = q.t - pad - 44;
          if (ly < 100) ly = q.b + pad + 10;   // no room above (sticky header): put it below
          lb.style.left = Math.max(16, Math.min(innerWidth - lw - 16, q.l - pad)) + 'px'; lb.style.top = ly + 'px';
        }
      }
    }
    raf = requestAnimationFrame(track);
  }

  window.V = {
    find,
    async ready() {   // wait until the app has rendered real data (CFG.ready), then settle
      for (let i = 0; i < 80 && CFG.ready; i++) { if (document.querySelector(CFG.ready)) break; await sleep(100); }
      if (window.V_BLUR) blurSetup(window.V_BLUR);
      await sleep(500);
    },
    spot(targets, label) {
      const sp = $('v-spot'), lb = $('v-label');
      if (!targets) { spotTargets = null; sp.classList.remove('on', 'pulse'); lb.classList.remove('on'); return true; }
      if (!rectOf(targets)) return false;
      spotTargets = targets; spotLabel = label || null;
      if (!raf) raf = requestAnimationFrame(track);
      track_once();
      sp.classList.add('on', 'pulse');
      lb.classList.toggle('on', !!label);
      return true;
    },
    // caption(text, {words:[[start,end],...], delay}) - with word times (seconds from the start of the narration clip)
    // the spoken word is highlighted as it is said (karaoke style); without them the sentence is shown plain.
    caption(text, opts) {
      const c = $('v-cap');
      capTimers.forEach(clearTimeout); capTimers = [];
      if (!text) { c.classList.remove('on'); return; }
      const parts = text.split(/\s+/).filter(Boolean);
      const tm = opts && opts.words && opts.words.length === parts.length ? opts.words : null;
      if (!tm) { c.textContent = text; c.classList.add('on'); return; }
      c.innerHTML = parts.map(w => `<span class="vw">${w.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</span>`).join(' ');
      c.classList.add('on');
      const sp = c.querySelectorAll('.vw'), d = (opts.delay || 0) * 1000;
      tm.forEach(([a], i) => {
        capTimers.push(setTimeout(() => { if (i) { sp[i - 1].classList.remove('vw-cur'); sp[i - 1].classList.add('vw-done'); } sp[i].classList.add('vw-cur'); }, d + a * 1000));
      });
      capTimers.push(setTimeout(() => { sp[sp.length - 1].classList.remove('vw-cur'); sp[sp.length - 1].classList.add('vw-done'); }, d + tm[tm.length - 1][1] * 1000));
    },
    // blur sensitive things: V.blur({selectors:['.email','#api-key'], auto:true}); auto also masks emails, API keys/tokens, IPs
    blur(cfg) { blurSetup(cfg || {}); },
    badge(text) { const b = $('v-badge'); if (!text) { b.classList.remove('on'); return; } b.textContent = text; b.classList.add('on'); },
    rect(target) {   // viewport centre of an element, for cursor moves
      const q = rectOf(target); if (!q) return null;
      return { x: (q.l + q.r) / 2, y: (q.t + q.b) / 2, l: q.l, t: q.t, r: q.r, b: q.b };
    },
    scrollInfo(target, center) {
      const el = find(target); if (!el) return null;
      const q = el.getBoundingClientRect(), max = document.scrollingElement.scrollHeight - innerHeight;
      let y = q.top + scrollY - 112;
      if (center && q.height < innerHeight - 220) y = q.top + scrollY - (innerHeight - q.height) / 2 + 20;
      return Math.max(0, Math.min(max, y));
    },
    async scrollTo(y, ms) {
      const y0 = scrollY, t0 = performance.now();
      if (Math.abs(y - y0) < 2) return;
      await new Promise(res => {
        const step = now => {
          const k = Math.min(1, (now - t0) / ms);
          scrollTo(0, y0 + (y - y0) * ease(k));
          k < 1 ? requestAnimationFrame(step) : res();
        };
        requestAnimationFrame(step);
      });
    },
    async hscroll(target, to, ms) {   // scroll a horizontally-overflowing table (to: 0..1)
      const el = find(target); if (!el) return null;   // null = target missing, false = nothing to scroll
      const sc = el.scrollWidth > el.clientWidth + 8 ? el : (Array.from(el.querySelectorAll('*')).find(n => n.scrollWidth > n.clientWidth + 8 && getComputedStyle(n).overflowX !== 'visible') || el);
      const max = sc.scrollWidth - sc.clientWidth; if (max < 8) return false;
      const x0 = sc.scrollLeft, x1 = max * to, t0 = performance.now();
      await new Promise(res => {
        const step = now => { const k = Math.min(1, (now - t0) / ms); sc.scrollLeft = x0 + (x1 - x0) * ease(k); k < 1 ? requestAnimationFrame(step) : res(); };
        requestAnimationFrame(step);
      });
      return true;
    },
    stage(spec) {
      const s = $('v-stage');
      if (!spec) { s.classList.remove('on'); return; }
      const mark = `<div class="vs-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg></div>`;
      const esc = x => String(x == null ? '' : x).replace(/&/g, '&amp;').replace(/</g, '&lt;');
      let h = '';
      const center = spec.align === 'center';
      h += `<div class="vs-in ${center ? 'center' : ''}">`;
      if (spec.mark) h += mark;
      if (spec.eyebrow) h += `<div class="vs-eyebrow">${esc(spec.eyebrow)}</div>`;
      if (spec.title) h += `<h1 class="vs-title">${esc(spec.title).replace(/\*\*(.+?)\*\*/g, '<em>$1</em>')}</h1>`;
      if (spec.sub) h += `<p class="vs-sub">${esc(spec.sub)}</p>`;
      if (spec.items) h += `<div class="vs-list">` + spec.items.map(i => i.k
        ? `<div class="vs-item"><span class="vs-k">${esc(i.k)}</span><span class="vs-v">${esc(i.v)}</span></div>`
        : `<div class="vs-item q"><span class="vs-v">${esc(i.v)}</span></div>`).join('') + `</div>`;
      if (spec.chips) h += `<div class="vs-chips">` + spec.chips.map(c => `<span class="vs-chip">${esc(c)}</span>`).join('') + `</div>`;
      if (spec.cmd) h += `<div class="vs-cmd">${esc(spec.cmd)}</div>`;
      if (spec.url) h += `<div class="vs-url">${esc(spec.url)}</div>`;
      h += `</div>`;
      s.innerHTML = h;
      requestAnimationFrame(() => s.classList.add('on'));
    },
    reveal(n) {   // show the first n list items / chips on the stage card
      const els = document.querySelectorAll('#v-stage .vs-item, #v-stage .vs-chip');
      els.forEach((e, i) => e.classList.toggle('on', i < n));
    },
    revealCount() { return document.querySelectorAll('#v-stage .vs-item, #v-stage .vs-chip').length; },
    hideCursor() { cur.classList.remove('on'); }
  };
  function track_once() { /* force a layout pass so the ring is placed before it fades in */
    const q = rectOf(spotTargets);
    if (q) { const sp = $('v-spot'), pad = 8;
      sp.style.left = (q.l - pad) + 'px'; sp.style.top = (q.t - pad) + 'px';
      sp.style.width = (q.r - q.l + pad * 2) + 'px'; sp.style.height = (q.b - q.t + pad * 2) + 'px'; }
  }
})();
