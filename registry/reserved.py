"""The domains the server ships or reserves, read from a checkout of the server."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import ReservedDomains


def add_server_domains(reserved: ReservedDomains, server: Path) -> ReservedDomains:
    """
    Return the reserved domains with the built-in providers and core controllers of a server added.

    Domains are only added, so running it against every supported channel reserves what
    any of them ships.

    :param reserved: The current reserved domains.
    :param server: The root of a checkout of the server repository.
    :raises ValueError: The folder is not a checkout of the server.
    :raises OSError: A folder cannot be read.
    """
    providers = server / "music_assistant" / "providers"
    controllers = server / "music_assistant" / "controllers"
    if not providers.is_dir() or not controllers.is_dir():
        msg = f"{server} is not a checkout of the Music Assistant server"
        raise ValueError(msg)
    # every package in the providers folder, also one without a manifest: the server's own
    # package of a name wins over an installed community provider of that name
    return replace(
        reserved,
        builtin_providers=sorted(_packages(providers) | set(reserved.builtin_providers)),
        core_controllers=sorted(_packages(controllers) | set(reserved.core_controllers)),
    )


def _packages(folder: Path) -> set[str]:
    """
    Return the names of the folders in a folder of the server, which name a domain each.

    A folder starting with _, such as __pycache__ or a demo provider that only loads in
    development mode, is left out.

    :param folder: The providers or controllers folder of the server.
    """
    return {
        path.name for path in folder.iterdir() if path.is_dir() and not path.name.startswith("_")
    }
