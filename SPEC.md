# btsum — Deterministic BitTorrent File Identity

**Status:** normative reference specification
**Reference implementation:** this repository (`btsum.py`)

---

## 1. Scope

btsum assigns any single file a reproducible BitTorrent identity: the v1
infohash (**btih**) and the v2 merkle hash (**btmh**) of one shared metadata
structure, side by side. Two parties who never meet, running btsum on
different machines, years apart, obtain byte-identical results for the same
(content bytes, file name) pair. The tool is not a torrent client, not a
swarm participant, and not a network program: it mints and verifies
identities, and it writes the minimal .torrent that carries them.

Three design laws govern everything in this document:

1. **Determinism above all.** The output is a pure function of the file
   bytes and the bound name. No timestamps, no creator tags, no comments,
   no locale, no environment.
2. **No nameless hashes.** The file name is part of the identity. A hash
   without its name is a checksum; an identity names its file.
3. **Omit, never empty-write.** A metadata key exists if and only if the
   spec requires it. There are no empty placeholders: `comment: ""` is
   still a key, and two compliant producers would disagree byte-wise.

## 2. Terms

* **btih** — SHA-1 of the bencoded `info` dictionary (BEP-3 infohash).
* **btmh** — SHA-256 of the bencoded `info` dictionary (BEP-52 infohash);
  announced and looked up truncated to 20 bytes on trackers and DHT.
* **Identity** — the pair (btih, btmh) together with the bound file name.
* **Wrapper** — the .torrent bytes btsum writes: the `info` dictionary plus
  the top-level `piece layers` key and nothing else.

## 3. Identity structure

The info dictionary carries both hash schemes side by side. For a
single file named `NAME` of `SIZE` bytes:

```
info = {
  "file tree":   { NAME: { "": { "length": SIZE,
                                 "pieces root": <32-byte merkle root> } } },
  "length":      SIZE,
  "meta version": 2,
  "name":        NAME,
  "piece length": PIECE_LENGTH,
  "pieces":      <concatenated 20-byte SHA-1 piece hashes>,
}
```

Normative points:

* The `file tree` follows BEP-52 exactly: the file is keyed by its name at
  the top of the tree; the leaf dictionary carries `length` and, for
  non-empty files, `pieces root`. For a **zero-length file** the
  `pieces root` key is omitted (BEP-52 defines no root for empty files)
  and `pieces` is the empty string (a required key with a legal empty
  value — required keys are never omitted).
* `meta version` is the integer 2, per BEP-52.
* `name` and the `file tree` key are the same byte string. Names
  are single path components of **valid UTF-8** (RFC 3629): non-empty,
  no NUL, no `/`, not `.` or `..`. UTF-8 is required — not merely
  conventional — because every mainstream BitTorrent client decodes
  names as UTF-8; an identity with a non-UTF-8 name is well-formed per
  BEP-3 but practically unusable. Non-UTF-8 names are rejected with
  exit code 2; the diagnostic points at `--name`.
* **No normalization** of any kind is applied. NFC, NFD, NFKC
  and NFKD are all distinct byte sequences and therefore distinct
  identities. A file named `café` in NFD (macOS) and `café` in NFC
  (Linux) hash differently. Callers who need cross-platform stability
  normalize before invoking `btsum -n NAME`.
* **No BOM** is added and none is stripped. If a name contains
  U+FEFF (encoded `EF BB BF`), that byte sequence is part of the name
  and is hashed verbatim. btsum never edits names.
* Because `pieces` (v1) and `file tree` (v2) live in one dictionary, **each
  hash covers and interlocks with both halves**: btih = SHA-1 over the v2
  tree too, btmh = SHA-256 over the v1 piece hashes too. Tampering with
  either half changes both hashes. A consequence: neither hash can be
  computed without doing both hash passes.
* Hashes are always computed over the canonical bencode form produced by
  this implementation (keys sorted as raw bytes, minimal integers).

## 4. Piece-size ladder

```
piece length = smallest power of two p, 16 KiB <= p, with ceil(size / p) <= 1024
capped at 1 GiB; past the cap the piece count grows instead
```

| file size | piece length | pieces | wrapper size ≈ |
|---|---|---|---|
| ≤ 16 MiB | 16 KiB | ≤ 1024 | ~52 KiB |
| 64 MiB | 64 KiB | 1024 | ~52 KiB |
| 1 GiB | 1 MiB | 1024 | ~52 KiB |
| 4 GiB | 4 MiB | 1024 | ~52 KiB |
| 32 GiB | 32 MiB | 1024 | ~52 KiB |
| 128 GiB | 128 MiB | 1024 | ~52 KiB |
| 1 TiB | 1 GiB | 1024 | ~52 KiB |
| 2 TiB | 1 GiB | 2048 | ~104 KiB |
| 100 TiB | 1 GiB | 102 400 | ~5.2 MiB |
| 1 PiB | 1 GiB | 1 048 576 | ~52 MiB |

