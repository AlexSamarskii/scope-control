/* Progressive enhancement: server validation remains authoritative. */
(() => {
    document.querySelectorAll('form[data-conditional-kind]').forEach(form => {
        const decision = form.dataset.conditionalKind === 'decision';
        const selector = form.querySelector(decision ? '[name="category"]' : '[name="kind"]');
        const names = decision ? ['amount', 'currency', 'deadline_effect'] : ['sender_name', 'occurred_at', 'evidence_text', 'source_description'];
        const update = () => {
            const visible = decision ? selector.value === 'possible_change' : ['accepted', 'rejected'].includes(selector.value);
            names.forEach(name => {
                const field = form.querySelector(`[name="${name}"]`);
                const wrapper = form.querySelector(`#div_id_${name}`);
                if (wrapper) { wrapper.hidden = !visible; wrapper.style.display = visible ? "" : "none"; }
                if (field) field.disabled = !visible; // Values stay available when toggling back.
            });
        };
        selector.addEventListener('change', update);
        update();
    });
    const sidebar = document.getElementById('logo-sidebar');
    const toggle = document.querySelector('[aria-controls="logo-sidebar"]');
    if (!sidebar || !toggle) return;
    const mobile = matchMedia('(max-width: 1023px)');
    const update = () => {
        const open = toggle.getAttribute('aria-expanded') === 'true';
        sidebar.inert = mobile.matches && !open;
        if (sidebar.inert) sidebar.setAttribute('aria-hidden', 'true');
        else sidebar.removeAttribute('aria-hidden');
    };
    new MutationObserver(update).observe(toggle, {attributes: true, attributeFilter: ['aria-expanded']});
    mobile.addEventListener('change', update);
    update();
    document.addEventListener('keydown', event => {
        if (event.key !== 'Tab' || !mobile.matches || toggle.getAttribute('aria-expanded') !== 'true') return;
        const links = [...sidebar.querySelectorAll('a,button,select,input,[tabindex="0"]')].filter(el => el.getClientRects().length && !el.disabled);
        const cycle = [toggle, ...links];
        const index = cycle.indexOf(document.activeElement);
        event.preventDefault();
        const next = index < 0 ? 0 : (index + (event.shiftKey ? -1 : 1) + cycle.length) % cycle.length;
        cycle[next].focus();
    });
})();
