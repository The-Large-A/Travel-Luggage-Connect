/*
 * Depot smart intake — camera side (Phase 2, Feature C).
 *
 *  1. Detect phone vs PC (only to choose the camera and the wording; what the
 *     page can do is decided by feature detection), with a manual switch.
 *  2. Live camera (getUserMedia — needs HTTPS or localhost). While "scanning",
 *     send a frame every 500 ms; the server keeps only sharp, well-lit, NEW views
 *     and answers with a hint for the staff member.
 *  3. Fallback when there is no live camera: the phone's own camera / files.
 *  4. Save → background analysis with a progress bar → result with the QR tag link.
 */
(function () {
    const root = document.getElementById('intake-root');
    if (!root) return;
    const CFG = {
        target: +root.dataset.target, minViews: +root.dataset.minViews,
        maxInside: +root.dataset.maxInside, maxCloseups: +root.dataset.maxCloseups,
    };
    const CSRF = document.querySelector('meta[name="csrf-token"]').content;
    const $ = id => document.getElementById(id);
    const video = $('video');
    const state = { draft: null, step: 'outside', scanning: false, inflight: false, timer: null, stream: null,
                    phone: false, live: false, counts: { views: 0, inside: 0, closeups: 0 } };

    // ── 1. phone or PC? ────────────────────────────────────────────────────
    function detectPhone() {
        if (navigator.userAgentData && typeof navigator.userAgentData.mobile === 'boolean') return navigator.userAgentData.mobile;
        const touchOnly = matchMedia('(pointer: coarse) and (hover: none)').matches;
        const ua = /Android|iPhone|iPad|iPod|Mobi/i.test(navigator.userAgent);
        const iPadOS = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1;
        return touchOnly || ua || iPadOS;
    }
    state.phone = localStorage.getItem('intakeMode') ? localStorage.getItem('intakeMode') === 'phone' : detectPhone();

    const GUIDE = {
        phone: {
            outside: 'Place the item on a plain surface. Walk slowly around it (or turn it), then flip it over. Keep it inside the dashed box.',
            inside: 'Open it and show what is inside, one item at a time if you can.',
            closeup: 'Move close to any name tag, label or printed text, then to the zip pull or a sticker, and tap the close-up buttons.',
        },
        pc: {
            outside: 'Hold the item up to the webcam inside the dashed box and turn it slowly: front, sides, back, then flip it.',
            inside: 'Open it and show the contents to the webcam.',
            closeup: 'Bring any name tag or label close to the webcam, then the zip pull or a sticker, and click the close-up buttons.',
        },
    };

    function setMode() {
        $('device-mode').textContent = state.phone ? '📱 Phone camera (rear)' : '💻 Laptop webcam';
        $('device-switch').textContent = state.phone ? 'Using a laptop? Switch' : 'Using a phone? Switch';
        showStep(state.step);
    }
    $('device-switch').addEventListener('click', async () => {
        state.phone = !state.phone;
        localStorage.setItem('intakeMode', state.phone ? 'phone' : 'pc');
        setMode();
        await startCamera();
    });

    // ── API helpers ────────────────────────────────────────────────────────
    async function api(method, url, body, isForm) {
        const opts = { method, credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF } };
        if (body && !isForm) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
        if (isForm) opts.body = body;
        const r = await fetch(url, opts);
        let data = {}; try { data = await r.json(); } catch (e) { data = {}; }
        return { status: r.status, data };
    }
    async function ensureDraft() {
        if (state.draft) return state.draft;
        const { status, data } = await api('POST', '/api/intake/drafts');
        if (status !== 201) throw new Error((data.error && data.error.message) || 'Could not start a scan.');
        state.draft = data.draft.draftId;
        return state.draft;
    }

    // ── 2. live camera ─────────────────────────────────────────────────────
    async function startCamera() {
        if (state.stream) { state.stream.getTracks().forEach(t => t.stop()); state.stream = null; }
        const can = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
        if (!can) return cameraOff(window.isSecureContext
            ? 'This browser cannot open the camera here. Use the photo button below.'
            : 'The live camera needs HTTPS (or localhost). Use the photo button below, or open this page over HTTPS.');
        try {
            state.stream = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: { facingMode: { ideal: state.phone ? 'environment' : 'user' },
                         width: { ideal: 1280 }, height: { ideal: 720 } },
            });
            video.srcObject = state.stream;
            await video.play();
            state.live = true;
            $('cam-off').classList.add('hidden');
            $('fallback').classList.add('hidden');
            $('live-controls').classList.remove('hidden');
            hint('Camera ready. Press “Start scanning”.');
        } catch (e) {
            cameraOff(e && e.name === 'NotAllowedError' ? 'Camera permission was denied. Allow it in the browser, or use the photo button below.'
                                                         : 'No camera found. Use the photo button below.');
        }
    }
    function cameraOff(why) {
        state.live = false;
        $('cam-off').classList.remove('hidden');
        $('cam-off-why').textContent = why;
        $('fallback').classList.remove('hidden');
        $('scan-btn').classList.add('hidden');
    }
    const hint = text => { $('cam-hint').textContent = text; };

    function grab(maxEdge, quality) {
        const w = video.videoWidth, h = video.videoHeight;
        if (!w || !h) return Promise.resolve(null);
        const scale = Math.min(1, maxEdge / Math.max(w, h));
        const c = document.createElement('canvas');
        c.width = Math.round(w * scale); c.height = Math.round(h * scale);
        c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
        return new Promise(res => c.toBlob(res, 'image/jpeg', quality));
    }

    async function sendFrame(blob, step) {
        const draft = await ensureDraft();
        const fd = new FormData();
        fd.append('frame', blob, 'frame.jpg'); fd.append('step', step);
        const { status, data } = await api('POST', `/api/intake/drafts/${draft}/frames`, fd, true);
        if (status === 200) applyFeedback(data);
        else hint((data.error && data.error.message) || 'Frame rejected.');
        return data;
    }
    async function sendCloseup(blob, kind) {
        const draft = await ensureDraft();
        const fd = new FormData();
        fd.append('photo', blob, 'closeup.jpg'); fd.append('kind', kind);
        const { status, data } = await api('POST', `/api/intake/drafts/${draft}/closeups`, fd, true);
        if (status === 200) applyFeedback(data);
        else hint((data.error && data.error.message) || 'Close-up rejected.');
    }

    async function tick() {
        if (!state.scanning || state.inflight || state.step === 'closeup') return;
        state.inflight = true;
        try {
            const blob = await grab(960, 0.75);
            if (blob) await sendFrame(blob, state.step);
        } catch (e) { hint('Network problem — retrying…'); }
        state.inflight = false;
    }

    $('scan-btn').addEventListener('click', async () => {
        if (state.scanning) return stopScan();
        try { await ensureDraft(); } catch (e) { return hint(e.message); }
        state.scanning = true;
        $('scan-btn').textContent = '■ Stop scanning';
        hint('Scanning… turn the item slowly.');
        state.timer = setInterval(tick, 500);
    });
    function stopScan() {
        state.scanning = false;
        clearInterval(state.timer);
        $('scan-btn').textContent = '▶ Start scanning';
    }
    ['tag', 'detail'].forEach(kind => $('closeup-' + kind).addEventListener('click', async () => {
        const blob = await grab(1600, 0.9);
        if (blob) await sendCloseup(blob, kind);
    }));

    // ── 3. fallback: native camera / files ─────────────────────────────────
    async function shrink(file, edge, q) {
        try {
            const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
            const s = Math.min(1, edge / Math.max(bmp.width, bmp.height));
            const c = document.createElement('canvas');
            c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
            c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
            return await new Promise(res => c.toBlob(res, 'image/jpeg', q));
        } catch (e) { return file; }
    }
    $('fallback-input').addEventListener('change', async e => {
        for (const file of e.target.files) {
            if (state.step === 'closeup') await sendCloseup(await shrink(file, 1600, 0.9), 'tag');
            else await sendFrame(await shrink(file, 960, 0.8), state.step);
        }
        e.target.value = '';
    });

    // ── feedback, steps and thumbnails ─────────────────────────────────────
    function applyFeedback(d) {
        state.counts = { views: d.views, inside: d.inside, closeups: d.closeups };
        if (d.hint) hint(d.hint);
        updateCoverage();
        if (d.kept) refreshThumbs();
        if (state.step === 'outside' && d.views >= CFG.target && state.scanning) {
            stopScan();
            hint(`✔ ${d.views} views captured. Next: show the inside.`);
        }
        if (state.step === 'inside' && (d.full || d.inside >= CFG.maxInside) && state.scanning) stopScan();
        $('save-btn').disabled = state.counts.views < CFG.minViews;
    }
    function updateCoverage() {
        const c = state.counts;
        let label, n, max;
        if (state.step === 'outside') { label = 'Different views of the outside'; n = c.views; max = CFG.target; }
        else if (state.step === 'inside') { label = 'Views of the contents (optional)'; n = c.inside; max = CFG.maxInside; }
        else { label = 'Close-ups (optional)'; n = c.closeups; max = CFG.maxCloseups; }
        $('coverage-label').textContent = label;
        $('coverage-count').textContent = `${n} / ${max}`;
        $('coverage-bar').style.width = Math.min(100, 100 * n / max) + '%';
    }
    async function refreshThumbs() {
        if (!state.draft) return;
        const { status, data } = await api('GET', `/api/intake/drafts/${state.draft}`);
        if (status !== 200) return;
        const d = data.draft;
        const paths = [...d.frames.outside, ...d.frames.inside, ...d.closeupPaths];
        $('thumbs').innerHTML = '';
        paths.forEach(p => {
            const img = document.createElement('img');
            img.src = '/static/' + p; img.alt = 'Captured view';
            img.className = 'aspect-square object-cover rounded border border-slate-200';
            $('thumbs').appendChild(img);
        });
    }
    function showStep(step) {
        state.step = step;
        if (state.scanning && step === 'closeup') stopScan();
        document.querySelectorAll('.step-btn').forEach(b => {
            const on = b.dataset.step === step;
            b.classList.toggle('bg-tn-blue', on); b.classList.toggle('text-white', on); b.classList.toggle('border-tn-blue', on);
            b.classList.toggle('bg-white', !on); b.classList.toggle('text-slate-600', !on);
        });
        $('step-guide').textContent = GUIDE[state.phone ? 'phone' : 'pc'][step];
        const close = step === 'closeup';
        $('closeup-tag').classList.toggle('hidden', !close || !state.live);
        $('closeup-detail').classList.toggle('hidden', !close || !state.live);
        $('scan-btn').classList.toggle('hidden', close || !state.live);
        updateCoverage();
    }
    document.querySelectorAll('.step-btn').forEach(b => b.addEventListener('click', () => showStep(b.dataset.step)));

    // ── where it was found ─────────────────────────────────────────────────
    $('found-date').value = new Date().toISOString().slice(0, 10);
    $('found-date').max = $('found-date').value;
    let searchTimer = null;
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    $('route-search').addEventListener('input', e => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(async () => {
            const routes = await (await fetch(`/api/transport/routes?depot_only=1&limit=20&q=${encodeURIComponent(e.target.value.trim())}`)).json();
            const box = $('route-results');
            box.innerHTML = routes.length ? '' : '<div class="px-3 py-2 text-xs text-slate-400">No routes for this depot.</div>';
            routes.forEach(rt => {
                const item = document.createElement('div');
                item.className = 'px-3 py-2 text-sm hover:bg-blue-50 cursor-pointer border-b border-slate-50';
                item.innerHTML = `<b class="mono text-tn-blue">${esc(rt.route_no)}</b> · ${esc(rt.origin_name)} → ${esc(rt.dest_name)}`;
                item.addEventListener('click', () => pickRoute(rt));
                box.appendChild(item);
            });
            box.classList.remove('hidden');
        }, 220);
    });
    document.addEventListener('click', e => { if (e.target !== $('route-search')) $('route-results').classList.add('hidden'); });
    async function pickRoute(rt) {
        $('route-id').value = rt.route_id;
        $('route-search').value = `${rt.route_no} · ${rt.origin_name} → ${rt.dest_name}`;
        const trips = await (await fetch(`/api/transport/routes/${rt.route_id}/trips`)).json();
        const sel = $('trip');
        sel.innerHTML = '<option value="">Not sure (optional)</option>';
        trips.forEach(t => sel.add(new Option(`${t.departure_time} · ${t.registration_no || 'Bus'}`, t.trip_id)));
        sel.disabled = !trips.length;
    }

    // ── 4. save → progress → result ────────────────────────────────────────
    $('save-btn').addEventListener('click', async () => {
        stopScan();
        $('save-error').classList.add('hidden');
        if (!$('route-id').value) return showSaveError('Choose the route the item was found on.');
        $('save-btn').disabled = true;
        const body = { route_id: $('route-id').value, trip_id: $('trip').value || null,
                       date: $('found-date').value, found_seat_area: $('seat-area').value || null };
        const { status, data } = await api('POST', `/api/intake/drafts/${state.draft}/finalize`, body);
        if (status !== 202) { $('save-btn').disabled = false; return showSaveError((data.error && data.error.message) || 'Could not save.'); }
        $('progress-card').classList.remove('hidden');
        poll();
    });
    function showSaveError(msg) { $('save-error').textContent = msg; $('save-error').classList.remove('hidden'); }
    async function poll() {
        const { status, data } = await api('GET', `/api/intake/drafts/${state.draft}`);
        if (status !== 200) return setTimeout(poll, 2000);
        const d = data.draft;
        const p = d.progress || { pct: 0, stage: '' };
        $('progress-bar').style.width = p.pct + '%';
        $('progress-pct').textContent = p.pct + '%';
        $('progress-stage').textContent = p.stage;
        if (d.status === 'done') return showResult(d.result);
        if (d.status === 'failed') {
            $('progress-card').classList.add('hidden');
            $('save-btn').disabled = false;
            return showSaveError(d.error || 'Saving failed. Your photos are kept; try again.');
        }
        setTimeout(poll, 1500);
    }
    function showResult(r) {
        $('progress-card').classList.add('hidden');
        $('result-card').classList.remove('hidden');
        $('res-public').textContent = r.publicDescription;
        $('res-tag').textContent = r.qrTagId;
        $('res-matches').textContent = r.matches;
        $('res-questions').textContent = r.questions.length ? r.questions.length + ' (' + r.questions.join(', ').replace(/_/g, ' ') + ')'
            : 'None confident enough — passengers will be verified by depot staff';
        $('res-print').href = `/staff/items/${r.foundId}/tag`;
        if (state.stream) state.stream.getTracks().forEach(t => t.stop());
    }

    setMode();
    startCamera();
})();
