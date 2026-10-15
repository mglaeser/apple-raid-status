# Contributing

Small, focused improvements are welcome. Open an issue for a bug or proposal before a substantial change so its scope can be discussed. Security reports belong in the [private reporting channel](SECURITY.md).

## Development

Use Python 3.10 or newer. There are no third-party Python dependencies.

```sh
git clone https://github.com/mglaeser/apple-raid-status.git
cd apple-raid-status
python3 -m unittest discover -s tests -v
python3 -m py_compile apple_raid_status.py install.py
```

Portable tests run on Linux and macOS using synthetic fixtures and mocked command execution. They must not require a real RAID, run installer mutations on the CI host, or invoke repair/rebuild operations. Manual native collection is a separate macOS check with `python3 apple_raid_status.py --once`.

## Scope and code review

- Preserve a small standard-library-only runtime and read-only API.
- Keep subprocess commands fixed and arguments validated. Never execute request-supplied shell commands.
- Parse plist, not localized display output, except for explicitly documented state text.
- Do not turn unknown or stale observations into healthy results.
- Keep identity checks, collection timeouts, authentication, and private-file checks intact.
- Add regression tests for behavior changes, especially error paths and state transitions.
- Update the API/schema documentation and Homepage example if their contract changes.
- Use sanitized synthetic fixtures. Never submit tokens, real hostnames/addresses, device serials, or private storage UUIDs.
- Explain the problem, resulting behavior and test evidence in the pull request. Disclose substantial AI assistance; maintainers still review the resulting code and evidence.

Avoid unrelated formatting, new frameworks, automatic repair controls, or unsupported compatibility claims. Larger features such as SMART, historical storage and alert delivery should be discussed as separate integrations.

## Licensing and conduct

Contributions are provided under this project's [MIT License](LICENSE). Be respectful, assume good intent, and keep discussion focused on the technical issue. Harassment and disclosure of another person's private information are not acceptable.

## Releases

A release should update the version and changelog, pass CI, document native smoke-test evidence, and use a signed or otherwise reviewable tag. Publish checksums for downloadable release artifacts. Do not bundle host configuration, tokens, logs, or production fixtures. A passing portable test suite alone is not a claim that every macOS release or RAID arrangement has been verified.
