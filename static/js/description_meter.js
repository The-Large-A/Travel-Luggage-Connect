/*
 * Live description strength meter — shared by the passenger report form
 * (index.html) and the depot found-item form (depot.html). Loaded from base.html.
 *
 * Usage: <textarea data-description-meter data-category-select="#category_select">
 * The meter is inserted right after the textarea. It scores five factors, the same
 * way as the server (vocabulary.description_strength): item type, colour, brand or
 * unique identifier, contents, distinctive features. Factors that do not apply to the
 * chosen kind of item are left out (a phone has no "contents").
 *   weak 0-30 · moderate 31-60 · strong 61-85 · excellent 86-100
 * Below the bar: tips for what is missing and clickable chips worded for the item
 * ("+ Brand (Wildcraft, Skybags…)", "+ Folding or long?") that add a prompt to the text.
 * It never blocks submitting.
 */
(function () {
    const FACTOR_TIPS = {
        type: 'Say what the item is (backpack, purse, phone…).',
        colour: 'Mention the colour.',
        identifier: 'Add a brand, a name on it, or a serial / IMEI number.',
        contents: 'Say what was inside it.',
        features: 'Describe a zip, strap, sticker, keychain, scratch or other mark.',
    };
    // Which item-specific prompt answers each missing factor (first one that exists).
    const FACTOR_PROMPTS = {
        type: ['type', 'what'], colour: ['colour', 'case', 'folder'], identifier: ['brand', 'serial', 'name'],
        contents: ['contents', 'pockets'], features: ['marks', 'zip', 'handle', 'case', 'sleeve'],
    };
    const GENERIC_CHIPS = {
        type: ['Say what it is', 'It is a '], colour: ['Add colour', 'Colour: '],
        identifier: ['Add brand / name', 'Brand: '], contents: ['What was inside?', 'Inside: '],
        features: ['Add a mark / feature', 'Marks: '],
    };
    const BANDS = [[30, 'Weak', 'bg-tn-red'], [60, 'Moderate', 'bg-amber-500'], [85, 'Strong', 'bg-tn-gold'],
                   [100, 'Excellent', 'bg-green-600']];
    let V = null;
    const waiting = [];

    const words = t => (t || '').normalize('NFC').toLowerCase().match(/[\p{L}\p{M}\p{N}]+/gu) || [];
    const norm = t => words(t).join(' ');
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    function hasAny(list, set, joined) {
        return list.some(x => {
            const w = norm(x);
            return w.includes(' ') ? joined.includes(' ' + w + ' ') : set.has(w);
        });
    }
    function labelFilled(insert, joined) {
        const l = norm(insert);
        return !!l && new RegExp('(^| )' + esc(l) + ' \\S').test(joined);
    }

    function analyse(text, category) {
        const list = words(text), set = new Set(list), joined = ' ' + list.join(' ') + ' ';
        const compact = (text || '').replace(/[\s-]/g, '');
        const found = {
            type: hasAny(V.itemNouns, set, joined),
            colour: Object.values(V.colours).some(v => hasAny(v, set, joined)),
            identifier: hasAny(V.brands, set, joined) || /\d{5,}/.test(compact) ||
                /(^|[^a-z0-9])(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{5,}/.test(norm(text)) ||
                labelFilled('Brand:', joined) || ['name', 'initials', 'nametag'].some(w => set.has(w)),
            contents: Object.values(V.contents).some(v => hasAny(v, set, joined)),
            features: hasAny(V.features, set, joined) || hasAny(V.marks, set, joined),
        };
        const applicable = ['type', 'colour', 'identifier', 'contents', 'features']
            .filter(f => !(f === 'contents' && V.noContents.includes(category)));
        const score = Math.round(100 * applicable.filter(f => found[f]).length / applicable.length);
        return { found, applicable, score, missing: applicable.filter(f => !found[f]) };
    }

    function promptsFor(category) {
        const c = V.categories.find(x => x.key === category) || V.categories.find(x => x.key === 'other');
        return c ? c.prompts : [];
    }

    function attach(area) {
        if (area.dataset.meterReady) return;
        area.dataset.meterReady = '1';
        const catSel = area.dataset.categorySelect ? document.querySelector(area.dataset.categorySelect) : null;
        const box = document.createElement('div');
        box.className = 'space-y-1.5 mt-2';
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', 'Description strength');
        const head = document.createElement('div');
        head.className = 'flex justify-between text-[11px] font-bold';
        const name = document.createElement('span');
        name.className = 'text-slate-500 uppercase tracking-wide';
        name.textContent = 'Description strength';
        const label = document.createElement('span');
        label.className = 'text-slate-700';
        label.setAttribute('aria-live', 'polite');
        head.append(name, label);
        const track = document.createElement('div');
        track.className = 'h-2 bg-slate-200 rounded-full overflow-hidden';
        track.setAttribute('role', 'meter');
        track.setAttribute('aria-valuemin', '0');
        track.setAttribute('aria-valuemax', '100');
        track.setAttribute('aria-label', 'Description strength');
        const bar = document.createElement('div');
        bar.className = 'h-full rounded-full transition-all duration-300';
        track.append(bar);
        const tips = document.createElement('ul');
        tips.className = 'text-[11px] text-slate-600 list-disc pl-4 space-y-0.5';
        const chips = document.createElement('div');
        chips.className = 'flex flex-wrap gap-1.5';
        box.append(head, track, tips, chips);
        area.insertAdjacentElement('afterend', box);

        function addText(insert) {
            const v = area.value;
            const sep = v && !/\s$/.test(v) ? (/[.,;]$/.test(v.trim()) ? ' ' : '. ') : '';
            area.value = v + sep + insert;
            area.focus();
            area.setSelectionRange(area.value.length, area.value.length);
            area.dispatchEvent(new Event('input', { bubbles: true }));
        }
        function chip(text, insert) {
            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = '+ ' + text;
            b.className = 'px-2.5 py-1 rounded-full border border-tn-blue/40 text-[11px] font-bold text-tn-blue bg-blue-50 hover:bg-blue-100 min-h-[32px]';
            b.addEventListener('click', () => addText(insert));
            return b;
        }
        function update() {
            const category = (catSel && catSel.value) || 'other';
            const r = analyse(area.value, category);
            const [, bandName, colour] = BANDS.find(([top]) => r.score <= top);
            bar.style.width = Math.max(r.score, 4) + '%';
            bar.className = 'h-full rounded-full transition-all duration-300 ' + colour;
            label.textContent = `${bandName} · ${r.score}%`;
            track.setAttribute('aria-valuenow', String(r.score));
            track.setAttribute('aria-valuetext', `${bandName}, ${r.score} percent`);
            tips.replaceChildren(...r.missing.map(f => {
                const li = document.createElement('li');
                li.textContent = FACTOR_TIPS[f];
                return li;
            }));
            // Chips: one per missing factor, worded for this item; then extra item-specific details.
            const prompts = promptsFor(category);
            const used = new Set();
            const list = [];
            for (const f of r.missing) {
                const p = FACTOR_PROMPTS[f].map(k => prompts.find(x => x.key === k)).find(Boolean);
                if (p && !used.has(p.key)) { used.add(p.key); list.push(chip(p.chip, p.insert)); }
                else if (!p) list.push(chip(...GENERIC_CHIPS[f]));
            }
            const joined = ' ' + words(area.value).join(' ') + ' ';
            for (const p of prompts) {
                if (list.length >= 6) break;
                if (!used.has(p.key) && !p.core && !labelFilled(p.insert, joined)) {
                    used.add(p.key);
                    list.push(chip(p.chip, p.insert));
                }
            }
            chips.replaceChildren(...list);
        }
        let t = null;
        area.addEventListener('input', () => { clearTimeout(t); t = setTimeout(update, 150); });
        area.addEventListener('voice-text', update);
        if (catSel) catSel.addEventListener('change', update);
        update();
    }

    function init() {
        document.querySelectorAll('textarea[data-description-meter]').forEach(a => (V ? attach(a) : waiting.push(a)));
    }
    fetch('/api/vocabulary').then(r => r.json()).then(v => { V = v; waiting.splice(0).forEach(attach); init(); })
        .catch(() => { /* the meter is optional */ });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    window.DescriptionMeter = { analyse: (text, category) => (V ? analyse(text, category) : null) };
})();
