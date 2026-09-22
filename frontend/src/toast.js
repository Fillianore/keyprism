/** Dismissible toast (Phase 3.6 D5): one precise availability/error
 *  message at a time — lazy (only shown from a user action such as
 *  clicking Notes), never auto-raised on panel load. Click × or wait
 *  for the auto-dismiss. */

import { t } from './i18n.js';

let stack = null;

function ensureStack() {
  if (stack && stack.isConnected) return stack;
  stack = document.createElement('div');
  stack.className = 'toast-stack';
  document.body.appendChild(stack);
  return stack;
}

export function showToast(message, { timeout = 8000 } = {}) {
  const host = ensureStack();
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  const msg = document.createElement('span');
  msg.className = 'toast-msg';
  msg.textContent = message; // textContent: message text is never HTML
  const x = document.createElement('button');
  x.type = 'button';
  x.className = 'toast-x';
  x.textContent = '×';
  x.setAttribute('aria-label', t('toastDismiss'));
  el.append(msg, x);
  host.appendChild(el);
  let timer = 0;
  const dismiss = () => {
    clearTimeout(timer);
    el.remove();
  };
  x.addEventListener('click', dismiss);
  if (timeout > 0) timer = setTimeout(dismiss, timeout);
  return dismiss;
}
