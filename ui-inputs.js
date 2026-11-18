/* שדות תאריך / שעה / חודש עם אפשרות הקלדה.
   כל שדה מסוג date / time / month / datetime-local מקבל תיבת הקלדה ליד כפתור לוח שנה.
   השדה המקורי נשאר בדף (מוסתר), כך שכל הקוד הקיים שקורא ממנו .value ממשיך לעבוד.
   אפשר להקליד בחופשיות: 25/12/2026, 25.12.26, 0930, 9:30, 12/2026 ועוד. */
(function () {
  const PH = { date: 'יום/חודש/שנה', time: 'שעה:דקות', month: 'חודש/שנה', 'datetime-local': 'יום/חודש/שנה שעה:דקות' };
  const ICON = { date: '📅', time: '🕒', month: '📅', 'datetime-local': '📅' };
  const pad = n => String(n).padStart(2, '0');

  function fmt(type, v) {
    if (!v) return '';
    if (type === 'date') { const [y, m, d] = v.split('-'); return d && `${d}/${m}/${y}`; }
    if (type === 'time') return v.slice(0, 5);
    if (type === 'month') { const [y, m] = v.split('-'); return m && `${m}/${y}`; }
    if (type === 'datetime-local') { const [dt, tm] = v.split('T'); return `${fmt('date', dt)} ${(tm || '').slice(0, 5)}`.trim(); }
    return v;
  }
  function year(y) { y = +y; return y < 100 ? 2000 + y : y; }
  function validDate(y, m, d) { const t = new Date(y, m - 1, d); return t.getFullYear() === y && t.getMonth() === m - 1 && t.getDate() === d; }
  function parseTime(s) {
    s = s.trim(); if (!s) return null;
    let h, mi; const m = s.match(/^(\d{1,2})\s*[:.\s]\s*(\d{1,2})$/);
    if (m) { h = +m[1]; mi = +m[2]; }
    else if (/^\d{1,4}$/.test(s)) { if (s.length <= 2) { h = +s; mi = 0; } else { h = +s.slice(0, s.length - 2); mi = +s.slice(-2); } }
    else return null;
    return h <= 23 && mi <= 59 ? `${pad(h)}:${pad(mi)}` : null;
  }
  function parseDate(s) {
    const p = s.trim().split(/[\/.\-\s]+/).filter(Boolean);
    let d, m, y;
    if (p.length === 1 && /^\d{6}(\d{2})?$/.test(p[0])) { d = +p[0].slice(0, 2); m = +p[0].slice(2, 4); y = year(p[0].slice(4)); }
    else if (p.length === 3) { if (p[0].length === 4) { y = +p[0]; m = +p[1]; d = +p[2]; } else { d = +p[0]; m = +p[1]; y = year(p[2]); } }
    else if (p.length === 2) { d = +p[0]; m = +p[1]; y = new Date().getFullYear(); }
    else return null;
    return validDate(y, m, d) ? `${y}-${pad(m)}-${pad(d)}` : null;
  }
  function parse(type, s) {
    s = (s || '').trim();
    if (!s) return '';
    if (type === 'time') return parseTime(s);
    if (type === 'date') return parseDate(s);
    if (type === 'month') {
      const p = s.split(/[\/.\-\s]+/).filter(Boolean); if (p.length !== 2) return null;
      let m, y; if (p[0].length === 4) { y = +p[0]; m = +p[1]; } else { m = +p[0]; y = year(p[1]); }
      return m >= 1 && m <= 12 ? `${y}-${pad(m)}` : null;
    }
    if (type === 'datetime-local') {
      const mm = s.match(/^(.*?\d)\s+(\d{1,2}[:.]?\d{0,2})$/);
      if (!mm) { const d = parseDate(s); return d ? `${d}T00:00` : null; }
      const d = parseDate(mm[1]), t = parseTime(mm[2]);
      return d && t ? `${d}T${t}` : null;
    }
    return null;
  }

  function enhance(inp) {
    if (inp._typed || inp.closest('.dt-wrap')) return;
    const type = inp.type; inp._typed = true;
    const wrap = document.createElement('span');
    wrap.className = 'dt-wrap';
    wrap.style.cssText = 'display:inline-flex;align-items:center;gap:4px;position:relative;max-width:100%;' + (inp.style.width === '100%' ? 'width:100%;' : '');
    const txt = document.createElement('input');
    txt.type = 'text'; txt.className = (inp.className || '') + ' dt-text'; txt.placeholder = PH[type];
    txt.inputMode = type === 'time' ? 'numeric' : 'text'; txt.autocomplete = 'off';
    txt.style.cssText = (inp.getAttribute('style') || '') + ';flex:1;min-width:' + (type === 'datetime-local' ? '150px' : type === 'time' ? '70px' : '105px') + ';margin:0;';
    txt.setAttribute('aria-label', (inp.getAttribute('aria-label') || '') + ' (אפשר להקליד ' + PH[type] + ')');
    const btn = document.createElement('button');
    btn.type = 'button'; btn.textContent = ICON[type]; btn.title = 'בחירה מהלוח';
    btn.setAttribute('aria-label', 'פתיחת בחירה');
    btn.style.cssText = 'border:1px solid #FFD1DC;background:#fff;border-radius:10px;padding:6px 8px;cursor:pointer;font-size:1em;line-height:1;flex:0 0 auto;';
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(txt); wrap.appendChild(btn); wrap.appendChild(inp);
    // השדה המקורי נשאר בדף אבל בלתי נראה, כדי שלוח השנה של הדפדפן יוכל להיפתח ממנו
    inp.style.cssText += ';position:absolute;inset:auto 0 0 auto;width:1px;height:1px;opacity:0;pointer-events:none;border:0;padding:0;';
    inp.tabIndex = -1;
    let last = inp.value;
    txt.value = fmt(type, inp.value);
    function commit() {
      const v = parse(type, txt.value);
      if (v === null) { txt.style.borderColor = '#d9534f'; txt.title = 'לא הבנתי. לדוגמה: ' + PH[type]; return; }
      txt.style.borderColor = ''; txt.title = '';
      if (v !== inp.value) {
        inp.value = v; last = v;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
      }
      txt.value = fmt(type, v);
    }
    txt.addEventListener('blur', commit);
    txt.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); commit(); } });
    btn.addEventListener('click', () => {
      if (inp.disabled) return;
      try { inp.showPicker(); } catch (e) { inp.style.pointerEvents = 'auto'; inp.focus(); inp.click(); inp.style.pointerEvents = 'none'; }
    });
    inp.addEventListener('change', () => { last = inp.value; txt.value = fmt(type, inp.value); txt.style.borderColor = ''; });
    inp._typedSync = () => {
      if (!inp.isConnected) return;
      txt.disabled = btn.disabled = inp.disabled;
      wrap.style.opacity = inp.disabled ? '.55' : '';
      if (inp.value !== last && document.activeElement !== txt) { last = inp.value; txt.value = fmt(type, inp.value); }
    };
  }

  const SEL = 'input[type="date"],input[type="time"],input[type="month"],input[type="datetime-local"]';
  function scan(root) {
    if (root.matches && root.matches(SEL)) enhance(root);
    if (root.querySelectorAll) root.querySelectorAll(SEL).forEach(enhance);
  }
  function start() {
    scan(document);
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.nodeType === 1) scan(n); })))
      .observe(document.body, { childList: true, subtree: true });
    setInterval(() => document.querySelectorAll(SEL).forEach(i => i._typedSync && i._typedSync()), 400);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  window.bpParseTyped = parse;   // לבדיקות
})();
