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
| `providers/<domain>/refused/<version>.json` | A release that could not become a version, and why. Generated, so the release is not tried again. |
| `server_lines.json` | The server lines an index is built for, each with its server image. |
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

### refused/\<version\>.json

```json
{
  "version": "1.3.0",
  "tag": "v1.3.0",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "released": "2026-11-20T12:00:00Z",
  "refused": "2026-11-21T03:17:00Z",
  "reason": "providers/sample_music/versions/1.3.0.json: manifest.domain: 'sample' is not sample_music"
}
```

A version has a version file or a refused file, never both. Deleting a refused file makes the
pipeline try the release again.

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
- Run the checks of the catalog on your own pull requests with the reusable workflow
  `.github/workflows/check-provider.yml` (its header shows how to call it).
- A release that is refused or fails a check gets an issue on your repository, or on this
  repository when yours has issues switched off.

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
| `uv run python -m registry set-status <domain> listed\|withdrawn\|blocked [--reason <text>] [--reason-url <url>]` | Withdraws, blocks or lists a provider again, and writes the index files. |
| `uv run python -m registry block-version <domain> <version> --reason <text>` | Blocks a version, and writes the index files; `unblock-version <domain> <version>` lifts it. |

Blocking is one command and one commit:

```sh
uv run python -m registry set-status sample_music blocked --reason "Sends the library elsewhere" \
  --reason-url https://github.com/music-assistant/community-catalog/issues/12
git commit -am "Block sample_music"
```

The commands of the release pipeline, which the workflows run:

| Command | What it does |
|---|---|
| `fetch` | Reads the releases of every provider that is not blocked and writes the version file of the newest new one, or its refused file. Downloads the archives the check step needs into the work folder and writes `fetch.json` there. Needs `GITHUB_TOKEN`. |
| `check --line <line> --output <file>` | Runs inside the server image of the line. Checks every version that needs a result and writes the results file; it changes nothing else. |
| `check-folder <folder>` | Runs inside a server image. Checks one provider folder, for a provider's own CI. |
| `apply-results <folder>` | Records the results files in the version files and writes the index files. |
| `report <folder>` | Writes an issue for every failed check and every refused release into the work folder. |
| `post-issues` | Opens those issues, each one unless an issue with its key exists. Needs `GITHUB_TOKEN`. |
| `prepare-admission <domain> --pull-request <checkout> --author <handle>` | The admission checks that need GitHub, and the latest release as a version file. Needs `GITHUB_TOKEN`. |
| `check-admission <domain> <folder>` | Prints the result of the admission checks with the check results of the latest release. |

The work folder is `work/` in the root unless `--work` names another. It is never committed.

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
{"lines": {"beta": {"frozen": false, "image": "ghcr.io/music-assistant/server:beta"}, "nightly": {"frozen": false, "image": "ghcr.io/music-assistant/server:nightly"}}}
```

`image` is the official server image the checks of the line run in. The server's release
workflow tags every image with its channel (`nightly`, `beta`) and every stable release also with
its minor line, so the image of a stable line is `ghcr.io/music-assistant/server:2.11`.

A stable line is added as `"2.11": {"frozen": false, "image": "ghcr.io/music-assistant/server:2.11"}`,
followed by `build`. A line stays in the
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

## The release pipeline

The workflow `releases.yml` runs every night and on request. Its jobs share only files:

1. **Fetch** reads the releases of every provider that is not blocked, many repositories per
   GraphQL request. It takes the most recently published release whose tag names a version of
   the provider (drafts and pre-releases are ignored), when that version has neither a version
   file nor a refused file. So of several releases since the last night only the newest is taken.
   It downloads the archive of the tagged commit and runs `add-version` on it: what that refuses
   becomes a refused file with the reason, and the release is not tried again. An archive that
   cannot be downloaded is tried again the next night. It then downloads the archives of every
   version the check step needs.
2. **Check** runs once per line that is not frozen, inside the official server image of that
   line. It checks every version without a result for the line, and the current `latest` of each
   provider on the line again, which is how a provider that stops working on a line is found:
   * the content: the provider folder is unpacked from the archive and its content hash compared
     with the version file;
   * the dependencies: the requirements are resolved against the packages of that server by the
     server's own code (`CommunityDependencies` of the community catalog controller), so the
     verdict is what a server of that line decides. A clash with the server, or a requirement
     that cannot be installed, fails the version with the explanation of the resolver;
   * the load: the requirements are installed into a scratch folder, and in a process of its
     own the manifest is parsed and the module imported the way the server does for a community
     provider. A manifest the server refuses or an import that fails fails the version.

   A check that cannot run (the package index cannot be reached) gives no result; the version is
   checked again the next night. The provider's own tests are not run.
3. **Apply** records the results with the rules of `record-check`, writes the index files,
   validates and pushes, as the Music Assistant Bot app. A version that stops passing on a line
   leaves that line's index: the newest version that still passes there becomes `latest`. The
   push starts the Publish workflow.
4. **Report** writes an issue for every version that failed a check on a line (first checks and
   versions that stopped passing) and every release refused in this run. The issue goes to the
   provider's repository, or to this repository, mentioning the maintainers, when that repository
   has issues switched off. Each issue has a key, written into its body, that is the same for the
   same failure; an issue is not opened when one with its key exists. Opening them is switched on
   with the repository variable `POST_ISSUES`; until then they are kept as an artifact of the run.

### Provider code runs without secrets

The check executes provider code, so it runs in a job of its own:

- the job has no permissions, no checkout and no token: it gets the registry and the archives
  from the artifact of the fetch job;
- the provider code runs only inside `docker run` of the server image, which gets none of the
  job's environment, the files of the registry read-only, and one empty folder for its results
  file;
- the job only uploads that results file. The apply job, which can push, reads it as data with
  the same checks as any other change to a version file, runs only its own checked out code, and
  takes nothing else from the check jobs.

The admission and the provider's own CI run the same check in the same way.

## Admission

A provider is admitted by a pull request that adds its `provider.json`. The workflow
`admission.yml` runs the code of the base branch, takes only the `provider.json` of the pull
request, and checks:

- that `provider.json` is valid and its domain is free: not reserved, and not taken (a pull
  request that changes an existing provider says so and gets the same checks);
- that the repository is public, not archived, and has an open source licence GitHub recognises;
- that the author of the pull request owns the repository or is a code owner in the manifest of
  its latest release;
- that the latest release becomes a version and passes the checks on at least one line.

It prints the result in the job summary, for the person who decides and for Bot-MArley. A pull
request that only changes the status or the blocked versions needs no admission.

## Still open

- Running the provider's own tests in the check step.
- The reading of every release against the red lines by Bot-MArley, and its summary on an
  admission pull request.
- Merging an admitted pull request by the Music Assistant Bot app.
- Checking that a requirement has a wheel for arm64 as well: the checks run on amd64.
