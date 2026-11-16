# Music Assistant community catalog

The community catalog holds music sources, players and plugins that people from the community
build and maintain. An administrator enables the community catalog in the Marketplace of Music
Assistant and installs from it. Each of them lives in its maintainer's own repository; this
repository is the list of them and holds no code.

## For users

Everything listed was accepted for what it does, and every release of it is checked
automatically: that it installs and loads. The Music Assistant project does not review its code, does not test
whether it works and does not support it. What you install from the community catalog runs inside
your server, at your own risk, and its maintainer is the one to ask for help.

- **Withdrawn**: no longer offered; where it is installed, it keeps working.
- **Blocked**: servers stop loading it at their next daily check, and the administrator sees why.
  Its settings and library items are kept.

Where to report a problem:

- a bug, or a question: its maintainer, through the **Get help** button in the app or its own
  repository;
- something that crosses the [rules of the community catalog](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md):
  an [issue here](https://github.com/music-assistant/community-catalog/issues), saying which one
  it is, what it does and how you noticed;
- a risk to its users: "Report a vulnerability" on the
  [Security tab](https://github.com/music-assistant/community-catalog/security) of this
  repository, not in public.

<a name="admission"></a><a name="for-provider-maintainers"></a>

## Getting your provider listed

1. **Start from the [provider template](https://github.com/music-assistant/community-provider-template).**
   It holds a working provider, its tests and the CI described in step 4.
2. **Make sure your repository:**
   - is public, not archived, and has an open source licence GitHub recognises;
   - keeps the provider in a folder named after its domain (as the template does, so a server in
     development mode, the test fixture and the Dev app can import it), or named `provider`;
   - uses a domain that is free: not in [`reserved_domains.json`](reserved_domains.json) and not
     a folder in [`providers/`](providers);
   - has a `manifest.json` that is plain JSON (no `NaN` or `Infinity`, no key twice), at most
     16 KiB, with at most 10 `codeowners` and 50 `requirements`;
   - lists requirements as package names with version specifiers (no URLs, markers or options),
     available as wheels for linux amd64 and arm64;
   - <a name="the-icon"></a>may have an `icon.svg` of at most 5 KB that only draws
     ([the icon rules](MAINTAINING.md#the-icon)); an icon that does not pass is left out.

   Once installed, a provider never writes into its own folder: the server replaces a folder that
   differs from the release.
3. **Publish a GitHub release** whose tag is the version, `MAJOR.MINOR.PATCH` with or without
   `v`, such as `v1.2.0`. Drafts and pre-releases are ignored. A repository with several
   providers puts a tag prefix in front, such as `sample_music-v1.2.0`.
4. **Run the catalog's checks in your own CI** with the reusable workflow (the template does this):

   ```yaml
   jobs:
     catalog:
       uses: music-assistant/community-catalog/.github/workflows/check-provider.yml@main
       with:
         folder: my_provider
         server-line: nightly
   ```

5. **Open a pull request here that adds one file**, `providers/<domain>/provider.json`, and
   nothing else:

   ```json
   {
     "domain": "sample_music",
     "repository": "someone/ma-sample-music",
     "repository_id": 123456789,
     "owner_id": 987654,
     "folder": "sample_music",
     "maintainers": ["someone"],
     "status": "listed",
     "added": "2026-11-16T12:00:00Z",
     "admission": {"decided_by": "someone", "decided": "2026-11-16T12:00:00Z", "pull_request": 1}
   }
   ```

   - `domain`: the `domain` of your manifest, and the name of the folder in `providers/`.
   - `repository`: your repository as `owner/name`.
   - `repository_id`, `owner_id`: the ids GitHub gives your repository and its owner, which
     `gh api repos/<owner>/<name> --jq '{repository_id: .id, owner_id: .owner.id}'` prints.
   - `folder`: the path of the provider folder in your repository, such as `provider` or
     `providers/sample_music`.
   - `maintainers`: GitHub handles without `@`, at most 10, each the owner of the repository or
     a code owner in the manifest of the latest release. The author of the pull request must be
     one of those as well.
   - `added`, `admission`: the current time in UTC in this format, your own handle, and
     `pull_request` `1`. A registry maintainer fills in the decision when admitting.
   - Add `"tag_prefix": "sample_music-"` only when the repository holds several providers.

Then the admission checks run by themselves and report in a comment on the pull request: the
repository, the ids, the licence, the domain, the maintainers, and whether your latest release
installs and loads on at least one server line. When something fails, fix it and push; for a new
release, close and reopen the pull request. A registry maintainer then decides once, against the
[rules](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md), about
what the provider does.

## Releasing updates

After admission you only publish releases. Every night the registry takes the newest release it
has not seen (of several since the night before, only the newest), and checks it on each server
line inside the official server image: its requirements must install next to that server's own
packages, also on arm64, and the provider must load. A version that passes reaches the servers of
that line without waiting for anyone; your tests are not run.

A release that is refused, or fails on a line, gets an issue on your repository with the reason
and the output (on this repository when yours has issues disabled). A refused release is not tried
again: publish a new version. A version that fails on a line is not offered there; that line
keeps offering the newest version that still passes.

Each server line has a list of its own: the nightly, the beta and the stable release lines in
[`server_lines.json`](server_lines.json). An older stable line is frozen: it takes no new versions,
but a block still reaches it.

## Changing or leaving

- **Renaming or transferring the repository** keeps its ids. Open a pull request that changes
  `repository` in your `provider.json`; a registry maintainer decides on it. A different
  repository under the old name is never taken for yours.
- **Moving the provider folder or changing the tag prefix**: a pull request that changes `folder`
  or `tag_prefix` gets the same admission checks.
- **Changing maintainers or withdrawing**: open an issue or pull request here; a registry
  maintainer decides on it. A withdrawn provider keeps working where it is installed.

Every field of `provider.json` is described in [MAINTAINING.md](MAINTAINING.md#providerjson).

## For registry maintainers

How the registry works inside, its file formats, commands and pipelines: [MAINTAINING.md](MAINTAINING.md).
Setting up the repository on GitHub: [SETUP.md](SETUP.md).