Rationale, for the record:

* The 1024-piece band keeps the wrapper at ~52 KiB for every realistic
  file and matches the "fewest pieces worth checking" doctrine.
* The 1 GiB cap is **not** a client policy: 2 GiB (2^31) overflows the
  signed 32-bit integer every mainstream client uses for `piece length`.
  1 GiB is the largest power of two the whole ecosystem can represent, so
  identities minted today stay parseable everywhere, forever. Growing the
  piece count past the cap is free: each piece costs 52 bytes of wrapper
  (20 v1 + 32 v2), about 1/20,000,000 of the payload.
* **Domain limit:** the ladder is defined for files up to
  (2^31 − 1) × 1 GiB ≈ **2 EiB**. Beyond it no v1-valid encoding exists —
  the piece index itself runs out. btsum refuses such sizes with exit
  code 2. This is a property of the format, not a concession to any
  client's mood.
* All size arithmetic in the reference implementation fits signed 64-bit
  with a factor of two headroom (largest product 2^62 < 2^63).

## 5. Hashing rules

### 5.1 v1 pieces (BEP-3)

Each piece is `piece length` raw bytes; the last piece is whatever
remains. `pieces` is the concatenation of SHA-1 over each piece. No
padding is added. A zero-length file has zero pieces and `pieces` is the
empty string.

### 5.2 v2 merkle tree (BEP-52)

* Leaves are 16 KiB blocks read sequentially. The last partial leaf is
  hashed **as-is** (no zero padding of data).
