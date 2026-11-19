#!/usr/bin/env python3
"""Layer trees for the overlayfs test, built the same way on the host
(under Linux, as root) and in the Hurd guest (as root).

  overlayfs_layers.py sets
        print each layer set: name, number of lowers, upper kind, stages
  overlayfs_layers.py build SET DIR --convention linux|hurd [--bin BINDIR] [--sq-source]
        write DIR/l1 .. DIR/lN (l1 is the topmost lower) and, for a set
        with an ext2 upper, DIR/upper and an empty DIR/work (a tmpfs
        upper gets the mount point DIR/t instead).  Opaque
        directories use the convention's marker: the trusted.overlay.opaque
        xattr (linux) or a zero-length .wh..wh..opq entry (hurd)
  overlayfs_layers.py merged SET DIR [--bin BINDIR]
        write the merged view of SET as a plain tree at DIR (the "cp -a"
        of the overlay that the kernel documentation describes), computed
        from the description, not from any overlay.  It is the same
        whether an existing upper is mounted as the upper or as the
        topmost lower (stage 1)
  overlayfs_layers.py build-upper SET UPPERDIR --convention linux|hurd
        write the upper of SET at UPPERDIR, inside a mounted tmpfs
  overlayfs_layers.py lowers SET [--no-upper]
        print the lowerdir layer names, topmost first, for the mount line
  overlayfs_layers.py snapshot DIR
        print a manifest of DIR with ctime and mtime (lower-untouched)

The description is the single source: both sides build from it, and the
control's plain directory is its merge.
"""
import collections
import errno
import hashlib
import os
import random
import socket
import stat
import sys

OPAQUE_MARKER = ".wh..wh..opq"
T_OLD = 86400            # 1970-01-02
T_MID = 981173106        # 2001-02-03 04:05:06 UTC
T_FAR = 2147483647       # 2038-01-19, the last second a 32-bit time holds
T_DIR = 1609459200       # 2021-01-01
FIXED_TIMES = (T_OLD, T_MID, T_FAR, T_DIR)

# kind: f file, d dir, l symlink, h hard link to `target` (same layer),
# p fifo, s socket, c/b device (rdev), w whiteout.  data: bytes, or
# ("rand", seed, size), or ("sparse", size, ((offset, seed, length), ...)),
# or ("bin", name) for a file from --bin.
E = collections.namedtuple("E", "kind mode uid gid mtime data target rdev opaque")


def f(data=b"", mode=0o644, uid=0, gid=0, mtime=T_MID):
    return E("f", mode, uid, gid, mtime, data, None, None, False)


def d(mode=0o755, uid=0, gid=0, mtime=T_DIR, opaque=False):
    return E("d", mode, uid, gid, mtime, None, None, None, opaque)


def l(target, uid=0, gid=0):
    return E("l", 0o777, uid, gid, T_MID, None, target, None, False)


def h(target):
    return E("h", None, None, None, None, None, target, None, False)


def node(kind, rdev=None, mode=0o644, uid=0, gid=0):
    return E(kind, mode, uid, gid, T_MID, None, None, rdev, False)


WHITEOUT = E("w", 0, 0, 0, T_MID, None, None, (0, 0), False)
LONG_NAME = "n" * 251 + ".txt"
MANY = 5000


