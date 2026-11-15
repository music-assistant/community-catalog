# Setting up the repository on GitHub

What has to be created or switched on before servers can read the index. Each step is done once,
by an owner of the `music-assistant` organisation.

## Repository

- [ ] Create the public repository `music-assistant/community-catalog`, without a README or
      licence (both are here), and push `main`.
- [ ] Settings → General: enable Issues; disable Wiki and Projects.
- [ ] Give the team of registry maintainers named in `.github/CODEOWNERS`
      (`@music-assistant/core`) write access to this repository.
- [ ] Settings → Rules: a ruleset for `main` that requires a pull request with the review of a
      code owner, requires the status check "Lint, types, tests and validation", and blocks
      force pushes and deletion. Add the Music Assistant Bot app to its bypass list: the Releases
      workflow pushes the new version files and results to `main` as that app.

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
- [ ] Settings → Actions → General → "Approval for running fork pull request workflows from
      contributors": **Require approval for all external contributors**.
- [ ] Settings → Security: enable private vulnerability reporting, Dependabot alerts and
      Dependabot security updates.

A pull request from a fork runs the workflow files of that pull request, not those of `main`.
Its author can therefore change `admission.yml` or `ci.yml` in the same pull request and make
the admission checks or CI print whatever they like, a pass included. Those runs get only a
read-only token and no secrets, so they cannot write anything, but their verdict cannot be
trusted on its own. What closes it: the approval above, so a registry maintainer sees the
changed files before any workflow of an outside contributor runs, and the code owner review of
the ruleset, so nothing is merged without a registry maintainer, who sees a change to
`.github/` or `registry/` in the pull request. An admission pull request changes one
`provider.json` and nothing else; anything more is a reason to look closely.

## Release pipeline

- [ ] Install the Music Assistant Bot app on this repository with Contents read and write.
- [ ] Settings → Environments: create the environment `releases`, with deployment branches
      limited to `main`. The jobs of the Releases workflow that push or open issues run in it,
      and only on `main`.
- [ ] Make the organisation secret `MUSIC_ASSISTANT_BOT_PRIVATE_KEY` (the app's private key)
      available to this repository. Any workflow run of a branch in this repository can read
      it, as in the server repository, so only registry maintainers get write access here.
- [ ] Create the repository variable `MUSIC_ASSISTANT_BOT_CLIENT_ID` with the client id of the
      app (Settings → Secrets and variables → Actions → Variables). The Releases workflow reads
      it as a variable, as the workflows of the server repository do.
- [ ] Run the Releases workflow once by hand on `main` (Actions → Releases → Run workflow) and
      check that every job succeeds; with no provider yet it records nothing.
- [ ] To have failures reported to the maintainers: create a GitHub account of its own for the
      issues, that is a member of no organisation and has write access to no repository, and a
      classic personal access token for it with only the `public_repo` scope (an app can open
      issues only on repositories it is installed on). Store the token as the secret
      `REPORT_TOKEN` of the environment `releases`, and set the repository variable
      `POST_ISSUES` to `true`. The pipeline only counts issues this account opened, so nobody
      can keep an issue from being opened by writing its key first. Until then the issues are
      kept as the artifact `issues` of each run.
- [ ] Check that the server images of the lines in `server_lines.json` can be pulled without
      logging in (`docker pull ghcr.io/music-assistant/server:nightly`).

The other workflows need nothing beyond their own `GITHUB_TOKEN`: Admission reads the files of
the pull request and the public data of the provider's repository, and the check jobs have no
token at all.

## What only a run on GitHub can settle

The tests cover the commands and the check script with a stand-in for docker. These need a look
in the first real runs:

- [ ] That `actions/upload-artifact` does not follow a link in the folder it uploads. The check
      jobs upload only copies of regular files, so this matters only as a second line.
- [ ] How long the token of a job lives (the token of the runner that uploads artifacts in
      particular), which bounds what a leaked one could do.
- [ ] What the GraphQL API answers for a repository that was renamed or transferred: the new
      repository with the same ids, as the fetch step expects, or nothing.
- [ ] That the issue search finds the key in the body of an issue the report account opened
      (`"<key>" repo:<owner>/<name> is:issue in:body author:<account>`), also right after it was
      opened, so an issue is not opened twice.
- [ ] That the server images can be pulled without logging in, and that the check runs in them
      as an unprivileged user (`--user` of the runner, `HOME=/tmp`) without needing to write
      anywhere else.

## Before the first provider is admitted

- [ ] Run `uv run python -m registry refresh-reserved <server checkout>` against the current
      stable branch of the server as well (the list here was read from `dev`), and commit the
      result.

## When a stable release reads the community catalog

- [ ] Add its line to `server_lines.json` with its image (for example
      `"2.12": {"frozen": false, "image": "ghcr.io/music-assistant/server:2.12"}`) and run
      `build`, so its servers find their index and the Releases workflow checks it. Mark the
      previous stable line `frozen` when the next one is added.
