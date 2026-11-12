/*
 * Passenger report form helpers (Phase 2):
 *   - per-category hint prompts under the description;
 *   - one-tap storage-location pills.
 * The live description strength meter and its bubbles are in
 * static/js/description_meter.js (shared with the depot form).
 */
(function () {
    const category = document.getElementById('category_select');
    const hints = document.getElementById('category_hints');
    let V = null;

    function showHints() {
        if (!V || !category || !hints) return;
        const cat = V.categories.find(c => c.key === category.value);
        hints.textContent = cat && cat.hints.length ? 'Helpful to mention: ' + cat.hints.join(' · ') : '';
    }
    if (category) category.addEventListener('change', showHints);
    fetch('/api/vocabulary').then(r => r.json()).then(v => { V = v; showHints(); }).catch(() => {});

    // Storage-location pills: a radio group styled as chips.
    document.querySelectorAll('[data-storage-pill] input').forEach(r => r.addEventListener('change', () => {
        document.querySelectorAll('[data-storage-pill]').forEach(p => {
            const on = p.querySelector('input').checked;
            p.classList.toggle('border-tn-blue', on); p.classList.toggle('bg-blue-50', on); p.classList.toggle('text-tn-blue', on);
        });
    }));
})();
