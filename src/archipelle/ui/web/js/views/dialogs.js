import { t } from '../i18n.js';
import { element } from '../render.js';

const host = () => document.getElementById('dialogs');

function close() {
  host().replaceChildren();
}

export function show({ title, body, actions }) {
  const backdrop = element('div', 'dialog__backdrop');
  const dialog = element('div', 'dialog');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  if (title) dialog.append(element('h2', 'card__title', title));
  if (typeof body === 'string') dialog.append(element('p', null, body));
  else if (body) dialog.append(body);
  const row = element('div', 'dialog__actions');
  for (const action of actions || [{ label: t('ui.common.close') }]) {
    const button = element('button', `button ${action.variant || ''}`.trim(), action.label);
    button.type = 'button';
    button.addEventListener('click', () => {
      close();
      if (action.onClick) action.onClick();
    });
    row.append(button);
  }
  dialog.append(row);
  backdrop.append(dialog);
  backdrop.addEventListener('click', (event) => {
    if (event.target === backdrop) close();
  });
  host().replaceChildren(backdrop);
  const first = dialog.querySelector('input, button');
  if (first) first.focus();
}

export function confirm({ title, body, confirmLabel, onConfirm, danger }) {
  show({
    title,
    body,
    actions: [
      { label: t('ui.common.cancel') },
      {
        label: confirmLabel || t('ui.common.confirm'),
        variant: danger ? 'button--danger' : 'button--primary',
        onClick: onConfirm,
      },
    ],
  });
}

// Un lien n'est jamais ouvert : on montre l'adresse, avec un bouton Copier (CDC §9).
export function linkDialog(href) {
  const body = element('div', 'field');
  body.append(element('p', 'muted', t('ui.dialog.link_explain')));
  const field = element('input', 'input');
  field.value = href;
  field.readOnly = true;
  body.append(field);
  show({
    title: t('ui.dialog.link_title'),
    body,
    actions: [
      { label: t('ui.common.close') },
      {
        label: t('ui.common.copy'),
        variant: 'button--primary',
        onClick: () => navigator.clipboard && navigator.clipboard.writeText(href),
      },
    ],
  });
}

export function toast(message, kind) {
  const node = element('div', `toast ${kind === 'error' ? 'toast--error' : ''}`.trim(), message);
  document.getElementById('toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 8000 : 4000);
}
