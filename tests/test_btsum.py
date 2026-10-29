"""btsum test suite (stdlib unittest)."""

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import btsum  # noqa: E402

BTSUM = os.path.join(ROOT, "btsum.py")

# --- golden vectors (SPEC.md section 11) -----------------------------------

V = {
    "hello.txt": dict(
        data=b"hello btsum\n",
        btih="7fe09bdbcee8c4d588e6f4193973d7cdd12d380f",
        btmh="fcd26dfd1a8902f1832ad1613e82fac6d80e25eb2a59335c3d7ad46dab0dd633",
        torrent_sha="eb975502da8a14ceb51eaeffd3e15d68d8d10f00e7a5409c30e79595d867b368",
        torrent_len=218,
    ),
    "empty.bin": dict(
        data=b"",
        btih="a2cbc9d1534378ef64d1e0619d738b2f3d2b7dd4",
        btmh="882b4b77461bf690ee114e776aa263b6850ea6caf0cfae73a7437e64da8fa8b6",
        torrent_sha="b7319ccffba6395c58df45dbe6891dfa7a70fdce60cf6232d8d893fc8cd1fe19",
        torrent_len=146,
    ),
    "three-leaves.bin": dict(
        data=b"btsum three leaves vector\n" * 1600,
        btih="92166ef8a8d5548f51204bbd71b796c9ff0ec791",
        btmh="8fcfeece6d1c589db95e820b3268935dee1733bdfaed76778c3dbdf081409992",
        torrent_sha="96c5ff0703e244890c0d05861687bb910e9ab9a9e3b84fd5f2ccd198e26b1729",
        torrent_len=414,
    ),
    "split-piece.bin": dict(
        data=b"A" * 16384 + b"B",
        btih="5572e2e889ddd2ea2d0478ad1bfe3e2b42e96385",
        btmh="9887734327d915d4ee26ab34d2ab6dac1742e51f78f9255529ae81b07cf33492",
        torrent_sha="61ed02904fb759053c9db5fa6745673ac7b76343f2b9adcb0a5cc122fbd324cf",
        torrent_len=360,
    ),
    "two-pieces.bin": dict(
        data=b"B" * 32768,
        btih="5e539dbb67a2c44270beef40033616b20cebd6f2",
        btmh="3198f5a6ad31d5289fbf02d440faddff7ce3b1f5b5f5616b9300b51afb854b5b",
        torrent_sha="e76da1971e939d6ad785625b143b97f9849435852cc3af9d9c6b6d879fea8775",
        torrent_len=358,
    ),
}