def base_lower():
    """The lowest layer of every set: every kind of object the brief lists."""
    t = {
        ".": d(),
        "empty": f(b""),
        "small": f(b"small file in the lowest layer\n"),
        "medium": f(("rand", 1, 300 * 1024)),
        "sparse": f(("sparse", 8 << 20, ((0, 2, 4096), (3 << 20, 3, 4096), ((8 << 20) - 100, 4, 100)))),
        "big2m": f(("rand", 5, 2 << 20)),
        "both": f(b"both: lowest\n"),
        "a": d(), "a/b": d(), "a/b/c": d(), "a/b/c/deep": f(b"three levels down\n"),
        "a/sib": f(b"sibling of b\n"),
        "d": d(), "d/low-only": f(b"d/low-only\n"), "d/shared": f(b"d/shared: lowest\n"),
        "d/sub": d(), "d/sub/x": f(b"d/sub/x\n"),
        "fd": d(), "fd/inside": f(b"fd is a dir in the lowest layer\n"),
        "df": f(b"df is a file in the lowest layer\n"),
        "m1": d(), "m1/m2": d(), "m1/m2/m3": d(), "m1/m2/m3/low": f(b"m3 low\n"),
        "ldir": d(), "ldir/x": d(), "ldir/x/y": f(b"y\n"),
        "onlydir": d(), "onlydir/keep": f(b"keep\n"),
        "sl-rel": l("small"), "sl-abs": l("/etc/hostname"), "sl-dangling": l("no-such-target"),
        "sl-dir": l("a/b"),
        "hl-a": f(b"hard link pair\n"), "hl-b": h("hl-a"),
        "fifo": node("p"), "sock": node("s"), "chr": node("c", (1, 3)), "blk": node("b", (7, 0)),
        "setuid": f(b"#!/bin/sh\n", mode=0o4755), "setgid": f(b"g\n", mode=0o2755),
        "mode0": f(b"unreadable\n", mode=0o000), "ro644": f(b"root 0644\n", mode=0o644),
        "sticky": d(mode=0o1777), "sticky/root-file": f(b"root's\n"),
        "sticky/user-file": f(b"user's\n", uid=1000, gid=1000),
        "user": d(uid=1000, gid=1000), "user/own": f(b"user owns\n", uid=1000, gid=1000),
        "old": f(b"old\n", mtime=T_OLD), "future": f(b"future\n", mtime=T_FAR),
        LONG_NAME: f(b"a 255-byte name\n"),
        "many": d(),
    }
    for i in range(MANY):
        t[f"many/e{i:04d}"] = f(b"")
    return t


def top_lower():
    """A second lower over base_lower: the four same-name combinations."""
    return {
        ".": d(),
        "both": f(b"both: top lower\n", mode=0o600),
        "d": d(mode=0o750), "d/top-only": f(b"d/top-only\n"), "d/shared": f(b"d/shared: top\n"),
        "fd": f(b"fd is a file over a lower dir\n"),
        "df": d(), "df/inside": f(b"df is a dir over a lower file\n"),
        "m1": d(), "m1/m2": d(), "m1/m2/m3": d(), "m1/m2/m3/top": f(b"m3 top\n"),
        "topfile": f(b"only in the top lower\n"),
    }


def pre_upper():
    """An upper that a previous mount left: whiteouts, an opaque
    directory, copied-up files and an upper-only tree."""
    return {
        ".": d(),
        "small": WHITEOUT,
        "ldir": WHITEOUT,
        "d": d(mode=0o700, opaque=True), "d/upper-only": f(b"in the opaque d\n"),
        "medium": f(b"copied up and rewritten\n", mode=0o600),
        "a": d(), "a/new-in-upper": f(b"a is merged\n"), "a/sib": WHITEOUT,
        "m1": d(), "m1/m2": d(), "m1/m2/m3": d(), "m1/m2/m3/up": f(b"m3 up\n"),
        "uonly": d(), "uonly/u2": d(), "uonly/u2/u3": d(), "uonly/u2/u3/f": f(b"upper only\n"),
    }


def middle_lower():
    """Whiteouts and an opaque directory in a middle lower layer."""
    return {
        ".": d(),
        "small": WHITEOUT,
        "a": d(), "a/b": WHITEOUT,
        "d": d(opaque=True), "d/mid-only": f(b"mid, opaque d\n"),
        "midfile": f(b"middle layer\n"),
    }


def lowest_with_whiteouts():
    t = base_lower()
    t["lowwh"] = WHITEOUT
    t["onlydir/w"] = WHITEOUT
    return t


# name: (lowers topmost first, upper, upper kind, stages)
#   upper kind: "ext2" a directory on the root filesystem, "tmpfs" a tmpfs
#   mount, "tmpfs-1m" a 1 MiB tmpfs, None no upper.  A lower ("sq", tree)
#   is served from a squashfs image of the tree.
SETS = {
    "ext2-1": ([base_lower], None, "ext2", (2,)),
    "ext2-2": ([top_lower, base_lower], None, "ext2", (2,)),
    "sq-1": ([("sq", base_lower)], None, "ext2", (2,)),
    "sq-tmp": ([("sq", base_lower)], None, "tmpfs", (2,)),
    "ro": ([top_lower, base_lower], None, None, (1,)),
    "pre": ([base_lower], pre_upper, "ext2", (1, 2)),
    "wh-lower": ([top_lower, middle_lower, lowest_with_whiteouts], None, "ext2", (1, 2)),
    "enospc": ([base_lower], None, "tmpfs-1m", (2,)),
}


def layer_tree(spec):
    return spec[1]() if isinstance(spec, tuple) else spec()


