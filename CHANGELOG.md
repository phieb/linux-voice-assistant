# Changelog

tbc

## Unreleased

- Add Wyoming wake word detection mode (`--wake-uri tcp://host:port`) — connects directly to wyoming-microwakeword (or any compatible Wyoming service), bypassing Home Assistant for wake word detection
- Add `--wake-word-name` argument to specify which wake word to request from a Wyoming service (default: `okay_nabu`)
- Add `WAKE_URI`, `WAKE_WORD_NAME`, and `EXTERNAL_WAKE_WORD` environment variable support in `docker-entrypoint.sh`
- Stop word detection now always runs locally in all wake word modes
- Add support for custom/external wake words
- Add `--download-dir <DIR>` to store downloaded wake word models/configs
- Switch to `soundcard` instead of `sounddevice`
- Add `--list-input-devices` and `--list-output-devices`
- Use `pymicro-wakeword` for microWakeWord
- Add zeroconf/mDNS discovery
- Support openWakeWord with `pyopen-wakeword`
- Support multiple wake words
- Save active wake words to preferences JSON file
- Refactor main into separate files

## 1.0.0

- Initial release
