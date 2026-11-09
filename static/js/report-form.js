/*
 * Passenger report form helpers (Phase 2):
 *   - match readiness meter + "add detail" chips under the description;
 *   - per-category hint prompts;
 *   - one-tap storage-location pills.
 *
 * The meter uses the same word lists as the server (GET /api/vocabulary →
 * vocabulary.detail_slots), so what the passenger sees matches the stored score.
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

    const SLOTS = [
        { key: 'colour', chip: '+ Add colour / material', insert: 'Colour: ' },
        { key: 'brand', chip: '+ Add brand', insert: 'Brand: ' },
        { key: 'contents', chip: '+ Mention inside items', insert: 'Inside: ' },
        { key: 'marks', chip: '+ Any stickers / tags', insert: 'Marks: ' },
    ];
    let V = null;

    const words = text => (text || '').normalize('NFC').toLowerCase().match(/[\p{L}\p{M}\p{N}]+/gu) || [];

    function slots(text) {
        const list = words(text);
        const set = new Set(list);
        const joined = ' ' + list.join(' ') + ' ';
        const has = arr => arr.some(x => {
            const w = words(x).join(' ');
            return w.includes(' ') ? joined.includes(' ' + w + ' ') : set.has(w);
        });
        return {
            colour: Object.values(V.colours).some(has) || has(V.materials),
            brand: has(V.brands),
            contents: Object.values(V.contents).some(has),
            marks: has(V.marks),
        };
    }

    function update() {
        if (!V) return;
        const found = slots(desc.value);
        const score = 25 * SLOTS.filter(s => found[s.key]).length;
        bar.style.width = Math.max(score, 4) + '%';
        bar.className = 'h-full rounded-full transition-all duration-300 ' +
            (score >= 100 ? 'bg-green-600' : score >= 50 ? 'bg-tn-gold' : 'bg-tn-red');
        label.textContent = score >= 100 ? 'Excellent match potential' : score >= 75 ? 'Good — one more detail helps'
            : score >= 50 ? 'Getting there' : 'Needs more detail';
        meter.setAttribute('aria-valuenow', score);
        chips.querySelectorAll('button').forEach(b => b.classList.toggle('hidden', found[b.dataset.slot]));
    }

    function addChips() {
        SLOTS.forEach(s => {
            const b = document.createElement('button');
            b.type = 'button'; b.dataset.slot = s.key; b.textContent = s.chip;
            b.className = 'px-2.5 py-1 rounded-full border border-tn-blue/40 text-[11px] font-bold text-tn-blue bg-blue-50 hover:bg-blue-100';
            b.addEventListener('click', () => {
                const sep = desc.value && !/\s$/.test(desc.value) ? (/[.,;]$/.test(desc.value.trim()) ? ' ' : '. ') : '';
                desc.value += sep + s.insert;
                desc.focus();
                desc.setSelectionRange(desc.value.length, desc.value.length);
                update();
            });
            chips.appendChild(b);
        });
    }

    function showHints() {
        if (!V || !category || !hints) return;
        const cat = V.categories.find(c => c.key === category.value);
        hints.textContent = cat && cat.hints.length ? 'Helpful to mention: ' + cat.hints.join(' · ') : '';
    }

    let timer = null;
    desc.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(update, 200); });
    desc.addEventListener('voice-text', update);
    if (category) category.addEventListener('change', showHints);

    fetch('/api/vocabulary').then(r => r.json()).then(v => {
        V = v; addChips(); update(); showHints(); meter.classList.remove('hidden');
    }).catch(() => { /* the meter is optional */ });

    // Storage-location pills: a radio group styled as chips.
    document.querySelectorAll('[data-storage-pill] input').forEach(r => r.addEventListener('change', () => {
        document.querySelectorAll('[data-storage-pill]').forEach(p => {
            const on = p.querySelector('input').checked;
            p.classList.toggle('border-tn-blue', on); p.classList.toggle('bg-blue-50', on); p.classList.toggle('text-tn-blue', on);
        });
    }));
})();
