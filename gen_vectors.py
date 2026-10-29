#!/usr/bin/env python3
"""Generate golden test vectors for btsum (deterministic inputs only)."""

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btsum  # noqa: E402


def main():
    """Generate golden test vectors for btsum (deterministic inputs only)."""
    vectors = [
        ("hello.txt", b"hello btsum\n"),
        ("empty.bin", b""),
        (
            "three-leaves.bin",
            b"btsum three leaves vector\n" * 1600,
        ),  # 41600 B -> 3 leaves, 3 pieces of 16 KiB
        ("split-piece.bin", b"A" * 16384 + b"B"),  # 16385 B -> 2 pieces, last 1 byte
        ("two-pieces.bin", b"B" * 32768),  # 2 exact 16 KiB pieces
    ]
    tmp = tempfile.mkdtemp(prefix="btsum-vectors-")
    for name, data in vectors:
        path = os.path.join(tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        ident = btsum.build_identity(path, name.encode("utf-8"))
        print("=== %s" % name)
        print("size         : %d" % ident.size)
        print("piece_length : %d" % ident.piece_length)
        print("btih         : %s" % ident.btih)
        print("btmh         : %s" % ident.btmh)
        print("torrent_sha  : %s" % hashlib.sha256(ident.torrent).hexdigest())
        print("torrent_len  : %d" % len(ident.torrent))
        print("magnet       : %s" % btsum.magnet_for(ident.btih, ident.btmh, ident.name))
        if name == "hello.txt":
            print("torrent_hex  : %s" % ident.torrent.hex())
    print("=== tmpdir: %s" % tmp)


if __name__ == "__main__":
    main()
