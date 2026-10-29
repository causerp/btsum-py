# btsum

Deterministic BitTorrent identity for single files.

`btsum` assigns any single file a reproducible BitTorrent identity: the
v1 infohash (**btih**) and the v2 merkle hash (**btmh**) of one shared
metadata structure, side by side. Two parties who never meet, running
btsum on different machines years apart, obtain byte-identical results
for the same (content bytes, file name) pair.

btsum is not a torrent client, not a swarm participant, not a network
program: it mints and verifies identities, and it writes the minimal
`.torrent` that carries them.

Python 3.8+, standard library only. One file: `btsum.py`.

## Design laws

1. **Determinism above all.** Output is a pure function of the file
   bytes and the bound name. No timestamps, no creator tags, no
   comments, no locale, no environment.
2. **No nameless hashes.** The file name is part of the identity.
3. **Omit, never empty-write.** Keys exist iff the spec requires them;
   no empty placeholders.

## Install

Run from source, no install required:

```sh
python3 btsum.py --version
```

Install as a console script (creates a `btsum` on `PATH`):

```sh
python3 -m pip install .
```

Or build a single-file executable zipapp (like `yt-dlp.pyz`):

```sh
python3 devscripts/make_zipapp.py dist/btsum.pyz
./dist/btsum.pyz --version
```

## Usage

```sh
btsum FILE...                        # dual magnet URI, one per file
btsum -1 FILE                        # v1 infohash only
btsum -2 FILE                        # v2 merkle root only
btsum --hashes FILE                  # both hex hashes, md5sum-style
btsum --pair FILE                    # both hashes on one line: <40hex>+<64hex>  <name>
btsum -n NAME FILE                   # bind NAME instead of basename
btsum -t FILE                        # also write FILE.torrent beside input
btsum -t OUT FILE                    # also write the wrapper to OUT (path or name)
btsum --torrent=OUT FILE             # same, equals form
btsum -c REF FILE...                 # verify FILE(s) against REF
btsum -c CHECKSUMS                   # verify a checksum listing
```

Default output (one line per file):

```
magnet:?xt=urn:btih:<40 lowercase hex>&xt=urn:btmh:1220<64 lowercase hex>&dn=<percent-encoded name>
```

References accepted by `-c`:

- `40 hex` — v1 infohash
- `64 hex` — v2 merkle root
- `1220` + `64 hex` — v2 multihash form
- `32 chars base32` — v1 infohash
- `<40hex>+<64hex>` (either order) — both hashes
- `urn:btih:…` / `urn:btmh:…` fragments
- full or partial magnet URIs (unknown parameters ignored)
- sha256sum-style lines: `<hash>␣␣<name>` or `<hash>␣*<name>`; the `*`
  marker is not part of the name; a stray `*` after `dn=` is also
  stripped
- a path to a checksum listing: one reference per line, `#` comments
  and blank lines allowed; each entry must carry a hash and a name
  (`dn=` or a sha256sum-style trailing name)

Exit codes: `0` all checks passed, `1` at least one mismatch, `2` usage
error / unparseable reference / invalid name / I/O failure.

## Compatibility

The wrapper is hybrid-capable per BEP-52: v1 `pieces` and v2 `file tree`
describe the same bytes in the same order. Trackers index it by its v1
infohash today; v2-aware clients additionally join the v2 swarm. The
`piece layers` key is always present (empty dict when no file qualifies).

Domain: files up to ~2 EiB. Piece length is the smallest power of two
from 16 KiB up to 1 GiB with `ceil(size / piece_length) ≤ 1024` where
the cap permits; past 1 GiB the piece count grows instead.

Names must be valid UTF-8 (RFC 3629), a single path component: non-empty,
no NUL, no `/`, not `.` or `..`. No Unicode normalization is applied;
NFC and NFD names are distinct identities. No BOM is added or stripped.

## Development

```sh
python3 -m pip install ruff pytest
ruff check .
ruff format --check .
python3 -m pytest
```

Build the release zipapp:

```sh
python3 devscripts/make_zipapp.py dist/btsum.pyz
```

## Specification

`SPEC.md` is the normative reference. Golden vectors live in §11 and in
`tests/test_btsum.py`.

## License

MIT No Attribution (MIT-0). See `License.txt`.
