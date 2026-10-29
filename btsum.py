#!/usr/bin/env python3
"""btsum -- deterministic BitTorrent identity tool.

btsum gives any single file a reproducible BitTorrent identity:

  * v1 infohash  (btih, SHA-1 over the bencoded info dict)
  * v2 merkle hash (btmh, SHA-256 over the same info dict)

The info dict carries both hash schemes side by side (v1 `pieces` and the
v2 `file tree`), so each hash covers and interlocks with both halves.
The identity always binds the original file name: there are no nameless
hashes. The default output is the canonical dual magnet URI that carries
both hashes plus the bound name (dn=).

Names are valid UTF-8 (RFC 3629), un-normalized and BOM-free. This is
the encoding every mainstream BitTorrent client decodes names as; btsum
refuses names that would produce an unusable identity.

Everything is a pure function of (file bytes, name): no timestamps, no
comments, no optional keys. The .torrent wrapper contains exactly two
top-level keys (`info`, `piece layers`) and is byte-for-byte identical
across runs, machines and versions.

Python 3.8+, standard library only. See SPEC.md for the normative
description of the format, the piece-size ladder and the CLI grammar.
"""

import argparse
import base64
import functools
import hashlib
import os
import re
import sys
from collections import namedtuple
from urllib.parse import quote as _urlquote
from urllib.parse import unquote_to_bytes

__version__ = "1.0.0"
PROG = "btsum"

# ---------------------------------------------------------------------------
# Format constants (see SPEC.md sections 3 and 4)
# ---------------------------------------------------------------------------

KIB = 1024
LEAF_SIZE = 16 * KIB  # BEP-52 merkle leaf block size (also the wire request size)
MIN_PIECE_SIZE = LEAF_SIZE  # BEP-52: piece length must be a power of two >= 16 KiB
MAX_PIECE_SIZE = 1 << 30  # 1 GiB: largest piece length every client can represent (signed int32)
PIECE_BAND = 1024  # max piece count before the piece size grows
MAX_PIECES = (1 << 31) - 1  # signed int32 piece index: the format's own domain boundary
MAX_FILE_SIZE = MAX_PIECES * MAX_PIECE_SIZE  # ~2 EiB

ZERO_HASH = b"\x00" * 32  # BEP-52: leaf hashes beyond EOF "are set to zero"

META_VERSION = 2


class BtsumError(Exception):
    """Fatal error, reported as `btsum: error: ...` with exit code 2."""


class UsageError(BtsumError):
    """Bad flag/operand combination (exit code 2)."""


Identity = namedtuple("Identity", "btih btmh torrent piece_length name size")

# ---------------------------------------------------------------------------
# Canonical bencode (encoder only: btsum never parses bencode)
# ---------------------------------------------------------------------------


def bencode(obj):
    """Canonical bencode. Dict keys must be bytes and are sorted as raw bytes.

    One Python value has exactly one correct encoding -- this is what makes
    cross-producer byte equality possible.
    """
    if isinstance(obj, bool):
        raise TypeError("bool is not a bencode type")
    if isinstance(obj, int):
        return b"i" + str(obj).encode("ascii") + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode("ascii") + b":" + obj
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        seen = set()
        items = []
        for key, value in obj.items():
            if not isinstance(key, bytes):
                raise TypeError("dict keys must be bytes, got %r" % type(key))
            if key in seen:
                raise ValueError("duplicate dict key %r" % key)
            seen.add(key)
            items.append((key, value))
        items.sort(key=lambda kv: kv[0])
        body = b"".join(bencode(k) + bencode(v) for k, v in items)
        return b"d" + body + b"e"
    raise TypeError("cannot bencode %r" % type(obj))


# ---------------------------------------------------------------------------
# Piece-size ladder (SPEC.md section 4)
#
#   piece length = smallest power of two >= 16 KiB with ceil(size / p) <= 1024,
#   capped at 1 GiB; past the cap the piece count grows instead.
#
# Rationale: the 1024-piece band keeps .torrent files at ~52 KiB regardless
# of size; the 1 GiB cap is the largest piece length representable in the
# signed 32-bit integers every client uses for `piece length`, so identities
# stay parseable everywhere, forever. Past 1 GiB pieces the count grows:
# each piece costs 52 bytes of torrent (20 B v1 + 32 B v2), still ~1/20,000,000
# of the payload. The domain ends at 2^31 - 1 pieces (~2 EiB) where the piece
# index itself runs out; beyond that no v1-valid encoding exists.
# ---------------------------------------------------------------------------


