# Music Assistant community catalog

The community catalog holds providers (music sources, plugins etc.) that the community builds and
maintains. Each provider lives in the repository of its maintainer, with its own documentation,
support and releases. This repository is the registry: the list of those providers. It holds no
code.

An administrator of a Music Assistant server (a user with the admin role) enables the community
catalog in the Marketplace of that server, and installs providers from it.

## For users

Every provider in the community catalog was accepted for what it does, and every release is
checked automatically: it must install and load. The Music Assistant project does not review the
code of a provider, does not test whether it works and does not support it. A provider runs
inside your server, at your own risk.

A registry maintainer can withdraw or block a provider. A withdrawn provider is not offered in the
Marketplace; where it is installed, it keeps working. A blocked provider stops loading at the next
daily check of the server, which tells the administrator why; its settings and library items are
kept.

This repository gives no support. Help with a provider comes from its maintainer: the **Get
help** button in the app, or the repository of the provider. Help with Music Assistant itself
comes from the [support repository](https://github.com/music-assistant/support/issues/new/choose).

To report a provider that breaks the
[rules of the community catalog](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md),
or to make a copyright or takedown request, use the form
[Report or change a provider](https://github.com/music-assistant/community-catalog/issues/new?template=report-provider.yml).
Report a risk to users privately: "Report a vulnerability" on the
[Security tab](https://github.com/music-assistant/community-catalog/security).

<a name="admission"></a><a name="for-provider-maintainers"></a>

## Getting your provider listed

1. **Start from the [provider template](https://github.com/music-assistant/community-provider-template).**
   It holds a working provider, its tests and the checks of the community catalog for your CI.
2. **Meet the requirements:**
   - a public repository with an open source licence;
   - a valid `manifest.json`;
   - a `domain` in that manifest (the unique id of the provider, such as `sample_music`) that no
     other provider uses;
   - the provider in a folder named after that domain, or `provider`;
   - requirements that install as wheels;
   - <a name="the-icon"></a>optionally an `icon.svg` ([what it may hold](MAINTAINING.md#the-icon)).

   When something does not pass, the checks say exactly what is wrong. The exact limits are in
   [What a provider needs](MAINTAINING.md#what-a-provider-needs); the
   [developer documentation](https://developers.music-assistant.io/building-a-provider/) explains
   how to build a provider.
3. **Publish a GitHub release** whose tag is the version, such as `v1.2.0`. A repository with
   several providers puts a tag prefix in front, such as `sample_music-v1.2.0`.
4. **Run the checks in your CI** with the reusable workflow (the template does this):

   ```yaml
   jobs:
     catalog:
       uses: music-assistant/community-catalog/.github/workflows/check-provider.yml@main
       with:
         folder: my_provider
         channel: nightly
   ```

5. **Submit the provider** with the form
   [Submit a provider](https://github.com/music-assistant/community-catalog/issues/new?template=submit-provider.yml).

The result of the checks appears as a comment on your issue. When a check fails, fix it, then
edit the issue, or close and reopen it after a new release. When every check passes, a registry
maintainer reviews the provider against the rules and decides once about what it does. Closing
the issue withdraws the submission.

## Releasing updates

After admission, you release whenever you want: publish a GitHub release, and nothing else is
needed. The registry looks for new releases once a night and checks the newest one on each
channel (nightly, beta, and one per stable version such as stable 2.11). A release that passes
reaches the servers on that channel without review. A release that is refused or fails gets an
issue on this repository that mentions you, with the reason; it closes by itself when a later
release passes. A refused release is not tried again; publish a new version.

## Changing or leaving

To move your provider to another repository, folder or tag prefix, to withdraw it, or to have it
listed again, use the form
[Report or change a provider](https://github.com/music-assistant/community-catalog/issues/new?template=report-provider.yml).
A registry maintainer makes the change.

## For registry maintainers

How the registry works: [MAINTAINING.md](MAINTAINING.md).
