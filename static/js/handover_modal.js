/*
 * Counter security check before a depot hands an item over (depot dashboard).
 *
 *   1. Staff only (screen away from the claimant): confirm the true answer of each
 *      question the AI derived from photos, skip any they cannot tell, optionally
 *      add questions by picking the true value while holding the item.
 *   2. The claimant answers (multiple choice). Only the outcome comes back.
 *   3. pass → hand over (the existing resolve route); escalate → call / photo ID,
 *      then staff confirm; locked → claims desk.
 *
 * Accessible: role="dialog", aria-modal, labelled title, focus trapped while open,
 * Esc closes, focus returns to the button that opened it. Works on phones.
 * Everything rendered from the server is inserted with textContent (never HTML).
 */
(function () {
    const modal = document.getElementById('sec-modal');
    if (!modal) return;
    const body = document.getElementById('sec-body');
    const title = document.getElementById('sec-title');
    const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    let current = null;          // { matchId, requestId, trackingId, btn }
    let lastFocus = null;

    function el(tag, attrs, text) {
        const e = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([k, v]) => {
            if (k === 'class') e.className = v; else if (v !== undefined && v !== null) e.setAttribute(k, v);
        });
        if (text !== undefined) e.textContent = text;
        return e;
    }
    function btn(label, cls, onClick, type) {
        const b = el('button', { type: type || 'button', class: 'px-4 py-2 rounded-md text-sm font-bold ' + cls }, label);
        if (onClick) b.addEventListener('click', onClick);
        return b;
    }
    async function api(method, url, data) {
        try {
            const r = await fetch(url, { method, credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
                body: data === undefined ? undefined : JSON.stringify(data) });
            let json = {};
            try { json = await r.json(); } catch (e) { json = {}; }
            return { status: r.status, data: json };
        } catch (e) {
            return { status: 0, data: { error: { message: 'Network error. Check the connection and try again.' } } };
        }
    }
    function note(text, tone) {
        const tones = { info: 'bg-blue-50 border-blue-200 text-slate-700', ok: 'bg-green-50 border-green-200 text-green-800',
                        warn: 'bg-amber-50 border-amber-200 text-amber-900', bad: 'bg-red-50 border-red-200 text-tn-red' };
        return el('p', { class: 'p-3 rounded-md border text-sm ' + (tones[tone] || tones.info), role: tone === 'bad' ? 'alert' : 'status' }, text);
    }
    function busy(text) {
        body.replaceChildren(el('p', { class: 'text-sm text-slate-600', role: 'status', 'aria-live': 'polite' }, text));
    }

    // ── open / close with a focus trap ───────────────────────────────────────
    function focusables() {
        return [...modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
            .filter(x => !x.disabled && x.offsetParent !== null);
    }
    function open(ctx) {
        current = ctx;
        lastFocus = document.activeElement;
        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        load();
    }
    function close() {
        modal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
        body.replaceChildren();
        if (lastFocus && lastFocus.focus) lastFocus.focus();
        current = null;
    }
    modal.addEventListener('keydown', e => {
        if (e.key === 'Escape') { e.preventDefault(); close(); return; }
        if (e.key !== 'Tab') return;
        const f = focusables();
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    modal.querySelectorAll('[data-sec-close]').forEach(b => b.addEventListener('click', close));
    function focusFirst() { const f = focusables(); (f.find(x => x.tagName === 'INPUT') || f[0] || modal).focus(); }

    // ── states ───────────────────────────────────────────────────────────────
    async function load() {
        title.textContent = 'Security check before handover';
        busy('Preparing the questions… (the first time can take a few seconds)');
        const r = await api('GET', `/api/matches/${current.matchId}/security-questions`);
        if (r.status !== 200) return showError(r);
        render(r.data);
    }
    function showError(r) {
        const msg = (r.data && r.data.error && r.data.error.message) || 'Could not load the check.';
        body.replaceChildren(note(msg, 'bad'), el('div', { class: 'flex justify-end' }));
        body.lastChild.append(btn('Close', 'bg-slate-200 text-slate-800', close));
        focusFirst();
    }
    function render(s) {
        ({ staff_check: renderStaffCheck, ready: renderClaimant, passed: renderPassed,
           escalated: renderEscalated, insufficient: renderInsufficient, locked: renderLocked }[s.state] || showError)(s);
        focusFirst();
    }

    function radioGroup(q, name, withSkip) {
        const fs = el('fieldset', { class: 'space-y-2 p-3 border border-slate-200 rounded-md' });
        fs.append(el('legend', { class: 'px-1 text-sm font-bold text-slate-800' }, q.text));
        q.options.forEach((opt, i) => {
            const id = `${name}-${i}`;
            const row = el('label', { class: 'flex items-center gap-3 p-2 rounded-md hover:bg-slate-50 cursor-pointer min-h-[44px]', for: id });
            const input = el('input', { type: 'radio', name, id, value: String(i), class: 'h-5 w-5' });
            row.append(input, el('span', { class: 'text-sm text-slate-800' }, opt));
            fs.append(row);
        });
        if (withSkip) {
            const id = `${name}-skip`;
            const row = el('label', { class: 'flex items-center gap-3 p-2 rounded-md hover:bg-slate-50 cursor-pointer min-h-[44px]', for: id });
            row.append(el('input', { type: 'radio', name, id, value: 'skip', class: 'h-5 w-5' }),
                       el('span', { class: 'text-sm text-slate-500 italic' }, "None is right / can't tell — skip (add the true value below)"));
            fs.append(row);
        }
        return fs;
    }

    function renderStaffCheck(s) {
        title.textContent = 'Step 1 of 2 — Staff only';
        const form = el('form', { class: 'space-y-4', novalidate: '' });
        form.append(note('Keep this screen away from the claimant. Look at the item and pick the correct answer for each ' +
                         'question below (the camera suggested these questions but can be wrong). Skip any you cannot tell.', 'warn'));
        s.questions.forEach(q => form.append(radioGroup(q, `staff-${q.id}`, true)));
        if (s.addable && s.addable.length) {
            const box = el('fieldset', { class: 'space-y-3 p-3 border border-dashed border-slate-300 rounded-md' });
            box.append(el('legend', { class: 'px-1 text-sm font-bold text-slate-700' }, 'Add questions from the item (optional)'));
            box.append(el('p', { class: 'text-xs text-slate-500' },
                'Pick the TRUE value while holding the item; the system builds the wrong options so they cannot be confused with it. ' +
                'If the camera guessed one of these wrongly above, entering it here replaces that guess.'));
            s.addable.forEach(a => {
                const id = `add-${a.attribute}`;
                const wrap = el('div', { class: 'space-y-1' });
                wrap.append(el('label', { for: id, class: 'block text-xs font-bold text-slate-600' }, a.label));
                const sel = el('select', { id, 'data-attr': a.attribute, class: 'w-full h-10 px-2 border border-slate-300 rounded-md bg-white text-sm' });
                sel.append(el('option', { value: '' }, '— not adding —'));
                a.values.forEach(v => sel.append(el('option', { value: v }, v)));
                wrap.append(sel);
                box.append(wrap);
            });
            form.append(box);
        }
        const nameId = 'sec-staff-name';
        form.append(el('label', { for: nameId, class: 'block text-xs font-bold text-slate-600' }, 'Your name (logged)'));
        form.append(el('input', { id: nameId, required: '', maxlength: '60', autocomplete: 'name',
                                  class: 'w-full h-10 px-3 border border-slate-300 rounded-md text-sm' }));
        const err = el('div');
        const actions = el('div', { class: 'flex flex-wrap gap-2 justify-end' });
        actions.append(btn('Cancel', 'bg-slate-200 text-slate-800', close),
                       btn('Confirm — hand the screen to the claimant', 'bg-tn-blue text-white', null, 'submit'));
        form.append(err, actions);
        form.addEventListener('submit', async e => {
            e.preventDefault();
            const staffName = form.querySelector('#' + nameId).value.trim();
            if (staffName.length < 2) { err.replaceChildren(note('Enter your name first.', 'bad')); return; }
            const picks = {};
            for (const q of s.questions) {
                const c = form.querySelector(`input[name="staff-${q.id}"]:checked`);
                if (!c) { err.replaceChildren(note('Answer or skip every question.', 'bad')); return; }
                picks[q.id] = c.value === 'skip' ? null : Number(c.value);
            }
            const added = [...form.querySelectorAll('select[data-attr]')].filter(x => x.value)
                .map(x => ({ attribute: x.dataset.attr, value: x.value }));
            busy('Saving…');
            const r = await api('POST', `/api/matches/${current.matchId}/security-staff-check`, { picks, added, staffName });
            if (r.status !== 200) return showError(r);
            render(r.data);
        });
        body.replaceChildren(form);
    }

    function renderClaimant(s) {
        title.textContent = 'Step 2 of 2 — Questions for the claimant';
        const form = el('form', { class: 'space-y-4', novalidate: '' });
        form.append(note(`Hand the screen to the person collecting the item. ${s.questions.length} questions; ` +
                         `pick one answer each. Attempts left: ${s.attemptsLeft}.`, 'info'));
        s.questions.forEach(q => form.append(radioGroup(q, `ans-${q.id}`, false)));
        const err = el('div');
        const actions = el('div', { class: 'flex flex-wrap gap-2 justify-end' });
        actions.append(btn('Cancel', 'bg-slate-200 text-slate-800', close),
                       btn('Submit answers', 'bg-green-600 text-white', null, 'submit'));
        form.append(err, actions);
        form.addEventListener('submit', async e => {
            e.preventDefault();
            const answers = {};
            for (const q of s.questions) {
                const c = form.querySelector(`input[name="ans-${q.id}"]:checked`);
                if (!c) { err.replaceChildren(note('Please answer every question.', 'bad')); return; }
                answers[q.id] = Number(c.value);
            }
            busy('Checking…');
            const r = await api('POST', `/api/matches/${current.matchId}/security-answers`, { answers });
            if (r.status !== 200) return showError(r);
            const res = r.data.result;
            if (res === 'pass') return renderPassed();
            if (res === 'escalate') return renderEscalated({}, 'Some answers were wrong. Do not hand over yet.');
            if (res === 'locked') return renderLocked();
            const again = await api('GET', `/api/matches/${current.matchId}/security-questions`);
            if (again.status !== 200) return showError(again);
            render(again.data);
            body.prepend(note(`Not enough correct answers. Attempts left: ${r.data.attemptsLeft}.`, 'bad'));
        });
        body.replaceChildren(form);
    }

    function renderPassed() {
        title.textContent = 'Check passed';
        const wrap = el('div', { class: 'space-y-4' });
        wrap.append(note('The claimant passed the security check. Check their photo ID, then hand the item over.', 'ok'));
        const actions = el('div', { class: 'flex flex-wrap gap-2 justify-end' });
        actions.append(btn('Not now', 'bg-slate-200 text-slate-800', close),
                       btn('Hand over and mark resolved', 'bg-green-600 text-white', () => {
                           const ctx = current; close();
                           window.resolveMatch(ctx.matchId, ctx.requestId, ctx.trackingId, ctx.btn);
                       }));
        wrap.append(actions);
        body.replaceChildren(wrap);
    }

    function staffVerifyForm(intro, tone) {
        const form = el('form', { class: 'space-y-3', novalidate: '' });
        form.append(note(intro, tone));
        const nameId = 'sec-ov-name', last4Id = 'sec-ov-last4', chkId = 'sec-ov-check';
        form.append(el('label', { for: nameId, class: 'block text-xs font-bold text-slate-600' }, 'Your name (logged)'));
        form.append(el('input', { id: nameId, maxlength: '60', autocomplete: 'name', class: 'w-full h-10 px-3 border border-slate-300 rounded-md text-sm' }));
        form.append(el('label', { for: last4Id, class: 'block text-xs font-bold text-slate-600' }, 'Last 4 digits of the photo ID checked (optional)'));
        form.append(el('input', { id: last4Id, inputmode: 'numeric', maxlength: '4', pattern: '[0-9]{4}', class: 'w-40 h-10 px-3 border border-slate-300 rounded-md text-sm' }));
        const row = el('label', { for: chkId, class: 'flex items-start gap-3 text-sm text-slate-800 min-h-[44px]' });
        row.append(el('input', { type: 'checkbox', id: chkId, class: 'mt-1 h-5 w-5' }),
                   el('span', {}, 'I called the passenger on their registered phone and checked a photo ID whose name matches the report.'));
        form.append(row);
        const err = el('div');
        const actions = el('div', { class: 'flex flex-wrap gap-2 justify-end' });
        actions.append(btn('Cancel', 'bg-slate-200 text-slate-800', close),
                       btn('Verified by staff', 'bg-tn-blue text-white', null, 'submit'));
        form.append(err, actions);
        form.addEventListener('submit', async e => {
            e.preventDefault();
            const staffName = form.querySelector('#' + nameId).value.trim();
            const idChecked = form.querySelector('#' + chkId).checked;
            if (staffName.length < 2 || !idChecked) { err.replaceChildren(note('Enter your name and confirm the ID check.', 'bad')); return; }
            busy('Saving…');
            const r = await api('POST', `/api/matches/${current.matchId}/security-override`,
                                { staffName, idChecked, idLast4: form.querySelector('#' + last4Id).value.trim() || null });
            if (r.status !== 200) return showError(r);
            renderPassed();
        });
        return form;
    }
    function renderEscalated(s, lead) {
        title.textContent = 'Staff verification needed';
        body.replaceChildren(staffVerifyForm((lead ? lead + ' ' : '') +
            'Call the passenger on the phone number in their report and check a photo ID before handing over.', 'warn'));
    }
    function renderInsufficient(s) {
        title.textContent = 'Not enough questions for this item';
        body.replaceChildren(staffVerifyForm('This item has too few details the system can ask about reliably. ' +
            'Verify the person yourself: call the passenger and check a photo ID.', 'warn'));
    }
    function renderLocked() {
        title.textContent = 'Check locked';
        const wrap = el('div', { class: 'space-y-4' });
        wrap.append(note('Three failed attempts. Do not hand this item over here. The passenger must use the claims desk with a photo ID.', 'bad'));
        const actions = el('div', { class: 'flex justify-end' });
        actions.append(btn('Close', 'bg-slate-200 text-slate-800', close));
        wrap.append(actions);
        body.replaceChildren(wrap);
    }

    window.openSecurityCheck = function (matchId, requestId, trackingId, btnEl) {
        open({ matchId, requestId, trackingId, btn: btnEl });
    };
})();
