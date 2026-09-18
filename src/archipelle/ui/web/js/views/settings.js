import { formatBytes, t } from '../i18n.js';
import { element } from '../render.js';
import { state } from '../store.js';
import { confirm } from './dialogs.js';

function card(title) {
  const box = element('div', 'card');
  box.append(element('h2', 'card__title', title));
  return box;
}

function field(label, control, hint) {
  const wrapper = element('div', 'field');
  wrapper.append(element('label', 'field__label', label));
  wrapper.append(control);
  if (hint) wrapper.append(element('div', 'field__hint', hint));
  return wrapper;
}

export function renderSettings(actions) {
  const host = document.getElementById('screen');
  host.hidden = false;
  host.replaceChildren();
  const inner = element('div', 'screen__inner');

  const top = element('div', 'row row--between');
  top.append(element('h1', null, t('ui.settings.title')));
  const close = element('button', 'button', t('ui.common.close'));
  close.type = 'button';
  close.addEventListener('click', () => actions.openScreen(null));
  top.append(close);
  inner.append(top);

  inner.append(providersCard(actions));
  inner.append(behaviourCard(actions));
  inner.append(dataCard(actions));

  const advanced = element('button', 'button', t('ui.settings.advanced'));
  advanced.type = 'button';
  advanced.addEventListener('click', () => actions.openScreen('advanced'));
  inner.append(advanced);

  host.append(inner);
}

function providersCard(actions) {
  const box = card(t('ui.settings.providers'));
  const defaultSelect = element('select', 'select');
  for (const provider of state.providers) {
    const option = element('option', null, provider.label);
    option.value = provider.id;
    if (provider.id === state.settings.default_provider) option.selected = true;
    defaultSelect.append(option);
  }
  defaultSelect.addEventListener('change', (event) =>
    actions.updateSettings({ default_provider: event.target.value }));
  box.append(field(t('ui.settings.default_provider'), defaultSelect));

  for (const provider of state.providers) {
    const row = element('div', 'row row--between');
    const label = element('div');
    label.append(element('strong', null, provider.label));
    if (provider.outside_eu) {
      label.append(element('div', 'muted', t('ui.settings.outside_eu')));
    }
    row.append(label);

    const controls = element('div', 'row');
    const input = element('input', 'input');
    input.type = 'password';
    input.placeholder = provider.masked_key || t('ui.settings.key_placeholder');
    input.autocomplete = 'off';
    controls.append(input);
    const save = element('button', 'button', t('ui.common.save'));
    save.type = 'button';
    save.addEventListener('click', () => {
      actions.saveKey(provider.id, input.value);
      input.value = '';
    });
    controls.append(save);
    if (provider.masked_key) {
      const remove = element('button', 'button button--danger', t('ui.common.delete'));
      remove.type = 'button';
      remove.addEventListener('click', () => actions.deleteKey(provider.id));
      controls.append(remove);
    }
    row.append(controls);
    box.append(row);

    if (provider.is_custom) {
      const url = element('input', 'input');
      url.value = state.settings.custom_provider.base_url || '';
      url.placeholder = 'http://localhost:11434/v1';
      url.addEventListener('change', (event) => actions.setCustomUrl(event.target.value));
      box.append(field(t('ui.settings.custom_url'), url, t('ui.settings.custom_url_hint')));
      const models = element('input', 'input');
      models.value = (state.settings.custom_provider.models || []).map((m) => m.id).join(', ');
      models.placeholder = t('ui.settings.custom_models_placeholder');
      models.addEventListener('change', (event) => actions.setCustomModels(event.target.value));
      box.append(field(t('ui.settings.custom_models'), models));
    }
  }
  return box;
}

