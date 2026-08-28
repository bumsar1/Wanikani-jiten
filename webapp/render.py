"""Page rendering for the hosted version.

Deliberately reuses the stylesheet and the interactive components from the
command-line tool rather than forking them: the grid, chart, slider, browse
panel and reach panel are all plain strings in wkjiten.py, so both versions
stay in step.
"""

from __future__ import annotations

import calendar
import hashlib
import re
import json
import time
import urllib.parse
from collections import Counter

import wkjiten as w

STATUS_LABELS = w.STATUS_LABELS
MEDIA_TYPES = w.MEDIA_TYPES

# Served as a real file rather than inlined: the local tool writes one page that
# has to stand alone, this one serves dozens and can let the browser cache it.
ICON = "/icon.png"


def esc(s) -> str:
    return w.esc(s)


AUTH_CSS = """
.authbox { max-width:400px; margin:8vh auto; background:var(--raise);
  border:1px solid var(--line); border-radius:14px; padding:28px;
  box-shadow:var(--shadow); }
.authbox h1 { font-size:24px; margin-bottom:18px; }
.authbox .mark { width:60px; height:60px; display:block; margin:0 auto 12px;
  border-radius:14px; }
.authbox .mark + h1 { text-align:center; }   /* only where there is a mark */
.field { margin-bottom:14px; }
.field label { display:block; font-size:12.5px; color:var(--faint); margin-bottom:5px;
  text-transform:uppercase; letter-spacing:.07em; font-weight:600; }
.field input { width:100%; font:inherit; padding:10px 13px; border-radius:10px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); }
.field .hint { font-size:12.5px; color:var(--faint); margin-top:5px; }
.err { background:var(--accent-soft); color:var(--accent); padding:10px 14px;
  border-radius:10px; font-size:14px; margin-bottom:14px; }
.ok { background:var(--accent-soft); color:var(--good); padding:10px 14px;
  border-radius:10px; font-size:14px; margin-bottom:14px; }
.topbar { display:flex; justify-content:space-between; align-items:center;
  gap:12px; padding:14px 0; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.topbar .who { color:var(--muted); font-size:13px; }
.topbar nav a.here { color:var(--accent); background:var(--accent-soft); }

.tier { display:flex; align-items:center; gap:var(--s4); margin:0 0 var(--s3);
  background:var(--raise); border:1px solid var(--line);
  border-radius:var(--r-panel); padding:var(--s3) var(--s4) var(--s3) var(--s3);
  box-shadow:var(--shadow); }
.tier img { width:96px; height:64px; object-fit:cover; border-radius:var(--r-box);
  flex:none; }
.tier h3 { margin:0; font-size:20px; letter-spacing:-.01em; }
.tier .sub { margin:0; display:block; font-size:13px; }
/* The line for the level you are on. It is the only sentence on the page that
   is not a number, so it gets room and a colour of its own. */
.tier .verse { margin:7px 0 0; font-size:15px; color:var(--fg); max-width:52ch;
  font-style:italic; letter-spacing:-.005em; }
.tier .verse::before { content:"“"; color:var(--accent); margin-right:2px; }
.tier .verse::after { content:"”"; color:var(--accent); margin-left:1px; }

/* Thirty days of what could burn. Bars rather than a table, because the
   question is which week is heavy, not what Tuesday's exact number is. */
.burncal { display:flex; align-items:flex-end; gap:3px; height:104px;
  margin:0 0 var(--s4); padding:var(--s3) var(--s3) 0;
  border:1px solid var(--line); border-radius:var(--r-panel);
  background:var(--raise); box-shadow:var(--shadow); }
.burncal .bar { flex:1 1 0; display:flex; flex-direction:column;
  align-items:center; justify-content:flex-end; height:100%; min-width:0;
  cursor:default; }
.burncal .bar i { display:block; width:100%; border-radius:3px 3px 0 0;
  background:var(--accent); opacity:.85;
  transition:opacity var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
  transform-origin:bottom; animation:grow 420ms var(--ease) both; }
.burncal .bar.we i { background:var(--good); }
.burncal .bar.none i { background:var(--line); }
.burncal .bar:hover i { opacity:1; transform:scaleY(1.04); }
.burncal .bar em { font-style:normal; font-size:10px; color:var(--faint);
  margin-top:4px; font-variant-numeric:tabular-nums; }
@keyframes grow { from { transform:scaleY(0); } }
.burncal .bar:nth-child(n+2) i { animation-delay:20ms; }
.burncal .bar:nth-child(n+8) i { animation-delay:60ms; }
.burncal .bar:nth-child(n+15) i { animation-delay:100ms; }
.burncal .bar:nth-child(n+22) i { animation-delay:140ms; }

/* The level-up. Loud by the standards of this page, which is a low bar, and
   gone again at the next refresh. */
.levelup { display:flex; align-items:center; gap:var(--s4); position:relative;
  overflow:hidden; margin:0 0 var(--s3); padding:var(--s3) var(--s4);
  border:1px solid var(--accent); border-radius:var(--r-panel);
  background:linear-gradient(100deg,var(--accent-soft),var(--raise) 60%);
  box-shadow:0 12px 40px -22px var(--accent);
  animation:settle 420ms var(--ease) both; }
.levelup .crab { image-rendering:pixelated; flex:none; width:112px; height:72px;
  animation:hop 900ms var(--ease) 220ms 1; }
@keyframes hop {
  0%   { transform:translateY(26px); opacity:0; }
  35%  { transform:translateY(-14px); opacity:1; }
  60%  { transform:translateY(0); }
  70%  { transform:translateY(-5px); }
  100% { transform:translateY(0); }
}
.levelup .say { min-width:0; }
.levelup h3 { margin:0; font-size:22px; letter-spacing:-.015em; }
.levelup h3 b { color:var(--accent); }
.levelup .sub { margin:2px 0 0; }
.levelup .verse { margin:8px 0 0; font-style:italic; font-size:15px;
  max-width:46ch; }
.levelup .tierart { width:150px; height:100px; object-fit:cover; flex:none;
  border-radius:var(--r-box); margin-left:auto; }
@media (max-width:700px) { .levelup .tierart { display:none; } }

/* What is behind a delta. */
.card button.d { font:inherit; font-size:12.5px; font-weight:650; padding:0;
  border:0; background:none; color:var(--accent); cursor:pointer;
  border-bottom:1px dashed var(--accent); border-radius:0; margin-top:5px; }
.card button.d:hover { filter:brightness(1.15); background:none; }
.card .newly { display:flex; flex-wrap:wrap; gap:4px; margin-top:9px; }
.card .newly[hidden] { display:none; }
.card .ni { font-size:15px; padding:1px 6px; border-radius:var(--r-ctl);
  background:var(--accent-soft); color:var(--accent);
  animation:tilein 260ms var(--ease) both; }
.card .ni.more { background:none; color:var(--faint); }

.ladder { display:grid; gap:var(--s3); margin:0 0 var(--s4);
  grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }
.rung { margin:0; border:1px solid var(--line); border-radius:var(--r-panel);
  overflow:hidden; background:var(--raise); position:relative;
  transition:transform var(--t-base) var(--ease),
             border-color var(--t-base) var(--ease); }
.rung img { display:block; width:100%; height:auto; }
.rung { padding:0; font:inherit; text-align:left; cursor:pointer;
  white-space:normal; display:block; width:100%; }
.rung .cap { display:flex; justify-content:space-between; align-items:baseline;
  gap:var(--s2); padding:9px 12px; font-size:13px; color:var(--muted); }
.rung .cap b { color:var(--fg); font-size:14px; }
.rung[aria-expanded="true"] { border-color:var(--accent); }
.rung[aria-expanded="true"] img { filter:none; }
.rung.done img { filter:grayscale(1) brightness(.55); }
.rung.ahead img { filter:brightness(.75) saturate(.7); }
.rung.now { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent),
  0 10px 30px -12px var(--accent); }
.rung.now figcaption b { color:var(--accent); }
.rung:hover { transform:translateY(-2px); border-color:var(--accent); }
.rung:hover img { filter:none; }

/* What is inside a stretch, opened from its card. */
.tierbox { margin:0 0 var(--s4); border:1px solid var(--line);
  border-radius:var(--r-panel); background:var(--raise); padding:var(--s4);
  box-shadow:var(--shadow); animation:settle 300ms var(--ease) both; }
.tierbox[hidden] { display:none; }
.tierbox h4 { margin:0 0 2px; font-size:16px; }
.tierbox .sub { margin:0 0 var(--s3); }
.tierbox .lvlrow { margin-bottom:6px; }
.tierbox .words { display:flex; flex-wrap:wrap; gap:5px; margin-top:var(--s2); }
.tierbox .wd { font-size:14px; padding:3px 9px; border-radius:var(--r-pill);
  border:1px solid var(--line); color:var(--muted); background:var(--bg); }
.tierbox .wd.in { animation:tilein 300ms var(--ease) both; }
.tierbox .k, .tierbox .wd { cursor:pointer; }
.tierbox .k.sel { outline:2px solid var(--fg); outline-offset:2px; }
.tierbox .wd.sel { border-color:var(--accent); color:var(--accent); }

/* The answer to a click, in one place under the panel rather than a tooltip
   that follows the pointer around. It sticks, so it is still there when you
   have scrolled down a few levels looking for the next one. */
.pick { position:sticky; bottom:0; display:flex; align-items:center; gap:var(--s3);
  margin-top:var(--s3); padding:10px 14px; border-radius:var(--r-box);
  border:1px solid var(--line); background:var(--bg);
  animation:settle 220ms var(--ease) both; }
.pick[hidden] { display:none; }
.pick .big { font-size:30px; line-height:1; }
.pick .what { display:flex; flex-direction:column; gap:1px; min-width:0; }
.pick .what b { font-size:15px; }
.pick .rd { color:var(--muted); font-size:14px; }
.pick .sub { margin:0; font-size:12.5px; }
.tierbox .wd.on { color:var(--fg); border-color:var(--good); }
.tierbox .more { color:var(--faint); font-size:13px; margin-top:var(--s2); }

/* WaniKani's own stage colours, the same five the kanji grid paints with, so a
   tile means the same thing on both pages. */
.tierbox .b-unstarted   { background:var(--k0); }
.tierbox .b-apprentice  { background:#dd0093; }
.tierbox .b-guru        { background:#882d9e; }
.tierbox .b-master      { background:#294ddb; }
.tierbox .b-enlightened { background:#0093dd; }
.tierbox .b-burned      { background:#8a7355; }

/* A shape in the right place beats a spinner in the middle of nowhere. */
.skel { display:grid; gap:8px; }
.skel i { display:block; height:26px; border-radius:var(--r-ctl);
  background:linear-gradient(90deg,var(--line-soft),var(--line),var(--line-soft));
  background-size:220% 100%; animation:shimmer 1.1s linear infinite; }
.skel i:nth-child(2) { width:82%; }
.skel i:nth-child(3) { width:64%; }
@keyframes shimmer { from { background-position:120% 0; }
                     to   { background-position:-120% 0; } }
.topbar nav { margin:0; }
form.inline { display:inline; }
.code { font-family:ui-monospace,Menlo,Consolas,monospace; background:var(--bg);
  padding:2px 7px; border-radius:6px; border:1px solid var(--line); }
"""


