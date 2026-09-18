import { t } from '../i18n.js';
import { element } from '../render.js';
import { state, isBusy } from '../store.js';

export function renderHeader(actions) {
  const host = document.getElementById('header');
  host.replaceChildren();
  const conversation = state.conversation;
  if (!conversation) return;
  const busy = isBusy(conversation.id);

  const toggleSidebar = element('button', 'button button--ghost', '☰');
  toggleSidebar.type = 'button';
  toggleSidebar.title = t('ui.header.toggle_sidebar');
  toggleSidebar.addEventListener('click', actions.toggleSidebar);
  host.append(toggleSidebar);

  const workdir = element('button', 'button header__workdir');
  workdir.type = 'button';
  workdir.disabled = busy;
  workdir.textContent = conversation.workdir || t('ui.header.no_workdir');
  workdir.title = t('ui.header.change_workdir');
  workdir.addEventListener('click', actions.chooseWorkdir);
  host.append(workdir);

  host.append(element('div', 'header__spacer'));

  const provider = element('select', 'select');
  provider.disabled = busy || conversation.provider_locked;
  for (const option of state.providers) {
    const node = element('option', null, option.label);
    node.value = option.id;
    if (option.id === conversation.provider_id) node.selected = true;
    provider.append(node);
  }
  provider.addEventListener('change', (event) => actions.setProvider(event.target.value));
  provider.title = conversation.provider_locked
    ? t('ui.header.provider_locked')
    : t('ui.header.provider');
  host.append(provider);

  const profile = state.providers.find((p) => p.id === conversation.provider_id);
  const model = element('select', 'select');
  model.disabled = busy;
  const models = profile ? profile.models : [];
  if (models.length === 0) {
    const node = element('option', null, conversation.model || t('ui.header.no_model'));
    node.value = conversation.model || '';
    model.append(node);
  }
  for (const option of models) {
    const node = element('option', null, option.label);
    node.value = option.id;
    node.title = option.description || '';
    if (option.id === conversation.model) node.selected = true;
    model.append(node);
  }
  model.addEventListener('change', (event) => actions.setModel(event.target.value));
  host.append(model);

  if (conversation.needs_key) {
    const warning = element('button', 'button button--danger', t('ui.header.missing_key'));
    warning.type = 'button';
    warning.addEventListener('click', () => actions.askKey(conversation.provider_id));
    host.append(warning);
  }
}
