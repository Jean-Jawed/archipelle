import { t } from '../i18n.js';
import { element } from '../render.js';
import { state } from '../store.js';

// Premier lancement : dossier, fournisseur, clé facultative (CDC §10).
export function renderOnboarding(actions) {
  const host = document.getElementById('screen');
  host.hidden = false;
  host.replaceChildren();
  const inner = element('div', 'screen__inner');
  inner.append(element('h1', null, t('ui.onboarding.title')));
  inner.append(element('p', 'muted', t('ui.onboarding.intro')));

  const folderCard = element('div', 'card');
  folderCard.append(element('h2', 'card__title', t('ui.onboarding.folder')));
  folderCard.append(element('p', 'muted', t('ui.onboarding.folder_hint')));
  const folder = element('button', 'button');
  folder.type = 'button';
  folder.textContent = state.settings.default_workdir || t('ui.settings.choose_folder');
  folder.addEventListener('click', actions.chooseDefaultWorkdir);
  folderCard.append(folder);
  inner.append(folderCard);

  const providerCard = element('div', 'card');
  providerCard.append(element('h2', 'card__title', t('ui.onboarding.provider')));
  for (const provider of state.providers) {
    const row = element('label', 'row');
    const radio = element('input');
    radio.type = 'radio';
    radio.name = 'provider';
    radio.value = provider.id;
    radio.checked = state.settings.default_provider === provider.id;
    radio.addEventListener('change', () =>
      actions.updateSettings({ default_provider: provider.id }));
    row.append(radio);
    const label = element('div');
    label.append(element('strong', null, provider.label));
    label.append(element('div', 'muted', provider.outside_eu
      ? t('ui.onboarding.outside_eu')
      : t('ui.onboarding.inside_eu')));
    row.append(label);
    providerCard.append(row);
  }
  const key = element('input', 'input');
  key.type = 'password';
  key.placeholder = t('ui.onboarding.key_placeholder');
  providerCard.append(key);
  providerCard.append(element('p', 'field__hint', t('ui.onboarding.key_optional')));
  inner.append(providerCard);

  const done = element('button', 'button button--primary', t('ui.onboarding.start'));
  done.type = 'button';
  done.addEventListener('click', () => actions.finishOnboarding(key.value));
  inner.append(done);
  host.append(inner);
}