def is_squashfs(spec):
    return isinstance(spec, tuple) and spec[0] == "sq"


def _children(tree, dirpath):
    prefix = "" if dirpath == "." else dirpath + "/"
    for p in tree:
        if p != "." and p.startswith(prefix) and "/" not in p[len(prefix):]:
            yield p[len(prefix):]


def _join(a, b):
    return b if a == "." else a + "/" + b


def merge(trees):
    """The merged view of TREES (topmost first): {path: (entry, layer)}.
    Linux lookup order: the topmost layer that has a name decides it.
    A directory merges with the same-named directories below it until a
    non-directory, a whiteout or an opaque directory stops the search."""
    out = {".": (trees[0]["."], 0)}

    def visit(dirpath, srcs):
        names = {}
        for i in srcs:
            for name in _children(trees[i], dirpath):
                names.setdefault(name, i)
        for name in sorted(names):
            i = names[name]
            p = _join(dirpath, name)
            e = trees[i][p]
            if e.kind == "w":
                continue
            out[p] = (e, i)
            if e.kind != "d":
                continue
            sub = [i]
            if not e.opaque:
                for j in srcs[srcs.index(i) + 1:]:
                    g = trees[j].get(p)
                    if g is None:
                        continue
                    if g.kind != "d":
                        break
                    sub.append(j)
                    if g.opaque:
                        break
            visit(p, sub)

    visit(".", list(range(len(trees))))
    return out


def _data(spec, bindir):
    if isinstance(spec, bytes):
        return spec, None
    if spec[0] == "rand":
        return random.Random(spec[1]).randbytes(spec[2]), None
    if spec[0] == "sparse":
        return None, spec
    if spec[0] == "bin":
        with open(os.path.join(bindir, spec[1]), "rb") as fh:
            return fh.read(), None
    raise ValueError(spec)


def _write_file(path, spec, bindir):
    data, sparse = _data(spec, bindir)
    with open(path, "wb") as fh:
        if sparse:
            _, size, pieces = sparse
            for off, seed, length in pieces:
                fh.seek(off)
                fh.write(random.Random(seed).randbytes(length))
            fh.truncate(size)
        else:
            fh.write(data)


def _with_bins(tree, bindir):
    if bindir:
        tree["bin"] = d()
        for name in sorted(os.listdir(bindir)):
            tree["bin/" + name] = f(("bin", name), mode=0o755)
    return tree


def materialize(entries, root, convention, bindir=None):
    """Create ENTRIES ({path: E}) under ROOT.  Hard links ("h") become
    links when their target is in ENTRIES, else copies of it."""
    os.makedirs(root, exist_ok=True)
    dirs = sorted(p for p, e in entries.items() if e.kind == "d")
    for p in dirs:
        if p != ".":
            os.mkdir(os.path.join(root, p))
    links = []
    for p, e in sorted(entries.items()):
        full = os.path.join(root, p)
        if e.kind == "f":
            _write_file(full, e.data, bindir)
        elif e.kind == "l":
            os.symlink(e.target, full)
        elif e.kind == "h":
            links.append((p, e))
        elif e.kind == "p":
            os.mkfifo(full)
        elif e.kind == "s":
            here = os.getcwd()
            os.chdir(os.path.dirname(full))
            try:
                s = socket.socket(socket.AF_UNIX)
                s.bind(os.path.basename(full))
                s.close()
            finally:
                os.chdir(here)
        elif e.kind in "cbw":
            kind = stat.S_IFBLK if e.kind == "b" else stat.S_IFCHR
            os.mknod(full, kind | 0o600, os.makedev(*e.rdev))
    for p, e in links:
        os.link(os.path.join(root, e.target), os.path.join(root, p))
    # Owner before mode (chown clears setuid), children before parents
    # (creating an entry changes its directory's mtime).
    for p, e in sorted(entries.items(), key=lambda kv: (kv[1].kind == "d", -kv[0].count("/"), kv[0])):
        if e.kind == "h":
            continue
        full = os.path.join(root, p)
        if e.kind == "d" and e.opaque:
            if convention == "linux":
                os.setxattr(full, "trusted.overlay.opaque", b"y")
            elif convention == "hurd":
                with open(os.path.join(full, OPAQUE_MARKER), "wb"):
                    pass
        os.chown(full, e.uid, e.gid, follow_symlinks=False)
        if e.kind != "l":
            os.chmod(full, e.mode)
        try:
            os.utime(full, (e.mtime, e.mtime), follow_symlinks=False)
        except (NotImplementedError, OSError):
            if e.kind != "l":
                raise


