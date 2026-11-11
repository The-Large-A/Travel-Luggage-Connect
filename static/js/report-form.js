/*
 * Passenger report form helpers (Phase 2):
 *   - match readiness meter + item-specific "add detail" bubbles under the description;
 *   - per-category hint prompts;
 *   - one-tap storage-location pills.
 *
 * The bubbles depend on WHAT was lost: an umbrella asks "Folding or long?" and
 * about the handle, a phone asks for the case colour and IMEI, documents ask whose
 * name is on them. They come from the server (GET /api/vocabulary →
 * vocabulary.DETAIL_PROMPTS) and are detected with the same word lists, so what
 * the passenger sees matches the stored score. Identifying details come first:
 * they are what let the matcher notify the owner without waiting for staff.
 * It never blocks submitting: it only nudges.
 */
(function () {
    const desc = document.getElementById('desc_input');
    if (!desc) return;
    const meter = document.getElementById('readiness');
    const bar = document.getElementById('readiness_bar');
    const label = document.getElementById('readiness_label');
    const chips = document.getElementById('readiness_chips');
    const category = document.getElementById('category_select');
    const hints = document.getElementById('category_hints');
    const MAX_VISIBLE = 5;
    let V = null;
    let prompts = [];

    const words = text => (text || '').normalize('NFC').toLowerCase().match(/[\p{L}\p{M}\p{N}]+/gu) || [];
    const norm = text => words(text).join(' ');

    function has(list, set, joined) {
        return list.some(x => {
            const w = norm(x);
            return w.includes(' ') ? joined.includes(' ' + w + ' ') : set.has(w);
        });
    }

    function done(p, text) {
        const list = words(text), set = new Set(list), joined = ' ' + list.join(' ') + ' ';
        const lbl = norm(p.insert);
        if (lbl && joined.match(new RegExp('(^| )' + lbl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\S'))) return true;
        switch (p.match) {
            case 'colour': return Object.values(V.colours).some(c => has(c, set, joined)) || has(V.materials, set, joined);
            case 'brand': return has(V.brands, set, joined);
            case 'contents': return Object.values(V.contents).some(c => has(c, set, joined));
            case 'marks': return has(V.marks, set, joined);
            case 'digits': return /\d{5,}/.test((text || '').replace(/[\s-]/g, ''));
            case 'digits_or_count': return /\d/.test(text || '') || ['one', 'two', 'three', 'few', 'many', 'several'].some(w => set.has(w));
            case 'anything': return list.length >= 3;
            default: return Array.isArray(p.match) && has(p.match, set, joined);
        }
    }

    function update() {
        if (!V) return;
        const text = desc.value;
        const state = prompts.map(p => ({ p, ok: done(p, text) }));
        const core = state.filter(s => s.p.core);
        const score = core.length ? Math.round(100 * core.filter(s => s.ok).length / core.length) : 0;
        bar.style.width = Math.max(score, 4) + '%';
        bar.className = 'h-full rounded-full transition-all duration-300 ' +
            (score >= 100 ? 'bg-green-600' : score >= 50 ? 'bg-tn-gold' : 'bg-tn-red');
        label.textContent = score >= 100 ? 'Excellent match potential' : score >= 75 ? 'Good — one more detail helps'
            : score >= 50 ? 'Getting there' : 'Needs more detail';
        meter.setAttribute('aria-valuenow', score);
        // Show the first few unanswered bubbles, in priority order.
        let shown = 0;
        chips.querySelectorAll('button').forEach(b => {
            const s = state.find(x => x.p.key === b.dataset.slot);
            const visible = s && !s.ok && shown < MAX_VISIBLE;
            if (visible) shown += 1;
            b.classList.toggle('hidden', !visible);
        });
    }

    function renderChips() {
        const cat = (category && category.value) || 'other';
        const entry = V.categories.find(c => c.key === cat) || V.categories.find(c => c.key === 'other');
        prompts = entry ? entry.prompts : [];
        chips.replaceChildren();
        prompts.forEach(p => {
            const b = document.createElement('button');
            b.type = 'button'; b.dataset.slot = p.key; b.textContent = '+ ' + p.chip;
            b.className = 'px-2.5 py-1 rounded-full border border-tn-blue/40 text-[11px] font-bold text-tn-blue bg-blue-50 hover:bg-blue-100';
            b.addEventListener('click', () => {
                const sep = desc.value && !/\s$/.test(desc.value) ? (/[.,;]$/.test(desc.value.trim()) ? ' ' : '. ') : '';
                desc.value += sep + p.insert;
                desc.focus();
                desc.setSelectionRange(desc.value.length, desc.value.length);
                update();
            });
            chips.appendChild(b);
        });
        update();
    }

    function showHints() {
        if (!V || !category || !hints) return;
        const cat = V.categories.find(c => c.key === category.value);
        hints.textContent = cat && cat.hints.length ? 'Helpful to mention: ' + cat.hints.join(' · ') : '';
    }

    let timer = null;
    desc.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(update, 200); });
    desc.addEventListener('voice-text', update);
    if (category) category.addEventListener('change', () => { if (V) { renderChips(); showHints(); } });

    fetch('/api/vocabulary').then(r => r.json()).then(v => {
        V = v; renderChips(); showHints(); meter.classList.remove('hidden');
    }).catch(() => { /* the meter is optional */ });

    // Storage-location pills: a radio group styled as chips.
    document.querySelectorAll('[data-storage-pill] input').forEach(r => r.addEventListener('change', () => {
        document.querySelectorAll('[data-storage-pill]').forEach(p => {
            const on = p.querySelector('input').checked;
            p.classList.toggle('border-tn-blue', on); p.classList.toggle('bg-blue-50', on); p.classList.toggle('text-tn-blue', on);
        });
    }));
})();