def _ceil_div(a, b):
    """Return ceil(a/b) for non-negative a and positive b."""
    return -(-a // b)


def piece_count_for(file_size, piece_length):
    """Number of pieces for a file of the given size and piece length."""
    if file_size == 0:
        return 0
    return _ceil_div(file_size, piece_length)


def piece_length_for(file_size):
    """Ladder function: file size in bytes -> piece length in bytes."""
    if file_size < 0:
        raise BtsumError("negative file size")
    if file_size > MAX_FILE_SIZE:
        raise BtsumError(
            "file size %d exceeds the format domain limit (%d bytes, ~2 EiB); "
            "no v1-valid encoding exists beyond it" % (file_size, MAX_FILE_SIZE)
        )
    p = MIN_PIECE_SIZE
    while p < MAX_PIECE_SIZE and _ceil_div(file_size, p) > PIECE_BAND:
        p <<= 1
    return p


# ---------------------------------------------------------------------------
# Merkle tree (BEP-52): 16 KiB leaves, the last partial leaf is hashed as-is,
# leaf hashes beyond EOF are the 32-byte zero string, the leaf layer is padded
# to a power of two, and upper layers are the pairwise SHA-256 of children.
# ---------------------------------------------------------------------------


def _pow2_at_least(n):
    """Return the smallest power of two greater than or equal to n (n >= 1)."""
    p = 1
    while p < n:
        p <<= 1
    return p


@functools.lru_cache(maxsize=None)
def _zero_subtree(levels):
    """Root of a perfect subtree of 2**levels zero leaves.

    Memoized: the same handful of zero-subtree roots are used for every
    partial piece and for the final file-root padding, so caching turns
    the whole run into one hash per distinct level.
    """
    if levels == 0:
        return ZERO_HASH
    prev = _zero_subtree(levels - 1)
    return hashlib.sha256(prev + prev).digest()


def _bits_desc(n):
    """Set bit positions of n, high to low: subtree spans of [0, n) in order."""
    return [i for i in range(n.bit_length() - 1, -1, -1) if (n >> i) & 1]


def _bits_asc(n):
    """Set bit positions of n, low to high: subtree spans of a suffix aligned
    to the right edge, in left-to-right order.
    """
    return [i for i in range(n.bit_length()) if (n >> i) & 1]


def _fold(spans):
    """Combine (level, hash) spans that tile a range left-to-right into one root."""
    st = []
    for lvl, h in spans:
        st.append((lvl, h))
        while len(st) >= 2 and st[-1][0] == st[-2][0]:
            right_lvl, right = st.pop()
            _, left = st.pop()
            st.append((right_lvl + 1, hashlib.sha256(left + right).digest()))
    if len(st) != 1:
        raise BtsumError("internal error: merkle spans do not tile")
    return st[0][1]


def _piece_hash(real_leaves, span):
    """Root of the perfect subtree covering `span` leaf slots of which the
    first len(real_leaves) are real and the rest are zero leaves.
    """
    spans = [(0, h) for h in real_leaves]
    pad = span - len(real_leaves)
    if pad:
        spans += [(lvl, _zero_subtree(lvl)) for lvl in _bits_asc(pad)]
    return _fold(spans)


def _file_root(leaf_count, stack):
    """Root of the padded perfect tree over leaf_count real leaves.

    `stack` is the streaming binary-counter tiling of [0, leaf_count).
    """
    total = _pow2_at_least(leaf_count)
    spans = [(lvl, stack[lvl]) for lvl in _bits_desc(leaf_count)]
    pad = total - leaf_count
    if pad:
        spans += [(lvl, _zero_subtree(lvl)) for lvl in _bits_asc(pad)]
    return _fold(spans)


# ---------------------------------------------------------------------------
# Identity minting (SPEC.md section 3)
# ---------------------------------------------------------------------------


def validate_name(name):
    """The bound name is a single path component of bytes, valid UTF-8.

    BitTorrent clients decode `name` as UTF-8; a non-UTF-8 name produces an
    identity that is well-formed per BEP-3 but unusable in practice. btsum
    therefore rejects such names with exit code 2 and points at `--name`.

    No normalization and no BOM handling: two names are the same identity
    iff their bytes are equal. NFC, NFD, NFKC, NFKD are distinct. A BOM in
    the name (U+FEFF, `EF BB BF`) is part of the name.
    """
    if not name:
        raise UsageError("name must not be empty")
    if b"\x00" in name:
        raise UsageError("name must not contain NUL bytes")
    if b"/" in name:
        raise UsageError("name must be a single path component (no '/')")
    if name in (b".", b".."):
        raise UsageError("name must not be '.' or '..'")
    try:
        name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(
            "name is not valid UTF-8 (bad byte at offset %d); BitTorrent "
            "clients decode names as UTF-8, so this identity would not be "
            "usable. Rename the file, or pass -n NAME to bind a portable "
            "name." % exc.start
        ) from exc
    return name


def build_identity(path, name):
    """One read pass over `path`; returns an Identity.

    Both hashes interlock: the info dict carries the v1 `pieces` field and
    the v2 `file tree`, so btih and btmh each cover both halves. There is no
    way to compute either hash without hashing the file once for SHA-1 and
    once for SHA-256 -- which is the point.
    """
    validate_name(name)
    if not os.path.isfile(path):
        raise BtsumError("%s: not a regular file" % path)
    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE:
        raise BtsumError("%s: file size %d exceeds the format domain limit (~2 EiB)" % (path, size))
    piece_length = piece_length_for(size)
    per_piece = piece_length // LEAF_SIZE

    stack: dict[int, bytes] = {}  # streaming merkle tiling of leaves read so far
    leaf_count = 0
    piece_sha1s = []  # v1: SHA-1 per piece, raw bytes, no padding
    layer_hashes = []  # v2 piece-layer hashes (piece subtree roots)
    pending_piece_leaves = []
    sha1_cur = None
    sha1_blocks = 0

    with open(path, "rb") as fh:
        while True:
            block = fh.read(LEAF_SIZE)
            if not block:
                break
            # v2 leaf: SHA-256 over the block as-is (last partial leaf included)
            leaf = hashlib.sha256(block).digest()
            # streaming file-root tiling: a binary counter of complete subtrees
            cur = leaf
            lvl = 0
            while (leaf_count >> lvl) & 1:
                left = stack.pop(lvl)
                cur = hashlib.sha256(left + cur).digest()
                lvl += 1
            stack[lvl] = cur
            leaf_count += 1
            # v1 piece bytes and v2 piece-layer accumulation
            pending_piece_leaves.append(leaf)
            if sha1_cur is None:
                sha1_cur = hashlib.sha1()
            sha1_cur.update(block)
            sha1_blocks += 1
            if sha1_blocks == per_piece:
                piece_sha1s.append(sha1_cur.digest())
                layer_hashes.append(_piece_hash(pending_piece_leaves, per_piece))
                pending_piece_leaves = []
                sha1_cur = None
                sha1_blocks = 0
        if sha1_blocks:
            # final partial piece: SHA-1 over the raw remainder; the merkle
            # piece hash pads the span with zero leaves, the data is not padded
            assert sha1_cur is not None
            piece_sha1s.append(sha1_cur.digest())
            layer_hashes.append(_piece_hash(pending_piece_leaves, per_piece))

    # v2 root; BEP-52 defines no pieces root for zero-length files
    file_entry = {b"length": size}
    if leaf_count:
        file_entry[b"pieces root"] = _file_root(leaf_count, stack)

    info = {
        b"file tree": {name: {b"": file_entry}},
        b"length": size,
        b"meta version": META_VERSION,
        b"name": name,
        b"piece length": piece_length,
        b"pieces": b"".join(piece_sha1s),
    }
    info_bytes = bencode(info)

    # top-level piece layers: present iff the file spans more than one piece
    piece_layers = {}
    if size > piece_length:
        piece_layers[file_entry[b"pieces root"]] = b"".join(layer_hashes)

    torrent = bencode({b"info": info, b"piece layers": piece_layers})
    return Identity(
        hashlib.sha1(info_bytes).hexdigest(),
        hashlib.sha256(info_bytes).hexdigest(),
        torrent,
        piece_length,
        name,
        size,
    )


# ---------------------------------------------------------------------------
# Magnet URI (SPEC.md section 7)
# ---------------------------------------------------------------------------


def magnet_for(btih, btmh, name):
    """Canonical dual magnet URI: btih first, then multihash btmh, then dn=.

    btih comes first so v1-only clients that honor only the first `xt`
    still work. Unreserved characters stay literal in dn=; space is %20,
    never '+'.
    """
    return "magnet:?xt=urn:btih:%s&xt=urn:btmh:1220%s&dn=%s" % (
        btih.lower(),
        btmh.lower(),
        _urlquote(name, safe=""),
    )


# ---------------------------------------------------------------------------
# Reference parsing (SPEC.md section 8.1)
# ---------------------------------------------------------------------------

_BTIH_LEN = 20
_BTMH_LEN = 32
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")


def _decode_hash_token(token):
    """One bare hash token -> raw digest bytes, or None.

    40 hex -> v1 (20 B); 64 hex -> v2 (32 B); `1220` + 64 hex -> v2;
    32 base32 chars -> v1. The two hash lengths can never be confused.
    """
    if len(token) == 32:
        try:
            raw = base64.b32decode(token, casefold=True)
        except ValueError:
            return None
        return raw if len(raw) == _BTIH_LEN else None
    if _HEX.match(token):
        if len(token) == 40:
            return bytes.fromhex(token)
        if len(token) == 64:
            return bytes.fromhex(token)
        if len(token) == 68 and token.startswith("1220"):
            return bytes.fromhex(token[4:])
    return None


def _split_sha256sum(text):
    """Split a sha256sum-style line into (hash_part, name_part), or None.

    Accepts both coreutils separators -- `␣␣` (text mode) and `␣*` (binary
    mode) -- and additionally tolerates a stray `*` after a two-space
    separator (`␣␣*name`). The `*` is never part of the bound name.
    """
    idx = text.find(" ")
    if idx < 0:
        return None
    rest = text[idx + 1 :]
    if not rest or rest[0] not in " *":
        return None
    name_part = rest[1:]
    if name_part.startswith("*"):
        name_part = name_part[1:]
    if not name_part:
        return None
    return text[:idx], name_part


def _parse_hash_pair(hash_part):
    """Parse a bare hash token or `A+B` combined reference.

    Returns (v1, v2) with at least one non-None, or None.
    """
    parts = hash_part.split("+")
    if len(parts) == 1:
        raw = _decode_hash_token(parts[0])
        if raw is None:
            return None
        if len(raw) == _BTIH_LEN:
            return (raw, None)
        return (None, raw)
    if len(parts) != 2:
        return None
    a = _decode_hash_token(parts[0])
    b = _decode_hash_token(parts[1])
    if a is None or b is None:
        return None
    if len(a) == len(b):
        return None
    return (a, b) if len(a) == _BTIH_LEN else (b, a)


def _urn_hash(urn, text):
    """`urn:btih:` / `urn:btmh:` value -> (kind, raw bytes). Raises for
    malformed hash values.
    """
    if urn.startswith("urn:btih:"):
        raw = _decode_hash_token(urn[9:])
        if raw is None or len(raw) != _BTIH_LEN:
            raise BtsumError("malformed btih in reference: %s" % text)
        return ("v1", raw)
    if urn.startswith("urn:btmh:"):
        raw = _decode_hash_token(urn[9:])
        if raw is None or len(raw) != _BTMH_LEN:
            raise BtsumError("malformed btmh in reference: %s" % text)
        return ("v2", raw)
    raise BtsumError("unsupported URN in reference: %s" % text)


def parse_hash_ref(text):
    """Parse a hash reference (SPEC.md section 8.1).

    Returns (v1, v2, name): raw hash bytes or None per slot, and the bound
    name as raw bytes or None. Returns None when `text` is not a hash or
    magnet reference at all -- the caller may then treat it as a path to a
    checksum listing. Raises BtsumError for input that looks like a hash or
    magnet but is malformed.

    Accepted shapes, in order of detection:

    * full magnet URI (`magnet:?...`);
    * fragment starting with `xt=` or `urn:` (partial magnet);
    * sha256sum-style line `<hash>␣␣<name>` or `<hash>␣*<name>`, where the
      `*` marker is not part of the name; a stray `*` after `dn=` is also
      stripped;
    * bare hash: 40 hex (v1), 64 hex (v2), `1220` + 64 hex (v2 multihash),
      or 32 base32 chars (v1);
    * combined form `<40hex>+<64hex>` in either order.
    """
    if not text:
        return None

    if text.startswith("magnet:?"):
        body = text[len("magnet:?") :]
    elif text.startswith("urn:") or "xt=" in text:
        body = text  # bare fragment or partial magnet; leading magnet:? optional
    else:
        # sha256sum-style line: <hash>  <name> or <hash> *<name>
        split = _split_sha256sum(text)
        if split is not None:
            pair = _parse_hash_pair(split[0])
            if pair is not None:
                name_bytes = split[1].encode("utf-8", errors="surrogateescape")
                return (pair[0], pair[1], name_bytes)

        combined = text.split("+")
        if len(combined) == 1:
            raw = _decode_hash_token(text)
            if raw is None:
                return None
            return (raw, None, None) if len(raw) == _BTIH_LEN else (None, raw, None)
        if len(combined) != 2:
            raise BtsumError("malformed hash reference: %s" % text)
        a = _decode_hash_token(combined[0])
        b = _decode_hash_token(combined[1])
        if a is None and b is None:
            return None
        if a is None or b is None:
            raise BtsumError("malformed combined reference: %s" % text)
        if len(a) == len(b):
            raise BtsumError("combined reference needs one btih and one btmh: %s" % text)
        v1 = a if len(a) == _BTIH_LEN else b
        v2 = a if len(a) == _BTMH_LEN else b
        return (v1, v2, None)

    v1 = v2 = None
    name = None
    seen_hash = False
    for part in body.split("&"):
        if not part:
            continue
        if part.startswith("dn="):
            name = unquote_to_bytes(part[3:])
            # Tolerate a stray leading `*` (some tools emit `dn=*name`).
            if name.startswith(b"*"):
                name = name[1:]
            continue
        if part.startswith("xt="):
            urn = part[3:]
        elif part.startswith("urn:"):
            urn = part  # bare fragment form
        else:
            continue  # unknown keys (tr=, so=, ...) are ignored
        kind, raw = _urn_hash(urn, text)
        seen_hash = True
        if kind == "v1":
            if v1 is not None and v1 != raw:
                raise BtsumError("conflicting btih values in reference: %s" % text)
            v1 = raw
        else:
            if v2 is not None and v2 != raw:
                raise BtsumError("conflicting btmh values in reference: %s" % text)
            v2 = raw
    if not seen_hash:
        raise BtsumError("reference carries no hash: %s" % text)
    return (v1, v2, name)


# ---------------------------------------------------------------------------
# Check mode (SPEC.md sections 8.1-8.3)
# ---------------------------------------------------------------------------


def _emit(data):
    """Write bytes to stdout unbuffered."""
    sys.stdout.buffer.write(data)


def _check_file(display, path, bound_name, v1, v2, hint):
    """Verify one file against (v1, v2); print the result line; return 0 or 1.

    Raises BtsumError (exit 2) on I/O or domain failures.
    """
    ident = build_identity(path, bound_name)
    compared = []
    ok = True
    if v1 is not None:
        compared.append("v1")
        ok = ok and ident.btih == v1.hex()
    if v2 is not None:
        compared.append("v2")
        ok = ok and ident.btmh == v2.hex()
    label = "+".join(compared)
    if ok:
        _emit(display + b": OK (" + label.encode("ascii") + b")\n")
        return 0
    _emit(display + b": FAILED (" + label.encode("ascii") + b" mismatch)\n")
    if hint:
        sys.stderr.write(
            "%s: note: the reference carries no file name and the name is part "
            "of the identity; check against the original magnet or pass "
            "--name NAME to rebind\n" % PROG
        )
    return 1


def _check_listing(listing_path, name_override):
    """Verify every reference line of a checksum listing.

    Accepts magnets, bare hashes, combined pairs and sha256sum-style lines.
    `#` comments and blank lines are ignored. Every entry must carry a name
    (`dn=` or sha256sum-style trailing name).
    """
    with open(listing_path, "rb") as fh:
        data = fh.read()
    override = None if name_override is None else os.fsencode(name_override)
    status = 0
    for lineno, raw in enumerate(data.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(b"#"):
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BtsumError(
                "%s: line %d: not valid UTF-8 (bad byte at offset %d)"
                % (listing_path, lineno, exc.start)
            ) from exc
        parsed = parse_hash_ref(text)
        if parsed is None:
            raise BtsumError(
                "%s: line %d: not a magnet or hash line: %s" % (listing_path, lineno, text)
            )
        v1, v2, name = parsed
        if name is None:
            raise BtsumError(
                "%s: line %d: listing entry carries no name "
                "(no dn= and no sha256sum-style trailing name)" % (listing_path, lineno)
            )
        bound = override if override is not None else name
        status = max(status, _check_file(name, name, bound, v1, v2, False))
    return status


def _run_check(args):
    """Handle `-c` (check) mode."""
    if args.torrent is not None:
        raise UsageError("-c cannot be combined with -t/--torrent")
    if args.v1 or args.v2 or args.hashes or args.pair:
        raise UsageError("-c cannot be combined with -1/-2/--hashes/--pair")

    parsed = parse_hash_ref(args.check)

    if parsed is None:
        # not a hash: the reference is a path to a checksum listing
        if args.files:
            raise UsageError("extra operand after checksum listing: %s" % args.files[0])
        return _check_listing(args.check, args.name)
    v1, v2, name = parsed

    if not args.files:
        raise UsageError("-c needs at least one FILE to check")

    status = 0
    hint = name is None and args.name is None
    for path in args.files:
        display = os.fsencode(path)
        if args.name is not None:
            bound = os.fsencode(args.name)
        elif name is not None:
            bound = name  # dn= / sha256sum name rebinds renamed copies
        else:
            bound = os.fsencode(os.path.basename(path))
        status = max(status, _check_file(display, path, bound, v1, v2, hint))
    return status


# ---------------------------------------------------------------------------
# Hash mode and command line (SPEC.md section 8)
# ---------------------------------------------------------------------------


def _run_hash(args):
    """Handle the default hash mode."""
    files = list(args.files)
    explicit_torrent = None
    if args.torrent:
        # a PATH value rides on -t/--torrent
        if not files:
            # "-t PATH" with no other operand: PATH is the file, auto path
            files = [args.torrent]
        elif len(files) == 1:
            explicit_torrent = args.torrent
        else:
            raise UsageError("--torrent PATH is only valid with a single FILE")
    if args.name is not None and len(files) > 1:
        raise UsageError("--name is only valid with a single FILE")
    if not files:
        raise UsageError("no input files")

    for path in files:
        if args.name is not None:
            name = os.fsencode(args.name)
        else:
            name = os.fsencode(os.path.basename(path))
        ident = build_identity(path, name)
        if args.v1:
            _emit(ident.btih.encode("ascii") + b"  " + name + b"\n")
        elif args.v2:
            _emit(ident.btmh.encode("ascii") + b"  " + name + b"\n")
        elif args.hashes:
            _emit(ident.btih.encode("ascii") + b"  " + name + b"\n")
            _emit(ident.btmh.encode("ascii") + b"  " + name + b"\n")
        elif args.pair:
            _emit(
                ident.btih.encode("ascii")
                + b"+"
                + ident.btmh.encode("ascii")
                + b"  "
                + name
                + b"\n"
            )
        else:
            _emit(magnet_for(ident.btih, ident.btmh, name).encode("ascii") + b"\n")
        if args.torrent is not None:
            torrent_path = explicit_torrent if explicit_torrent is not None else path + ".torrent"
            with open(torrent_path, "wb") as tfh:
                tfh.write(ident.torrent)
    return 0


def _build_parser():
    """Construct the argparse parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Deterministic BitTorrent identity for single files.",
    )
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, __version__))
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-1",
        "--v1",
        dest="v1",
        action="store_true",
        help="print only the v1 infohash: <40 hex>  <name>",
    )
    output.add_argument(
        "-2",
        "--v2",
        dest="v2",
        action="store_true",
        help="print only the v2 merkle hash: <64 hex>  <name>",
    )
    output.add_argument(
        "--hashes",
        dest="hashes",
        action="store_true",
        help="print both hash lines, v1 first (md5sum style)",
    )
    output.add_argument(
        "--pair",
        dest="pair",
        action="store_true",
        help="print v1 and v2 on one line: <40 hex>+<64 hex>  <name>",
    )
    parser.add_argument(
        "-n",
        "--name",
        dest="name",
        metavar="NAME",
        default=None,
        help="bind NAME instead of the file's basename (single FILE)",
    )
    parser.add_argument(
        "-t",
        "--torrent",
        dest="torrent",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="also write the canonical .torrent (default path: the input file plus .torrent)",
    )
    parser.add_argument(
        "-c",
        "--check",
        dest="check",
        metavar="REF",
        default=None,
        help="check FILE(s) against a hash, magnet, sha256sum-style line, "
        "or checksum listing instead of hashing",
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        nargs="*",
        help="files to hash (or to check with -c)",
    )
    return parser


def main(argv=None):
    """CLI entry point; returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_intermixed_args(argv)
    try:
        if args.check is not None:
            return _run_check(args)
        return _run_hash(args)
    except BtsumError as exc:
        sys.stderr.write("%s: error: %s\n" % (PROG, exc))
        return 2
    except OSError as exc:
        sys.stderr.write("%s: error: %s\n" % (PROG, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
