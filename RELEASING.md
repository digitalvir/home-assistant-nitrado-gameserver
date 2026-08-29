# Releasing

This repository uses time-derived Calendar Versioning. Every build is
`YYYY.M.D.N`, with no leading zeroes or leading `v`. `N` is the integer result
of dividing by two the elapsed real seconds between the pinned build timestamp
and the first instant of that local date in `America/New_York`:

```text
N = floor((build_epoch - local_midnight_epoch) / 2)
```

Using epoch elapsed time keeps the suffix monotonic across both daylight-saving
transitions. A build pins its timestamp once in `RELEASE-METADATA.json`; every
manifest, frontend cache/custom-element identity, artifact directory, Git tag,
and GitHub release uses the version derived from that pin. A same-bucket build
is a collision and must fail instead of inventing a sequence number.

## Release gates

1. Run the unit, real Home Assistant, Ruff, Bandit, JavaScript, and JSON gates
   documented in the README. Use the pinned Python environment; a bare system
   `python3` may be older than the Home Assistant version supported here.
2. Curate the public tree into an independent directory, validate it, commit the
   clean development tree, and run release preparation once. Preparation creates
   `RELEASE-METADATA.json`, promotes `Unreleased`, and stamps every source identity:

   ```bash
   python3 scripts/curate_public_repository.py /tmp/nitrado-public
   python3 /tmp/nitrado-public/scripts/validate_public_repository.py /tmp/nitrado-public
   git -C /tmp/nitrado-public commit -m 'Prepare publication candidate'
   python3 /tmp/nitrado-public/scripts/prepare_release.py --root /tmp/nitrado-public
   git -C /tmp/nitrado-public add --all
   git -C /tmp/nitrado-public commit -m 'Release YYYY.M.D.N'
   git -C /tmp/nitrado-public tag -a YYYY.M.D.N -m 'Release YYYY.M.D.N'
   ```

   Refuse a dirty tree, an existing pin/tag, a same-bucket collision, or an
   identity mismatch. Do not create a remote in this local-publication gate.

3. Verify that the annotated tag resolves to the exact reviewed commit and
   build its deterministic source archive plus the full evidence artifact:

   ```bash
   python3 scripts/verify_release_provenance.py \
     --root . --tag YYYY.M.D.N --output dist/provenance
   scripts/build_release_artifact.sh 'dist/release-{version}-rc1'
   ```

   On tag-triggered GitHub runs, `actions/checkout` may replace the local
   annotated tag ref with the event commit. CI explicitly refetches the remote
   tag object before provenance verification; do not remove that step.

4. Inspect the generated `SHA256SUMS` and run validation from inside
   the artifact:

   ```bash
   python3 dist/release-YYYY.M.D.N-rc1/scripts/validate_release_artifact.py \
     dist/release-YYYY.M.D.N-rc1
   ```

   After this succeeds, treat that directory as frozen evidence. Never run
   unit, Home Assistant, compile, or browser tests in place because Python and
   test tools can create cache/output files that invalidate the exact file-set
   contract. Copy the artifact to a temporary directory for executable tests,
   discard that copy, then rerun the validator against the untouched frozen
   artifact.

5. Run HACS and hassfest against the exact staged tree. Locally extract the
   tagged source archive and prove the exact
   `custom_components/nitrado_gameserver` subtree installs, upgrades, removes,
   and reinstalls in an isolated Home Assistant fixture. This is HACS-shaped
   evidence only; it does not prove GitHub/HACS remote delivery:

   ```bash
   python3 scripts/verify_local_hacs_lifecycle.py \
     --archive dist/provenance/home-assistant-nitrado-gameserver-YYYY.M.D.N.tar.gz \
     --artifact dist/release-YYYY.M.D.N-rc1 \
     --previous-runtime /path/to/previous/custom_components/nitrado_gameserver
   ```

   Omitting `--previous-runtime` still proves install, code removal with
   Home Assistant-owned data retention, and reinstall; it does not prove an
   upgrade from a previous release.
6. Perform a final privacy, documentation, architecture, authorization, and
   lifecycle review against that exact tree.
7. Create a GitHub repository, push tags, and configure repository security
   settings only after the maintainer explicitly approves those external
   actions. HACS officially supports only public repositories. While this
   repository is private, CI runs the local HACS schema and lifecycle gates;
   `hacs/action` and a real HACS download run only after an explicitly approved
   visibility change.

The repository validator refuses Git remotes by default for the local
publication gate. GitHub Actions and explicit clean-clone verification use
`--allow-remote`; this relaxes only the remote-presence check and keeps the
clean-tree, tracked-file, privacy, and generated-content checks intact.

The defined HACS payload is the integration subtree in GitHub's annotated-tag
source archive. `hacs.json` deliberately does not set `zip_release`; the
separate full-source artifact is evidence, not the package HACS installs.

## Claim boundaries

- Secure FTPS capability and read-only provider access are live-proven.
- Recursive provider mutation is not claimed until the disposable temporary-tree
  checkpoint passes.
- Native backup creation is not implemented.
- Native restore is not claimed as live-proven until it is exercised on a
  disposable Nitrado service. Never use an active production save tree as the
  restore test target.
- The Provider Connector API is a cooperative compatibility and authority
  boundary inside Home Assistant, not a sandbox for malicious Python.
