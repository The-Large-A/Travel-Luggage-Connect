/*
 * Photo dropzone (Phase 2) — enhances <input type="file" data-photo-input>.
 *
 *  - drag-and-drop on a PC, "Take photo" (rear camera) on phones, "Choose file";
 *  - instant preview with Remove;
 *  - compresses in the browser to ≤1600 px JPEG (q 0.85) before upload, which
 *    also strips EXIF (including GPS location) because the canvas re-encodes it.
 *
 * The original <input> stays in the form, so the server code is unchanged.
 */
(function () {
    const MAX_EDGE = 1600;
    const QUALITY = 0.85;

    async function compress(file) {
        if (!file || !file.type.startsWith('image/') || !window.createImageBitmap) return file;
        try {
            const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
            const scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height));
            const canvas = document.createElement('canvas');
            canvas.width = Math.round(bmp.width * scale);
            canvas.height = Math.round(bmp.height * scale);
            canvas.getContext('2d').drawImage(bmp, 0, 0, canvas.width, canvas.height);
            const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', QUALITY));
            if (!blob || blob.size >= file.size) return file;          // already small: keep it
            const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
            return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
        } catch (e) {
            return file;                                               // e.g. HEIC the browser cannot decode
        }
    }

    function kb(bytes) { return bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB'; }

    function enhance(input) {
        const zone = document.createElement('div');
        zone.className = 'photo-zone border-2 border-dashed border-slate-300 rounded-md p-4 text-center bg-slate-50 transition-colors';
        zone.innerHTML = `
            <div class="pz-empty space-y-2">
                <p class="text-sm font-bold text-slate-600">Drag a photo here</p>
                <div class="flex flex-wrap justify-center gap-2">
                    <button type="button" class="pz-camera btn-gov-primary text-xs uppercase tracking-widest px-3">📷 Take photo</button>
                    <button type="button" class="pz-browse btn-gov-secondary text-xs uppercase tracking-widest px-3">Choose file</button>
                </div>
                <span class="inline-block mt-1 text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-blue-100 text-tn-blue">AI vision enabled (CLIP &amp; OCR)</span>
            </div>
            <div class="pz-full hidden items-center gap-4 text-left">
                <img class="pz-preview w-24 h-24 object-cover rounded border border-slate-200" alt="Selected photo">
                <div class="flex-1 min-w-0">
                    <p class="pz-name text-sm font-bold text-slate-700 truncate"></p>
                    <p class="pz-size text-[11px] text-slate-500"></p>
                    <button type="button" class="pz-remove mt-2 text-xs font-bold text-tn-red uppercase hover:underline">✕ Remove / retake</button>
                </div>
            </div>`;
        const camera = document.createElement('input');
        camera.type = 'file'; camera.accept = 'image/*'; camera.setAttribute('capture', 'environment');
        camera.className = 'hidden';
        input.classList.add('hidden');
        input.after(zone);
        zone.after(camera);

        const $ = sel => zone.querySelector(sel);
        let previewUrl = null;

        async function use(file) {
            if (!file) return;
            const original = file.size;
            const out = await compress(file);
            const dt = new DataTransfer();
            dt.items.add(out);
            input.files = dt.files;
            if (previewUrl) URL.revokeObjectURL(previewUrl);
            previewUrl = URL.createObjectURL(out);
            $('.pz-preview').src = previewUrl;
            $('.pz-name').textContent = out.name;
            $('.pz-size').textContent = out.size < original ? `${kb(original)} → ${kb(out.size)} (compressed)` : kb(out.size);
            $('.pz-empty').classList.add('hidden');
            $('.pz-full').classList.remove('hidden'); $('.pz-full').classList.add('flex');
            input.dispatchEvent(new Event('photo-selected', { bubbles: true }));
        }
        function clear() {
            input.value = ''; camera.value = '';
            if (previewUrl) URL.revokeObjectURL(previewUrl);
            $('.pz-full').classList.add('hidden'); $('.pz-full').classList.remove('flex');
            $('.pz-empty').classList.remove('hidden');
        }

        $('.pz-browse').addEventListener('click', () => input.click());
        $('.pz-camera').addEventListener('click', () => camera.click());
        $('.pz-remove').addEventListener('click', clear);
        // Assigning input.files from code does not fire 'change', so this cannot loop.
        input.addEventListener('change', () => { if (input.files[0]) use(input.files[0]); });
        camera.addEventListener('change', () => use(camera.files[0]));
        ['dragenter', 'dragover'].forEach(t => zone.addEventListener(t, e => {
            e.preventDefault(); zone.classList.add('border-tn-blue', 'bg-blue-50');
        }));
        ['dragleave', 'drop'].forEach(t => zone.addEventListener(t, e => {
            e.preventDefault(); zone.classList.remove('border-tn-blue', 'bg-blue-50');
        }));
        zone.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) use(f); });
    }

    document.querySelectorAll('input[type=file][data-photo-input]').forEach(enhance);
})();