MOBILE_CSS = """
/* A phone gets one thing at a time: every section folds to its heading, and
   the jump links become a bar that stays put while you scroll. The wide layout
   is untouched - this is the same page, read on a narrow screen. */
@media (max-width:700px) {
  main > nav { position:sticky; top:0; z-index:30; flex-wrap:nowrap;
    overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;
    margin:0 -14px 4px; padding:9px 14px;
    background:var(--bg); border-bottom:1px solid var(--line); }
  main > nav::-webkit-scrollbar { display:none; }
  main > nav a { white-space:nowrap; padding:7px 12px; font-size:13px; }

  h2.acc { display:flex; align-items:center; gap:10px; cursor:pointer;
    margin:0; padding:16px 2px; border-bottom:1px solid var(--line);
    scroll-margin-top:54px; }
  h2.acc::after { content:"+"; margin-left:auto; font-size:18px; font-weight:400;
    color:var(--faint); line-height:1; }
  h2.acc.open { color:var(--accent); }
  h2.acc.open::after { content:"–"; color:var(--accent); }
  .acc-hidden { display:none !important; }
}
"""

MOBILE_JS = """
(function(){
  const NARROW = 700;
  const main = document.querySelector('main');
  if (!main) return;

  // Each h2 owns everything up to the next one. Grouping it here means no
  // section had to be written twice to be foldable, and the hero, the cards
  // and the footer - which belong to no heading - stay put.
  const groups = [];
  let cur = null;
  for (const el of [...main.children]){
    if (el.tagName === 'H2'){ cur = {head: el, body: []}; groups.push(cur); }
    else if (el.tagName === 'FOOTER'){ cur = null; }
    else if (cur){ cur.body.push(el); }
  }
  if (!groups.length) return;

  let on = false;
  const isOpen = g => g.head.classList.contains('open');
  function show(g, open){
    g.head.classList.toggle('open', open);
    g.body.forEach(el => el.classList.toggle('acc-hidden', !open));
    // The heading is the fold now, so a section that keeps its own content
    // behind a "show me" summary would cost two taps to read. Open those on
    // the way in - but only the content ones: the tag picker is a menu
    // wearing a <details>, and it has to stay shut.
    if (!open) return;
    for (const el of g.body){
      if (el.matches && el.matches('details.fold')) el.open = true;
      if (el.querySelectorAll)
        el.querySelectorAll('details.fold').forEach(d => { d.open = true; });
    }
  }
  function enable(){
    on = true;
    groups.forEach(g => {
      g.head.classList.add('acc');
      g.head.setAttribute('role', 'button');
      g.head.setAttribute('tabindex', '0');
      show(g, false);
    });
  }
  function disable(){
    on = false;
    groups.forEach(g => {
      g.head.classList.remove('acc', 'open');
      g.head.removeAttribute('role');
      g.head.removeAttribute('tabindex');
      g.body.forEach(el => el.classList.remove('acc-hidden'));
    });
  }

  groups.forEach(g => {
    const toggle = () => { if (on) show(g, !isOpen(g)); };
    g.head.addEventListener('click', toggle);
    g.head.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggle(); }
    });
  });

  // A jump link has to open what it jumps to, or the anchor lands on a closed
  // heading and the tap looks like it did nothing. Runs before the browser
  // scrolls, so it lands on the section rather than above it.
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]');
    if (!a || !on) return;
    const g = groups.find(g => g.head.id === a.getAttribute('href').slice(1));
    if (g) show(g, true);
  });

  function sync(){
    const narrow = window.innerWidth <= NARROW;
    if (narrow && !on) enable();
    else if (!narrow && on) disable();
  }
  addEventListener('resize', sync);
  sync();
})();
"""


BURN_CSS = """
.pager { display:flex; align-items:center; gap:12px; margin:10px 0 2px;
  font-size:13px; color:var(--muted); }
.pager button { font:inherit; font-size:13px; color:var(--muted);
  background:var(--raise); border:1px solid var(--line); border-radius:10px;
  padding:5px 11px; cursor:pointer; }
.pager button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.pager button:disabled { opacity:.4; cursor:default; }
.pager span { font-variant-numeric:tabular-nums; }

.dn { margin-top:2px; }
.dn > button { font-size:14px; line-height:1; padding:4px 10px; opacity:.45;
  transition:opacity var(--t-fast) var(--ease); }
.dn > button:hover, .dn > button.done { opacity:1; }
.dnbox { margin-top:10px; max-width:420px; border:1px solid var(--line);
  border-radius:var(--r-panel); overflow:hidden; background:var(--raise);
  box-shadow:var(--lift); animation:dnin var(--t-base) var(--ease); }
.dnbox[hidden] { display:none; }
.dnbox video { display:block; width:100%; height:auto; }
.dnbox .sub { margin:0; padding:9px 14px 11px; font-size:13px; }
@keyframes dnin { from { opacity:0; transform:translateY(-6px); } }

/* A kanji cell is 26px, and the shared "td:first-child:not(.num)" rule asks
   for 11em of it - 286px of mostly empty column. td.kanji answers that with
   min-width:auto but loses on specificity, so say it again from the id. */
#burn td.kanji { min-width:auto; }

/* Six columns of this needed dragging sideways on a phone. Type and level are
   the two you would not have opened the list for, so they go and the rest
   fits. */
@media (max-width:700px) {
  #burn { min-width:0; font-size:13px; }
  #burn th, #burn td { padding-left:9px; padding-right:9px; }
  #burn th:nth-child(4), #burn td:nth-child(4),
  #burn th:nth-child(5), #burn td:nth-child(5) { display:none; }
}
"""

DN_JS = """
(function(){
  const btn = document.getElementById('dnplay'), box = document.getElementById('dnbox');
  if (!btn || !box) return;
  const v = box.querySelector('video');
  btn.addEventListener('click', () => {
    const opening = box.hasAttribute('hidden');
    if (opening){
      if (!v.querySelector('source')){
        v.innerHTML = '<source src="/asset/deathnote.webm" type="video/webm">'
                    + '<source src="/asset/deathnote.mp4" type="video/mp4">';
        v.load();
      }
      box.removeAttribute('hidden');
      v.play().catch(() => {});
    } else {
      v.pause();
      box.setAttribute('hidden', '');
    }
    btn.classList.toggle('done', opening);
  });
})();
"""

CARD_JS = """
(function(){
  // He jumps, he lands. Two files, so the swap happens when the hop ends
  // rather than being animated as one sheet.
  const crab = document.querySelector('.levelup .crab');
  if (crab){
    crab.addEventListener('animationend', () => {
      crab.src = '/asset/crabigator-land.png';
      setTimeout(() => { crab.src = '/asset/crabigator-idle.png'; }, 420);
    }, {once: true});
  }

  // The chips are already on the page; this only decides whether they are
  // shown, and staggers them so a wall of thirty becomes a run of thirty.
  for (const btn of document.querySelectorAll('.card button.d')){
    const box = btn.nextElementSibling;
    if (!box) continue;
    btn.addEventListener('click', () => {
      const opening = box.hidden;
      box.hidden = !opening;
      btn.setAttribute('aria-expanded', String(opening));
      if (opening)
        [...box.children].forEach((c, i) => {
          c.style.animation = 'none';
          void c.offsetWidth;
          c.style.animation = `tilein 260ms var(--ease) ${Math.round(i * 12)}ms both`;
        });
    });
  }
})();
"""

TIER_JS = """
(function(){
  const box = document.getElementById('tierbox');
  const rungs = [...document.querySelectorAll('.rung')];
  if (!box || !rungs.length) return;

  // The same five bands the kanji grid uses, so a tile means the same thing on
  // both pages.
  const BAND = s => s >= 9 ? 'burned' : s >= 8 ? 'enlightened' : s >= 7 ? 'master'
                  : s >= 5 ? 'guru' : s >= 1 ? 'apprentice' : 'unstarted';
  const cache = new Map();
  let open = null;

  // Same sweep as the kanji grid: the tiles fill in on a diagonal rather than
  // landing in one frame, and the step shrinks as the rows multiply so ten
  // levels take about as long as one.
  let front = 0;
  function rows(items){
    const byLevel = new Map();
    for (const it of items){
      if (!byLevel.has(it.l)) byLevel.set(it.l, []);
      byLevel.get(it.l).push(it);
    }
    const list = [...byLevel].sort((a, b) => a[0] - b[0]);
    const step = Math.min(34, 420 / Math.max(1, list.length));
    front = 0;
    return list.map(([lv, tiles], row) =>
      `<div class="lvlrow"><span class="lvlnum">${lv}</span><div class="kanjis">` +
      tiles.map((k, col) => {
        const wait = Math.round(row * step + col * 3);
        if (wait > front) front = wait;
        return `<span class="k in b-${BAND(k.s)}" data-i="${k.i}"` +
               ` style="animation-delay:${wait}ms">${k.c}</span>`;
      }).join('') +
      '</div></div>').join('');
  }

  function draw(d){
    const wordCap = 240;
    // Number them before the rows are built, or the tiles carry no id and a
    // click has nothing to look up.
    d.kanji.forEach((k, i) => { k.i = 'k' + i; });
    const kanjiHtml = rows(d.kanji);
    // The words come in behind the kanji, quickly, so the panel reads top to
    // bottom once rather than everywhere at once.
    const words = d.words.slice(0, wordCap).map((x, i) =>
      `<span class="wd in${x.s >= 5 ? ' on' : ''}" data-i="w${i}"` +
      ` style="animation-delay:${Math.round(front + 60 + i * 1.6)}ms">${x.c}</span>`)
      .join('');
    const rest = d.words.length - wordCap;
    box.innerHTML =
      `<h4>${d.name} &middot; levels ${d.lo}&ndash;${d.hi}</h4>` +
      `<p class="sub">${d.kanji.length.toLocaleString()} kanji, ` +
      `<b>${d.passed.kanji.toLocaleString()}</b> of them passed &middot; ` +
      `${d.words.length.toLocaleString()} words, ` +
      `<b>${d.passed.words.toLocaleString()}</b> passed</p>` +
      kanjiHtml +
      `<div class="words">${words}</div>` +
      (rest > 0 ? `<p class="more">and ${rest.toLocaleString()} more words</p>` : '') +
      '<div class="pick" id="tierpick" hidden></div>';
  }

  const STAGE = s => s >= 9 ? 'Burned' : s >= 8 ? 'Enlightened' : s >= 7 ? 'Master'
                   : s >= 5 ? 'Guru' : s >= 1 ? 'Apprentice' : 'not started';

  // Every tile and every chip answers what it means, without leaving the page.
  // The meanings came down with the panel, so this asks nobody anything.
  function pick(el, d){
    const id = el.dataset.i;
    if (!id) return;
    const it = id[0] === 'w' ? d.words[+id.slice(1)] : d.kanji[+id.slice(1)];
    if (!it) return;
    const out = document.getElementById('tierpick');
    const kind = id[0] === 'w' ? 'vocabulary' : 'kanji';
    // WaniKani's address is not always the characters - 今日は lives at
    // /vocabulary/こんにちは - so use the slug it gives us when it differs.
    const url = 'https://www.wanikani.com/' + kind + '/' +
                encodeURIComponent(it.u || it.c);
    out.innerHTML =
      `<span class="big">${it.c}</span>` +
      `<span class="what"><b>${it.m || '&mdash;'}</b>` +
      (it.r ? `<span class="rd">${it.r}</span>` : '') +
      `<span class="sub">level ${it.l} &middot; ${STAGE(it.s)} &middot; ` +
      `<a href="${url}" target="_blank" rel="noopener">on WaniKani &#8599;</a>` +
      `</span></span>`;
    out.hidden = false;
    for (const other of box.querySelectorAll('.k.sel, .wd.sel'))
      other.classList.remove('sel');
    el.classList.add('sel');
  }

  async function show(btn){
    const name = btn.dataset.tier;
    rungs.forEach(r => r.setAttribute('aria-expanded', String(r === btn)));
    box.hidden = false;
    if (cache.has(name)){ draw(cache.get(name)); wire(cache.get(name)); return; }
    // A shape where the answer will be, rather than a spinner somewhere else.
    box.innerHTML = '<div class="skel"><i></i><i></i><i></i></div>';
    try {
      const d = await (await fetch('/tier/' + encodeURIComponent(name))).json();
      cache.set(name, d);
      if (box.hidden === false){ draw(d); wire(d); }
    } catch (e){
      box.innerHTML = '<p class="sub">Could not load that one. Try again?</p>';
    }
  }

  let showing = null;
  function wire(d){ showing = d; }
  box.addEventListener('click', e => {
    const el = e.target.closest('.k, .wd');
    if (el && showing) pick(el, showing);
  });

  for (const btn of rungs){
    btn.addEventListener('click', () => {
      if (open === btn){
        open = null;
        box.hidden = true;
        rungs.forEach(r => r.setAttribute('aria-expanded', 'false'));
        return;
      }
      open = btn;
      show(btn);
      box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    });
  }
})();
"""

