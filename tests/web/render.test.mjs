// Tests du rendu sûr et des utilitaires d'interface (exécutés par Node).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, '..', '..', 'src', 'archipelle', 'ui', 'web');

// Sous Windows, un chemin absolu (« D:\… ») n'est pas une adresse de module valide :
// Node exige une URL « file:// ». Cette aide rend les imports portables.
const moduleUrl = (...parts) => pathToFileURL(path.join(web, ...parts)).href;

// La fenêtre est simulée par jsdom, puis les bibliothèques RÉELLEMENT LIVRÉES
// (ui/web/vendor) y sont exécutées : les tests portent sur ce qui sera embarqué.
const { JSDOM } = await import('jsdom');
const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
dom.window.eval(readFileSync(path.join(web, 'vendor', 'markdown-it.min.js'), 'utf8'));
dom.window.eval(readFileSync(path.join(web, 'vendor', 'purify.min.js'), 'utf8'));
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const render = await import(moduleUrl('js', 'render.js'));
const i18n = await import(moduleUrl('js', 'i18n.js'));
const store = await import(moduleUrl('js', 'store.js'));

test('les chemins cités sont normalisés', () => {
  assert.equal(render.normalizePath('./dossier//fichier.pdf'), 'dossier/fichier.pdf');
  assert.equal(render.normalizePath('bureau\\budget.xlsx'), 'bureau/budget.xlsx');
  assert.equal(render.normalizePath('  /a/b.txt/  '), 'a/b.txt');
});

test('les marqueurs deviennent des références numérotées', () => {
  const sources = [{ path: 'notes.txt' }, { path: 'bail.pdf' }];
  const { text, paths } = render.replaceCitations('Voir [[bail.pdf]] et [[notes.txt]].', sources);
  assert.doesNotMatch(text, /\[\[/);
  assert.equal(paths[2], 'bail.pdf');
  assert.equal(paths[1], 'notes.txt');
});

test('une citation inconnue reçoit un numéro à la suite', () => {
  const { paths } = render.replaceCitations('[[inconnu.pdf]]', [{ path: 'a.txt' }]);
  assert.equal(paths[2], 'inconnu.pdf');
});

test('une citation s’affiche comme une vraie référence, jamais comme du code', () => {
  // Régression observée en usage réel : la balise apparaissait en clair dans la réponse.
  const html = render.renderAnswer('Né le 27 février [[CNI/CNI.pdf]].', [{ path: 'CNI/CNI.pdf' }]);
  assert.doesNotMatch(html, /&lt;span/);
  const holder = document.createElement('div');
  holder.innerHTML = html;
  const citation = holder.querySelector('span.citation');
  assert.ok(citation, 'la référence doit être un élément réel');
  assert.equal(citation.textContent, '[1]');
  assert.equal(citation.getAttribute('data-path'), 'CNI/CNI.pdf');
});

test('une citation reste inoffensive même avec un chemin piégé', () => {
  const html = render.renderAnswer('[[a"><img src=x onerror=alert(1)>.pdf]]');
  const holder = document.createElement('div');
  holder.innerHTML = html;
  assert.equal(holder.querySelectorAll('img').length, 0, 'aucun élément image créé');
  assert.equal(holder.querySelectorAll('[onerror]').length, 0, 'aucun attribut actif');
  // Le chemin piégé reste confiné, en texte, dans l'attribut data-path.
  assert.match(holder.querySelector('span.citation').getAttribute('data-path'), /<img/);
});

test('une citation fonctionne dans une liste et dans du gras', () => {
  const html = render.renderAnswer('- **Montant** : 850 € [[bail.pdf]]', [{ path: 'bail.pdf' }]);
  assert.match(html, /<li>/);
  assert.match(html, /<strong>Montant<\/strong>/);
  const holder = document.createElement('div');
  holder.innerHTML = html;
  const citation = holder.querySelector('li span.citation');
  assert.ok(citation, 'la référence est bien dans l’élément de liste');
  assert.equal(citation.getAttribute('data-path'), 'bail.pdf');
  assert.equal(citation.textContent, '[1]');
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
