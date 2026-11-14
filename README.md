# Music Assistant community catalog

This repository is the registry of the Music Assistant community catalog. It lists the community
providers a Music Assistant server can install, and publishes the index every server reads. It
holds no provider code: each community provider lives in its maintainer's own repository, with
its own documentation, support and releases.

Being listed means a provider passed the checks and a person admitted what it does. It does not
mean the Music Assistant project reviewed, tested or supports it. Everything in the community
catalog is used at the user's own risk.

## How a server uses it

- A server reads one file, once a day: the index of its own server line, at
  `https://music-assistant.github.io/community-catalog/v1/<line>.json`. The line is `nightly`,
  `beta`, or the stable minor line such as `2.11`. A server with the community catalog switched
  off and no community provider installed makes no request at all.
- It installs only a version its index lists: it downloads the GitHub archive of the pinned
  commit, takes the provider folder from it and compares its content hash with the index.
- It stops loading a provider, or a version, the index blocks.

## Layout

| Path | What it is |
|---|---|
| `providers/<domain>/provider.json` | A provider: where it lives, who maintains it, its status. Written once, at admission. |
| `providers/<domain>/versions/<version>.json` | A released version: the pinned commit, its content hash, its manifest and the result of the checks per server line. Generated. |
| `server_lines.json` | The server lines an index is built for. |
| `reserved_domains.json` | The domains a community provider cannot take. |
| `site/v1/<line>.json` | The index of each server line, generated from the files above. `site/` is what is published, so it holds nothing else. |
| `registry/` | The code that checks the files and builds the index. |

No file or folder the commands read or write may be a symbolic link.

### provider.json

```json
{
  "domain": "sample_music",
  "repository": "someone/ma-sample-music",
  "folder": "sample_music",
  "tag_prefix": "",
  "maintainers": ["someone"],
  "status": "listed",
  "reason": null,
  "reason_url": null,
  "blocked_versions": {},
  "added": "2026-11-14T12:00:00Z",
  "admission": {"decided_by": "a-maintainer", "decided": "2026-11-14T12:00:00Z", "pull_request": 12}
}
```

| Key | Rule |
|---|---|
| `domain` | Lower case letters, digits and underscores, starting with a letter. Equals the name of the folder in `providers/`, and is not a domain the server ships or reserves. |
| `repository` | The GitHub repository as `owner/name`. |
| `folder` | The provider folder in that repository, such as `sample_music` or `providers/sample_music`. Its last part is the domain. |
| `tag_prefix` | Optional. What the release tags of this provider start with, for a repository that holds several providers. |
| `maintainers` | GitHub handles, without `@`. At least one. |
| `status` | `listed`, `withdrawn` (offered no longer; what is installed keeps loading) or `blocked` (loaded by no server). |
| `reason`, `reason_url` | Why the provider is withdrawn or blocked, and an `https` link to more. Required for those two, absent for `listed`. |
| `blocked_versions` | Version to reason. A blocked version is loaded by no server. |
| `added` | When the provider entered the catalog. |
| `admission` | Who admitted the provider, when, and the pull request on this repository that added it. |

### versions/\<version\>.json

```json
{
  "version": "1.2.0",
  "tag": "v1.2.0",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "folder": "sample_music",
  "content_hash": "8c33d2fc23165aa1d2614cd70625ae7d1592a73207a6ecacee4fa9f65b51737e",
  "manifest": {"type": "music", "domain": "sample_music", "name": "Sample music", "description": "...", "codeowners": ["@someone"], "requirements": ["some-package==1.0"]},
  "requirements": ["some-package==1.0"],
  "icon_svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\">...</svg>",
  "released": "2026-11-14T12:00:00Z",
  "checks": {
    "nightly": {"passed": true, "checked": "2026-11-15T03:00:00Z", "reason": null},
    "beta": {"passed": false, "checked": "2026-11-15T03:00:00Z", "reason": "dependencies: some-package==1.0 clashes with the server"}
  }
}
```

| Key | Rule |
|---|---|
| `version` | `MAJOR.MINOR.PATCH`, numbers without leading zeros. Equals the file name. |
| `tag` | The release tag: the tag prefix and the version, with or without `v`. |
| `commit` | The full SHA of the tagged commit, in lower case. |
| `folder` | The provider folder in the repository at this commit. |
| `content_hash` | The content hash of that folder in the GitHub archive of the commit (see below). |
| `manifest` | The provider's `manifest.json` as it is in the release. Its `domain` is the provider's, its `type` is `music`, `player`, `metadata`, `plugin` or `audio_analysis`. |
| `requirements` | The requirements of the manifest: package names with version specifiers only, no URLs, markers or options, each one line of printable characters. |
| `icon_svg` | The provider's `icon.svg`, when it has one that passes the icon rules below. |
| `released` | When the version was released. |
| `checks` | The result per server line in `server_lines.json`: passed or not, when, and why not. |

All dates are UTC, as `2026-11-14T12:00:00Z`. A file with an unknown key, a missing key or a
value of another type is refused, as is a JSON object with a key that appears twice.

Every file, and the manifest of a provider, is plain JSON that the server's parser (orjson)
reads: `NaN`, `Infinity`, a number beyond the range of a double and a lone surrogate are
refused. A server that cannot read its index keeps its old copy, and with it misses every new
block.

### The index

Each `site/v1/<line>.json` holds, for every provider with at least one version file:

- its status, reason and reason link, repository, maintainers, the date it entered the catalog,
  and its blocked versions;
