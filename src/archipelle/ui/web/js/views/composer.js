import { t } from '../i18n.js';
import { element } from '../render.js';
import { state, isBusy } from '../store.js';

export function renderComposer(actions) {
  const host = document.getElementById('composer');
  host.replaceChildren();
  const conversation = state.conversation;
  if (!conversation) return;
  const busy = isBusy(conversation.id);

  const box = element('div', 'composer__box');
  const input = element('textarea', 'textarea composer__input');
  input.placeholder = t('ui.composer.placeholder');
  input.value = state.draft;
  input.disabled = busy;
  input.addEventListener('input', (event) => actions.draft(event.target.value));
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (!busy) actions.send();
    }
  });
  box.append(input);

  const row = element('div', 'composer__row');
  const toggle = element('div', 'toggle');
  for (const mode of ['quick', 'explore']) {
    const option = element('button', 'toggle__option', t(`ui.mode.${mode}`));
    option.type = 'button';
    option.disabled = busy;
    option.setAttribute('aria-pressed', String(conversation.mode === mode));
    option.addEventListener('click', () => actions.setMode(mode));
    toggle.append(option);
  }
  row.append(toggle);

  const browse = element('button', 'button', t('ui.composer.browse'));
  browse.type = 'button';
  browse.disabled = busy;
  browse.title = t('ui.composer.browse_hint');
  browse.addEventListener('click', actions.chooseScope);
  row.append(browse);

  if (conversation.scope_rel) {
    const badge = element('span', 'badge');
    badge.append(element('span', null, conversation.scope_rel));
    const remove = element('button', 'badge__remove', '×');
    remove.type = 'button';
    remove.title = t('ui.composer.clear_scope');
    remove.disabled = busy;
    remove.addEventListener('click', () => actions.setScope(null));
    badge.append(remove);
    row.append(badge);
  }

  row.append(element('div', 'header__spacer'));

  const actionsRow = element('div', 'composer__actions');
  if (busy) {
    const stop = element('button', 'button button--danger', t('ui.composer.stop'));
    stop.type = 'button';
    stop.addEventListener('click', actions.stop);
    actionsRow.append(stop);
  } else {
    if (state.items.some((item) => item.role === 'assistant' || item.role === 'notice')) {
      const deeper = element('button', 'button', t('ui.composer.continue'));
      deeper.type = 'button';
      deeper.title = t('ui.composer.continue_hint');
      deeper.addEventListener('click', actions.continueDeeper);
      actionsRow.append(deeper);
    }
    const send = element('button', 'button button--primary', t('ui.composer.send'));
    send.type = 'button';
    send.addEventListener('click', actions.send);
    actionsRow.append(send);
  }
  row.append(actionsRow);
  box.append(row);
  host.append(box);
}
