"""Récupère les dépendances web embarquées (markdown-it, DOMPurify).

Versions figées et empreintes vérifiées : aucun CDN à l'exécution, et une modification du
fichier téléchargé est détectée. Usage : ``uv run python scripts/fetch_vendor.py``.
"""

from __future__ import annotations

import hashlib
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "src" / "archipelle" / "ui" / "web" / "vendor"


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    inner: str  # chemin du fichier dans l'archive npm
    target: str
    sha256: str  # empreinte du fichier extrait ("" : à renseigner au premier appel)
    license_file: str = "LICENSE"

    @property
    def url(self) -> str:
        short = self.name.split("/")[-1]
        return f"https://registry.npmjs.org/{self.name}/-/{short}-{self.version}.tgz"


PACKAGES = (
    Package(
        name="markdown-it",
        version="14.1.0",
        inner="package/dist/markdown-it.min.js",
        target="markdown-it.min.js",
        sha256="38c70a1e7ca91ab40e2d9e6e60129851a717ed1c7d4acbbdd41bf9503791cf68",
    ),
    Package(
        name="dompurify",
        version="3.2.4",
        inner="package/dist/purify.min.js",
        target="purify.min.js",
        sha256="8eb41b658831fab175fad9bcd00fcb2d84e0ed3a25a55053d4ecd4444b8b43a0",
    ),
)


def fetch(package: Package, *, update: bool = False) -> bool:
    VENDOR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as workspace:
        archive = Path(workspace) / "package.tgz"
        with urllib.request.urlopen(package.url, timeout=60) as response:
            archive.write_bytes(response.read())
        with tarfile.open(archive) as tar:
            member = tar.extractfile(package.inner)
            if member is None:
                print(f"{package.name} : {package.inner} absent de l'archive")
                return False
            content = member.read()
            license_member = tar.extractfile(f"package/{package.license_file}")
            license_text = license_member.read() if license_member else b""

    digest = hashlib.sha256(content).hexdigest()
    if package.sha256 and digest != package.sha256 and not update:
        print(
            f"{package.name} : empreinte inattendue"
            f"\n  attendue {package.sha256}\n  obtenue  {digest}"
            f"\n  attendue {package.sha256}\n  obtenue  {digest}"
        )
        return False
    (VENDOR / package.target).write_bytes(content)
    if license_text:
        (VENDOR / f"{package.target}.LICENSE.txt").write_bytes(license_text)
    print(f"{package.name} {package.version} → {package.target} ({digest})")
    return True


def main(argv: list[str]) -> int:
    update = "--update-hashes" in argv
    ok = all(fetch(package, update=update) for package in PACKAGES)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