BURN_JS = """
(function(){
  const tbl = document.getElementById('burn');
  const pager = document.getElementById('burnpager');
  if (!tbl || !pager) return;
  const PER = 10;
  const label = pager.querySelector('span');
  const [back, fwd] = pager.querySelectorAll('button');
  let page = 0;
  function draw(){
    const rows = [...tbl.rows].slice(1);          // read the live order
    const pages = Math.max(1, Math.ceil(rows.length / PER));
    page = Math.min(Math.max(page, 0), pages - 1);
    const from = page * PER;
    rows.forEach((r, i) => {
      r.style.display = (i >= from && i < from + PER) ? '' : 'none';
    });
    label.textContent = rows.length
      ? `${from + 1}\u2013${Math.min(from + PER, rows.length)} of ${rows.length}`
      : 'nothing here';
    back.disabled = page === 0;
    fwd.disabled = page >= pages - 1;
  }
  back.onclick = () => { page--; draw(); };
  fwd.onclick = () => { page++; draw(); };
  // Sorting re-appends every row, so the page has to be redrawn over the new
  // order. The header's own handler runs first; this fires as the click
  // bubbles up to the table, by which time the rows have moved.
  tbl.addEventListener('click', e => {
    if (e.target.closest('th')) { page = 0; draw(); }
  });
  draw();
})();
"""


# ---------------------------------------------------------------- navigation

# The hosted app is server-rendered, so a click on the top bar leaves the old
# page standing there, frozen, until the next one arrives - 287kB of it on the
# dashboard. The rail says the click landed, and the crabigator says the wait
# is a wait and not a hang. It only appears after 150ms, so a fast page never
# flashes it.
LOAD_CSS = """
.loadrail { position:fixed; top:0; left:0; right:0; height:2px; z-index:200;
  pointer-events:none; opacity:0; transition:opacity 160ms linear; }
.loadrail.on { opacity:1; }
.loadrail i { display:block; height:100%; width:0; background:var(--accent);
  box-shadow:0 0 12px var(--accent); }
.loadrail b { position:absolute; top:2px; left:0; width:70px; height:45px;
  margin-left:-38px;
  background:url("/asset/crabigator-run.png") 0 0 / 560px 45px;
  image-rendering:pixelated; }

/* He draws the line rather than reporting on it. A server-rendered page sends
   no progress, so a bar that creeps to 90% is inventing a number; a runner
   crossing the screen and going round again says the same thing honestly. */
.loadrail.on i { animation:railfill .9s linear infinite; }
.loadrail.on b { animation:crabrun .42s steps(8) infinite,
                           crabdash .9s linear infinite; }
@keyframes crabrun  { to { background-position:-560px 0; } }
@keyframes crabdash { from { left:0; } to { left:100%; } }
@keyframes railfill { from { width:0; } to { width:100%; } }

@media (prefers-reduced-motion: reduce) {
  .loadrail.on i { width:100%; opacity:.5; animation:none; }
  .loadrail.on b { display:none; }
}

/* Moving between pages: both documents opt in, and the browser holds the old
   one on screen while the new one arrives. The tabs are in an order, so the
   pages travel in that order too - forward comes in from the right, back from
   the left - which says which way you moved without a word of copy. */
@view-transition { navigation: auto; }

::view-transition-old(root) { animation:pageout 160ms var(--ease) both; }
::view-transition-new(root) { animation:pagein 300ms var(--ease) both; }
@keyframes pageout { to { opacity:0; transform:scale(.985); } }
@keyframes pagein { from { opacity:0; transform:scale(1.01); } }

html[data-dir="fwd"]::view-transition-old(root) { animation:outleft 190ms var(--ease) both; }
html[data-dir="fwd"]::view-transition-new(root) { animation:inright 320ms var(--ease) both; }
html[data-dir="back"]::view-transition-old(root) { animation:outright 190ms var(--ease) both; }
html[data-dir="back"]::view-transition-new(root) { animation:inleft 320ms var(--ease) both; }
@keyframes outleft  { to   { opacity:0; transform:translateX(-4%); } }
@keyframes inright  { from { opacity:0; transform:translateX(7%); } }
@keyframes outright { to   { opacity:0; transform:translateX(4%); } }
@keyframes inleft   { from { opacity:0; transform:translateX(-7%); } }

/* Two things sit still while the rest travels: the bar, which is the same bar
   on both sides, and the line saying whose numbers these are. Naming them
   takes them out of the page snapshot and gives them their own, so the browser
   moves them from where they were to where they are going. */
.topbar { view-transition-name: topbar; }
.hero { view-transition-name: hero; }

/* Arriving under its own steam, for browsers that do not do the above and for
   the first load of the session: the sections come up in order, quickly enough
   that it reads as the page settling rather than as a performance. */
main > *:not(.topbar):not(nav) { animation:settle 380ms var(--ease) both; }
main > *:nth-child(2)  { animation-delay:20ms; }
main > *:nth-child(3)  { animation-delay:45ms; }
main > *:nth-child(4)  { animation-delay:70ms; }
main > *:nth-child(5)  { animation-delay:95ms; }
main > *:nth-child(6)  { animation-delay:115ms; }
main > *:nth-child(n+7) { animation-delay:135ms; }
@keyframes settle { from { opacity:0; transform:translateY(12px); } }

@media (prefers-reduced-motion: reduce) {
  ::view-transition-old(root), ::view-transition-new(root),
  main > * { animation:none; }
}
"""

LOAD_JS = """
(function(){
  const rail = document.createElement('div');
  rail.className = 'loadrail';
  rail.innerHTML = '<i></i><b></b>';
  document.body.appendChild(rail);
  const CROSS = 900;          // one run across, in ms - keep in step with the CSS
  const FLAG = 'wkjiten:navigating';
  let timer = null, shown = 0;

  function note(obj){
    try { sessionStorage.setItem(FLAG, JSON.stringify(obj)); } catch (e) {}
  }
  function show(at){
    rail.classList.add('on');
    shown = performance.now();
    if (at) note({t: Date.now(), shown: at});
  }
  function start(){
    if (timer) return;
    // The loader lives on the page being left, and that page is destroyed the
    // moment the next one commits - locally that is under a millisecond, so he
    // never got off the mark. Hand him over instead: the click leaves a note,
    // and the arriving page picks it up.
    note({t: Date.now(), shown: 0});
    timer = setTimeout(() => show(Date.now()), 80);
  }
  function stop(){
    clearTimeout(timer); timer = null;
    rail.classList.remove('on');
  }

  // Arriving from a click of our own: show it now, and keep it up until the
  // page has finished loading AND he has had time to cross once. Anything
  // else - a typed URL, a fresh tab - starts quiet.
  let handover = null;
  try {
    handover = JSON.parse(sessionStorage.getItem(FLAG) || 'null');
    sessionStorage.removeItem(FLAG);
  } catch (e) {}
  if (handover && Date.now() - handover.t < 10000){
    // If he was already running when the page was left, pick him up mid-stride
    // rather than putting him back on the start line. A negative delay is the
    // animation saying "assume this much has already happened", which is what
    // stops him crossing twice for one click.
    let carried = 0;
    if (handover.shown){
      carried = (Date.now() - handover.shown) % CROSS;
      for (const el of [rail.querySelector('i'), rail.querySelector('b')])
        el.style.animationDelay = `-${carried}ms` + (el.tagName === 'B' ? `, -${carried}ms` : '');
    }
    show(0);
    const done = () => {
      // What is left of *his* lap, not a fresh one - he is already part way in.
      const left = Math.max(0, (CROSS - carried) - (performance.now() - shown));
      setTimeout(stop, left);
    };
    if (document.readyState === 'complete') done();
    else addEventListener('load', done);
  }

  addEventListener('click', e => {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download')) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.hash) return;   // in-page jump
    start();
  });
  addEventListener('submit', e => { if (!e.defaultPrevented) start(); });
  // Which way the tabs run, so a page can travel in the direction you moved.
  const ORDER = ['/', '/levels', '/kanji', '/browse'];
  const at = path => ORDER.indexOf(path);
  const DIR = 'wkjiten:from';
  function face(from, to){
    document.documentElement.dataset.dir =
      (from < 0 || to < 0 || from === to) ? 'none' : (to > from ? 'fwd' : 'back');
  }
  addEventListener('pageswap', e => {
    try { sessionStorage.setItem(DIR, String(at(location.pathname))); } catch (_){}
    const url = e.activation && e.activation.entry && e.activation.entry.url;
    if (e.viewTransition && url) face(at(location.pathname), at(new URL(url).pathname));
  });
  addEventListener('pagereveal', e => {
    if (!e.viewTransition) return;
    let from = -1;
    try { from = Number(sessionStorage.getItem(DIR)); } catch (_){}
    face(Number.isFinite(from) ? from : -1, at(location.pathname));
  });

  // A restore from the back/forward cache shows the old page instantly, so the
  // rail must not be left running on top of it. Only that case: pageshow fires
  // on every load, and an unconditional stop here cancelled the handover in
  // the same breath that started it.
  addEventListener('pageshow', e => { if (e.persisted) stop(); });
  addEventListener('pagehide', stop);
})();
"""


def shell(title: str, body: str, *, user=None, extra_css: str = "",
          scripts: str = "", page: str = "") -> str:
    """Common HTML skeleton. Same tokens as the local dashboard."""
    bar = ""
    if user:
        tabs = [("/", "today", "today"), ("/levels", "levels", "levels"),
                ("/kanji", "kanji", "kanji"), ("/browse", "browse", "browse"),
                ("/together", "together", ""), ("/settings", "settings", "")]
        if user.get("is_admin"):
            tabs.append(("/invites", "invites", ""))
        bar = ('<div class="topbar"><nav>'
               + "".join(f'<a href="{href}"'
                         + (' class="here"' if key and key == page else "")
                         + f">{label}</a>" for href, label, key in tabs)
               + f'</nav><span class="who">{esc(user["username"])} &middot; '
                 f'<a href="/logout">log out</a></span></div>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title>{w.favicon_link(ICON)}'
            f'<link rel="stylesheet" href="{CSS_URL}">'
            f'{extra_css}</head><body>'
            f'<main>{bar}{body}</main>'
            f'<script src="{CORE_URL}" defer></script>{scripts}</body></html>')