* Leaf hashes beyond the end of file that are needed to balance the tree
  **are the 32-byte zero string** ("remaining leaf hashes ... are set to
  zero"). The leaf layer is padded to the next power of two; upper layers
  are the pairwise SHA-256 of children (left ‖ right).
* `pieces root` is the root of that tree. Identical files under identical
  names always produce identical roots.
* **Piece layer:** the layer whose hashes each cover `piece length` bytes
  (for 16 KiB pieces this is the leaf layer itself). For a file **larger
  than the piece size**, `piece layers` maps the file's `pieces root` to
  the concatenation of the first `ceil(size / piece length)` hashes of
  that layer (hashes covering exclusively beyond-EOF data are omitted).
  Files not larger than one piece have no entry: their single piece hash
  *is* the root.
* `piece layers` is a **top-level** key and is **always present** (an
  empty dictionary when no file qualifies) — BEP-52 declares a torrent
  invalid if the field is absent.

### 5.3 Cost model

Hashing cost is piece-size-blind: SHA-1 over `size` bytes plus SHA-256
over `size / 16 KiB` leaves (~2× including interior nodes). Piece size
changes only the wrapper size and verification granularity. One read
pass computes both schemes; there is no single-hash fast path, by design
(section 3, interlocking).

## 6. Wrapper

```
wrapper = { "info": info, "piece layers": {...} }
```

Exactly two top-level keys, always both present. No `announce`, no
`comment`, no `created by`, no `creation date`, no `encoding`, no
`httpseeds`. btsum mints identities, not swarm configurations: tracker
URLs are what magnet `tr=` parameters and client options are for, and
their absence is what makes the wrapper reproducible. Every generated
.torrent is byte-for-byte identical across runs, machines and versions.

## 7. Magnet URI

The default output, one line per file:

```
magnet:?xt=urn:btih:<40 lowercase hex>&xt=urn:btmh:1220<64 lowercase hex>&dn=<name>
```

* `urn:btmh` values are multihash-encoded: the `1220` prefix declares
  SHA-256 with a 32-byte digest, then the hex root.
* **btih comes first**: modern clients join both swarms, while minimal
  v1-only parsers sometimes honor only the first `xt`.
* `dn` is the bound name, percent-encoded byte-for-byte (unreserved
  characters `A-Za-z0-9_.-~` stay literal, space is `%20`, never `+`).
  Percent-encoding is performed on the raw UTF-8 bytes, so the URI is
  safe for any valid UTF-8 name; `dn=` round-trips through
  percent-decoding without loss.
* No `tr=`. The magnet is the identity; discovery is DHT's job.
* The dual magnet degrades gracefully: v1-only clients use the btih `xt`
  and ignore `btmh`; v2-aware clients join both swarms.

## 8. Command line

```
btsum [OPTIONS] FILE...          hash files; default prints the dual magnet
btsum -c REF FILE...             check FILE(s) against a reference
btsum -c CHECKSUM_LISTING        verify every reference line in a listing
btsum --help | --version
```

Options:

| flag | effect |
|---|---|
| `-1`, `--v1` | print only the v1 infohash: `<40 hex>  <name>` |
| `-2`, `--v2` | print only the v2 merkle root: `<64 hex>  <name>` |
| `--hashes` | print both lines, v1 first, md5sum-style |
| `--pair` | print one line: `<40hex>+<64hex>  <name>` (round-trips through `-c`) |
| `-n`, `--name NAME` | bind NAME instead of the file's basename (single FILE) |
| `-t`, `--torrent[=VALUE]` | also write the wrapper to `VALUE` (see below) |
| `-c`, `--check REF [FILE...]` | verify; see below |

`-1`, `-2`, `--hashes` and `--pair` are mutually exclusive and are output
filters only — both hash passes always run (section 5.3).

`-t VALUE` writes the wrapper to `VALUE` verbatim: a bare name resolves
from the current directory (`-t custom.torrent file.bin` writes
`./custom.torrent`), a path resolves from where it points
(`-t /tmp/custom.torrent file.bin` writes `/tmp/custom.torrent`). With
no `VALUE`, the default is `<input>.torrent` beside the input.
`btsum -t file.bin` (no positional) treats `file.bin` as the input and
uses the default path.

Options may appear before or after the positional files; `btsum file.bin
-1` and `btsum -1 file.bin` are equivalent.

### 8.1 Reference grammar for `-c`

`REF` is auto-detected by length and prefix; the two hash lengths can
never be confused:

| input | interpretation |
|---|---|
| 40 hex | v1 infohash |
| 64 hex | v2 merkle root |
| `1220` + 64 hex | v2 root, multihash form |
| 32 chars base32 | v1 infohash |
| `<40hex>+<64hex>` (either order) | both hashes |
| `urn:btih:...` / `urn:btmh:...` | fragment |
| `xt=...&xt=...` | partial magnet |
| `magnet:?xt=...` (leading `magnet:?` optional) | full magnet; unknown keys (`tr=` etc.) ignored |
| `<hash>␣␣<name>` or `<hash>␣*<name>` | sha256sum-style line; the `*` marker is not part of the name; a stray `*` after `dn=` is also stripped |

Anything else is treated as a path to a checksum listing: one reference
per line (magnet, bare hash, combined pair, or sha256sum-style line),
`#` comments and blank lines allowed. Listing lines must carry a hash
and a name (`dn=` or sha256sum-style trailing name).

### 8.2 Name resolution during checks

1. `--name` if given (explicit wins). `NAME` is validated like a
   discovered basename: a single path component of valid UTF-8.
2. else `dn` (or sha256sum-style trailing name) from the reference — it
   is the name the identity was minted with,
3. else the local file's basename.

Checking a renamed file against its original magnet therefore works with
no flags at all. A bare-hash check of a renamed file fails **by design**
(the name is inside the hash); the failure hint suggests `--name`.

### 8.3 Output and exit codes

* Hash lines: `HASH␣␣NAME` (two spaces, md5sum grammar).
* Check lines: `FILE: OK (v1+v2)` / `FILE: FAILED (v2 mismatch)` — the
  parenthesized set names the hashes actually compared (or mismatched).
* Exit `0` all checks passed; `1` at least one mismatch; `2` usage error,
  unparseable reference, invalid name, or I/O failure. Diagnostics go to
  stderr.

## 9. Determinism guarantees and limits

* Byte-identical wrapper and magnet for equal (bytes, name), forever.
* Both hashes always interlock (section 3); there is no partial identity.
* Domain: files up to ~2 EiB (section 4). Empty files are valid
  identities; note some clients may refuse to *load* a zero-piece
  .torrent — the identity itself is well-defined.
* Names are valid UTF-8, un-normalized, BOM-free. Same name
  bytes on any platform → same identity. Different normalization forms
  are different identities.
* `?` and `&` in magnets are URI syntax: harmless on output, quote the
  magnet when passing it back through a shell.

## 10. Compatibility notes

* The wrapper is what the ecosystem calls a hybrid-capable torrent: v1
  `pieces` and the v2 `file tree` describe the same bytes in the same
  order, per the BEP-52 upgrade path. Sites and trackers index it by its
  v1 infohash today; v2-aware clients additionally join the v2 swarm.
* The 1024-piece band deliberately exceeds no creation policy until
  1 GiB pieces; the 1 GiB ceiling is the format's own int32 wall, not a
  client mood (section 4).
* Trackers and DHT receive the 20-byte truncated btmh for v2 swarms —
  the announce format is unchanged from v1.
* `name` is valid UTF-8 by construction; downstream consumers
  that decode the info dict's `name` as UTF-8 will succeed. btsum emits
  no `encoding` key, so the BEP-3 default (UTF-8) applies.

## 11. Test vectors

Golden vectors minted by the reference implementation. Inputs are fully
deterministic; any conforming implementation must reproduce every hash
below. All vector names are ASCII (a subset of valid UTF-8), so the
name rule does not affect them. (`torrent sha` is SHA-256 over
the wrapper bytes; V.1 also embeds the complete wrapper in hex for
byte-level comparison.)

**V.1 — hello.txt, 12 bytes, content `hello btsum\n`**

```
piece length : 16384
btih         : 7fe09bdbcee8c4d588e6f4193973d7cdd12d380f
btmh         : fcd26dfd1a8902f1832ad1613e82fac6d80e25eb2a59335c3d7ad46dab0dd633
torrent sha  : eb975502da8a14ceb51eaeffd3e15d68d8d10f00e7a5409c30e79595d867b368
magnet       : magnet:?xt=urn:btih:7fe09bdbcee8c4d588e6f4193973d7cdd12d380f&xt=urn:btmh:1220fcd26dfd1a8902f1832ad1613e82fac6d80e25eb2a59335c3d7ad46dab0dd633&dn=hello.txt
```

Wrapper hex (218 bytes):

```
64343a696e666f64393a66696c65207472656564393a68656c6c6f2e74787464303a
64363a6c656e6774686931326531313a70696563657320726f6f7433323a21088184
08c7ade7c71fc9ee9064dee95ab0dcd60a2bfd9546ac12f5d8c77a33656565363a6c
656e6774686931326531323a6d6574612076657273696f6e693265343a6e616d6539
3a68656c6c6f2e74787431323a7069656365206c656e67746869313633383465363a
70696563657332303ab382cd2bba066a903f24e97c2cff3861e1158f706531323a70
69656365206c6179657273646565
```

**V.2 — empty.bin, 0 bytes**

```
piece length : 16384
btih         : a2cbc9d1534378ef64d1e0619d738b2f3d2b7dd4
btmh         : 882b4b77461bf690ee114e776aa263b6850ea6caf0cfae73a7437e64da8fa8b6
torrent sha  : b7319ccffba6395c58df45dbe6891dfa7a70fdce60cf6232d8d893fc8cd1fe19
```

**V.3 — three-leaves.bin, 41600 bytes, content `btsum three leaves vector\n` × 1600**

Exercises: 3 leaves padded to 4 with one zero hash; 3 pieces of 16 KiB
(each hash = its leaf hash); `piece layers` entry present.

```
piece length : 16384
btih         : 92166ef8a8d5548f51204bbd71b796c9ff0ec791
btmh         : 8fcfeece6d1c589db95e820b3268935dee1733bdfaed76778c3dbdf081409992
torrent sha  : 96c5ff0703e244890c0d05861687bb910e9ab9a9e3b84fd5f2ccd198e26b1729
```

**V.4 — split-piece.bin, 16385 bytes, content `A`×16384 + `B`**

Exercises: 2 v1 pieces (last piece 1 raw byte, unpadded); 2 leaves (no
zero padding needed); `piece layers` = leaf hashes of both pieces.

```
piece length : 16384
btih         : 5572e2e889ddd2ea2d0478ad1bfe3e2b42e96385
btmh         : 9887734327d915d4ee26ab34d2ab6dac1742e51f78f9255529ae81b07cf33492
torrent sha  : 61ed02904fb759053c9db5fa6745673ac7b76343f2b9adcb0a5cc122fbd324cf
```

**V.5 — two-pieces.bin, 32768 bytes, content `B`×32768**

Exercises: exact piece boundary; 2 pieces; root over two leaves.

```
piece length : 16384
btih         : 5e539dbb67a2c44270beef40033616b20cebd6f2
btmh         : 3198f5a6ad31d5289fbf02d440faddff7ce3b1f5b5f5616b9300b51afb854b5b
torrent sha  : e76da1971e939d6ad785625b143b97f9849435852cc3af9d9c6b6d879fea8775
```

## 12. References

* BEP 3 — The BitTorrent Protocol Specification (bencode, v1 pieces, infohash)
* BEP 5 — DHT (20-byte truncated identifier announce)
* BEP 9 — Extension for peers to send metadata (magnets ship the info dict only)
* BEP 52 — The BitTorrent Protocol Specification v2 (file tree, piece layers, merkle rules)
* BEP 53 — Magnet URI extension; multihash `urn:btmh` (`1220` = SHA-256/32)
* RFC 3629 — UTF-8, a transformation format of ISO 10646
* Multihash specification (prefix grammar)
