(function () {
  'use strict';

  var TOAST_LIFETIME_MS = 5000;

  var toggle = document.querySelector('[data-nav-toggle]');
  var nav = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
  if (nav) {
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('nav--open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // Registration: the server validates this and falls back to UTC.
  document.querySelectorAll('input[data-browser-timezone]').forEach(function (input) {
    try {
      input.value = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (error) {
      input.value = '';
    }
  });

  var toasts = document.getElementById('toasts');
  if (!toasts) {
    return;
  }

  // Toasts arrive out of band from the server; each stays for a few seconds.
  new MutationObserver(function (records) {
    records.forEach(function (record) {
      record.addedNodes.forEach(function (node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
          window.setTimeout(function () {
            node.remove();
          }, TOAST_LIFETIME_MS);
        }
      });
    });
  }).observe(toasts, { childList: true });

  // A 5xx or a lost connection has no fragment to show, so say something here.
  function showFailure() {
    var toast = document.createElement('div');
    toast.className = 'toast toast--error';
    toast.textContent = toasts.getAttribute('data-failure-text');
    toasts.appendChild(toast);
  }

  document.body.addEventListener('htmx:sendError', showFailure);
  document.body.addEventListener('htmx:responseError', function (event) {
    if (event.detail.xhr.status >= 500) {
      showFailure();
    }
  });
})();
