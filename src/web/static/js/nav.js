/* The site frame's own behaviour: the UTC clock, the Tools menu (desktop and
 * phone) and the phone navigation drawer. Loaded on every page by base.html. */
(() => {
  'use strict';

  (function tick() {
    const time = Fmt.time(new Date());
    document.querySelectorAll('[data-utc-clock]').forEach(el => {
      el.textContent = time;
    });
    setTimeout(tick, 1000);
  })();

  const toolsNavControls = [...document.querySelectorAll('[data-tools-nav]')];
  function setToolsMenu(control, open, restoreFocus = false) {
    const trigger = control.querySelector('[data-tools-trigger]');
    control.querySelector('[data-tools-links]').classList.toggle('hidden', !open);
    trigger.setAttribute('aria-expanded', String(open));
    if (restoreFocus) trigger.focus();
  }
  toolsNavControls.forEach(control => {
    const trigger = control.querySelector('[data-tools-trigger]');
    trigger.addEventListener('click', () => {
      const open = trigger.getAttribute('aria-expanded') !== 'true';
      toolsNavControls.forEach(other => setToolsMenu(other, other === control && open));
    });
    control.addEventListener('keydown', event => {
      if (event.key === 'Escape' && trigger.getAttribute('aria-expanded') === 'true') {
        event.preventDefault();
        event.stopPropagation();
        setToolsMenu(control, false, true);
      } else if (event.target === trigger && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        toolsNavControls.forEach(other => setToolsMenu(other, other === control));
        const links = control.querySelectorAll('[data-tools-links] a');
        links[event.key === 'ArrowDown' ? 0 : links.length - 1].focus();
      }
    });
  });
  document.addEventListener('pointerdown', event => {
    toolsNavControls.forEach(control => {
      if (!control.contains(event.target)) setToolsMenu(control, false);
    });
  });
  document.addEventListener('focusin', event => {
    toolsNavControls.forEach(control => {
      if (!control.contains(event.target)) setToolsMenu(control, false);
    });
  });

  const mobileMenuTrigger = document.getElementById('mobile-menu-trigger');
  const mobilePrimaryNav = document.getElementById('mobile-primary-nav');
  function setMobileMenu(open) {
    if (!mobileMenuTrigger || !mobilePrimaryNav) return;
    mobilePrimaryNav.classList.toggle('hidden', !open);
    mobileMenuTrigger.setAttribute('aria-expanded', String(open));
    mobileMenuTrigger.textContent = open ? 'Close' : 'Menu';
    if (!open) toolsNavControls.forEach(control => setToolsMenu(control, false));
  }
  mobileMenuTrigger?.addEventListener('click', () => {
    setMobileMenu(mobileMenuTrigger.getAttribute('aria-expanded') !== 'true');
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && mobileMenuTrigger?.getAttribute('aria-expanded') === 'true') {
      setMobileMenu(false);
      mobileMenuTrigger.focus();
    }
  });
  window.matchMedia('(min-width: 1024px)').addEventListener('change', () => {
    setMobileMenu(false);
  });
})();
