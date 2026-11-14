# Setting up the repository on GitHub

What has to be created or switched on before servers can read the index. Each step is done once,
by an owner of the `music-assistant` organisation.

## Repository

- [ ] Create the public repository `music-assistant/community-catalog`, without a README or
      licence (both are here), and push `main`.
- [ ] Settings → General: enable Issues; disable Wiki and Projects.
- [ ] Settings → Rules: a ruleset for `main` that requires a pull request, requires the status
      check "Lint, types, tests and validation", and blocks force pushes and deletion. Add the
      Music Assistant Bot app to its bypass list: the Releases workflow pushes the new version
      files and results to `main` as that app.

## GitHub Pages

- [ ] Settings → Pages → Build and deployment → Source: **GitHub Actions**. The address becomes
      `https://music-assistant.github.io/community-catalog/`, which is the address the server reads.
- [ ] Settings → Environments → `github-pages`: deployment branches limited to `main`.
- [ ] Run the Publish workflow once by hand (Actions → Publish → Run workflow) and check that
      `https://music-assistant.github.io/community-catalog/v1/nightly.json` and `…/v1/beta.json`
      answer with the index.

## Actions and security

- [ ] Settings → Actions → General: workflow permissions "Read repository contents", and "Allow
      GitHub Actions to create and approve pull requests" off.
- [ ] Settings → Actions → General: require actions to be pinned to a full-length commit SHA.
- [ ] Settings → Security: enable private vulnerability reporting, Dependabot alerts and
      Dependabot security updates.

## Release pipeline

- [ ] Install the Music Assistant Bot app on this repository with Contents read and write. The
      Releases workflow reads the organisation variable `MUSIC_ASSISTANT_BOT_CLIENT_ID` and the
      organisation secret `MUSIC_ASSISTANT_BOT_PRIVATE_KEY` (the ones the server's workflows
      use); make both available to this repository.
- [ ] Run the Releases workflow once by hand (Actions → Releases → Run workflow) and check that
      every job succeeds; with no provider yet it records nothing.
- [ ] To have failures reported to the maintainers: create a classic personal access token with
      the `public_repo` scope for the account that opens the issues (an app can open issues only
      on repositories it is installed on), store it as the repository secret `REPORT_TOKEN`,
      and set the repository variable `POST_ISSUES` to `true`. Until then the issues are kept as
      the artifact `issues` of each run.
- [ ] Check that the server images of the lines in `server_lines.json` can be pulled without
      logging in (`docker pull ghcr.io/music-assistant/server:nightly`).

The other workflows need nothing beyond their own `GITHUB_TOKEN`: Admission reads the files of
the pull request and the public data of the provider's repository, and the check jobs have no
token at all.

## Before the first provider is admitted

- [ ] Run `uv run python -m registry refresh-reserved <server checkout>` against the current
      stable branch of the server as well (the list here was read from `dev`), and commit the
      result.

## When a stable release reads the community catalog

- [ ] Add its line to `server_lines.json` with its image (for example
      `"2.12": {"frozen": false, "image": "ghcr.io/music-assistant/server:2.12"}`) and run
      `build`, so its servers find their index and the Releases workflow checks it. Mark the
      previous stable line `frozen` when the next one is added.
