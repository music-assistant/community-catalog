"""
Import a community provider the way the server does, in a process of its own.

Run inside a server image as `python -m registry.load <location> <domain> <cache folder>`:
the provider folder `<location>/<domain>` is imported as a community provider, with the
requirements the check step installed for it. It exits with 0 when the provider loads; else the
last line it prints says why.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import music_assistant.providers as providers_package
from music_assistant.controllers.community_catalog.dependencies import CommunityDependencies
from music_assistant.helpers.util import load_provider_module, set_provider_tier
from music_assistant_models.enums import ProviderTier
from music_assistant_models.provider import ProviderManifest


def main(argv: list[str]) -> int:
    """
    Import a provider and return the exit code.

    :param argv: The folder that holds the provider folder, the domain and the cache folder.
    """
    location, domain, cache = argv
    return asyncio.run(_load(location, domain, cache))


async def _load(location: str, domain: str, cache: str) -> int:
    """
    Import a provider, printing why when it does not load, and return the exit code.

    :param location: The folder that holds the provider folder.
    :param domain: The domain of the provider, also the name of its folder.
    :param cache: The cache folder the requirements were installed in.
    """
    # what MusicAssistant.add_provider_location and the start of the catalog do
    providers_package.__path__.append(location)
    CommunityDependencies(SimpleNamespace(cache_path=cache)).activate()
    try:
        manifest = await ProviderManifest.parse(str(Path(location, domain, "manifest.json")))
    except Exception as err:  # noqa: BLE001 - every error of the parser refuses the manifest
        print(f"manifest: {err}")
        return 1
    set_provider_tier(manifest, ProviderTier.COMMUNITY)
    if manifest.domain != domain:
        print(f"manifest: the domain {manifest.domain} is not {domain}")
        return 1
    try:
        await load_provider_module(manifest)
    except BaseException as err:  # noqa: BLE001 - also a module that exits the interpreter
        traceback.print_exc(file=sys.stdout)
        print(f"load: {type(err).__name__}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