def run_cli(*args):
    proc = subprocess.run([sys.executable, BTSUM] + list(args), capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def make_file(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --- bencode ----------------------------------------------------------------


class TestBencode(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(btsum.bencode(0), b"i0e")
        self.assertEqual(btsum.bencode(-3), b"i-3e")
        self.assertEqual(btsum.bencode(1024), b"i1024e")
        self.assertEqual(btsum.bencode(b""), b"0:")
        self.assertEqual(btsum.bencode(b"spam"), b"4:spam")

    def test_containers(self):
        self.assertEqual(btsum.bencode([b"a", 1]), b"l1:ai1ee")
        self.assertEqual(btsum.bencode({b"cow": b"moo"}), b"d3:cow3:mooe")

    def test_dict_keys_sort_as_raw_bytes(self):
        # "piece layers" < "piece length" < "pieces" (" " = 0x20 < "s")
        enc = btsum.bencode({b"pieces": b"x", b"piece length": 1, b"piece layers": {}})
        self.assertEqual(enc, b"d12:piece layersde12:piece lengthi1e6:pieces1:xe")

    def test_rejects_bad_types(self):
        with self.assertRaises(TypeError):
            btsum.bencode(True)
        with self.assertRaises(TypeError):
            btsum.bencode("str not bytes")
        with self.assertRaises(TypeError):
            btsum.bencode({b"a": 1, "a": 2})
        with self.assertRaises(TypeError):
            btsum.bencode(1.5)

    def test_duplicate_keys_rejected(self):
        class DuplicateItems(dict):
            def items(self):
                return [(b"a", 1), (b"a", 2)]

        with self.assertRaises(ValueError):
            btsum.bencode(DuplicateItems())


# --- ladder -----------------------------------------------------------------


class TestLadder(unittest.TestCase):
    KIB = 1024
    MIB = 1024 * KIB
    GIB = 1024 * MIB
    TIB = 1024 * GIB

    # One row per transition of the ladder (SPEC.md section 4):
    # (size at the boundary, piece length there, piece length at +1 byte).
    #
    # At every boundary S = 1024 * p_old, the old piece length still applies
    # with exactly 1024 pieces. At S + 1 the ladder doubles the piece length,
    # dropping the count to 513 (which then climbs back to 1024 by the next
    # boundary).
    TRANSITIONS = [
        (16 * MIB, 16 * KIB, 32 * KIB),
        (32 * MIB, 32 * KIB, 64 * KIB),
        (64 * MIB, 64 * KIB, 128 * KIB),
        (128 * MIB, 128 * KIB, 256 * KIB),
        (256 * MIB, 256 * KIB, 512 * KIB),
        (512 * MIB, 512 * KIB, MIB),
        (GIB, MIB, 2 * MIB),
        (2 * GIB, 2 * MIB, 4 * MIB),
        (4 * GIB, 4 * MIB, 8 * MIB),
        (8 * GIB, 8 * MIB, 16 * MIB),
        (16 * GIB, 16 * MIB, 32 * MIB),
        (32 * GIB, 32 * MIB, 64 * MIB),
        (64 * GIB, 64 * MIB, 128 * MIB),
        (128 * GIB, 128 * MIB, 256 * MIB),
        (256 * GIB, 256 * MIB, 512 * MIB),
        (512 * GIB, 512 * MIB, GIB),
    ]

    def test_small_sizes_use_minimum_piece_length(self):
        for size in (0, 1, 16 * self.KIB, 16 * self.MIB - 1):
            self.assertEqual(
                btsum.piece_length_for(size),
                16 * self.KIB,
                "size=%d" % size,
            )
        self.assertEqual(btsum.piece_count_for(0, 16 * self.KIB), 0)

    def test_every_boundary_transitions_cleanly(self):
        for size, p_at, p_above in self.TRANSITIONS:
            self.assertEqual(
                btsum.piece_length_for(size),
                p_at,
                "size=%d: piece length at boundary" % size,
            )
            self.assertEqual(
                btsum.piece_count_for(size, p_at),
                1024,
                "size=%d: piece count at boundary" % size,
            )
            self.assertEqual(
                btsum.piece_length_for(size + 1),
                p_above,
                "size=%d: piece length one byte past boundary" % (size + 1),
            )
            self.assertEqual(
                btsum.piece_count_for(size + 1, p_above),
                513,
                "size=%d: piece count one byte past boundary" % (size + 1),
            )

    def test_transitions_are_monotonic(self):
        prev = 0
        for size, _, _ in self.TRANSITIONS:
            p = btsum.piece_length_for(size)
            self.assertGreater(p, prev, "size=%d" % size)
            prev = p

    def test_cap_holds_beyond_last_transition(self):
        # 512 GiB + 1 is where the last transition happens; past it the
        # piece length is 1 GiB forever.
        for size in (
            512 * self.GIB + 1,
            self.TIB,
            self.TIB + 1,
            2 * self.TIB,
            100 * self.TIB,
            1024 * self.TIB,
        ):
            self.assertEqual(
                btsum.piece_length_for(size),
                self.GIB,
                "size=%d (cap)" % size,
            )
        self.assertEqual(btsum.piece_count_for(self.TIB, self.GIB), 1024)
        self.assertEqual(btsum.piece_count_for(self.TIB + 1, self.GIB), 1025)
        self.assertEqual(btsum.piece_count_for(1024 * self.TIB, self.GIB), 1 << 20)

    def test_domain_limit(self):
        self.assertEqual(
            btsum.piece_length_for(btsum.MAX_FILE_SIZE),
            btsum.MAX_PIECE_SIZE,
        )
        self.assertEqual(
            btsum.piece_count_for(btsum.MAX_FILE_SIZE, btsum.MAX_PIECE_SIZE),
            btsum.MAX_PIECES,
        )
        with self.assertRaises(btsum.BtsumError):
            btsum.piece_length_for(btsum.MAX_FILE_SIZE + 1)

    def test_always_power_of_two_within_bounds(self):
        for exp in range(0, 50):
            size = (1 << exp) + (1 << exp) // 3 + 7
            p = btsum.piece_length_for(size)
            self.assertTrue(
                (p & (p - 1)) == 0 and 16384 <= p <= 1 << 30,
                "size=%d: p=%d" % (size, p),
            )


# --- merkle -----------------------------------------------------------------


class TestMerkle(unittest.TestCase):
    @staticmethod
    def _streaming_stack(n):
        """Independent re-implementation of the streaming counter."""
        stack = {}
        for count in range(n):
            leaf = hashlib.sha256(str(count).encode("ascii")).digest()
            cur = leaf
            lvl = 0
            while (count >> lvl) & 1:
                left = stack.pop(lvl)
                cur = hashlib.sha256(left + cur).digest()
                lvl += 1
            stack[lvl] = cur
        return stack

    def test_file_root_matches_brute_force(self):
        for n in range(1, 41):
            leaves = [hashlib.sha256(str(i).encode("ascii")).digest() for i in range(n)]
            total = btsum._pow2_at_least(n)
            layer = leaves + [btsum.ZERO_HASH] * (total - n)
            while len(layer) > 1:
                layer = [
                    hashlib.sha256(layer[i] + layer[i + 1]).digest()
                    for i in range(0, len(layer), 2)
                ]
            expected = layer[0]
            got = btsum._file_root(n, self._streaming_stack(n))
            self.assertEqual(got, expected, "leaf count %d" % n)

    def test_piece_hash_partial_span(self):
        h = hashlib.sha256(b"x").digest()
        z0 = btsum.ZERO_HASH
        z1 = hashlib.sha256(z0 + z0).digest()
        expected = hashlib.sha256(hashlib.sha256(h + z0).digest() + z1).digest()
        self.assertEqual(btsum._piece_hash([h], 4), expected)

    def test_piece_hash_full_span_single_leaf(self):
        h = hashlib.sha256(b"x").digest()
        self.assertEqual(btsum._piece_hash([h], 1), h)

    def test_zero_subtree(self):
        z0 = btsum.ZERO_HASH
        z1 = hashlib.sha256(z0 + z0).digest()
        z2 = hashlib.sha256(z1 + z1).digest()
        self.assertEqual(btsum._zero_subtree(0), z0)
        self.assertEqual(btsum._zero_subtree(1), z1)
        self.assertEqual(btsum._zero_subtree(2), z2)


# --- identity ---------------------------------------------------------------


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="btsum-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def ident(self, name, data=None, key=None):
        key = key or name
        path = make_file(self.tmp, name, data if data is not None else V[key]["data"])
        return btsum.build_identity(path, name.encode("utf-8"))

    def test_golden_vectors(self):
        for name, vec in V.items():
            ident = self.ident(name)
            self.assertEqual(ident.btih, vec["btih"], name)
            self.assertEqual(ident.btmh, vec["btmh"], name)
            self.assertEqual(hashlib.sha256(ident.torrent).hexdigest(), vec["torrent_sha"], name)
            self.assertEqual(len(ident.torrent), vec["torrent_len"], name)

    def test_determinism(self):
        a = self.ident("hello.txt")
        b = self.ident("hello.txt")
        self.assertEqual(a.torrent, b.torrent)
        self.assertEqual(a.btih, b.btih)
        self.assertEqual(a.btmh, b.btmh)

    def test_name_is_bound(self):
        a = self.ident("hello.txt")
        b = self.ident("other.txt", data=V["hello.txt"]["data"])
        self.assertNotEqual(a.btih, b.btih)
        self.assertNotEqual(a.btmh, b.btmh)

    def test_single_leaf_root_is_content_hash(self):
        ident = self.ident("hello.txt")
        root = hashlib.sha256(b"hello btsum\n").digest()
        self.assertIn(b"11:pieces root32:" + root, ident.torrent)

    def test_structure_hello(self):
        torrent = self.ident("hello.txt").torrent
        self.assertTrue(torrent.startswith(b"d4:info"))
        self.assertIn(b"9:file treed9:hello.txtd0:", torrent)
        self.assertIn(b"12:meta versioni2e", torrent)
        self.assertIn(b"6:pieces20:", torrent)
        self.assertTrue(torrent.endswith(b"12:piece layersdee"))

    def test_structure_split_piece(self):
        torrent = self.ident("split-piece.bin").torrent
        self.assertIn(b"6:pieces40:", torrent)  # two v1 pieces
        self.assertIn(b"12:piece layersd", torrent)
        self.assertNotIn(b"12:piece layersde", torrent)
        # piece layers value: 2 x 32 bytes (16 KiB pieces -> leaf hashes)
        self.assertIn(b"12:piece layersd32:", torrent)

    def test_structure_empty(self):
        torrent = self.ident("empty.bin").torrent
        self.assertIn(b"6:pieces0:", torrent)
        self.assertNotIn(b"11:pieces root", torrent)
        self.assertIn(b"d6:lengthi0e", torrent)
        self.assertTrue(torrent.endswith(b"12:piece layersdee"))

    def test_v1_piece_hashes_are_raw(self):
        data = V["split-piece.bin"]["data"]
        torrent = self.ident("split-piece.bin").torrent
        expect = hashlib.sha1(data[:16384]).digest() + hashlib.sha1(data[16384:]).digest()
        self.assertIn(b"6:pieces40:" + expect, torrent)

    def test_piece_length_selection(self):
        self.assertEqual(self.ident("hello.txt").piece_length, 16384)
        big = make_file(self.tmp, "big.bin", b"x" * (16 * 1024 * 1024 + 1))
        ident = btsum.build_identity(big, b"big.bin")
        self.assertEqual(ident.piece_length, 32768)


# --- magnet -----------------------------------------------------------------


class TestMagnet(unittest.TestCase):
    def test_format(self):
        uri = btsum.magnet_for("a" * 40, "b" * 64, b"file.bin")
        self.assertEqual(
            uri,
            "magnet:?xt=urn:btih:" + "a" * 40 + "&xt=urn:btmh:1220" + "b" * 64 + "&dn=file.bin",
        )

    def test_btih_first_and_lowercase(self):
        uri = btsum.magnet_for("A" * 40, "B" * 64, b"f")
        self.assertIn("urn:btih:" + "a" * 40, uri)
        self.assertIn("urn:btmh:1220" + "b" * 64, uri)
        self.assertLess(uri.index("urn:btih"), uri.index("urn:btmh"))

    def test_dn_percent_encoding(self):
        uri = btsum.magnet_for("a" * 40, "b" * 64, b"ren amed.bin")
        self.assertIn("dn=ren%20amed.bin", uri)
        uri = btsum.magnet_for("a" * 40, "b" * 64, b"\xc3\xa9cho.bin")
        self.assertIn("dn=%C3%A9cho.bin", uri)
        self.assertNotIn("+", uri)


# --- name validation --------------------------------------------------------


class TestNameValidation(unittest.TestCase):
    def test_bad_names(self):
        for bad in (b"", b"a/b", b"a\x00b", b".", b"..", b"\xff\xfe"):
            with self.assertRaises(btsum.UsageError):
                btsum.validate_name(bad)

    def test_good_names_unchanged(self):
        self.assertEqual(btsum.validate_name(b"file.bin"), b"file.bin")
        self.assertEqual(btsum.validate_name(b"\xc3\xa9cho"), b"\xc3\xa9cho")

    def test_valid_utf8_names_accepted(self):
        nfc = b"caf\xc3\xa9.bin"
        nfd = b"cafe\xcc\x81.bin"
        self.assertEqual(btsum.validate_name(nfc), nfc)
        self.assertEqual(btsum.validate_name(nfd), nfd)
        self.assertNotEqual(nfc, nfd)

    def test_invalid_utf8_name_rejected(self):
        for bad in (b"\xff", b"a\xc3", b"\xc3\x28", b"\xed\xa0\x80"):
            with self.assertRaises(btsum.UsageError):
                btsum.validate_name(bad)

    def test_bom_is_not_special(self):
        bom_name = b"\xef\xbb\xbfhello.bin"
        self.assertEqual(btsum.validate_name(bom_name), bom_name)


# --- reference parser -------------------------------------------------------


class TestRefParser(unittest.TestCase):
    H1 = "ab" * 20
    H2 = "cd" * 32

    def test_bare_hashes(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertIsNone(v2)
        self.assertIsNone(name)
        v1, v2, name = btsum.parse_hash_ref(self.H2)
        self.assertIsNone(v1)
        self.assertEqual(v2, bytes.fromhex(self.H2))

    def test_multihash_form(self):
        v1, v2, name = btsum.parse_hash_ref("1220" + self.H2)
        self.assertIsNone(v1)
        self.assertEqual(v2, bytes.fromhex(self.H2))

    def test_base32(self):
        raw = bytes(range(20))
        v1, v2, name = btsum.parse_hash_ref(base64.b32encode(raw).decode())
        self.assertEqual(v1, raw)

    def test_combined(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "+" + self.H2)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(v2, bytes.fromhex(self.H2))
        v1, v2, name = btsum.parse_hash_ref(self.H2 + "+" + self.H1)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(v2, bytes.fromhex(self.H2))

    def test_combined_malformed(self):
        with self.assertRaises(btsum.BtsumError):
            btsum.parse_hash_ref(self.H1 + "+" + self.H1)

    def test_magnet_full_and_partial(self):
        ref = "magnet:?xt=urn:btih:%s&xt=urn:btmh:1220%s&dn=hello%%20world.bin" % (self.H1, self.H2)
        v1, v2, name = btsum.parse_hash_ref(ref)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(v2, bytes.fromhex(self.H2))
        self.assertEqual(name, b"hello world.bin")
        partial = "xt=urn:btih:%s&dn=x.bin" % self.H1
        v1, v2, name = btsum.parse_hash_ref(partial)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(name, b"x.bin")

    def test_magnet_unknown_params_ignored(self):
        ref = "magnet:?tr=udp%%3A%%2F%%2Fexample&xt=urn:btih:%s&dn=y.bin&so=0" % self.H1
        v1, v2, name = btsum.parse_hash_ref(ref)
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(name, b"y.bin")

    def test_dn_with_star_prefix_ignored(self):
        v1, v2, name = btsum.parse_hash_ref("magnet:?xt=urn:btih:%s&dn=*hello.txt" % self.H1)
        self.assertEqual(name, b"hello.txt")
        v1, v2, name = btsum.parse_hash_ref("magnet:?xt=urn:btih:%s&dn=%%2Ahello.txt" % self.H1)
        self.assertEqual(name, b"hello.txt")

    def test_sha256sum_text_form(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "  hello.txt")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertIsNone(v2)
        self.assertEqual(name, b"hello.txt")

    def test_sha256sum_binary_form(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + " *hello.txt")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(name, b"hello.txt")

    def test_sha256sum_double_space_star_tolerated(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "  *hello.txt")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(name, b"hello.txt")

    def test_sha256sum_v2_form(self):
        v1, v2, name = btsum.parse_hash_ref(self.H2 + " *hello.txt")
        self.assertIsNone(v1)
        self.assertEqual(v2, bytes.fromhex(self.H2))
        self.assertEqual(name, b"hello.txt")

    def test_sha256sum_combined_form(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "+" + self.H2 + "  hello.txt")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(v2, bytes.fromhex(self.H2))
        self.assertEqual(name, b"hello.txt")
        v1, v2, name = btsum.parse_hash_ref(self.H2 + "+" + self.H1 + " *hello.txt")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(v2, bytes.fromhex(self.H2))

    def test_sha256sum_name_with_plus(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "  file+name.bin")
        self.assertEqual(v1, bytes.fromhex(self.H1))
        self.assertEqual(name, b"file+name.bin")

    def test_sha256sum_name_with_internal_spaces(self):
        v1, v2, name = btsum.parse_hash_ref(self.H1 + "  file  name.bin")
        self.assertEqual(name, b"file  name.bin")

    def test_malformed(self):
        with self.assertRaises(btsum.BtsumError):
            btsum.parse_hash_ref("urn:btih:zzzz")
        with self.assertRaises(btsum.BtsumError):
            btsum.parse_hash_ref("magnet:?dn=only-name")
        with self.assertRaises(btsum.BtsumError):
            btsum.parse_hash_ref("magnet:?xt=urn:unknown:abcd")

    def test_non_hash_returns_none(self):
        self.assertIsNone(btsum.parse_hash_ref(""))
        self.assertIsNone(btsum.parse_hash_ref("sums.magnets"))
        self.assertIsNone(btsum.parse_hash_ref("/etc/hosts"))


# --- CLI --------------------------------------------------------------------


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="btsum-cli-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old_cwd)

    def write(self, name, data):
        return make_file(self.tmp, name, data)

    def test_version_and_help(self):
        rc, out, err = run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn(("btsum " + btsum.__version__).encode(), out)
        rc, out, err = run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn(b"usage:", out)

    def test_no_args_is_usage_error(self):
        rc, out, err = run_cli()
        self.assertEqual(rc, 2)

    def test_default_magnet_output(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("hello.txt")
        self.assertEqual(rc, 0)
        expect = "magnet:?xt=urn:btih:%s&xt=urn:btmh:1220%s&dn=hello.txt\n" % (
            V["hello.txt"]["btih"],
            V["hello.txt"]["btmh"],
        )
        self.assertEqual(out, expect.encode())

    def test_output_filters(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-1", "hello.txt")
        self.assertEqual(out, (V["hello.txt"]["btih"] + "  hello.txt\n").encode())
        rc, out, err = run_cli("--v2", "hello.txt")
        self.assertEqual(out, (V["hello.txt"]["btmh"] + "  hello.txt\n").encode())
        rc, out, err = run_cli("--hashes", "hello.txt")
        lines = out.decode().splitlines()
        self.assertEqual(
            lines,
            [
                V["hello.txt"]["btih"] + "  hello.txt",
                V["hello.txt"]["btmh"] + "  hello.txt",
            ],
        )
        rc, out, err = run_cli("--pair", "hello.txt")
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            (V["hello.txt"]["btih"] + "+" + V["hello.txt"]["btmh"] + "  hello.txt\n").encode(),
        )
        # mutually exclusive
        for combo in (
            ("-1", "-2"),
            ("-1", "--hashes"),
            ("-1", "--pair"),
            ("-2", "--pair"),
            ("--hashes", "--pair"),
        ):
            rc, out, err = run_cli(*combo, "hello.txt")
            self.assertEqual(rc, 2, "combo: %r" % (combo,))

    def test_pair_with_name_override(self):
        self.write("renamed.bin", V["hello.txt"]["data"])
        rc, out, err = run_cli("--pair", "-n", "hello.txt", "renamed.bin")
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            (V["hello.txt"]["btih"] + "+" + V["hello.txt"]["btmh"] + "  hello.txt\n").encode(),
        )

    def test_pair_roundtrips_as_check_ref(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("--pair", "hello.txt")
        self.assertEqual(rc, 0)
        ref = out.decode().strip()  # "7fe0…380f+fcd2…d633  hello.txt"
        rc, out2, err2 = run_cli("-c", ref, "hello.txt")
        self.assertEqual(rc, 0)
        self.assertIn(b": OK (v1+v2)", out2)

    def test_check_rejects_pair_flag(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-c", "0" * 40, "--pair", "hello.txt")
        self.assertEqual(rc, 2)

    def test_multiple_files(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        self.write("empty.bin", V["empty.bin"]["data"])
        rc, out, err = run_cli("hello.txt", "empty.bin")
        lines = out.decode().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("dn=hello.txt", lines[0])
        self.assertIn("dn=empty.bin", lines[1])

    def test_name_override_matches_original(self):
        data = V["hello.txt"]["data"]
        self.write("hello.txt", data)
        self.write("renamed-copy.bin", data)
        rc, orig, _ = run_cli("hello.txt")
        rc, renamed, _ = run_cli("-n", "hello.txt", "renamed-copy.bin")
        self.assertEqual(orig, renamed)
        self.assertIn(b"dn=hello.txt", renamed)

    def test_name_override_validation(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-n", "", "hello.txt")
        self.assertEqual(rc, 2)
        rc, out, err = run_cli("-n", "a/b", "hello.txt")
        self.assertEqual(rc, 2)
        rc, out, err = run_cli("-n", "x", "hello.txt", "split.tmp.bin")
        self.assertEqual(rc, 2)

    def test_torrent_output(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-t", "hello.txt")
        self.assertEqual(rc, 0)
        with open(os.path.join(self.tmp, "hello.txt.torrent"), "rb") as fh:
            data = fh.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), V["hello.txt"]["torrent_sha"])
        # explicit path, equals form
        rc, out, err = run_cli("--torrent=explicit.torrent", "hello.txt")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "explicit.torrent")))
        # multi-file with explicit path is rejected
        self.write("two-pieces.bin", V["two-pieces.bin"]["data"])
        rc, out, err = run_cli("--torrent=x.torrent", "hello.txt", "two-pieces.bin")
        self.assertEqual(rc, 2)

    def test_torrent_output_name_and_path(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        expected = V["hello.txt"]["torrent_sha"]

        # bare name (space form) — writes to cwd
        rc, _, _ = run_cli("-t", "named.torrent", "hello.txt")
        self.assertEqual(rc, 0)
        p = os.path.join(self.tmp, "named.torrent")
        self.assertTrue(os.path.exists(p))
        with open(p, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), expected)

        # bare name (equals form)
        rc, _, _ = run_cli("--torrent=eq.torrent", "hello.txt")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "eq.torrent")))

        # relative path with separator
        os.makedirs(os.path.join(self.tmp, "sub"))
        rc, _, _ = run_cli("-t", os.path.join("sub", "in.torrent"), "hello.txt")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "sub", "in.torrent")))

        # absolute path
        abs_out = os.path.join(self.tmp, "abs.torrent")
        rc, _, _ = run_cli("-t", abs_out, "hello.txt")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(abs_out))

        # flag after positional also works
        rc, _, _ = run_cli("hello.txt", "-t", "after.torrent")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "after.torrent")))

    def test_torrent_value_with_no_positional_is_input(self):
        # `-t VALUE` with no positional treats VALUE as the input file.
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, _, _ = run_cli("-t", "hello.txt")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "hello.txt.torrent")))

    def test_check_inline_ok(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        good_v1 = V["hello.txt"]["btih"]
        good_v2 = V["hello.txt"]["btmh"]
        for ref in (
            good_v1,
            good_v2,
            "1220" + good_v2,
            good_v1 + "+" + good_v2,
            "magnet:?xt=urn:btih:%s&xt=urn:btmh:1220%s&dn=hello.txt" % (good_v1, good_v2),
            "xt=urn:btih:%s&dn=hello.txt" % good_v1,
            good_v1 + "  hello.txt",
            good_v1 + " *hello.txt",
            good_v1 + "+" + good_v2 + "  hello.txt",
        ):
            rc, out, err = run_cli("-c", ref, "hello.txt")
            self.assertEqual(rc, 0, ref)
            self.assertIn(b": OK (", out)

    def test_check_combined_labels(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli(
            "-c", V["hello.txt"]["btih"] + "+" + V["hello.txt"]["btmh"], "hello.txt"
        )
        self.assertEqual(rc, 0)
        self.assertIn(b": OK (v1+v2)", out)
        rc, out, err = run_cli("-c", V["hello.txt"]["btih"], "hello.txt")
        self.assertIn(b": OK (v1)", out)
        rc, out, err = run_cli("-c", V["hello.txt"]["btmh"], "hello.txt")
        self.assertIn(b": OK (v2)", out)

    def test_check_mismatch_exit_1(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-c", "0" * 40, "hello.txt")
        self.assertEqual(rc, 1)
        self.assertIn(b": FAILED (v1 mismatch)", out)
        self.assertIn(b"--name", err)
        rc, out, err = run_cli("-c", "0" * 40 + "+" + "0" * 64, "hello.txt")
        self.assertEqual(rc, 1)
        self.assertIn(b": FAILED (v1+v2 mismatch)", out)

    def test_check_renamed_file_via_dn(self):
        data = V["hello.txt"]["data"]
        self.write("renamed.iso", data)
        ref = "magnet:?xt=urn:btih:%s&xt=urn:btmh:1220%s&dn=hello.txt" % (
            V["hello.txt"]["btih"],
            V["hello.txt"]["btmh"],
        )
        rc, out, err = run_cli("-c", ref, "renamed.iso")
        self.assertEqual(rc, 0)
        self.assertIn(b"renamed.iso: OK (v1+v2)", out)

    def test_check_renamed_file_via_sha256sum(self):
        data = V["hello.txt"]["data"]
        self.write("renamed.iso", data)
        ref = V["hello.txt"]["btih"] + "  hello.txt"
        rc, out, err = run_cli("-c", ref, "renamed.iso")
        self.assertEqual(rc, 0)
        self.assertIn(b"renamed.iso: OK (v1)", out)

    def test_check_renamed_file_via_name_flag(self):
        data = V["hello.txt"]["data"]
        self.write("renamed.iso", data)
        rc, out, err = run_cli("-n", "hello.txt", "-c", V["hello.txt"]["btih"], "renamed.iso")
        self.assertEqual(rc, 0)
        self.assertIn(b": OK (v1)", out)

    def test_check_missing_file_exit_2(self):
        rc, out, err = run_cli("-c", "0" * 40, "no-such-file.bin")
        self.assertEqual(rc, 2)

    def test_check_batch(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        self.write("empty.bin", V["empty.bin"]["data"])
        rc, out, err = run_cli("hello.txt", "empty.bin")
        listing = out
        with open("sums.magnets", "wb") as fh:
            fh.write(listing)
        rc, out, err = run_cli("-c", "sums.magnets")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count(b": OK ("), 2)
        with open("hello.txt", "wb") as fh:
            fh.write(b"corrupted!")
        rc, out, err = run_cli("-c", "sums.magnets")
        self.assertEqual(rc, 1)
        self.assertIn(b"hello.txt: FAILED", out)
        self.assertIn(b"empty.bin: OK", out)

    def test_check_batch_sha256sum_lines(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        listing = (V["hello.txt"]["btih"] + "  hello.txt\n").encode() + (
            V["hello.txt"]["btmh"] + " *hello.txt\n"
        ).encode()
        with open("sums.txt", "wb") as fh:
            fh.write(listing)
        rc, out, err = run_cli("-c", "sums.txt")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count(b": OK ("), 2)

    def test_check_batch_comments_and_bad_lines(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("hello.txt")
        with open("sums.magnets", "wb") as fh:
            fh.write(b"# comment line\n\n" + out + b"garbage-without-hash\n")
        rc, out, err = run_cli("-c", "sums.magnets")
        self.assertEqual(rc, 2)
        self.assertIn(b"not a magnet or hash line", err)

    def test_check_extra_operand_after_listing(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("hello.txt")
        with open("sums.magnets", "wb") as fh:
            fh.write(out)
        rc, out, err = run_cli("-c", "sums.magnets", "hello.txt")
        self.assertEqual(rc, 2)

    def test_flag_combination_errors(self):
        self.write("hello.txt", V["hello.txt"]["data"])
        rc, out, err = run_cli("-t", "x.torrent", "-c", "0" * 40, "hello.txt")
        self.assertEqual(rc, 2)
        rc, out, err = run_cli("-1", "-c", "0" * 40, "hello.txt")
        self.assertEqual(rc, 2)

    def test_unicode_name_end_to_end(self):
        name = "déjà vu.bin"
        make_file(self.tmp, name, b"unicode content\n")
        rc, out, err = run_cli(name)
        self.assertEqual(rc, 0)
        self.assertIn(b"dn=d%C3%A9j%C3%A0%20vu.bin", out)
        magnet_line = out.strip()
        rc, out2, err2 = run_cli("-c", magnet_line.decode(), name)
        self.assertEqual(rc, 0)
        self.assertIn(b": OK (v1+v2)", out2)

    def test_directory_operand_rejected(self):
        rc, out, err = run_cli(self.tmp)
        self.assertEqual(rc, 2)

    @unittest.skipIf(
        sys.platform in ("darwin", "win32"),
        "filesystem rejects non-UTF-8 filenames",
    )
    def test_non_utf8_filename_rejected(self):
        raw = b"bad\xffname.bin"
        path = os.path.join(os.fsencode(self.tmp), raw)
        with open(path, "wb") as fh:
            fh.write(b"payload")
        try:
            # os.fsdecode uses surrogateescape; subprocess re-encodes with
            # os.fsencode on spawn, so the child receives the original bytes.
            rc, out, err = run_cli(os.fsdecode(raw))
            self.assertEqual(rc, 2)
            self.assertIn(b"not valid UTF-8", err)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
