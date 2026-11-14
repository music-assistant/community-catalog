# Setting up the repository on GitHub

What has to be created or switched on before servers can read the index. Each step is done once,
by an owner of the `music-assistant` organisation.

## Repository

- [ ] Create the public repository `music-assistant/community-catalog`, without a README or
      licence (both are here), and push `main`.
- [ ] Settings → General: enable Issues; disable Wiki and Projects.
- [ ] Settings → Rules: a ruleset for `main` that requires a pull request, requires the status
      check "Lint, types, tests and validation", and blocks force pushes and deletion.

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

## Before the first provider is admitted

- [ ] Run `uv run python -m registry refresh-reserved <server checkout>` against the current
      stable branch of the server as well (the list here was read from `dev`), and commit the
      result.

## When a stable release reads the community catalog

- [ ] Add its line to `server_lines.json` (for example `"2.12": {"frozen": false}`) and run
      `build`, so its servers find their index. Mark the previous stable line `frozen` when the
      next one is added.
