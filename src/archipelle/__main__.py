"""Point d'entrée : ``uv run archipelle`` ou ``python -m archipelle``.

La garde ``if __name__ == "__main__"`` est indispensable : les sous-processus
d'extraction sont lancés en mode « spawn » et réimportent le module principal.
"""

from __future__ import annotations

from archipelle.app import main

if __name__ == "__main__":
    raise SystemExit(main())
