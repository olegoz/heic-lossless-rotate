# Release checklist

This is the process for publishing a new version of `heic-lossless-rotate`
to PyPI and GitHub Releases. Follow it in order — steps are ordered so
that anything irreversible (the actual PyPI upload) happens last, after
everything that can still be caught and fixed has been.

Per the project's versioning policy: `VERSION` in `src/heic_rotate/_version.py`
is the single source of truth. It is not bumped while a release is being
prepared, and is bumped exactly once, at the point of merging to `main`.
A released `VERSION` always maps to exactly one immutable, tagged, published
state of the code.

**If `.github/workflows/release.yml` is set up**, pushing a version tag
(step 4) automatically performs steps 5–7 for you — building the wheel,
sdist, and `.pyz`, publishing to PyPI via Trusted Publishing, and
attaching all three artifacts to a GitHub Release. Steps 5–7 are still
documented here in full, both as the manual fallback and so it's clear
exactly what the automation is doing on your behalf.

---

## 0. One-time setup (skip if already done)

- [ ] Create an account at [pypi.org](https://pypi.org) and, separately, at
      [test.pypi.org](https://test.pypi.org) — these are fully independent
      accounts/credentials, not shared.
- [ ] Enable 2FA on both accounts.
- [ ] Confirm the project name is registered: <https://pypi.org/project/heic-lossless-rotate/>
- [ ] Set up [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
      (OIDC) on the PyPI project's *Publishing* settings page, linked to
      this repo's `release.yml` workflow, so no long-lived API token
      needs to live in CI secrets. (One-time per project; skip only if
      you're deliberately uploading manually with `twine` instead of CI.)

## 1. Pre-release code checks

- [ ] All tests pass locally: `pytest` (or however the suite is invoked)
- [ ] `_version.py`'s `VERSION` bumped to the new version number (this is
      the *only* file that needs editing to change the version — confirm
      nothing else hardcodes a version string)
- [ ] `CHANGELOG.md` updated with a new entry (Keep a Changelog format),
      linking to a new `docs/releases/vX.Y.Z.md` per the project's
      changelog convention
- [ ] `README.md` reviewed for accuracy — especially any usage examples,
      since these should invoke `heic-lossless-rotate` / `heic-rotate`,
      not a bare script path — and that the `.pyz` download instructions
      point at `releases/latest/download/heic-lossless-rotate.pyz`
- [ ] No uncommitted changes: `git status` clean

## 2. Build

If CI is set up, this step is just a local sanity check before pushing
the tag — `release.yml` will redo it in a clean environment regardless.

- [ ] In a clean virtual environment: `pip install --upgrade build twine`
- [ ] From the repo root (where `pyproject.toml` lives): `python -m build`
- [ ] Confirm both artifacts were produced in `dist/`:
      `heic_lossless_rotate-X.Y.Z-py3-none-any.whl` and
      `heic_lossless_rotate-X.Y.Z.tar.gz`
- [ ] `twine check dist/*` — catches malformed metadata/README rendering
      before it ever reaches a server
- [ ] Spot-check the sdist/wheel contents don't include anything
      unintended: `tar tzf dist/*.tar.gz` / `unzip -l dist/*.whl` — should
      contain only the `heic_rotate` package (no `tests/`, no `.git`, etc.)
- [ ] Build the standalone zipapp and smoke-test it runs with no install
      step at all:
      ```
      python -m zipapp src -o dist/heic-lossless-rotate.pyz \
          -p "/usr/bin/env python3" -m "heic_rotate.cli:main"
      chmod +x dist/heic-lossless-rotate.pyz
      ./dist/heic-lossless-rotate.pyz -V
      ```

## 3. Dry run on TestPyPI

- [ ] `twine upload --repository testpypi dist/*.whl dist/*.tar.gz`
      (the `.pyz` is a GitHub Release asset, not a PyPI upload — leave it
      out of this command)
- [ ] In a throwaway venv:
      `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ heic-lossless-rotate`
- [ ] Run both entry points and confirm the version matches:
      `heic-lossless-rotate -V` and `heic-rotate -V`
- [ ] Run at least one real `rotate` / `reverse` / `info` cycle against a
      real test file through the installed command, not just `-V`
- [ ] Confirm the rendered project page looks right at
      <https://test.pypi.org/project/heic-lossless-rotate/>

## 4. Tag and merge

- [ ] Merge the release branch to `main`
- [ ] Tag the exact commit that was built: `git tag vX.Y.Z`
- [ ] Push the tag: `git push origin vX.Y.Z`

**If CI is set up, this push triggers `release.yml`**, which performs
steps 5 through 7 automatically. Watch the Actions run to confirm all
three jobs (build, publish-pypi, github-release) succeed, then skip to
step 8. The rest of this section is the manual procedure for when CI
isn't in place, or a step needs to be redone by hand.

## 5. Publish to PyPI

**This step is irreversible for this version number — PyPI never allows
re-uploading or replacing an already-published version.** Don't proceed
until every box above is checked.

- [ ] `twine upload dist/*.whl dist/*.tar.gz`
- [ ] Confirm the release appears at
      <https://pypi.org/project/heic-lossless-rotate/>

## 6. Publish the GitHub Release

- [ ] Create a GitHub Release for the pushed tag (via the web UI, or
      `gh release create vX.Y.Z`)
- [ ] Attach all three built artifacts to the release:
      `heic_lossless_rotate-X.Y.Z-py3-none-any.whl`,
      `heic_lossless_rotate-X.Y.Z.tar.gz`, and
      `heic-lossless-rotate.pyz`
- [ ] Confirm the "latest" download link resolves to the new `.pyz`:
      <https://github.com/olegoz/heic-lossless-rotate/releases/latest/download/heic-lossless-rotate.pyz>

## 7. Post-release verification

- [ ] In one more fresh venv (or `pipx install heic-lossless-rotate`):
      `pip install heic-lossless-rotate` pulls the version just published
- [ ] `heic-lossless-rotate -V` and `heic-rotate -V` both report the new
      version
- [ ] Download the `.pyz` fresh from the Release page (not the locally
      built copy) and confirm `python3 heic-lossless-rotate.pyz -V`
      reports the same version, with no install step
- [ ] GitHub release notes read correctly (CI's `generate_release_notes`
      output, if used, is worth a skim rather than trusting blindly)

---

### If something goes wrong after step 5

There is no fixing a bad upload in place. The only path forward is a new
patch version: fix the issue, bump `VERSION` again, and repeat the
process from step 1. If the published artifact is badly broken, PyPI
supports *yanking* a release (`pip install` won't select it by default,
but it stays downloadable for anyone already pinned to it) — this is
done from the project's PyPI management page, not via `twine`. A bad
GitHub Release/`.pyz` can simply be deleted and re-attached under a new
tag, since GitHub (unlike PyPI) has no immutability requirement.
