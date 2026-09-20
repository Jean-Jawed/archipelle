// Barre latérale : le renommage doit survivre au redessin provoqué par le clic.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, '..', '..', 'src', 'archipelle', 'ui', 'web');

const { JSDOM } = await import('jsdom');
const dom = new JSDOM('<!doctype html><body><div id="sidebar"></div><div id="dialogs"></div></body>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const i18n = await import(path.join(web, 'js', 'i18n.js'));
const store = await import(path.join(web, 'js', 'store.js'));
const { renderSidebar } = await import(path.join(web, 'js', 'views', 'sidebar.js'));

i18n.install({ 'ui.sidebar.new': 'Nouvelle', 'ui.sidebar.search': 'Rechercher' });

const conversations = [
  { id: 'c1', title: 'Bail 2022', updated_at: Date.now() / 1000 },
  { id: 'c2', title: 'Factures', updated_at: Date.now() / 1000 },
];

function actionsStub(calls) {
  return {
    newConversation() {},
    search() {},
    openScreen() {},
    openConversation(id) {
      calls.push(['open', id]);
      store.update({ conversation: { id } }); // provoque un redessin, comme dans l'app
      renderSidebar(actionsStub(calls));
    },
    startRename(id) {
      calls.push(['startRename', id]);
      store.update({ renamingId: id });
      renderSidebar(actionsStub(calls));
    },
    finishRename(title) {
      calls.push(['finishRename', title]);
      store.update({ renamingId: null });
    },
    renameConversation() {},
    deleteConversation(id) {
      calls.push(['delete', id]);
    },
  };
}

test('un double-clic ouvre un champ de saisie qui survit au redessin', () => {
  const calls = [];
  store.update({ conversations, conversation: null, renamingId: null, search: '' });
  renderSidebar(actionsStub(calls));

  const item = document.querySelector('.sidebar__item');
  item.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));   // 1er clic
  item.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true })); // double-clic

  assert.deepEqual(calls, [['open', 'c1'], ['startRename', 'c1']]);
  const input = document.querySelector('.sidebar__rename');
  assert.ok(input, 'le champ de saisie doit être présent');
  assert.equal(input.value, 'Bail 2022');

  // Un nouveau rendu (événement du worker, rafraîchissement de la liste…) le conserve.
  renderSidebar(actionsStub(calls));
  assert.ok(document.querySelector('.sidebar__rename'));
});

test('Entrée enregistre, Échap annule', () => {
  const calls = [];
  store.update({ conversations, renamingId: 'c1' });
  renderSidebar(actionsStub(calls));
  let input = document.querySelector('.sidebar__rename');
  input.value = 'Bail révisé';
  input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  assert.deepEqual(calls.at(-1), ['finishRename', 'Bail révisé']);

  store.update({ renamingId: 'c1' });
  renderSidebar(actionsStub(calls));
  input = document.querySelector('.sidebar__rename');
  input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.deepEqual(calls.at(-1), ['finishRename', null]);
});

test('la croix demande confirmation avant de supprimer', () => {
  const calls = [];
  store.update({ conversations, renamingId: null });
  renderSidebar(actionsStub(calls));
  document.querySelector('.sidebar__delete').dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }),
  );
  assert.equal(calls.length, 0, 'aucune suppression avant confirmation');
  assert.ok(document.querySelector('.dialog'), 'une boîte de confirmation est affichée');
});
