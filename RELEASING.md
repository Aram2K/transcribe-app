# Releasing Transcribe

Do not publish a release manually unless you intentionally want to bypass the tested build pipeline.

## Compatibility Rules

- Keep the Windows installer asset named `TranscribeApp-Windows-Setup.exe`.
- Keep the portable Windows asset named `TranscribeApp-Windows.zip`.
- Keep the macOS asset named `TranscribeApp-Mac.dmg`.
- Push semantic version tags like `v1.6.0`; do not reuse an old tag for a different build.
- Let GitHub Actions create the release and upload assets.
- Do not delete the installer asset from a published release.

Older versions discover updates through GitHub's latest release API and look for a setup `.exe`. Newer versions also use `update-manifest.json` and `.sha256` files when present.

## What Needs a Release

- App code, tray UI, installer behavior, bundled dependencies, icons, and anything users run locally require a new tagged release.
- README, website text, docs, GitHub release notes, and backend/server changes can update without a new app release.
- Remote config can change without a release only after the app has shipped code that reads that remote config.

## Release Steps

1. Make sure `APP_VERSION` in `main.py` matches the tag version without the `v` prefix.
2. Run tests locally:

   ```powershell
   .\venv\Scripts\python.exe -m pytest tests/ -v -p no:cacheprovider
   .\venv\Scripts\python.exe -m py_compile main.py history.py storage.py
   ```

3. Commit and push to `main`.
4. Create and push the tag:

   ```powershell
   git tag v1.6.0
   git push origin v1.6.0
   ```

5. Wait for GitHub Actions to finish.
6. Confirm the release contains:
   - `TranscribeApp-Windows-Setup.exe`
   - `TranscribeApp-Windows-Setup.exe.sha256`
   - `TranscribeApp-Windows.zip`
   - `TranscribeApp-Windows.zip.sha256`
   - `TranscribeApp-Mac.dmg`
   - `TranscribeApp-Mac.dmg.sha256`
   - `update-manifest.json`

7. Test these latest-download URLs:
   - `https://github.com/Aram2K/transcribe-app/releases/latest/download/TranscribeApp-Windows-Setup.exe`
   - `https://github.com/Aram2K/transcribe-app/releases/latest/download/update-manifest.json`
