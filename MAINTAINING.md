# Maintaining the registry

How the registry of the community catalog works inside: its files, its commands and its
workflows, and what a registry maintainer does with a submission and a report. What a user or a
provider maintainer needs is in the [README](README.md).

## How a server uses it

- A server reads one file, once a day: the index of its own channel, at
  `https://catalog.music-assistant.io/v1/<channel>.json`. The channel is `nightly`, `beta`, or
  the stable minor version such as `2.11`, which the issues of the catalog call stable 2.11. A
  server that has not enabled the community catalog and has no community provider installed
  makes no request at all.
- It installs only a version its index lists: it downloads the GitHub archive of the pinned
  commit, takes the provider folder from it and compares its content hash with the index.
- It stops loading a provider, or a version, the index blocks.

## What a provider needs

The exact rules a provider and its releases meet; the checks name the one that fails.

- The repository is public, not archived, and has an open source licence GitHub recognises.
- The provider is in a folder named after its domain, or named `provider`. The domain folder is
  what lets a server in development mode, the test fixture and the Dev app import it.
- The domain is free: not in `reserved_domains.json` (see [Reserved domains](#reserved-domains))
  and not a folder in `providers/`.
- The `manifest.json` is plain JSON the server reads (no `NaN` or `Infinity`, no key twice), at
  most 16 KiB, with at most 10 `codeowners` and 50 `requirements`.
- Each requirement is a package name with version specifiers (no URLs, markers or options) of at
  most 100 characters, available as wheels for linux amd64 and arm64.
- An `icon.svg` is optional: at most 5 KB, and it only draws ([The icon](#the-icon)); an icon
  that does not pass is left out.
- Once installed, a provider never writes into its own folder: the server replaces a folder that
  differs from the release.
- A release is a published GitHub release, not a draft or pre-release, whose tag is the version,
  `MAJOR.MINOR.PATCH` with or without `v`, after the tag prefix of the provider when it has one.

## Layout

| Path | What it is |
|---|---|
| `providers/<domain>/provider.json` | A provider: where it lives and its status. Written by the bot at admission. |
| `providers/<domain>/versions/<version>.json` | A released version: the pinned commit, its content hash, its manifest and the result of the checks per channel. Generated. |
| `providers/<domain>/refused/<version>.json` | A release that could not become a version, and why. Generated, so the release is not tried again. |
| `channels.json` | The channels an index is built for, each with its server image. |
| `reserved_domains.json` | The domains a community provider cannot take. |
| `site/v1/<channel>.json` | The index of each channel, generated from the files above. `site/` is what is published, so it holds nothing else. |
| `registry/` | The code that checks the files and builds the index. |

No file or folder the commands read or write may be a symbolic link.

### provider.json

```json
{
  "added": "2026-11-14T12:00:00Z",
  "domain": "sample_music",
  "folder": "sample_music",
  "owner_id": 987654,
  "repository": "someone/ma-sample-music",
  "repository_id": 123456789
}
```

The bot writes it from the submission (see [Admission](#admission)): the ids from GitHub, the
domain from the manifest of the latest release. A key that has its default value is left out.
Who decided on the admission, and when, is in the history of its pull request. The maintainers
of a provider are the owner of its repository and the code owners in the manifest of its highest
version that is offered (not blocked, and passed on a channel); without such a version, the
owner alone.

| Key | Rule |
|---|---|
| `domain` | Lower case letters, digits and underscores, starting with a letter. Equals the name of the folder in `providers/`, and is not a domain the server ships or reserves. |
| `repository` | The GitHub repository as `owner/name`. |
| `repository_id`, `owner_id` | The ids GitHub gives the repository and its owner (`databaseId` in the GraphQL API). A rename or a transfer keeps them; another repository under the same name has others, and nothing of it is fetched until a registry maintainer updates them. |
| `folder` | The provider folder in that repository, such as `sample_music`, `providers/sample_music` or `provider`. Its last part is the domain or `provider`: one predictable layout for a repository with a single provider (`provider`), and one for a repository with several (each in a folder named after its domain). A server installs it as a folder named after the domain. |
| `added` | When the provider entered the catalog, which servers show as new in the community catalog. |
| `tag_prefix` | Optional, default empty. What the release tags of this provider start with, for a repository that holds several providers. |
| `status` | Optional, default `listed`. `listed`, `withdrawn` (not offered; what is installed keeps loading) or `blocked` (loaded by no server). |
| `reason`, `reason_url` | Why the provider is withdrawn or blocked, and an `https` link to more. Required for those two, absent for `listed`. |
| `blocked_versions` | Optional. Version to reason. A blocked version is loaded by no server. |

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
| `manifest` | The provider's `manifest.json` as it is in the release. Its `domain` is the provider's, its `type` is `music`, `player`, `metadata`, `plugin` or `audio_analysis`. At most 16 KiB as compact JSON, with at most 10 `codeowners`. |
| `requirements` | The requirements of the manifest: at most 50 package names with version specifiers only, no URLs, markers or options, each one line of at most 100 printable characters. |
| `icon_svg` | The provider's `icon.svg`, when it has one that passes the icon rules below. |
| `released` | When the version was released. |
| `checks` | The result per channel in `channels.json`: passed or not, when, and why not, in one line of at most 300 printable characters. |

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

The reason holds at most 2000 characters. A version has a version file or a refused file, never
both. Deleting a refused file makes the pipeline try the release again.

All dates are UTC, as `2026-11-14T12:00:00Z`. A file with an unknown key, a missing required key
or a value of another type is refused, as is a JSON object with a key that appears twice.

Every file, and the manifest of a provider, is plain JSON that the server's parser (orjson)
reads: `NaN`, `Infinity`, a number beyond the range of a double and a lone surrogate are
refused. A server that cannot read its index keeps its old copy, and with it misses every new
block.

### The index

Each `site/v1/<channel>.json` holds, for every provider with at least one version file:

- its status, reason and reason link, repository, maintainers (see
  [provider.json](#providerjson), as handles without `@`), the date it entered the catalog, and
  its blocked versions;
- `latest`: the highest version that passed the checks for this channel and is not blocked.
  Versions are compared as versions (`1.10.0` is above `1.9.0`), not as text and not by date;
- `versions`: the five highest of those, each with its commit, folder, content hash,
  requirements and release date;
- the manifest and icon of `latest`; when no version passed for this channel, those of the
  highest version that is not blocked, or of the highest version when all are blocked.

It names its channel twice, as `channel` and as `server_line`: some servers read only the second.

Blocked and withdrawn providers stay in the index with their versions, so a server recognises
what it has installed. The output is deterministic: an unchanged registry gives byte-identical
files, and `generated` changes only when the content of that index changes.

An index file holds at most 4 MiB: a server gives up on an index it cannot download within 10
seconds and keeps its old copy, with which it misses every new block. `build` and `validate`
refuse a larger one. The limits on a manifest and its requirements keep each provider small.

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

## Commands

Requires [uv](https://docs.astral.sh/uv/). `uv sync` sets up the environment;
`uv run pre-commit install` adds the checks to every commit. Every command takes
`--root <folder>` (default: the current folder).

| Command | What it does |
|---|---|
| `uv run python -m registry validate` | Checks every file, and that the index files are up to date. CI runs it on every change. |
| `uv run python -m registry build` | Writes the index file of every channel. |
| `uv run python -m registry add-version <domain> <version> <source> --tag <tag> --commit <sha> [--released <date>]` | Writes the version file of a release. The source is the downloaded GitHub archive of the commit, or a local provider folder. |
| `uv run python -m registry record-check <domain> <version> <channel> (--passed \| --failed <reason>) [--checked <date>]` | Records the result of the checks of a version on a channel. |
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
| `fetch` | Reads the releases of every provider that is not blocked and writes the version file of the newest new one, or its refused file. Downloads the archives the check step needs into the work folder and writes `plan.json` (what to check on each channel) and `fetch.json` there. Needs `GITHUB_TOKEN`. |
| `check --channel <channel> --domain <domain> --version <version> --stage install\|load --scratch <folder> --output <file>` | Runs inside the server image of the channel. One stage of the checks of one version (see below); writes the results file when the stage has a verdict, and changes nothing else. |
| `check-folder <folder>` | Runs inside a server image. Checks one provider folder, for a provider's own CI. |
| `apply-results <folder>` | Records the results the plan names in the version files and writes the index files. A check that could not run in the server image removes a failed result of its version on that channel. |
| `report <folder>` | Writes the report of every provider into the work folder: its failures in this run that are not settled (failed checks, refused releases, a repository that is not the admitted one), and what settles them. Also the report of every channel with results: whether its checks could run. |
| `post-issues --repository <owner/name>` | Opens, adds to or closes the issue of each provider and of each channel whose checks cannot run on the registry after those reports (see [Release issues](#release-issues)). Needs `GITHUB_TOKEN`. |
| `prepare-admission --repository <owner/name> --issue <number>` | The admission checks of a submission that need GitHub: reads the issue through the API, finds the provider in the latest release, writes the `provider.json` it leads to and the version file of that release into the checkout, and the report and the plan into the work folder. An issue that is no valid submission gets a report with the refusal. Needs `GITHUB_TOKEN`. |
| `check-admission <folder> [--comment <file>] [--run-url <url>] [--proposal <result>] [--outcome <outcome>] [--pull-request <url>] [--others <urls>]` | Prints the result of the admission checks with the check results of the latest release, and writes it as the comment for the issue. What happens next follows from the result of the propose job and its outcome. Exits with 1 when the checks did not pass. |
| `propose-admission <folder> --repository <owner/name> --issue <number> --author <account> --run-url <url> [--output <file>]` | Opens the pull request of a submission whose checks passed, leaves standing the open one that holds the same, or closes it (see [Admission](#admission)). Adds its outcome (`opened`, `stands`, `not-passed`, `closed`, `changed` or `conflict`), the address of its pull request and those of other submissions of the domain to the outputs file of the job. Needs `GITHUB_TOKEN` of the app. |
| `post-admission-comment <file> --repository <owner/name> --issue <number>` | Writes the comment on the issue, or updates the one an earlier run wrote. Needs `GITHUB_TOKEN`. |
| `check-changes <file>` | Fails when a pull request adds or changes a version file or a refusal; the file lists the changed paths, separated by NUL characters. |

The work folder is `work/` in the root unless `--work` names another. It is never committed.

A version file starts without check results, so it is in no index until a channel records a pass.
The whole flow for one release:

```sh
curl -L -o release.tar.gz https://codeload.github.com/someone/ma-sample-music/tar.gz/<sha>
uv run python -m registry add-version sample_music 1.2.0 release.tar.gz --tag v1.2.0 --commit <sha>
uv run python -m registry record-check sample_music 1.2.0 nightly --passed
uv run python -m registry build
uv run python -m registry validate
```

## Channels

`channels.json` lists the channels an index is built for:

```json
{"channels": {"beta": {"frozen": false, "image": "ghcr.io/music-assistant/server:beta"}, "nightly": {"frozen": false, "image": "ghcr.io/music-assistant/server:nightly"}}}
```

`image` is the official server image the checks of the channel run in. The server's release
workflow tags every image with its channel (`nightly`, `beta`) and every stable release also with
its minor version, so the image of stable 2.11 is `ghcr.io/music-assistant/server:2.11`.

Every stable minor version is a channel of its own, added as
`"2.11": {"frozen": false, "image": "ghcr.io/music-assistant/server:2.11"}`, followed by `build`.
A channel that needs its index before its first release, because a provider moves out of the
server in it, is added during its beta with the beta image,
`"2.11": {"frozen": false, "image": "ghcr.io/music-assistant/server:beta"}`, and its image is
set to `ghcr.io/music-assistant/server:2.11` once that release is out. A channel stays in the
file for as long as servers on that channel exist. When the next stable version appears, the
previous channel is marked `frozen`: its versions are no longer tested, while blocks and
withdrawals still reach its index.

## Reserved domains

`reserved_domains.json` holds the domains a community provider cannot take:

- `builtin_providers`: the providers the server ships, every package in its `providers` folder
  with or without a manifest, as the server's own package wins over a community provider of
  the same name;
- `core_controllers`: the core controllers of the server, which share the namespace of provider
  domains;
- `reserved_names`: names the server gives a meaning of its own, such as `library`;
- `moving`: built-in providers that move to the community catalog under their own domain, and
  may be admitted while the server still ships them. Until it no longer does, the load check
  imports the release's code instead of the server's own copy, but the requirements still
  resolve against the server's packages, which include the pins of that copy: a release keeps
  those pins until the server no longer ships the provider.

`refresh-reserved` only adds domains. Run it against the `dev` branch and every supported stable
branch of the server; a domain leaves the list by hand, once no supported channel ships it.

## The release pipeline

The workflow `releases.yml` runs every night and on request; only a run on `main` records or
reports anything. Its jobs share only files:

1. **Fetch** reads the releases of every provider that is not blocked, many repositories per
   GraphQL request, with the ids of each repository and its owner. A provider whose repository
   has other ids than its `provider.json` is skipped and reported, and so is one whose
   repository GitHub could not answer for; the others are fetched all the same. It takes the
   most recently published release whose tag names a version of the provider (drafts and
   pre-releases are ignored), when that version has neither a version file nor a refused file.
   So of several releases since the last night only the newest is taken. It streams the archive
   of the tagged commit to disk, at most 100 MiB, and runs `add-version` on it: what that refuses
   (also a larger archive, or a provider folder of more than 100 MiB) becomes a refused file with
   the reason, and the release is not tried again. An archive that cannot be downloaded is tried
   again the next night. It then downloads the archives of every version the check step needs,
   and writes the plan: per channel that is tested, the versions to check whose archive it has.
   Those are the versions without a result for the channel, the current `latest` of each provider
   on the channel, and the three newest versions above it that are not blocked, so a version that
   failed once (on a night the server image was broken, say) can pass again.
2. **Check** runs once per channel that is not frozen, inside the official server image of that
   channel. For each version of the plan, `scripts/check-channel.sh` runs two containers:
   * **install**, with network, runs no provider code: the provider folder is unpacked from the
     archive and its content hash compared with the version file, then the requirements are
     resolved against the packages of that server by the server's own code
     (`CommunityDependencies` of the community catalog controller, wheels only) and installed
     into a scratch folder, so the verdict is what a server on that channel decides. The
     requirements are also resolved, without installing, for linux arm64 with the C library of
     the image: the arm64 image has the same packages, and a server installs wheels only, so
     this tells whether an arm64 server can install them. A clash with the server, a requirement
     that cannot be installed or one without wheels for arm64 fails the version with the
     explanation of the resolver;
   * **load**, without network and with the scratch folder read-only, unpacks the folder again
     and, in a process of its own, parses the manifest and imports the module the way the server
     does for a community provider, from the release's folder also where the image still ships a
     provider of that domain. A manifest the server refuses, an import that fails or one
     that takes more than 300 seconds fails the version; every process the import started is
     ended with it, and the issue gets the last 50 kB of its output.

   A check that cannot run (the package index cannot be reached, a container that runs out of
   time) gives no result; the version is checked again the next night. When the registry's own
   load script cannot start in the server image (it imports from the server what that server
   lacks), the load stage runs no provider code and gives a result that says so: no version
   fails because of it, and the channel gets an issue for the registry maintainers instead (see
   [Release issues](#release-issues)). The provider's own tests are not run.
3. **Apply** records the results with the rules of `record-check`, writes the index files,
   validates and pushes to `main` with the token of the Music Assistant Bot app. A version that
   stops passing on a channel leaves that channel's index: the newest version that still passes
   there becomes `latest`. A check that could not run removes a failed result of its version on that
   channel and keeps a passed one, so the version is not offered there until it passes. The push
   starts the Publish workflow.
4. **Report** tells the maintainers of a provider about every version that failed a check on a
   channel (first checks and versions that stopped passing), every release refused in this run and
   a repository that is not the admitted one, in one issue per provider on this repository (see
   [Release issues](#release-issues)). It writes the issues with the workflow's own token, which
   may write issues and nothing else.

### Provider code runs without secrets

The check executes provider code, so it runs in a job of its own:

- the job has no permissions, no checkout and no token: it gets the registry, the archives and
  the plan from the artifact of the fetch job, which ran no provider code;
- the provider code runs only inside `docker run` of the server image: one container per version
  and stage, which gets none of the job's environment, the files of the registry read-only, a
  scratch folder of that version and one empty folder for its results file, and runs as a user
  without privileges, with at most 512 processes, 4 GiB of memory and a time limit;
- after each container has ended, the script copies only the results file the container was
  asked for, when it is a regular file below 200 kB, to the name the plan gives that version
  (`<channel>/<domain>-<version>.json`); links, folders and anything else a container wrote are
  left behind, and only those copies are uploaded;
- the apply job, which can push, runs only its own checked out code and reads a result only at
  the name the plan gives it, when it holds exactly one result of that channel, provider and
  version, with a reason of one line. Anything else is ignored with a warning, and a result that
  cannot be recorded skips that version only.

The container of the load stage runs the provider's code and writes the results file itself. The
check therefore finds honest mistakes; a provider that wants to can write a pass for itself.

The admission runs the same check in the same way. The reusable workflow for a provider's own
CI runs the same checks on the provider's checkout, in one container.

## Admission

A provider is submitted with the issue form "Submit a provider"
(`.github/ISSUE_TEMPLATE/submit-provider.yml`), which gives the issue the label `submission`. It
holds the repository, and only when needed the provider folder and the tag prefix. Without a
folder, the checks take `provider` when it holds a `manifest.json`, and otherwise the one
top-level folder that holds one; several or none is a refusal that asks for the folder. The
search reads only the names in the archive of the release, at most 20,000 members and 1 GiB, and
then the one `manifest.json` it takes.

The workflow `admission.yml` runs when such an issue is opened, edited, reopened or closed, and by
hand for an issue number (Actions → Admission → Run workflow), for example after a new release. It
runs the workflow file and the code of the commit it runs at: `main` for an issue, the chosen branch
by hand. Nothing of the event reaches a script but through its environment; the registry reads the
issue (author, text) through the API, as data, and accepts only the fields of the form.

It checks:

- that the repository exists, is public, not archived, and has an open source licence GitHub
  recognises;
- that its latest release has a provider folder whose manifest names a domain that is free: not
  reserved, and not a provider in the catalog;
- that the `provider.json` it leads to is valid; its ids come from GitHub;
- that the author of the issue owns the repository or is a code owner in the manifest of the
  latest release;
- that the latest release becomes a version and passes the checks on at least one channel.

Its jobs:

1. **prepare** reads the issue and GitHub with a read-only token, and plans the check only when
   every requirement above the release checks passed;
2. **check** runs once per channel the plan holds a version for, exactly as the check of the
   release pipeline: no token, provider code only in the containers of `scripts/check-channel.sh`.
   A submission that failed a requirement starts no check job, so its code never runs;
3. **propose** runs only on `main`, in the environment `releases`, with a token of the Music
   Assistant Bot app limited to the contents and pull requests of this repository, and to
   reading its issues. It runs only its checked out code, and reads the report, the plan and the
   results as data. It runs also for every event of a closed submission. It reads the issue
   again, and a pull request is never changed: it holds what one run checked.
   * The checks passed and the issue is still open, labelled and holds the text that was
     checked: when the open pull request of the issue holds the same `provider.json` (whatever
     its `added`) and release, it stands. Otherwise that pull request is closed first, with a
     line that says why, and its branch deleted; then the branch `admit/<domain>-<issue>` is
     made from `main` with one commit that adds only the `provider.json`, and only then the pull
     request "Admit <domain>" is opened, with the result, and closes the issue when merged. A run
     cut off at any of these steps leaves no open pull request whose file and text disagree, and
     the next run finishes the work.
   * Nothing is written to the branch while another pull request (of a person, say) uses it:
     the outcome is a conflict, which the result names.
   * Otherwise, and when the issue is closed, the open pull request of the issue is closed with
     a line that says why and its branch deleted; a merged pull request gets no line.

   The branch belongs to the issue, so two submissions of one domain give two pull requests, and
   the result of the second names the first. Titles and commit messages hold the domain only:
   GitHub reads keywords such as "Closes #1" and "[skip ci]" in them. A branch of the issue
   that no open pull request uses, left by an interrupted run, is deleted first. One propose job
   per issue runs at a time: the next waits until the one before has ended, also when that one
   was cancelled. The job reports its outcome to the next one;
4. **result** writes the result in the job summary and as one comment on the issue, which every
   run updates, with the workflow's own token: what was checked (repository, release, commit and
   when), what passed and what happens next, also when the issue is no valid submission or the
   checks could not run. What happens next follows from the outcome of propose: only a pull
   request that holds what this run checked is offered for merging, with its link; a closed or
   changed issue, a conflict on the branch or a failed propose job says so instead. The job fails
   when the checks did not pass.

Everything the issue, the repository or the release holds is shown as code. A registry
maintainer reviews the provider against the
[rules](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md) and
decides by approving and merging the pull request. The next night the release pipeline takes its
latest release.

Removing the label `submission` takes an issue out of the flow: no run looks at it any more, so
whoever removes the label closes the bot's pull request of that issue by hand.

CI refuses a pull request that adds or changes a version file or a refusal, which only the
pipeline writes.

## Issues

This repository gives no support, and takes issues only through its two forms. The workflow
`close-issues.yml` closes any other issue as not planned, with one fixed comment that says where
to go (a reopened one is closed again without a second comment). Exempt are issues by people with
write access (a registry maintainer may open a plain issue about the registry), bots, and the
issues of the release pipeline (label `release-check`), also when a provider maintainer reopens
one. The workflow never reads the title or text of an issue.

An issue of the form "Report or change a provider" has the label `report`. No workflow acts on it;
a registry maintainer reads it and makes the change in an ordinary pull request:

- **it breaks the rules**, or **a copyright or takedown request**: when it holds, block the
  provider (`set-status <domain> blocked --reason <text> --reason-url <issue>`), withdraw it
  (`set-status <domain> withdrawn ...`), or block one version (`block-version`);
- **its maintainer changed the repository, folder or tag prefix**: check that the author owns the
  repository or is a code owner in its manifest, then edit `repository` (and `repository_id` and
  `owner_id` when it is another repository: `gh api repos/<owner>/<name> --jq
  '{repository_id: .id, owner_id: .owner.id}'`), `folder` or `tag_prefix` in its `provider.json`,
  and run `validate`;
- **its maintainer wants it removed**: after the same check,
  `set-status <domain> withdrawn --reason "Withdrawn by its maintainer" --reason-url <issue>`;
- **its maintainer asks for it to be listed again**: after the same check, and once the reason of
  the withdrawal is gone and its newest release passes the checks on a channel,
  `set-status <domain> listed`.

### Release issues

The release pipeline keeps one open issue per provider that needs its maintainers, with the label
`release-check`, opened by `github-actions[bot]`; `is:issue is:open label:release-check` lists
them. A new issue mentions the maintainers of the provider (at most 10): the owner of its
repository and the code owners in its manifest. A provider that uses its manifest to notify others
is blocked. A new failure while it is open becomes a comment without mentions, and a failure is never told twice: the first line of the
issue and of each comment of the pipeline names the failures it tells, and only those lines of
`github-actions[bot]` count. Everything a provider controls (reasons, output, manifest values) is
shown as code; the title shows the name of the manifest only when it is plain text.

A failure is settled when the newest version of the provider (the highest with a version file or
a refusal) passed on a channel that is tested, and:

- for a failed check, also on the channel of the check, unless that channel is not tested in this
  run (no longer tested, or its checks could not run);
- for a repository that was not the admitted one, GitHub gives the admitted one again.

A failure that is settled when it happens is not told. Once every failure an issue tells is
settled, the pipeline comments that the newest version passed and closes the issue as completed;
a failure after that opens a new issue.

An issue that stays open without a reaction of the provider's maintainer is a reason to withdraw
the provider: `set-status <domain> withdrawn --reason <text> --reason-url <issue>`. Nothing does
this automatically.

A channel whose checks cannot run in its server image gets one issue for the registry
maintainers, with the same label, titled "The release checks cannot run on the beta channel" (or
"on stable 2.11") and without mentions; its first line `<!-- release-check channel: <channel> -->`
names the channel. Its text shows the newest output and is kept up to date while the checks
cannot run, and the pipeline closes it once they run on that channel again.

## Still open

- The app token of `propose` could write to `main` if that job were ever compromised, since the
  same app pushes the commits of the release pipeline to `main`. A separate app for the pull
  requests would remove that.
- Running the provider's own tests in the check step.
- The reading of every release against the red lines by Bot-MArley, and its summary on the pull
  request of a submission.
- Making the changes a maintainer asks for with the form "Report or change a provider" by the bot.