- `latest`: the highest version that passed the checks for this line and is not blocked.
  Versions are compared as versions (`1.10.0` is above `1.9.0`), not as text and not by date;
- `versions`: the five highest of those, each with its commit, folder, content hash,
  requirements and release date;
- the manifest and icon of `latest`; when no version passed for this line, those of the highest
  version that is not blocked, or of the highest version when all are blocked.

Blocked and withdrawn providers stay in the index with their versions, so a server recognises
what it has installed. The output is deterministic: an unchanged registry gives byte-identical
files, and `generated` changes only when the content of that index changes.

### The content hash

For every regular file below the provider folder, except files inside a `__pycache__` folder
and `*.pyc` files, take the line `<sha256 of the file><two spaces><path relative to the
folder>\n`. Sort the lines by path as UTF-8 bytes; the hash is the sha256 of all lines together.
Modes, times and empty folders do not count. In the provider folder this equals:

```sh
find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's|^\./||' | LC_ALL=C sort \
  | xargs sha256sum | sha256sum
```

The hash a version file records is computed from the GitHub archive of the commit, which is what
a server downloads; a checkout can differ from it (for example through `export-ignore` in
`.gitattributes`). The provider folder in that archive holds only regular files and folders: a
link or a special file refuses the version.

### The icon

The `icon.svg` of a provider is handed to every server in the index, so it is at most 5 KB (the
budget the server sets for its own provider icons) and only draws: no scripts, no event
handlers, no elements that link, animate or embed other content, no reference to anything
outside the file (an `image` may embed a PNG, JPEG, GIF or WebP as a data URI), no document type
or entity declarations and no editor data such as Inkscape attributes. Rather than parse CSS,
the check also refuses a backslash in a style sheet or an attribute value, an element inside a
`style` element, `image-set()`, `src()` and `xml:base`. An icon that does not
pass is left out of the version, with a warning; a tool such as `svgo` removes most of what is
refused.

## For provider maintainers

- Keep the provider in a folder named after its domain.
- Release by publishing a GitHub release whose tag is the version, such as `v1.2.0`, or the tag
  prefix and the version when the repository holds several providers.
- Write requirements as package names with version specifiers.
- Keep `manifest.json` plain JSON: no `NaN` or `Infinity`, and no key twice.
- A provider never writes into its own folder: that changes its content hash, and the server
  replaces a folder whose hash differs from the index.

## Commands

Requires [uv](https://docs.astral.sh/uv/). `uv sync` sets up the environment;
`uv run pre-commit install` adds the checks to every commit. Every command takes
`--root <folder>` (default: the current folder).

| Command | What it does |
|---|---|
| `uv run python -m registry validate` | Checks every file, and that the index files are up to date. CI runs it on every change. |
| `uv run python -m registry build` | Writes the index file of every server line. |
| `uv run python -m registry add-version <domain> <version> <source> --tag <tag> --commit <sha> [--released <date>]` | Writes the version file of a release. The source is the downloaded GitHub archive of the commit, or a local provider folder. |
| `uv run python -m registry record-check <domain> <version> <line> (--passed \| --failed <reason>) [--checked <date>]` | Records the result of the checks of a version on a server line. |
| `uv run python -m registry content-hash <source> [--folder <folder>]` | Prints the content hash of a provider folder, or of a folder in an archive. |
| `uv run python -m registry refresh-reserved <server checkout>...` | Adds the built-in providers and core controllers of server checkouts to `reserved_domains.json`. |

A version file starts without check results, so it is in no index until a line records a pass.
The whole flow for one release:

```sh
curl -L -o release.tar.gz https://codeload.github.com/someone/ma-sample-music/tar.gz/<sha>
uv run python -m registry add-version sample_music 1.2.0 release.tar.gz --tag v1.2.0 --commit <sha>
uv run python -m registry record-check sample_music 1.2.0 nightly --passed
uv run python -m registry build
uv run python -m registry validate
```

## Server lines

`server_lines.json` lists the lines an index is built for:

```json
{"lines": {"beta": {"frozen": false}, "nightly": {"frozen": false}}}
```

A stable line is added as `"2.11": {"frozen": false}`, followed by `build`. A line stays in the
file for as long as servers of that line exist. When the next stable line appears, the previous
one is marked `frozen`: its versions are no longer tested, while blocks and withdrawals still
reach its index.

## Reserved domains

`reserved_domains.json` holds the domains a community provider cannot take:

- `builtin_providers`: the providers the server ships, every package in its `providers` folder
  with or without a manifest, as the server's own package wins over a community provider of
  the same name;
- `core_controllers`: the core controllers of the server, which share the namespace of provider
  domains;
- `reserved_names`: names the server gives a meaning of its own, such as `library`;
- `moving`: built-in providers that move to the community catalog under their own domain, and
  may be admitted while the server still ships them.

`refresh-reserved` only adds domains. Run it against the `dev` branch and every supported stable
branch of the server; a domain leaves the list by hand, once no supported server line ships it.

## Not part of this repository today

Nothing here fetches releases or runs the checks: a version file is added with `add-version`
and its results with `record-check`. The checks on every release (the manifest, the
dependencies against each server line, the load test in the server images), the issue for a
maintainer whose release fails, and the admission flow are not in this repository. A
`provider.json` is added by a pull request that a maintainer of the project reviews. Nothing
reads the `frozen` flag of a server line yet: the pipeline that runs the checks will skip a
frozen line.
