# Archipelle

**Interrogez vos documents, sans les indexer.**

![Démonstration d'Archipelle](https://raw.githubusercontent.com/Jean-Jawed/archipelle/main/assets/anim-demo.gif)

Archipelle est une application de bureau qui répond à vos questions à partir de vos propres fichiers. Pas de base vectorielle, pas d'indexation préalable : un agent explore votre dossier à la demande, lit les documents utiles, et répond **en citant ses sources**, vérifiées par le programme et non par le modèle.

Vos documents restent sur votre machine. Seuls les extraits nécessaires sont envoyés au fournisseur d'IA que vous choisissez.

---

## Installation

```bash
pip install archipelle
archipelle
```

Python 3.12 ou plus récent est requis. Avec [uv](https://docs.astral.sh/uv/) :

```bash
uv tool install archipelle
archipelle
```

### Documents scannés (facultatif)

Pour lire les PDF scannés et les photos de documents, installez **Tesseract**, un programme séparé :

| Système | Commande |
|---|---|
| Windows | Installeur [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki), en cochant le pack **français** |
| macOS | `brew install tesseract tesseract-lang` |
| Debian, Ubuntu | `sudo apt install tesseract-ocr tesseract-ocr-fra` |

Sans Tesseract, l'application fonctionne : les documents scannés sont simplement signalés comme non lus.

### Linux

L'interface utilise le moteur d'affichage du système. Sous Linux, installez-le si nécessaire :

```bash
sudo apt install libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1
```

---

## Premiers pas

1. Lancez `archipelle`. L'écran d'accueil demande un **dossier de travail** et un **fournisseur d'IA**.
2. Saisissez la clé API de ce fournisseur dans les Paramètres. Elle est stockée sur votre machine, dans un fichier séparé aux droits restreints, et n'est jamais réaffichée en clair.
3. Posez votre question.

**Fournisseurs pris en charge** : Mistral (traitement dans l'Union européenne), OpenAI, Anthropic, DeepSeek, Kimi, et tout serveur compatible OpenAI, y compris local (Ollama, LM Studio).

> **Offres gratuites :** certains paliers sans abonnement refusent les modèles phares avec une erreur de limite de débit. Chez Mistral, les modèles **Ministral 3** répondent dans ce cas. Vous pouvez aussi saisir l'identifiant d'un modèle de votre choix dans les Paramètres avancés.

---

## Ce que fait Archipelle

- **Explore à la demande.** Il parcourt l'arborescence, cherche dans le contenu, lit les passages utiles. Rien n'est indexé à l'avance, rien n'est copié ailleurs.
- **Cite ses sources.** Chaque affirmation renvoie à un fichier, cliquable. Une source que le programme n'a pas réellement lue est marquée comme **non vérifiée**.
- **Montre son travail.** Une zone de transparence affiche en direct les recherches, les fichiers consultés, et ceux qui ont été ignorés, avec la raison.
- **Dit ce qu'il n'a pas fait.** Recherche plafonnée, document scanné non traité, fichier protégé : tout est annoncé, jamais passé sous silence.
- **S'arrête quand vous le demandez.** Le bouton d'arrêt interrompt immédiatement la recherche en cours.

**Formats lus** : PDF (texte et scannés), Word, Excel, PowerPoint, e-mails, HTML, texte, Markdown, CSV, images, ainsi qu'une trentaine de formats de code et de configuration.

**Deux modes** : *Rapide* pour les recherches courantes, *Exploration* pour aller plus loin, avec OCR des documents scannés et davantage d'étapes.

---

## Vos données

- Les documents ne quittent votre machine que sous forme d'extraits, envoyés au fournisseur que vous avez choisi. Un avertissement signale les fournisseurs situés hors de l'Union européenne.
- Aucun outil d'écriture n'existe : Archipelle ne peut ni modifier, ni supprimer, ni créer de fichier dans votre dossier.
- Conversations, cache d'extraction et journaux restent locaux, et s'effacent depuis les Paramètres.
- Les clés API ne sont jamais écrites dans les journaux.

---

## Développement

```bash
git clone https://github.com/Jean-Jawed/archipelle
cd archipelle
uv sync                 # environnement et dépendances
uv run archipelle       # lancer l'application
uv run pytest           # tests Python
npm install && npm test # tests de l'interface web
uv run ruff check       # lint
uv run pyright          # types
```

L'architecture est décrite dans [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Pour isoler configuration, données, cache et journaux pendant le développement :

```bash
ARCHIPELLE_HOME=/tmp/archipelle-essai uv run archipelle
```

Un **mode démo** rejoue un scénario écrit à l'avance, sans clé ni appel réseau ; les outils s'exécutent réellement sur le dossier choisi :

```bash
ARCHIPELLE_FAKE_SCRIPT=src/archipelle/resources/demo/demo_script.json uv run archipelle
```

---

## Limites connues

- Les modèles très légers (moins de 8 milliards de paramètres) enchaînent mal les appels d'outils.
- Sur un dossier très volumineux, la recherche est plafonnée : la couverture annoncée est alors partielle, par choix.
- L'OCR demande Tesseract, installé séparément.
- La qualité des réponses dépend du modèle choisi.

---

## Licence

[MIT](LICENSE) — utilisation, modification et redistribution libres, y compris commerciales, à condition de conserver la mention de copyright.
