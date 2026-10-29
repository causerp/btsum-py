# AGENTS.md

btsum mints deterministic BitTorrent identities for single files. See
`SPEC.md` for the normative format and `README.md` for usage.

## Do not break these

1. **Determinism.** No timestamps, creator tags, comments, locale, or
   environment in output. Output is a pure function of (file bytes,
   name) and nothing else.
2. **The name is part of the identity.** Every output format includes
   it (`dn=`, trailing sha256sum-style name, or hashline).
3. **Bencode key sort is raw byte order.** Not locale-aware, not
   case-insensitive, not by Unicode codepoint.
4. **Hash interlocking.** `btih` is SHA-1 of the whole `info` dict
   (which contains v2 fields); `btmh` is SHA-256 of the whole `info`
   dict (which contains v1 `pieces`). Do not compute either hash over a
   subset, and do not remove the cross-fields.
5. **Exit codes.** 0 = all OK, 1 = mismatch, 2 = usage / I/O / invalid
   name. These are part of the contract with scripts.
6. **Version lives in exactly one place.** `btsum.py::__version__`.
   Nowhere else, no duplication in docs.
7. **`piece layers` is always present.** Empty dict when no file
   qualifies. BEP-52 declares a torrent invalid without it.

## Verify before committing

```
ruff check . && ruff format --check . && mypy btsum.py && python3 -m pytest -q
```

Golden vector tests must pass. If a golden vector fails, your change
is wrong, not the vector.

## Out of scope

- No bencode parser (encoder only)
- No network code, no `.torrent` reader, no trackers, no DHT
- No optional metadata in the wrapper (exactly `info` + `piece layers`)
- No Unicode normalization of names (NFC and NFD are distinct
  identities; so is a name with a BOM)
