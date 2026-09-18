import { formatDay, t } from '../i18n.js';
import { element } from '../render.js';
import { state } from '../store.js';
import { confirm } from './dialogs.js';

export function renderSidebar(actions) {
  const host = document.getElementById('sidebar');
  host.replaceChildren();

  const newButton = element('button', 'button button--primary', t('ui.sidebar.new'));
  newButton.type = 'button';
  newButton.addEventListener('click', actions.newConversation);
  host.append(newButton);

  const search = element('input', 'input');
  search.type = 'search';
  search.placeholder = t('ui.sidebar.search');
  search.value = state.search;
  search.addEventListener('input', (event) => actions.search(event.target.value));
  host.append(search);

  const list = element('div', 'sidebar__list');
  let currentGroup = null;
  for (const conversation of state.conversations) {
    const group = formatDay(conversation.updated_at);
    if (group !== currentGroup) {
      currentGroup = group;
      list.append(element('div', 'sidebar__group', t(group)));
    }
    list.append(conversationRow(conversation, actions));
  }
  if (state.conversations.length === 0) {
    list.append(element('p', 'muted', t('ui.sidebar.empty')));
  }
  host.append(list);

  const footer = element('div', 'sidebar__footer');
  const settings = element('button', 'button button--ghost', t('ui.sidebar.settings'));
  settings.type = 'button';
  settings.addEventListener('click', () => actions.openScreen('settings'));
  footer.append(settings);
  host.append(footer);
}

function conversationRow(conversation, actions) {
  const row = element('div', 'sidebar__row');
  if (state.renamingId === conversation.id) {
    row.append(renameField(conversation, actions));
    return row;
  }
  const item = element('button', 'sidebar__item', conversation.title);
  item.type = 'button';
  item.title = `${conversation.title}\n${t('ui.sidebar.rename')}`;
  if (state.conversation && state.conversation.id === conversation.id) {
    item.setAttribute('aria-current', 'true');
  }
  item.addEventListener('click', () => actions.openConversation(conversation.id));
  item.addEventListener('dblclick', () => actions.startRename(conversation.id));
  row.append(item);

  const remove = element('button', 'sidebar__delete', '×');
  remove.type = 'button';
  remove.title = t('ui.sidebar.delete');
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    confirm({
      title: t('ui.sidebar.delete'),
      body: t('ui.sidebar.delete_confirm'),
      danger: true,
      onConfirm: () => actions.deleteConversation(conversation.id),
    });
  });
  row.append(remove);
  return row;
}

// Le champ est reconstruit à chaque rendu : c'est l'état qui porte l'édition en cours,
// sinon l'ouverture de la conversation (clic précédant le double-clic) le ferait
// disparaître aussitôt.
function renameField(conversation, actions) {
  const input = element('input', 'input sidebar__rename');
  input.value = conversation.title;
  let finished = false;
  const finish = (save) => {
    if (finished) return;
    finished = true;
    actions.finishRename(save ? input.value : null);
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') finish(true);
    if (event.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
  // Le focus est posé après l'insertion dans le document.
  setTimeout(() => {
    input.focus();
    input.select();
  }, 0);
  return input;
}
