# Music Assistant community catalog

The community catalog holds music sources, players and plugins that people from the community
build and maintain: the niche service, the unusual speaker, the experiment that is not part of
Music Assistant itself, each with its own documentation, support and releases. An administrator
enables the community catalog in the Marketplace of Music Assistant and installs from it. Every
provider lives in its maintainer's own repository; this repository is the list of them and holds
no code.

## For users

Everything listed was accepted for what it does, and every release of it is checked
automatically: that it installs and loads. The Music Assistant project does not review its
code, does not test whether it works and does not support it. What you install from the
community catalog runs inside your server, at your own risk, and its maintainer is the one to
ask for help.

A registry maintainer can withdraw or block a provider:

- **Withdrawn**: the Marketplace does not offer it; where it is installed, it keeps working.
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

5. **[Submit your provider with the form](https://github.com/music-assistant/community-catalog/issues/new?template=submit-provider.yml)**:
   your repository, and only when needed the provider folder (when it is not `provider` or the
   one top-level folder with a `manifest.json`) and the tag prefix.

The checks run as soon as you submit, and report in one comment on your issue: the repository,
its licence, the domain, that you own the repository or are a code owner in its manifest, and
whether your latest release installs and loads on at least one server line. When something
fails, fix it and run the checks again: edit the issue, or close and reopen it (for example
after a new release). When everything passes, the bot opens the pull request that adds your
provider, and a registry maintainer decides once, against the
[rules](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md), about
what the provider does. Merging that pull request admits it; its first release reaches the
servers after the next nightly check. Closing your issue withdraws the submission and closes its
pull request.

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

A new repository name or owner, another provider folder or tag prefix, or withdrawing your
provider: [open an issue](https://github.com/music-assistant/community-catalog/issues) that says
what changes. A registry maintainer makes the change. A rename or transfer on GitHub keeps the ids
of your repository; a different repository under the old name is never taken for yours.

## For registry maintainers

How the registry works inside, its file formats, commands and pipelines: [MAINTAINING.md](MAINTAINING.md).
Setting up the repository on GitHub: [SETUP.md](SETUP.md).