function behaviourCard(actions) {
  const box = card(t('ui.settings.behaviour'));

  const workdir = element('button', 'button');
  workdir.type = 'button';
  workdir.textContent = state.settings.default_workdir || t('ui.settings.choose_folder');
  workdir.addEventListener('click', actions.chooseDefaultWorkdir);
  box.append(field(t('ui.settings.default_workdir'), workdir));

  box.append(checkbox(
    t('ui.settings.general_knowledge'),
    state.settings.general_knowledge,
    (checked) => actions.updateSettings({ general_knowledge: checked }),
    t('ui.settings.general_knowledge_hint'),
  ));
  box.append(checkbox(
    t('ui.settings.dark_theme'),
    state.settings.theme === 'dark',
    (checked) => actions.updateSettings({ theme: checked ? 'dark' : 'light' }),
  ));
  box.append(checkbox(
    t('ui.settings.diagnostic'),
    state.settings.diagnostic,
    (checked) => actions.updateSettings({ diagnostic: checked }),
    t('ui.settings.diagnostic_hint'),
  ));

  const exclusions = element('textarea', 'textarea');
  const current = state.settings.scan_exclusions || { dir_names: [], file_patterns: [] };
  exclusions.value = [...current.dir_names, ...current.file_patterns].join('\n');
  exclusions.addEventListener('change', (event) => actions.setExclusions(event.target.value));
  box.append(field(t('ui.settings.exclusions'), exclusions, t('ui.settings.exclusions_hint')));
  return box;
}

function checkbox(label, checked, onChange, hint) {
  const wrapper = element('div', 'field');
  const row = element('label', 'row');
  const input = element('input');
  input.type = 'checkbox';
  input.checked = Boolean(checked);
  input.addEventListener('change', (event) => onChange(event.target.checked));
  row.append(input);
  row.append(element('span', null, label));
  wrapper.append(row);
  if (hint) wrapper.append(element('div', 'field__hint', hint));
  return wrapper;
}

function dataCard(actions) {
  const box = card(t('ui.settings.data'));
  const info = element('div', 'muted', t('ui.settings.cache_size', {
    size: formatBytes(state.storage ? state.storage.cache_bytes : 0),
  }));
  box.append(info);
  const row = element('div', 'row');
  const buttons = [
    ['ui.settings.purge_cache', () => actions.purge('cache'), false],
    ['ui.settings.purge_history', () => actions.purge('history'), true],
    ['ui.settings.purge_logs', () => actions.purge('logs'), false],
  ];
  for (const [key, action, danger] of buttons) {
    const button = element('button', `button ${danger ? 'button--danger' : ''}`.trim(), t(key));
    button.type = 'button';
    button.addEventListener('click', () => confirm({
      title: t(key),
      body: t(`${key}_confirm`),
      danger,
      onConfirm: action,
    }));
    row.append(button);
  }
  box.append(row);
  return box;
}

export function renderAdvanced(actions) {
  const host = document.getElementById('screen');
  host.hidden = false;
  host.replaceChildren();
  const inner = element('div', 'screen__inner');
  const top = element('div', 'row row--between');
  top.append(element('h1', null, t('ui.advanced.title')));
  const back = element('button', 'button', t('ui.common.back'));
  back.type = 'button';
  back.addEventListener('click', () => actions.openScreen('settings'));
  top.append(back);
  inner.append(top);
  inner.append(element('p', 'muted', t('ui.advanced.explain')));

  for (const provider of state.providers) {
    const box = card(provider.label);
    const configured = (state.settings.mode_models || {})[provider.id] || {};
    for (const mode of ['quick', 'explore']) {
      const select = element('select', 'select');
      const auto = element('option', null, t('ui.advanced.automatic'));
      auto.value = '';
      select.append(auto);
      for (const model of provider.models) {
        const option = element('option', null, model.label);
        option.value = model.id;
        if (configured[mode] === model.id) option.selected = true;
        select.append(option);
      }
      select.addEventListener('change', (event) =>
        actions.setModeModel(provider.id, mode, event.target.value));
      box.append(field(t(`ui.mode.${mode}`), select));
    }
    // Saisie libre : un modèle absent du catalogue (offre restreinte, nouveauté…).
    if (!provider.is_custom) {
      const extra = element('input', 'input');
      extra.value = ((state.settings.extra_models || {})[provider.id] || [])
        .map((model) => model.id)
        .join(', ');
      extra.placeholder = 'ministral-14b-2512';
      extra.addEventListener('change', (event) =>
        actions.setExtraModels(provider.id, event.target.value));
      box.append(field(t('ui.advanced.extra_models'), extra, t('ui.advanced.extra_models_hint')));
    }
    inner.append(box);
  }
  host.append(inner);
}
