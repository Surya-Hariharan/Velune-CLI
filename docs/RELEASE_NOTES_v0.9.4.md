<div align="center">
  <img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/logo.png" alt="Velune Logo" width="100" />
  <h1>Velune CLI v0.9.4</h1>
</div>

Velune CLI v0.9.4 hardens the native-extension path, expands repository
intelligence, and improves the release/CI pipeline while keeping the standard
PyPI install pure Python and backward compatible.

## Highlights

- Go production launcher improvements for cross-platform startup, signal forwarding, update checks, and health diagnostics.
- Rust native module foundation for optional SHA-256 and directory-scan hot paths.
- Repository Knowledge Graph for structured repository context.
- Repository Intelligence Engine for file, git, and graph update events.
- Native integration layer with pure-Python fallbacks when native wheels are unavailable.
- Cross-platform CI across Python, Go, Rust, packaging, install smoke tests, and native benchmarks.

## Breaking Changes

None known. v0.9.4 is intended to be a backward-compatible release.

## Upgrade Notes

```bash
pip install --upgrade velune-cli
velune --version
velune doctor check
```

The standard PyPI package remains pure Python. Optional native components are validated in CI and can be installed separately when packaged, but Velune does not require them for normal operation.

## Known Limitations

- The Rust native module is optional; Python fallbacks remain the supported default path when the native wheel is absent.
- Native benchmark jobs are informational and do not enforce performance thresholds because hosted CI hardware is variable.
- Experimental plugin loading remains disabled by default and should only be used in trusted workspaces.

## Future Roadmap

- Continue hardening native packaging and fallback parity.
- Expand repository intelligence coverage without changing the public CLI contract.
- Improve release automation and artifact provenance checks.