def login_page(error: str = "", note: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    return shell("Sign in", f"""
      <div class="authbox">{w.brand_mark(ICON)}<h1>wkjiten</h1>{msg}
      <form method="post">
        <div class="field"><label for="u">username</label>
          <input id="u" name="username" autocomplete="username" autofocus required></div>
        <div class="field"><label for="p">password</label>
          <input id="p" name="password" type="password"
                 autocomplete="current-password" required></div>
        <button class="go" type="submit">Sign in</button>
      </form>
      <p class="sub" style="margin-top:16px">Accounts are invitation only.</p>
      </div>""")


def register_page(code: str, error: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    return shell("Create account", f"""
      <div class="authbox">{w.brand_mark(ICON)}<h1>Create your account</h1>{msg}
      <form method="post">
        <input type="hidden" name="code" value="{esc(code)}">
        <div class="field"><label for="u">username</label>
          <input id="u" name="username" autocomplete="username" autofocus required></div>
        <div class="field"><label for="p">password</label>
          <input id="p" name="password" type="password" minlength="8"
                 autocomplete="new-password" required>
          <div class="hint">At least 8 characters.</div></div>
        <button class="go" type="submit">Create account</button>
      </form></div>""")


def settings_page(user, creds, note: str = "", error: str = "",
                  age_hours: float | None = None, push: dict | None = None) -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    has_wk = "stored" if creds.get("wk_token") else "not set"
    has_jt = "stored" if creds.get("jiten_key") else "not set"
    has_jm = "stored" if creds.get("jimaku_key") else "not set"
    if age_hours is None:
        fetched = "Nothing fetched from WaniKani yet."
    elif age_hours < 1:
        fetched = f"Fetched from WaniKani {age_hours * 60:.0f} minutes ago."
    elif age_hours < 48:
        fetched = f"Fetched from WaniKani {age_hours:.0f} hours ago."
    else:
        fetched = f"Fetched from WaniKani {age_hours / 24:.0f} days ago."
    has_nt = "stored" if creds.get("nihongo_key") else "not set"
    # Whether the words actually reached jiten.moe. "Sent in the background"
    # looks identical whether it worked or was refused, so say which.
    if not creds.get("jiten_key"):
        sent = ("Without a Jiten key nothing is uploaded, so the "
                "<b>jiten</b> column stays at whatever that account already knew.")
    elif not push:
        sent = "Your words have not been sent to jiten.moe from here yet."
    elif push["ok"]:
        import json as _json
        try:
            did = _json.loads(push["note"])
        except (ValueError, TypeError):
            did = {}
        new = did.get("added")
        tail = ("" if new is None else
                (f' &mdash; <b>{new:,} of them new to it</b>.' if new
                 else ' &mdash; it already knew every one.'))
        sent = (f'Last sent to jiten.moe on '
                f'{esc(push["at"][:16].replace("T", " "))}: '
                f'<b>{push["words"]:,} words</b> recognised{tail}')
    else:
        sent = (f'The last upload to jiten.moe <b>failed</b> on '
                f'{esc(push["at"][:16].replace("T", " "))}: '
                f'<span class="code">{esc(push["note"][:120])}</span>')
    return shell("Settings", f"""
      <h1>Settings</h1>{msg}
      <h2>API keys</h2>
      <p class="sub">All of them are encrypted before they touch the database.
      Leave a field blank to keep what is already stored.</p>
      <form method="post" class="authbox" style="margin:0 0 24px;max-width:560px">
        <div class="field">
          <label for="wk">WaniKani token &mdash; currently {has_wk}</label>
          <input id="wk" name="wk_token" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Make it <b>read-only</b> at
            <a href="https://www.wanikani.com/settings/personal_access_tokens"
               target="_blank" rel="noopener"
               >wanikani.com/settings/personal_access_tokens</a>. Read-only is
            all this needs.</div>
        </div>
        <div class="field">
          <label for="jt">Jiten API key &mdash; currently {has_jt}</label>
          <input id="jt" name="jiten_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Without it you still get the full kanji
            analysis; with it you also get your account's word coverage, the
            known-words upload and the list buttons. Yours is at
            <a href="https://jiten.moe/settings" target="_blank" rel="noopener"
               >jiten.moe/settings</a>. Jiten's own docs say this key
            <b>carries every permission your account has</b> &mdash; treat it
            like a password, and remove it here whenever you like.</div>
        </div>
        <div class="field">
          <label for="jm">jimaku.cc API key &mdash; currently {has_jm}</label>
          <input id="jm" name="jimaku_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Adds a <b>subs</b> link beside anime,
            drama and film, straight to that title's Japanese subtitles. Yours
            is at <a href="https://jimaku.cc/account" target="_blank"
               rel="noopener">jimaku.cc/account</a>.</div>
        </div>
        <div class="field">
          <label for="nt">NihongoTracker API key &mdash; currently {has_nt}</label>
          <input id="nt" name="nihongo_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Adds the hours and episodes you have
            logged beside each title, matched on the AniList and VNDB ids both
            sites already use. Yours is at
            <a href="https://nihongotracker.app" target="_blank" rel="noopener"
               >nihongotracker.app</a>. This one is <b>not read-only</b>
            either &mdash; it is accepted everywhere your account can reach,
            including deleting logs.</div>
        </div>
        <button class="go" type="submit">Save</button>
      </form>
      <h2>Data</h2>
      <p class="sub">{fetched} The counters on the dashboard compare that
      against the fetch before it. {sent}</p>
      <form method="post" action="/refresh" class="inline">
        <button>Refresh stats</button></form>
      <form method="post" action="/settings/baseline" class="inline">
        <button>Count changes from now</button></form>
      <form method="post" action="/settings/drop-jiten" class="inline">
        <button>Forget my Jiten key</button></form>
      <form method="post" action="/settings/delete" class="inline"
            onsubmit="return confirm('Delete your account and all of its data?')">
        <button>Delete my account</button></form>
      <p class="sub" style="margin-top:10px"><b>Refresh stats</b> pulls your
      WaniKani data and then sends your known words on to jiten.moe, which is
      what its coverage column is worked out from. It adds to that list and
      never replaces it, so anything you know from elsewhere stays.
      <b>Count changes from now</b> moves
      that comparison point to this moment, so a session you are about to do
      shows up on its own. Press it before you study, then refresh afterwards.</p>
      """, user=user)


def invites_page(user, invites, base_url: str) -> str:
    cells = []
    for i in invites:
        used = esc(i["used_by_name"]) if i["used_by_name"] else "&mdash;"
        link = ("&mdash;" if i["used_by"] else
                f'<span class="code">{esc(base_url)}/register/{esc(i["code"])}</span>')
        cells.append(f'<tr><td class="code">{esc(i["code"])}</td>'
                     f'<td>{esc(i["created_at"][:10])}</td>'
                     f'<td>{used}</td><td>{link}</td></tr>')
    rows = "".join(cells)
    return shell("Invites", f"""
      <h1>Invitations</h1>
      <p class="sub">Accounts can only be created with a code. Send someone the
      link; it works once.</p>
      <form method="post"><button class="go">Create an invitation</button></form>
      <h2>Codes</h2>
      <div class="wrap"><table><tr><th>code</th><th>created</th><th>used by</th>
      <th>link</th></tr>{rows}</table></div>""", user=user)


TOGETHER_CSS = """
.people { display:grid; gap:20px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
  margin-bottom:8px; }
.person { background:var(--raise); border:1px solid var(--line); border-radius:14px;
  box-shadow:var(--shadow); overflow:hidden; }
.person > header { padding:16px 18px 12px; border-bottom:1px solid var(--line);
  display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.person h3 { margin:0; font-size:18px; font-weight:650; letter-spacing:-.015em; }
.person .meta { color:var(--faint); font-size:12.5px; }
.person .group { padding:12px 18px 4px; }
.person .group h4 { margin:0 0 8px; font-size:11px; letter-spacing:.09em;
  text-transform:uppercase; color:var(--faint); font-weight:650; }
.titlelist { list-style:none; margin:0 0 12px; padding:0; }
.titlelist li { display:flex; justify-content:space-between; gap:12px; padding:5px 0;
  border-bottom:1px solid var(--line-soft); font-size:14px; }
.titlelist li:last-child { border-bottom:0; }
.titlelist .covs { display:flex; gap:10px; white-space:nowrap; }
.titlelist .cov { color:var(--muted); font-variant-numeric:tabular-nums;
  font-size:13px; }
.titlelist .cov b { color:var(--fg); font-weight:600; }
.titlelist .kanjicov { color:var(--faint); }
.statsbox { display:flex; align-items:center; gap:7px; font-size:14px;
  color:var(--muted); cursor:pointer; }
.statsbox input { width:16px; height:16px; accent-color:var(--accent); }
.banner { height:120px; border-radius:14px 14px 0 0; background-size:cover;
  background-position:center; border-bottom:1px solid var(--line); }
.person .idbar { display:flex; gap:12px; align-items:center; padding:14px 18px 12px;
  border-bottom:1px solid var(--line); }
.person .idbar.pulled { margin-top:-34px; border-bottom:0; padding-bottom:6px; }
.avatar { width:52px; height:52px; border-radius:50%; object-fit:cover; flex:none;
  border:2px solid var(--raise); background:var(--sunk); }
.avatar.blank { display:grid; place-items:center; font:600 20px/1 var(--sans, sans-serif);
  color:var(--faint); }
.bio { color:var(--muted); font-size:13px; margin:2px 0 0; }
.profileform { display:grid; gap:14px; }
.profileform .row2 { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; }
.profileform input[type=file] { font-size:13px; color:var(--muted); }
.profileform input[type=text] { width:100%; font:inherit; padding:9px 12px;
  border-radius:10px; border:1px solid var(--line); background:var(--bg);
  color:var(--fg); }
.addbtn { font:600 11px/1 var(--sans, sans-serif); padding:3px 9px; border-radius:99px;
  border:1px solid var(--line); background:var(--bg); color:var(--muted);
  cursor:pointer; margin-left:8px; }
.addbtn:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.addbtn:disabled { opacity:.5; cursor:default; }
.addbtn.done { color:var(--good); border-color:var(--good); }
.profilewrap { max-width:620px; margin:0 auto; }
.backlink { padding-top:18px; margin:0; font-size:13px; }
.backlink a { color:var(--muted); border:0; }
.backlink a:hover { color:var(--accent); }
.nowplaying { display:flex; gap:16px; align-items:center; padding:16px 18px;
  border-bottom:1px solid var(--line); background:var(--accent-soft); }
.nowplaying img { width:64px; height:90px; object-fit:cover; border-radius:6px;
  flex:none; background:var(--line); box-shadow:var(--shadow); }
.nowplaying .lbl { font:650 10px/1 var(--sans, sans-serif); letter-spacing:.11em;
  text-transform:uppercase; color:var(--accent); }
.nowplaying h4 { margin:5px 0 4px; font-size:20px; line-height:1.25;
  letter-spacing:-.015em; font-weight:650; }
.nowplaying .covs { display:flex; gap:10px; }
.vs { display:inline-flex; gap:6px; align-items:baseline; margin-left:8px;
  font-size:12.5px; color:var(--faint); white-space:nowrap; }
.vs b { color:var(--accent); font-variant-numeric:tabular-nums; }
.titlelist .both { color:var(--accent); font-weight:600; }
.overlap { background:var(--accent-soft); border:1px solid var(--line);
  border-radius:14px; padding:4px 18px 14px; margin-bottom:8px; }
.who-has { color:var(--muted); font-size:13px; }
.sharebox { background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:16px 18px; box-shadow:var(--shadow);
  margin-bottom:22px; }
.sharebox .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.sharebox select { font:inherit; font-size:14px; padding:8px 11px; border-radius:10px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); }
.sharelink { margin-top:16px; padding-top:14px; border-top:1px solid var(--line-soft); }
.sharelink label { display:block; font:600 10.5px/1 var(--sans, inherit);
  letter-spacing:.09em; text-transform:uppercase; color:var(--faint);
  margin-bottom:7px; }
.sharelink input { flex:1 1 260px; font:13px ui-monospace,Consolas,monospace;
  padding:9px 12px; border-radius:10px; border:1px solid var(--line);
  background:var(--bg); color:var(--fg); }
.sharelink .hint { display:block; margin-top:8px; color:var(--faint); font-size:12.5px; }
.sharelink form.inline { display:inline; margin-top:8px; }
"""


SHARE_JS = """
<script>
document.querySelectorAll('[data-copy]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var text = document.getElementById(btn.dataset.copy).value;
    var done = function () {
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = 'Copy link'; }, 1500);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
    else {
      var el = document.getElementById(btn.dataset.copy);
      el.select(); try { document.execCommand('copy'); done(); } catch (e) {}
    }
  });
});
</script>
"""


def share_panel(visibility: str, token: str, base_url: str, username: str,
                share_stats: bool = False) -> str:
    """One control, four levels, each containing the one before it."""
    checked = "checked" if share_stats else ""
    opts = "".join(
        f'<option value="{v}"{" selected" if v == visibility else ""}>'
        f'{esc(w_labels[v])}</option>' for v in w_levels)

    links = ""
    if visibility in ("link", "public"):
        url = f"{base_url}/s/{token}"
        links += f"""
        <div class="sharelink">
          <label for="secret">Secret link &mdash; works for anyone you send it to</label>
          <div class="row">
            <input id="secret" value="{esc(url)}" readonly onclick="this.select()">
            <button type="button" data-copy="secret">Copy link</button>
          </div>
          <form method="post" action="/together/newlink" class="inline">
            <button>Make a new link</button></form>
          <span class="hint">Making a new one stops the old link working. That is
          the only way to take a shared link back.</span>
        </div>"""
    if visibility == "public":
        pub = f"{base_url}/u/{username}"
        links += f"""
        <div class="sharelink">
          <label for="pub">Permanent address &mdash; open to anyone, no link needed</label>
          <div class="row">
            <input id="pub" value="{esc(pub)}" readonly onclick="this.select()">
            <button type="button" data-copy="pub">Copy link</button>
          </div>
        </div>"""

    return f"""
      <div class="sharebox">
        <form method="post" action="/together/share" class="row">
          <label for="vis"><b>Who can see my lists</b></label>
          <select id="vis" name="visibility" onchange="this.form.submit()">{opts}</select>
          <label class="statsbox"><input type="checkbox" name="stats" value="1"
            {checked} onchange="this.form.submit()">
            Include my WaniKani stats</label>
          <noscript><button>Save</button></noscript>
        </form>
        <p class="sub" style="margin:10px 0 0">Shared: the title, whether you are
        watching, planning or finished, and your coverage on it. Never your keys,
        your reviews or anything from WaniKani.</p>
        {links}
      </div>"""


w_levels = ("private", "instance", "link", "public")
w_labels = {
    "private": "Just me",
    "instance": "People with an account here",
    "link": "Anyone with the secret link",
    "public": "Anyone at all, at a permanent address",
}


def profile_panel(profile: dict, user, choices=None) -> str:
    """Picture, banner, a line about yourself and the title you are on now.
    All optional, all removable."""
    now = profile.get("currently")
    options = '<option value="">nothing in particular</option>' + "".join(
        f'<option value="{c["deck_id"]}"{" selected" if c["deck_id"] == now else ""}>'
        f'{esc(c["title"])}</option>' for c in (choices or []))
    return f"""
      <div class="sharebox">
        <form method="post" action="/profile" enctype="multipart/form-data"
              class="profileform">
          <div class="row">
            {avatar_tag(user["id"], profile.get("has_avatar"), user["username"])}
            <div style="flex:1 1 260px">
              <b>Your profile</b>
              <div class="sub" style="margin:2px 0 0">Shown on the shared pages.
              Leave it all empty and your name stands on its own.</div>
            </div>
          </div>
          <input type="text" name="bio" maxlength="160" value="{esc(profile.get("bio") or "")}"
                 placeholder="A line about yourself, if you like">
          <label class="statsbox" style="gap:10px">Currently
            <select name="currently">{options}</select></label>
          <div class="row2">
            <label>Picture<br><input type="file" name="avatar"
              accept="image/png,image/jpeg,image/gif,image/webp"></label>
            <label>Banner<br><input type="file" name="banner"
              accept="image/png,image/jpeg,image/gif,image/webp"></label>
            <button class="go">Save profile</button>
          </div>
          <div class="row" style="gap:16px">
            {'<label class="statsbox"><input type="checkbox" name="clear_avatar" value="1"> Remove picture</label>' if profile.get("has_avatar") else ""}
            {'<label class="statsbox"><input type="checkbox" name="clear_banner" value="1"> Remove banner</label>' if profile.get("has_banner") else ""}
          </div>
          <span class="hint">PNG, JPEG, GIF or WebP, up to 3 MB.</span>
        </form>
      </div>"""


def together_page(user, visibility, token, base_url, people, by_user, overlap,
                  absent, share_stats=False, profile=None, can_add=False,
                  note="", featured=None) -> str:
    featured = featured or {}
    head = share_panel(visibility, token, base_url, user["username"], share_stats)
    head += profile_panel(profile or {}, user,
                          by_user.get(user["id"], {}).get("ongoing", []))
    if note:
        head = f'<div class="ok">{esc(note)}</div>' + head
    mine = {r["deck_id"]: r for st in by_user.get(user["id"], {}).values()
            for r in st}

    cards = []
    for p in people:
        groups = []
        for status, label in (("ongoing", "watching / reading"),
                              ("planning", "plan to watch / read"),
                              ("completed", "finished")):
            items = by_user.get(p["id"], {}).get(status, [])
            if not items:
                continue
            lis = ""
            for it in items[:30]:
                # Only worth offering for someone else's titles, and only when
                # you have a Jiten key to add it with.
                add = ""
                # If you have the same title, put the two figures side by side.
                versus = ""
                ours = mine.get(it["deck_id"])
                if ours is not None and p["id"] != user["id"] and ours.get("coverage"):
                    versus = (f'<span class="vs">you <b>{ours["coverage"]:.0f}%</b>'
                              f'</span>')
                if can_add and p["id"] != user["id"] and it["deck_id"] not in mine:
                    add = (f'<button class="addbtn" data-add="{it["deck_id"]}"'
                           f' title="Add to your plan to watch/read">+ my list</button>')
                lis += (
                    f'<li><span class="{"both" if it["deck_id"] in overlap else ""}">'
                    f'<a href="https://jiten.moe/decks/media/{it["deck_id"]}/detail"'
                    f' target="_blank" rel="noopener">{esc(it["title"])}</a>'
                    f'{versus}{add}</span>'
                    f'<span class="covs">{cov_cells(it)}</span></li>')
            more = (f'<li class="who-has">and {len(items) - 30} more</li>'
                    if len(items) > 30 else "")
            groups.append(f'<div class="group"><h4>{label} &middot; {len(items)}</h4>'
                          f'<ul class="titlelist">{lis}{more}</ul></div>')
        seen = (p.get("seen") or "")[:10]
        top = ""
        if p.get("has_banner"):
            top = (f'<div class="banner" style="background-image:url(/media/banner/'
                   f'{p["id"]})"></div>')
        idbar = (f'<div class="idbar{" pulled" if top else ""}">'
                 f'{avatar_tag(p["id"], p.get("has_avatar"), p["username"])}'
                 f'<div><h3>{esc(p["username"])}'
                 f'{" (you)" if p["id"] == user["id"] else ""}</h3>'
                 f'<div class="meta">{p["titles"]} titles &middot; {esc(seen)}</div>'
                 + (f'<p class="bio">{esc(p["bio"])}</p>' if p.get("bio") else "")
                 + '</div></div>')
        cards.append(
            f'<div class="person">{top}{idbar}'
            f'{now_playing(featured.get(p["id"]))}'
            f'{"".join(groups) or "<div class=group><p class=empty>Nothing yet.</p></div>"}</div>')

    overlap_html = ""
    if overlap:
        rows = "".join(
            f'<li><span><a href="https://jiten.moe/decks/media/{d}/detail"'
            f' target="_blank" rel="noopener">{esc(info["title"])}</a></span>'
            f'<span class="who-has">{esc(", ".join(info["who"]))}</span></li>'
            for d, info in sorted(overlap.items(),
                                  key=lambda kv: -len(kv[1]["who"]))[:20])
        overlap_html = (f'<h2>Both of you</h2><div class="overlap">'
                        f'<ul class="titlelist">{rows}</ul></div>')

    missing = ""
    if absent:
        missing = (f'<p class="sub">Not sharing yet: '
                   f'{esc(", ".join(absent))}. They can turn it on from this page.</p>')

    body = (f'<h1>Together</h1><p class="sub">What everyone here is watching, '
            f'reading and has finished, straight from their jiten.moe lists.</p>'
            f'{head}{overlap_html}<h2>Everyone</h2>'
            f'<div class="people">{"".join(cards) or ""}</div>{missing}')
    if not people:
        body = (f'<h1>Together</h1><p class="sub">Nobody is sharing their lists yet.'
                f'</p>{head}{missing}')
    return shell("Together", body, user=user, scripts=SHARE_JS + ADD_JS)


ADD_JS = """
<script>
document.querySelectorAll('[data-add]').forEach(function (btn) {
  btn.addEventListener('click', async function () {
    var was = btn.textContent;
    btn.disabled = true; btn.textContent = 'adding\u2026';
    try {
      var r = await fetch('/api/user/deck-preferences/' + btn.dataset.add + '/status', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 1})});
      if (!r.ok) throw new Error(r.status);
      btn.textContent = 'on your list';
      btn.classList.add('done');
    } catch (e) {
      btn.textContent = 'failed'; btn.disabled = false;
      setTimeout(function () { btn.textContent = was; }, 2000);
    }
  });
});
</script>
"""


def avatar_tag(user_id: int, has: bool, name: str, cls: str = "avatar") -> str:
    if has:
        return (f'<img class="{cls}" src="/media/avatar/{user_id}" alt=""'
                f' width="52" height="52">')
    return f'<div class="{cls} blank" aria-hidden="true">{esc(name[:1].upper())}</div>'


def now_playing(item, label="Currently") -> str:
    if not item:
        return ""
    return (f'<div class="nowplaying">'
            f'<img loading="lazy" alt="" src="{w.cover_url(item["deck_id"])}"'
            f' onerror="this.style.visibility=\'hidden\'">'
            f'<div><div class="lbl">{esc(label)}</div>'
            f'<h4><a href="https://jiten.moe/decks/media/{item["deck_id"]}/detail"'
            f' target="_blank" rel="noopener">{esc(item["title"])}</a></h4>'
            f'<span class="covs">{cov_cells(item)}</span></div></div>')


def cov_cells(it) -> str:
    """Words as jiten measures them, kanji as WaniKani reaches them.

    Finished titles are listed but never analysed, so they carry no kanji
    figure and say so rather than showing a zero.
    """
    word = f'{it["coverage"]:.0f}%' if it.get("coverage") else "&mdash;"
    kanji = f'{it["kanji_cov"]:.0f}%' if it.get("kanji_cov") else "&mdash;"
    return (f'<span class="cov"><b>{word}</b> words</span>'
            f'<span class="cov kanjicov">{kanji} kanji</span>')


def public_profile(owner, profile: dict, stats, lists, base_url: str,
                   featured=None, viewer=None) -> str:
    """The read-only view behind a share link. No login, no navigation."""
    username = owner["username"]
    groups = []
    total = 0
    for status, label in (("ongoing", "watching / reading"),
                          ("planning", "plan to watch / read"),
                          ("completed", "finished")):
        items = lists.get(status, [])
        total += len(items)
        if not items:
            continue
        lis = "".join(
            f'<li><span><a href="https://jiten.moe/decks/media/{it["deck_id"]}/detail"'
            f' target="_blank" rel="noopener">{esc(it["title"])}</a></span>'
            f'<span class="covs">{cov_cells(it)}</span></li>' for it in items)
        groups.append(f'<div class="group"><h4>{label} &middot; {len(items)}</h4>'
                      f'<ul class="titlelist">{lis}</ul></div>')

    inner = "".join(groups) or '<div class="group"><p class="empty">Nothing here yet.</p></div>'

    cards = ""
    if stats:
        pace = ""
        if stats.get("pace"):
            pace = (f'<div class="card"><div class="n">{stats["pace"]:.0f}</div>'
                    f'<div class="l">days per level</div></div>')
        cards = f"""
      <div class="cards" style="margin-bottom:20px">
        <div class="card"><div class="n">{stats["level"]}</div>
          <div class="l">WaniKani level</div></div>
        <div class="card"><div class="n">{stats["kanji"]:,}</div>
          <div class="l">kanji known</div></div>
        <div class="card"><div class="n">{stats["words"]:,}</div>
          <div class="l">words known</div></div>
        {pace}
      </div>"""

    asof = ""
    if stats and stats.get("as_of"):
        asof = f' &middot; as of {esc(stats["as_of"])}'
    banner = ""
    if profile.get("has_banner"):
        banner = (f'<div class="banner" style="margin-top:26px;'
                  f'border-radius:14px;background-image:url(/media/banner/'
                  f'{owner["id"]})"></div>')
    # A stranger gets no navigation at all; someone signed in should not be
    # stranded on a page with no way out.
    back = ('<p class="backlink"><a href="/">&larr; back to your dashboard</a></p>'
            if viewer else "")
    body = f"""
      <div class="profilewrap">
      {back}
      {banner}""" + f"""
      <div class="hero" style="display:block;padding:{"18px" if banner else "48px"} 0 22px">
        <div style="display:flex;gap:14px;align-items:center">
          {avatar_tag(owner["id"], profile.get("has_avatar"), username)}
          <div>
            <h1 style="margin:0">{esc(username)}</h1>
            <p class="sub" style="margin:2px 0 0">What {esc(username)} is watching
            and reading on jiten.moe &middot; {total} titles{asof}</p>
          </div>
        </div>
        {f'<p class="bio" style="margin-top:10px">{esc(profile["bio"])}</p>' if profile.get("bio") else ""}
      </div>
      {cards}
      <div class="person">{now_playing(featured)}{inner}</div>
      <footer>Word coverage is how much of each title's
      vocabulary {esc(username)} already knows, measured by
      <a href="https://jiten.moe" target="_blank" rel="noopener">jiten.moe</a>.
      Kanji coverage is how many of its kanji WaniKani has taught them, weighted
      by how often each one appears. This page is shared deliberately and can be
      withdrawn at any time.</footer>
      </div>"""
    return shell(f"{username} on jiten.moe", body)


# ---------------------------------------------------------------- dashboard

def _burn_when(at: str, now: float) -> tuple[str, float]:
    """How long until a review, and the epoch seconds it lands on.

    WaniKani stamps these in UTC; timegm reads them as such rather than as
    whatever the server happens to be set to.
    """
    try:
        t = calendar.timegm(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return "&mdash;", 0.0
    d = t - now
    if d <= 0:
        return "in your queue now", t
    if d < 86400:
        return f"in {d / 3600:.0f}h", t
    if d < 86400 * 14:
        return f"in {d / 86400:.0f} days", t
    return f"in {d / 86400 / 7:.0f} weeks", t


# WaniKani groups the sixty levels into six stretches with names of their own,
# and the names are half the fun of the climb. The artwork is the user's own.
TIERS = [("Pleasant", 1, 10), ("Painful", 11, 20), ("Death", 21, 30),
         ("Hell", 31, 40), ("Paradise", 41, 50), ("Reality", 51, 60)]


# One line per level, because "level 34 of 60" is a progress bar and not a
# reason to come back. They are short on purpose: this sits under a picture of
# a crocodile in a headband, and anything longer would be a lecture.
LEVEL_LINES = {
    1: "Everything is new, which is the only time everything is easy.",
    2: "Four strokes, and one of them is already a word.",
    3: "The radicals are lying to you kindly. Let them.",
    4: "You have started recognising things on signs. That was fast.",
    5: "Mnemonics you would never admit to out loud are working.",
    6: "The first ones come back around. Some of them stayed.",
    7: "Two hundred items in, and the shape of the thing appears.",
    8: "You can read a menu badly. Badly is a beginning.",
    9: "The reviews arrive whether or not you meant to open the app.",
    10: "Ten levels. A sixth of it, and the easy sixth at that.",
    11: "Painful is the name they gave it, and they were not joking.",
    12: "The pile stops being new and starts being a habit.",
    13: "Nothing is dramatic here. That is the danger.",
    14: "Two kanji you keep confusing. Everyone has a pair.",
    15: "A quarter through. Nobody claps at a quarter.",
    16: "The words are getting longer and the radicals stranger.",
    17: "Guru is not the same as knowing, and you have noticed.",
    18: "You read a sentence and only stopped twice.",
    19: "The leeches have names now.",
    20: "The end of Painful. It was not the worst of it.",
    21: "Death. It is a joke about the workload and also not.",
    22: "The reviews outnumber the lessons and will from here on.",
    23: "This is the stretch people quit in. Knowing that helps.",
    24: "Slow is not stopped. The counter only moves one way.",
    25: "Halfway. Say it out loud - it sounds better than it reads.",
    26: "You have burned things. They do not come back.",
    27: "Some days it is twenty items and that is a full day.",
    28: "The kanji you meet in the wild are yours now.",
    29: "You are past the point where anyone else would have quit.",
    30: "Half the alphabet of a language that has no alphabet.",
    31: "Hell, allegedly. By now you know what the names are worth.",
    32: "A hundred a day is not the goal. Coming back is the goal.",
    33: "You can guess a reading you have never seen and be right.",
    34: "The gap between what you know and what you can read closes.",
    35: "Manga stops being a wall of shapes and becomes slow reading.",
    36: "Nobody is watching the streak but you. That is enough.",
    37: "The ones you failed six times are the ones that stick hardest.",
    38: "Three fifths. The rest is shorter than what is behind you.",
    39: "You have learned more kanji than most people ever meet.",
    40: "The end of Hell. From here the names get kinder.",
    41: "Paradise. The joke is that the work is the same.",
    42: "Reading gets faster without you deciding to be faster.",
    43: "The fast levels start. Fewer radicals in the way.",
    44: "Words you never studied make sense from their parts.",
    45: "Three quarters. That is not a milestone, it is a slope.",
    46: "You look up fewer things. You notice you looked up fewer things.",
    47: "The reviews are maintenance now, not construction.",
    48: "Novels are still hard. Novels are no longer impossible.",
    49: "The last of the strange ones are arriving.",
    50: "Fifty. The mountain is behind you and you are still walking.",
    51: "Reality. The name is a compliment.",
    52: "The rare kanji arrive and you meet them once a year after this.",
    53: "You are learning the ones native readers also look up.",
    54: "Anything left is detail. Detail is the whole point.",
    55: "Five to go, and each is a week.",
    56: "You read for pleasure now and study on the side.",
    57: "The end is close enough to be a date rather than a hope.",
    58: "Two more. You know exactly what two more feels like.",
    59: "The last set of lessons you will ever be given here.",
    60: "Level 60. WaniKani is finished; Japanese is not. Go read.",
}


def tier_of(level: int):
    for name, lo, hi in TIERS:
        if lo <= level <= hi:
            return name, lo, hi
    return TIERS[-1]


def burn_calendar(cache, days: int = 30) -> str:
    """The next thirty days of what could burn, as thirty bars.

    The list answers "what is next"; this answers "when is the week that will
    hurt". Same numbers, read sideways.
    """
    counts = cache.get("burning_days")
    if not counts:
        # A snapshot from before the count existed still carries the dates of
        # the nearest few hundred, and the near days - the ones this is about -
        # are complete in there anyway.
        counts = {}
        for at in (cache.get("burning") or {}).values():
            counts[at[:10]] = counts.get(at[:10], 0) + 1
    if not counts:
        return ""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    start = calendar.timegm(time.strptime(today, "%Y-%m-%d"))
    row, top = [], 0
    for i in range(days):
        t = time.gmtime(start + i * 86400)
        key = time.strftime("%Y-%m-%d", t)
        n = counts.get(key, 0)
        top = max(top, n)
        row.append((key, t, n))
    if not top:
        return ""
    # Everything from before today lands in the first bar: those reviews are
    # waiting now, they did not evaporate.
    overdue = sum(n for day, n in counts.items() if day < today)
    if overdue:
        key, t, n = row[0]
        row[0] = (key, t, n + overdue)
        top = max(top, row[0][2])

    bars = []
    for key, t, n in row:
        h = 6 + round(94 * n / top) if n else 3
        weekend = t.tm_wday >= 5
        label = time.strftime("%a %d %b", t)
        bars.append(
            f'<span class="bar{" we" if weekend else ""}{" none" if not n else ""}"'
            f' title="{label}: {n:,}"><i style="height:{h}%"></i>'
            f'<em>{t.tm_mday}</em></span>')
    note = (f'{overdue:,} of them are waiting in your queue right now. '
            if overdue else "")
    return (f'<p class="sub">{note}The tallest day in the next {days} is '
            f'<b>{top:,}</b>.</p>'
            f'<div class="burncal">{"".join(bars)}</div>')


def levelup_banner(extras, level: int) -> str:
    """The one moment the app used to walk straight past.

    It shows until the next refresh rotates the snapshots, which is the same
    life as the "+31 since last refresh" beside it - both are answers to "what
    happened while I was away".
    """
    up = extras.get("leveled")
    if not up:
        return ""
    was, now = up["from"], up["to"]
    old_tier, new_tier = tier_of(was)[0], tier_of(now)[0]
    moved = old_tier != new_tier
    art = (f'<img class="tierart" src="/asset/tier-{new_tier.lower()}.webp"'
           f' alt="" width="720" height="480">' if moved else "")
    line = LEVEL_LINES.get(now, "")
    return (f'<div class="levelup" id="levelup">'
            f'<img class="crab" src="/asset/crabigator-jump.png" alt=""'
            f' width="112" height="72">'
            f'<div class="say"><h3>Level {was} &rarr; <b>{now}</b></h3>'
            + (f'<p class="sub">and out of {old_tier} into <b>{new_tier}</b></p>'
               if moved else f'<p class="sub">still in {new_tier}</p>')
            + (f'<p class="verse">{esc(line)}</p>' if line else "")
            + f'</div>{art}</div>')


def tier_strip(level: int) -> str:
    """Where you are on the climb, on the page you read every day."""
    name, lo, hi = tier_of(level)
    nxt = next((t for t in TIERS if t[1] > hi), None)
    to_go = (nxt[1] - level) if nxt else 0
    tail = (f'<span class="sub">{to_go} level{"" if to_go == 1 else "s"} to '
            f'<b>{nxt[0]}</b></span>' if nxt else
            '<span class="sub">the last of them</span>')
    line = LEVEL_LINES.get(level, "")
    return (f'<div class="tier"><img src="/asset/tier-{name.lower()}-sm.webp"'
            f' alt="" width="240" height="160" loading="lazy">'
            f'<div><h3>{name}</h3>'
            f'<p class="sub">levels {lo}&ndash;{hi}</p>{tail}'
            + (f'<p class="verse">{esc(line)}</p>' if line else "")
            + '</div></div>')


def tier_ladder(level: int) -> str:
    """All six, so the shape of what is left is visible at a glance."""
    here = tier_of(level)[0]
    cards = []
    for name, lo, hi in TIERS:
        state = ("now" if name == here else
                 "done" if hi < level else "ahead")
        cards.append(
            f'<button type="button" class="rung {state}" data-tier="{name.lower()}"'
            f' aria-expanded="false">'
            f'<img src="/asset/tier-{name.lower()}.webp" alt="" width="720"'
            f' height="480" loading="lazy">'
            f'<span class="cap"><b>{name}</b><span>{lo}&ndash;{hi}</span></span>'
            f'</button>')
    return (f'<div class="ladder">{"".join(cards)}</div>'
            f'<div class="tierbox" id="tierbox" hidden></div>')


# Which section belongs on which page. Everything used to be one 7,664px
# column; this splits it by how often you look at a thing rather than by what
# it is about. The ids are the slugs h2() makes from the headings.
PAGES = {
    "today":  ("Today", ["your-titles", "your-tracked-titles", "immersion",
                         "finished"]),
    "levels": ("Levels", ["the-climb", "what-each-level-would-buy-you",
                          "nearly-within-reach"]),
    # Burning is about single items, the same as the grid and the leeches, so
    # it sits with them rather than with the titles.
    "kanji":  ("Kanji", ["kanji-grid", "leeches-blocking-your-reading",
                         "one-answer-from-burned"]),
    "browse": ("Browse", ["browse", "read-check-any-text"]),
}


def dashboard(user, cache, known, decks, history, extras, page: str = "today") -> str:
    """decks is a list of (deck dict, analysis dict) for the user's titles."""
    lvl = cache.get("level") or 0
    subjects, assignments = cache["subjects"], cache["assignments"]
    sections: list[tuple[str, str]] = []

    def h2(label: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
        sections.append((slug, label))
        return f'<h2 id="{slug}">{esc(label)}</h2>'

    h = [f'<div class="hero">{w.brand_mark(ICON)}<div class="hd">'
         f'<h1>{esc(user["username"])}</h1>'
         f'<p class="sub">WaniKani level {lvl} &middot; '
         f'data from {esc((cache.get("fetched_at") or "")[:16].replace("T", " "))}'
         f'</p></div></div>', w.NAV_SLOT]

    def card(n, label, delta=None, items=None):
        d = f'<div class="d">{delta:+d} since last refresh</div>' if delta else ""
        # A count with the things behind it. "+31 kanji" is a receipt; the
        # thirty-one characters are the thing you actually wanted to see.
        if d and items:
            chips = "".join(f'<span class="ni">{esc(c)}</span>' for c in items[:120])
            more = (f'<span class="ni more">+{len(items) - 120}</span>'
                    if len(items) > 120 else "")
            d = (f'<button class="d" type="button" aria-expanded="false">'
                 f'{delta:+d} since last refresh</button>'
                 f'<div class="newly" hidden>{chips}{more}</div>')
        return (f'<div class="card"><div class="n">{n}</div>'
                f'<div class="l">{label}</div>{d}</div>')

    # The standing of the account - the counters, the bar, what has changed
    # since last time - is a thing you read once when you arrive, not something
    # to re-read above every screen. It stays on today; the other pages keep
    # only the line that says whose numbers these are and how old they are.
    on_today = page == "today"
    if not on_today:
        h.append(w.BROWSE_SLOT)
    if on_today:
        h.append('<div class="cards">')
        h.append(card(lvl, "level"))
        h.append(card(len(known["kanji_known"]), "kanji known",
                      extras.get("d_kanji"), extras.get("new_kanji")))
        h.append(card(len(known["words_known_set"]), "words known",
                      extras.get("d_words"), extras.get("new_words")))
        if decks:
            best = max(decks, key=lambda r: r[1]["kanji_cov_occ"])
            h.append(card(f'{best[1]["kanji_cov_occ"]:.0f}%',
                          f'best: {esc(w.deck_title(best[0])[:14])}'))
        ntot = extras.get("nihongo_totals")
        if ntot:
            h.append(f'<div class="card"><div class="n">{ntot["hours"]:.0f}h</div>'
                     f'<div class="l">immersion logged</div>'
                     f'<div class="d">{ntot["listening"]:.0f}h listening &middot; '
                     f'{ntot["reading"]:.0f}h reading'
                     + (f' &middot; {ntot["streak"]}d streak' if ntot["streak"] else "")
                     + '</div></div>')
        h.append("</div>")
        # The bar and the counters are one thought - where WaniKani has got you -
        # so they sit together at the top rather than as a section to scroll to.
        h.append(levelup_banner(extras, lvl))
        h.append(tier_strip(lvl))
        h.append(w.level_bar_html(w.level_progress(cache), w.wk_pace(cache)))
        counts = w.month_totals(cache)
        if counts:
            h.append(w.counters_html(counts))
        h.append(w.BROWSE_SLOT)

    if not decks:
        h.append(h2("Your titles"))
        h.append('<p class="empty">Nothing on your jiten.moe lists yet. Mark '
                 'something as watching/reading or plan to watch/read on '
                 'jiten.moe, or use the search above, then refresh.</p>')
    else:
        h.append(h2("Your tracked titles"))
        groups = w.by_media_type(decks)
        split = len(groups) > 1
        nprog = extras.get("nihongo") or {}
        tcls = "sortable tight grouped" if split else "sortable tight"
        if nprog:
            tcls += " nt"
        for mtype, group in groups:
            if split:
                h.append(f'<h3 class="mediahead">'
                         f'{esc(w.MEDIA_TYPES.get(mtype, "other"))}'
                         f' <span>{len(group)}</span></h3>')
            h.append(f'<div class="wrap"><table class="{tcls}"><tr>'
                     f'<th>title</th><th class="num">kanji</th>'
                     f'<th class="num">finish L{lvl}</th><th class="num">jiten</th>'
                     f'<th class="num">lvl 95%</th><th class="num">ceiling</th>'
                     f'<th class="num">trend</th>'
                     + ('<th class="num">immersion</th>' if nprog else "")
                     + '</tr>')
            for deck, res in group:
                did = deck.get("deckId")
                live = deck.get("coverage")
                k = res["kanji_cov_occ"]
                subs = w.jimaku_url(deck, extras.get("jimaku_key"))
                out = w.outside_link(deck)
                fin = w.finishing_level(res, lvl)
                trend = _trend(history.get(did, []))
                nsort, ncell = w.nihongo_cell(nprog.get(did))
                h.append(
                    f'<tr><td><a href="https://jiten.moe/decks/media/{did}/detail"'
                    f' target="_blank" rel="noopener">{esc(w.deck_title(deck))}</a>'
                    + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                       f' rel="noopener" title="Look it up on {esc(out[0])}">'
                       f'{esc(out[0])}</a>' if out else "")
                    + (f' <button class="subs" data-entry="{subs.rsplit("/", 1)[-1]}"'
                       f' data-title="{esc(w.deck_title(deck))}"'
                       f' title="Japanese subtitles on jimaku.cc">subs</button>'
                       if subs else "")
                    + (f' <button class="subs gapbtn" data-deck="{did}"'
                       f' data-title="{esc(w.deck_title(deck))}"'
                       f' title="The words you cannot read in this yet">'
                       f'words</button>')
                    + f' <button class="subs setst" data-deck="{did}" data-st="3"'
                      f' data-done="finished ✓"'
                      f' title="Mark as finished on jiten.moe">finished</button>'
                    + f' <button class="subs setst" data-deck="{did}" data-st="0"'
                      f' data-done="removed ✓" data-confirm="1" data-drop="1"'
                      f' title="Take it off your jiten.moe lists">remove</button>'
                    + f'</td>'
                    f'<td class="num">{k:.1f}%'
                    f'<span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
                    f'<td class="num">{f"{fin:.1f}% <span class=\'up\'>{fin-k:+.1f}</span>" if fin else "&mdash;"}</td>'
                    f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
                    f'<td class="num">{w.level_for(res["curve"], 95) or "&mdash;"}</td>'
                    f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
                    f'<td class="num">{trend}</td>'
                    + (f'<td class="num" data-sort="{nsort}">{ncell}</td>'
                       if nprog else "")
                    + '</tr>')
            h.append("</table></div>")
        h.append('<div id="subsbox" class="subsbox" hidden></div>')
        h.append('<div id="gapbox" class="gapbox" hidden></div>')

    done = extras.get("finished") or []
    h.append(h2("Finished"))
    if done:
        h.append('<div class="wrap"><table class="sortable"><tr><th>title</th>'
                 '<th class="num">chars</th>'
                 '<th class="num">jiten coverage</th></tr>')
        hide = "this.style.visibility='hidden'"
        for d in done:
            did = d["deck_id"]
            cov = d.get("coverage")
            out = d.get("link")
            h.append(
                f'<tr><td class="withcover"><span class="ct">'
                f'<img class="cover" loading="lazy" alt="" src="{w.cover_url(did)}"'
                f' onerror="{hide}">'
                f'<span><a href="https://jiten.moe/decks/media/{did}/detail"'
                f' target="_blank" rel="noopener">{esc(d["title"])}</a>'
                + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                   f' rel="noopener" title="Look it up on {esc(out[0])}">'
                   f'{esc(out[0])}</a>' if out else "")
                + '</span></span></td>'
                f'<td class="num">{d.get("chars") or 0:,}</td>'
                f'<td class="num">{f"{cov:.0f}%" if cov else "&mdash;"}</td></tr>')
        h.append("</table></div>")
    else:
        h.append('<p class="empty">Nothing marked finished yet. Search above for '
                 'something you have already seen and press <b>finished</b>, or '
                 'use the button beside a title you are tracking.</p>')

    if decks:
        if extras.get("has_nihongo_key"):
            h.append(h2("Immersion"))
            h.append(w.immersion_html(extras.get("nihongo_totals"),
                                      extras.get("nihongo_unmeasured")))

        h.append(h2("Read-check any text"))
        h.append(w.READ_HTML)

        h.append(h2("What each level would buy you"))
        h.append(w.CHART_HTML)
        h.append(w.SLIDER_HTML)

    if page == "levels":
        h.append(h2("The climb"))
        h.append('<p class="sub">WaniKani cuts the sixty levels into six and '
                 'gives them names. You are somewhere inside one of them.</p>')
        h.append(tier_ladder(lvl))

    # Kanji grid
    occ: Counter[str] = Counter()
    for _d, res in decks:
        occ.update(res["kanji_occ"])
    per_title: dict[str, list] = {}
    for i, (_d, res) in enumerate(decks):
        for ch, n in res["kanji_occ"].items():
            per_title.setdefault(ch, []).append([i, n])
    for ch in per_title:
        per_title[ch].sort(key=lambda p: -p[1])

    grid = sorted(
        ({"c": s["characters"], "l": s["level"], "s": assignments.get(sid, 0),
          "k": s["characters"] in known["kanji_known"],
          "n": occ.get(s["characters"], 0),
          "r": "、".join(s.get("readings") or []), "m": s.get("meaning") or "",
          "d": per_title.get(s["characters"], []),
          **({"up": 1} if s["characters"] in extras.get("moved_up", set()) else {})}
         for sid, s in subjects.items() if s["type"] == "kanji"),
        key=lambda k: (k["l"], -k["n"], k["c"]))

    h.append(h2("Kanji grid"))
    h.append(f'<p class="sub">All {len(grid):,} WaniKani kanji by level. '
             f'{sum(1 for k in grid if k["n"]):,} of them turn up in your '
             f'titles.</p>')
    h.append(w.GRID_HTML)

    # Leeches
    struggling = {s["characters"]: (assignments[sid], s["level"],
                                    "、".join(s.get("readings") or []),
                                    s.get("meaning") or "")
                  for sid, s in subjects.items()
                  if s["type"] == "kanji" and 1 <= assignments.get(sid, 0) <= 4}
    leeches = sorted(((n, ch) + struggling[ch] for ch, n in occ.items()
                      if ch in struggling and ch not in known["kanji_known"]),
                     reverse=True)[:24]
    if leeches:
        h.append(h2("Leeches blocking your reading"))
        blocked = sum(row[0] for row in leeches)
        h.append(f'<p class="sub">The {len(leeches)} worst: <b>{blocked:,} '
                 f'occurrences</b> you cannot read, all in items already sitting '
                 f'in your review queue.</p>')
        h.append('<p class="sub">Apprentice kanji, ranked by how often they turn '
                 'up in the titles above.</p>')
        h.append('<div class="wrap"><table class="sortable"><tr><th>kanji</th>'
                 '<th>reading</th><th>meaning</th><th class="num">occurrences</th>'
                 '<th>stage</th><th class="num">wk level</th></tr>')
        for n, ch, stage, klvl, rd, mean in leeches:
            url = "https://www.wanikani.com/kanji/" + urllib.parse.quote(ch)
            h.append(f'<tr><td class="kanji"><a href="{url}" target="_blank"'
                     f' rel="noopener">{esc(ch)}</a></td><td>{esc(rd)}</td>'
                     f'<td>{esc(mean)}</td><td class="num">{n:,}</td>'
                     f'<td>{w.SRS_STAGE_NAMES.get(stage, "?")}</td>'
                     f'<td class="num">{klvl}</td></tr>')
        h.append("</table></div>")

    # Items one correct answer from Burned. Enlightened is four months long, so
    # these are easy to miss entirely - the item is in the queue for a day and
    # then either gone for good or back down the ladder.
    burning = cache.get("burning") or {}
    if burning:
        now = time.time()
        rows = []
        for sid, at in burning.items():
            s = subjects.get(sid)
            if not s:
                continue
            label, t = _burn_when(at, now)
            rows.append((t or float("inf"), label, s))
        rows.sort(key=lambda r: r[0])
        # Ten at a time, ten pages deep: past that it is not "about to burn",
        # it is a list of everything, and 400 rows of table was 80kB of page
        # for the nine tenths nobody scrolls to.
        shown_rows, rows = len(rows), rows[:100]
        due = sum(1 for t, _l, _s in rows if t and t <= now)
        week = sum(1 for t, _l, _s in rows if t and now < t <= now + 86400 * 7)
        total = cache.get("burning_total") or shown_rows

        h.append(h2("One answer from burned"))
        shown = ("" if total <= len(rows) else
                 f", showing the soonest {len(rows):,} of {total:,}")
        h.append(f'<p class="sub"><b>{due:,}</b> in your queue now &middot; '
                 f'<b>{week:,}</b> within the week{shown}.</p>')
        h.append(burn_calendar(cache))
        h.append('<p class="sub">Enlightened items, whose next review is the one '
                 'that burns them &mdash; if you answer it correctly. Get it '
                 'wrong and the item drops back down for months.</p>')
        h.append('<div class="wrap"><table id="burn" class="sortable"><tr><th>item</th>'
                 '<th>reading</th><th>meaning</th><th>type</th>'
                 '<th class="num">wk level</th><th>could burn</th></tr>')
        for t, label, s in rows:
            ch = s["characters"]
            kind = {"kanji": "kanji", "vocabulary": "vocab",
                    "kana_vocabulary": "kana"}.get(s["type"], s["type"])
            url = ("https://www.wanikani.com/"
                   + ("kanji/" if s["type"] == "kanji" else "vocabulary/")
                   + urllib.parse.quote(s.get("slug") or ch))
            h.append(f'<tr><td class="kanji"><a href="{url}" target="_blank"'
                     f' rel="noopener">{esc(ch)}</a></td>'
                     f'<td>{esc("、".join(s.get("readings") or []))}</td>'
                     f'<td>{esc(s.get("meaning") or "")}</td><td>{kind}</td>'
                     f'<td class="num">{s["level"]}</td>'
                     f'<td data-sort="{t if t != float("inf") else 0:.0f}">'
                     f'{label}</td></tr>')
        h.append("</table></div>")
        # Ten at a time: the whole point is the next few, and a sortable table
        # of four hundred rows buries them.
        h.append('<div class="pager" id="burnpager">'
                 '<button type="button">&lsaquo; prev</button><span></span>'
                 '<button type="button">next &rsaquo;</button></div>')
        # Burning an item is striking a name off for good, and this is the list
        # of names one answer away. The clip is 212kB and is not fetched until
        # somebody actually asks for it.
        names = (f"{due:,} name{'' if due == 1 else 's'} in your queue right now"
                 if due else
                 f"{week:,} name{'' if week == 1 else 's'} this week"
                 if week else f"{len(rows):,} names waiting")
        h.append(f'<div class="dn"><button type="button" id="dnplay"'
                 f' title="the list">&#9760;</button>'
                 f'<div class="dnbox" id="dnbox" hidden>'
                 f'<video muted loop playsinline preload="none"></video>'
                 f'<p class="sub">{names}.</p></div></div>')

    h.append(h2("Nearly within reach"))
    h.append(w.REACH_HTML)
    h.append('<footer>Kanji figures computed here from Jiten word lists; the '
             'jiten column is your own account coverage. Deck word lists are '
             'cached and shared between accounts.</footer>')

    # Data the client scripts need.
    pace = round(w.wk_pace(cache) or 0, 1)
    blob = json.dumps({"level": lvl, "pace": pace,
                       "known": "".join(sorted(known["kanji_known"])),
                       "levels": known["kanji_level"], "types": MEDIA_TYPES},
                      ensure_ascii=False, separators=(",", ":"))
    track = json.dumps({"level": lvl, "pace": pace, "titles": [
        {"t": w.deck_title(d), "now": round(r["kanji_cov_occ"], 2),
         "c": [round(p, 2) for _l, p in r["curve"]]}
        for d, r in sorted(decks, key=lambda r: -r[1]["kanji_cov_occ"])]},
        ensure_ascii=False, separators=(",", ":"))

    # Keep the preamble - hero, cards, level bar, all of which belong to no
    # heading - and then only the sections this page is for. A section is
    # everything from its own h2 up to the next one.
    wanted = set(PAGES[page][1])
    kept, current = [], None
    for part in h:
        mark = re.match(r'<h2 id="([a-z0-9-]+)"', part)
        if mark:
            current = mark.group(1)
        if current is None or current in wanted or part.startswith("<footer"):
            kept.append(part)
    body = "".join(kept)
    links = [(s, t) for s, t in sections if s in wanted]
    if page == "browse":
        links = [("browse", "Browse jiten.moe")] + links
    body = body.replace(w.BROWSE_SLOT, w.BROWSE_HTML if page == "browse" else "")
    # One or two sections do not need jump links to themselves.
    body = body.replace(w.NAV_SLOT, "" if len(links) < 3 else "<nav>" + "".join(
        f'<a href="#{s}">{esc(t)}</a>' for s, t in links) + "</nav>")

    # Only the data this page can actually use. The grid is 52kB of it and
    # belongs to one section; sending it to the other three was most of what
    # made a page load heavy.
    data = [f"const WK={blob};const LIVE=true;const OTHERS=[];",
            f"const GRID_LEVEL={lvl};"]
    if page == "kanji":
        data.append(f"const GRID={w.grid_payload(grid)};")
        data.append(f"const GRID_TITLES="
                    f"{json.dumps([w.deck_title(d) for d, _ in decks], ensure_ascii=False)};")
    if page == "levels":
        data.append(f"const TRACK={track};")
        data.append(f"const REACH_TARGET={min(60, lvl + 5)};")
    if page == "browse":
        data.append(f"const TAGS={json.dumps(extras.get('tags', []), ensure_ascii=False)};")

    title = f'{user["username"]} - {PAGES[page][0].lower()}'
    return shell(title, body, user=user, page=page,
                 scripts=f'<script>{"".join(data)}</script>' + DASH_TAG)


def _trend(rows) -> str:
    pts = [(r["day"], r["kanji_cov"]) for r in rows if r.get("kanji_cov") is not None]
    if len(pts) < 2:
        return "&mdash;"
    delta = pts[-1][1] - pts[0][1]
    try:
        d0 = time.mktime(time.strptime(pts[0][0], "%Y-%m-%d"))
        d1 = time.mktime(time.strptime(pts[-1][0], "%Y-%m-%d"))
        days = round((d1 - d0) / 86400)
    except ValueError:
        days = 0
    cls = ' class="up"' if delta > 0 else ""
    return f'<span{cls}>{delta:+.1f}pp</span> / {days}d'


# ------------------------------------------------------------------ bundles

# The stylesheet and the shared scripts are the same bytes for every account
# and every page load, and they were being inlined into all of them: 30kB of
# CSS and 39kB of JavaScript, re-sent and re-parsed on every navigation. As
# files they are fetched once and then cost nothing. The name carries a hash of
# the contents, so a changed file is a changed URL and the cache never serves a
# stale one - which is what lets them be cached for a year.
#
# The offline report is untouched: it has to be one file that works with no
# server, so it keeps inlining everything.

def _bundle(*parts: str) -> str:
    return "\n".join(parts)


CSS_BUNDLE = _bundle(w.REPORT_CSS, AUTH_CSS, LOAD_CSS, MOBILE_CSS, BURN_CSS,
                     TOGETHER_CSS, w.SLIDER_CSS, w.GRID_CSS, w.CHART_CSS,
                     w.REACH_CSS, w.BROWSE_CSS, w.SUBS_CSS, w.READ_CSS, w.GAP_CSS)

# Every page needs these three; only the dashboard needs the rest.
CORE_JS = _bundle(LOAD_JS, MOBILE_JS, w.SORT_JS)
DASH_JS = _bundle(w.SLIDER_JS, w.CHART_JS, w.GRID_JS, w.REACH_JS, w.BROWSE_JS,
                  w.READ_JS, w.GAP_JS, w.SUBS_JS, w.STATUS_JS, BURN_JS, DN_JS,
                  TIER_JS, CARD_JS)

BUNDLES: dict[str, tuple[str, str]] = {}


def _serve(name: str, ext: str, body: str, mime: str) -> str:
    path = f"{name}.{hashlib.sha256(body.encode()).hexdigest()[:10]}.{ext}"
    BUNDLES[path] = (body, mime)
    return "/s/" + path


CSS_URL = _serve("app", "css", CSS_BUNDLE, "text/css")
CORE_URL = _serve("core", "js", CORE_JS, "text/javascript")
DASH_URL = _serve("dash", "js", DASH_JS, "text/javascript")
DASH_TAG = f'<script src="{DASH_URL}" defer></script>'