def build(name, root, convention, bindir=None, sq_source=False):
    """A squashfs lower lN is left as an empty mount point; with
    SQ_SOURCE its tree is written to lN.tree for mksquashfs.  The upper
    and work directories of a tmpfs upper are made by the caller after it
    mounts the tmpfs on DIR/t."""
    lowers, upper, upper_kind, _ = SETS[name]
    for i, spec in enumerate(lowers, 1):
        tree = _with_bins(layer_tree(spec), bindir) if i == len(lowers) else layer_tree(spec)
        where = os.path.join(root, f"l{i}")
        if is_squashfs(spec):
            os.makedirs(where)
            if not sq_source:
                continue
            where += ".tree"
        materialize(tree, where, convention, bindir)
    if upper_kind == "ext2":
        materialize(upper() if upper else {".": d()}, os.path.join(root, "upper"), convention)
        os.makedirs(os.path.join(root, "work"))
    elif upper_kind:
        os.makedirs(os.path.join(root, "t"))


def build_upper(name, upper_dir, convention):
    """The upper of SET at UPPER_DIR (for an upper on a tmpfs)."""
    upper = SETS[name][1]
    materialize(upper() if upper else {".": d()}, upper_dir, convention)


def merged(name, root, bindir=None):
    lowers, upper, _, _ = SETS[name]
    trees = [layer_tree(s) for s in lowers]
    trees[-1] = _with_bins(trees[-1], bindir)
    if upper is not None:
        trees.insert(0, upper())
    view = merge(trees)
    entries = {}
    for p, (e, i) in view.items():
        if e.kind == "h":
            target = trees[i][e.target]
            if e.target in view and view[e.target][1] == i:
                entries[p] = e
            else:
                entries[p] = target
        else:
            entries[p] = e._replace(opaque=False)
    materialize(entries, root, "none", bindir)


def lowers_arg(name, no_upper=False):
    lowers, upper, _, _ = SETS[name]
    names = [f"l{i}" for i in range(1, len(lowers) + 1)]
    if upper is not None and no_upper:
        names.insert(0, "upper")
    return names


def snapshot(root):
    """Every path under ROOT with type, mode, owner, size, mtime, ctime,
    content hash, rdev, target and the opaque xattr where readable."""
    lines = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        names = sorted(dirnames + filenames)
        if dirpath == root:
            names = [""] + names
        for name in names:
            full = os.path.join(dirpath, name) if name else dirpath
            st = os.lstat(full)
            digest = "-"
            if stat.S_ISREG(st.st_mode):
                hh = hashlib.sha256()
                with open(full, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        hh.update(chunk)
                digest = hh.hexdigest()[:16]
            target = os.readlink(full) if stat.S_ISLNK(st.st_mode) else "-"
            rdev = "%d,%d" % (os.major(st.st_rdev), os.minor(st.st_rdev)) \
                if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) else "-"
            rel = os.path.relpath(full, root)
            lines.append(f"{rel}\t{st.st_mode:o}\t{st.st_uid}:{st.st_gid}\t{st.st_size}\t"
                         f"{int(st.st_mtime)}\t{int(st.st_ctime)}\t{st.st_nlink}\t{digest}\t{rdev}\t{target}")
    return lines


def _opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "sets":
        for n, (lowers, upper, kind, stages) in SETS.items():
            sq = "sq" if any(is_squashfs(s) for s in lowers) else "-"
            print(f"{n}\t{len(lowers)}\t{kind or '-'}\t{'pre' if upper else '-'}\t{sq}\t"
                  + ",".join(map(str, stages)))
        return 0
    if cmd == "build" and len(argv) >= 5:
        build(argv[2], argv[3], _opt(argv, "--convention"), _opt(argv, "--bin"),
              "--sq-source" in argv)
        return 0
    if cmd == "build-upper" and len(argv) >= 5:
        build_upper(argv[2], argv[3], _opt(argv, "--convention"))
        return 0
    if cmd == "merged" and len(argv) >= 4:
        merged(argv[2], argv[3], _opt(argv, "--bin"))
        return 0
    if cmd == "lowers" and len(argv) >= 3:
        print(" ".join(lowers_arg(argv[2], "--no-upper" in argv)))
        return 0
    if cmd == "snapshot" and len(argv) == 3:
        print("\n".join(snapshot(argv[2])))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
