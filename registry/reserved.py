"""The domains the server ships or reserves, read from a checkout of the server."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import ReservedDomains


def add_server_domains(reserved: ReservedDomains, server: Path) -> ReservedDomains:
    """
    Return the reserved domains with the built-in providers and core controllers of a server added.

    Domains are only added, so running it against every supported server line reserves what
    any of them ships.

    :param reserved: The current reserved domains.
    :param server: The root of a checkout of the server repository.
    :raises ValueError: The folder is not a checkout of the server.
    :raises OSError: A manifest cannot be read.
    """
    providers = server / "music_assistant" / "providers"
    controllers = server / "music_assistant" / "controllers"
    if not providers.is_dir() or not controllers.is_dir():
        msg = f"{server} is not a checkout of the Music Assistant server"
        raise ValueError(msg)
    builtin = {
        json.loads(manifest.read_text(encoding="utf-8"))["domain"]
        for manifest in providers.glob("*/manifest.json")
        # a folder starting with _ only loads in development mode
        if not manifest.parent.name.startswith("_")
    }
    # each core controller lives in a folder named after its domain
    core = {
        path.name
        for path in controllers.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    }
    return replace(
        reserved,
        builtin_providers=sorted(builtin | set(reserved.builtin_providers)),
        core_controllers=sorted(core | set(reserved.core_controllers)),
    )
