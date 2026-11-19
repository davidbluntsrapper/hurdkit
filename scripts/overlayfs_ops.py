#!/usr/bin/env python3
"""Scenarios for the overlayfs test.  One file runs on the host under
Linux overlayfs and in the Hurd guest on the translator and on a plain
directory, so the record format cannot drift between them.

  overlayfs_ops.py scenarios [STAGE]
        print each scenario: name, stage, the sets it runs on
  overlayfs_ops.py plan
        print every run: set, mode (ro: no upper; rw), scenario, stage,
        and "plain" when the plain-directory control runs it too
  overlayfs_ops.py mount-args
        print the bad invocations and hostile layouts: name, must, setup,
        options, during (see MOUNT_ARGS)
  overlayfs_ops.py run SCENARIO ROOT [--upper DIR]
        run SCENARIO against ROOT (a mount point or a plain directory) and
        print one record per step.  --upper names the overlay's upper
        directory, for the statfs step
  overlayfs_ops.py compare SCENARIO LINUX_LOG OTHER_LOG --tags T1,T2 [--set SET]
        compare a log with the Linux log of the same scenario and set,
        allowing exactly the per-step differences the scenario lists for
        the tags (plain: the plain-directory control; hurd: anything in
        the guest).  Exit 1 on any other difference
  overlayfs_ops.py upper-state UPPER --convention linux|hurd
        print the meaning of an upper directory: whiteouts, opaque
        directories, and every other entry with its type
  overlayfs_ops.py ids ROOT
        two walks: st_ino unique and stable, one st_dev, d_ino == st_ino
  overlayfs_ops.py stress-ro ROOT SECS PROCS
  overlayfs_ops.py stress-rw ROOT SECS PROCS
        PROCS processes for SECS seconds; print counts and unexpected errors
  overlayfs_ops.py mutating SCENARIO
        print the index of each step that changes the tree (fault sweep)
  overlayfs_ops.py view-class BEFORE AFTER OBSERVED
        print before, after or mixed: which state the OBSERVED manifest
        record is (fault sweep)
  overlayfs_ops.py fault-selftest DIR
        prove view-class on states made by hand in DIR

A record line is  STEP<TAB>OP<TAB>RESULT .  RESULT is "ok", "ok VALUE"
or an errno name.  A manifest step prints one line per path,
STEP<TAB>M<TAB>PATH<TAB>FIELDS, where FIELDS are type, mode, uid:gid,
size, nlink, mtime, sha256, rdev and target.  An mtime that is not one
of the description's fixed times is printed "new": the two systems run
at different times.
"""
import ctypes
import errno
import hashlib
import mmap
import os
import random
import shutil
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overlayfs_layers as L  # noqa: E402

START = time.time()


def S(op, *args, diff=None, ignore=None, why=""):
    """A step.  DIFF {tag: result} is the result a system with that tag
    gives instead of Linux's, IGNORE {tag: (path, ...)} the paths whose
    manifest lines that system no longer compares from this step on, and
    WHY the reason, which the compare output quotes."""
    return {"op": op, "args": args, "diff": diff or {}, "ignore": ignore or {}, "why": why}


HL_BREAK = "Linux breaks a hard link at copy-up (no index); a plain directory keeps it"
DIR_EXDEV = "Linux gives EXDEV for a directory that exists in a lower layer (redirect_dir off)"
HL_NLINK = "Linux reports a copied-up name's st_nlink as 1 and the other name's as the lower's (H4)"
MAP_COPYUP = ("a plain file is one inode, so the mapping sees the write; Linux keeps a mapping "
              "made before copy-up on the lower file (F3)")
WH_EPERM = "Linux refuses to create a 0:0 character device (it is the whiteout)"

READ_STEPS = [
    S("manifest", "."),
    S("lookup-all"),
    S("stat-names", "small", "ldir", "a/sib", "a/b", "lowwh", "onlydir/w", "d/low-only",
      "d/shared", "d/sub", "d/top-only", "d/mid-only", "d/upper-only", "fd", "df",
      ".wh..wh..opq", "d/.wh..wh..opq"),
    S("readdir", "."), S("readdir", "d"), S("readdir", "a"), S("readdir", "m1/m2/m3"),
    S("readdir", "onlydir"), S("readdir", "many"), S("readdir", "uonly/u2/u3"),
    S("readdir-seek", ".", 7), S("readdir-seek", "many", 1000),
    S("getcwd", "a/b/c"), S("getcwd", "m1/m2/m3"), S("getcwd", "uonly/u2/u3"),
    S("read", "small"), S("read", "medium"), S("read", "sparse"), S("read", "both"),
    S("read", "d/shared"), S("read", "fd"), S("read", "df"), S("read", L.LONG_NAME),
    S("pread", "medium", 4095, 8193), S("pread", "sparse", (3 << 20) - 10, 100),
    S("pread", "sparse", (8 << 20) - 50, 200), S("pread", "small", 1000, 10),
    S("readlink", "sl-rel"), S("readlink", "sl-abs"), S("readlink", "sl-dangling"),
    S("readlink", "sl-dir"), S("follow", "sl-rel"), S("follow", "sl-dir"),
    S("follow", "sl-dangling"), S("stat", "sl-rel"), S("readlink", "small"),
    S("stat", "fifo"), S("stat", "sock"), S("stat", "chr"), S("stat", "blk"),
    S("open-fifo", "fifo"),
    S("stat", "hl-a"), S("stat", "hl-b"), S("stat", "setuid"), S("stat", "mode0"),
    S("statfs", diff={"plain": None}, why="a plain directory has no upper to compare statfs with"),
]

SCENARIOS = {
    "read": {"stage": 1, "sets": "all", "steps": READ_STEPS},
    "erofs": {"stage": 1, "sets": "ro", "plain": False, "steps": [S("erofs", "small", "a", "d")]},
    "copyup": {"stage": 2, "sets": "rw", "steps": [
        S("open", "small", "WRONLY"), S("stat", "small"),
        S("write", "new-file", "created\n"), S("read", "new-file"),
        S("append", "medium", "appended\n"), S("read", "medium"),
        S("open", "big2m", "WRONLY|TRUNC"), S("stat", "big2m"),
        S("truncate", "both", 3), S("read", "both"),
        S("chmod", "ro644", 0o600), S("stat", "ro644"),
        S("chown", "old", 1000, 1000), S("stat", "old"),
        S("utime", "future", 1000000000), S("stat", "future"),
        S("utime", "setuid", 1500000000), S("stat", "setuid"),
        S("append", "a/b/c/deep", "deeper\n"), S("stat", "a/b/c"), S("stat", "a/b"),
        S("stat", "a"), S("read", "a/b/c/deep"),
        S("append", "hl-a", "only through hl-a\n", ignore={"plain": ("hl-a", "hl-b")}, why=HL_BREAK),
        S("read", "hl-b", diff={"plain": None}, why=HL_BREAK),
        S("stat", "hl-a", diff={"plain": None}, why=HL_BREAK),
        S("stat", "hl-b", diff={"plain": None}, why=HL_BREAK),
        S("utime", "sparse", 1500000000), S("holes", "sparse"), S("read", "sparse"),
        S("lchown", "sl-rel", 1000, 1000),
        S("readlink", "sl-rel"), S("stat", "sl-rel"),
        S("chmod", "fifo", 0o600), S("stat", "fifo"), S("chmod", "chr", 0o600), S("stat", "chr"),
        S("open", "small", "WRONLY|CREAT|EXCL"), S("open", "brand-new", "WRONLY|CREAT|EXCL"),
        S("open", "empty", "WRONLY|APPEND"), S("append", "empty", "x"), S("read", "empty"),
        S("open-across-copy-up", "d/low-only", diff={"plain": "ok fd=new mmap=new"}, why=MAP_COPYUP),
        S("manifest", "."),
    ]},
    "remove": {"stage": 2, "sets": "rw", "steps": [
        S("unlink", "small"), S("stat", "small"), S("listdir", "."),
        S("write", "u-new", "upper only\n"), S("unlink", "u-new"), S("stat", "u-new"),
        S("unlink", "both"), S("stat", "both"),
        S("unlink", "d/shared"), S("unlink", "d/low-only"), S("listdir", "d"),
        S("rmdir", "d"), S("rmdir", "a/b/c"), S("unlink", "a/b/c/deep"), S("rmdir", "a/b/c"),
        S("listdir", "a/b"), S("rmdir", "onlydir"), S("rmtree", "m1"), S("stat", "m1"),
        S("write", "small", "created over a whiteout\n"), S("read", "small"),
        S("rmtree", "ldir"), S("mkdir", "ldir"), S("listdir", "ldir"), S("stat", "ldir/x"),
        S("rmtree", "d"), S("mkdir", "d"), S("listdir", "d"),
        S("unlink", "sl-rel"), S("stat", "sl-rel"), S("unlink", "fifo"), S("unlink", "chr"),
        S("unlink", "hl-a", ignore={"plain": ("hl-b",)}, why=HL_NLINK),
        S("stat", "hl-b", diff={"plain": None}, why=HL_NLINK),
        S("rmtree", "many"), S("stat", "many"),
        S("rmdir", "uonly/u2"), S("rmtree", "uonly"),
        S("manifest", "."),
    ]},
    "rename": {"stage": 2, "sets": "rw", "steps": [
        S("rename", "small", "small-renamed"), S("stat", "small"), S("read", "small-renamed"),
        S("rename", "medium", "a/medium-moved"), S("read", "a/medium-moved"),
        S("write", "u1", "u1\n"), S("write", "u2", "u2\n"), S("rename", "u1", "u2"), S("read", "u2"),
        S("rename", "u2", "both"), S("read", "both"),
        S("rename", "empty", "old"), S("read", "old"),
        S("write", "u3", "u3\n"), S("rename", "future", "u3"), S("read", "u3"),
        S("rename", "a/sib", "a/b/c/deep"), S("read", "a/b/c/deep"),
        S("mkdir", "newdir"), S("write", "newdir/f", "in newdir\n"),
        S("rename", "newdir", "newdir2"), S("listdir", "newdir2"),
        S("rename", "newdir2", "d/moved-in"), S("listdir", "d/moved-in"),
        S("rename", "ldir", "ldir2", diff={"plain": "ok"}, why=DIR_EXDEV),
        S("rename", "ldir2", "ldir", diff={"plain": "ok"}, why="undo the plain directory's rename"),
        S("rename", "a", "a2", diff={"plain": "ok"}, why=DIR_EXDEV),
        S("rename", "a2", "a", diff={"plain": "ok"}, why="undo the plain directory's rename"),
        S("editor-save", "d/sub/x", "saved by an editor\n"), S("read", "d/sub/x"),
        S("stat", "d/sub/x"),
        S("rename", "hl-a", "hl-renamed", ignore={"plain": ("hl-renamed", "hl-b")}, why=HL_NLINK),
        S("stat", "hl-b"),
        S("manifest", "."),
    ]},
    "create": {"stage": 2, "sets": "rw", "steps": [
        S("symlink", "small", "new-sl"), S("readlink", "new-sl"), S("read", "new-sl"),
        S("link", "medium", "medium-link"), S("stat", "medium"), S("read", "medium-link"),
        S("link", "hl-a", "hl-c", ignore={"plain": ("hl-a", "hl-b", "hl-c")}, why=HL_NLINK),
        S("stat", "hl-a", diff={"plain": None}, why=HL_NLINK),
        S("mkfifo", "new-fifo"), S("stat", "new-fifo"),
        S("mknod", "new-chr", "c", 1, 3), S("stat", "new-chr"),
        S("mknod", "new-wh", "c", 0, 0, diff={"plain": "ok"}, why=WH_EPERM),
        S("unlink", "new-wh", diff={"plain": "ok"}, why="remove the plain directory's node"),
        S("mkdir", "a/b/c/new-deep"), S("stat", "a/b/c/new-deep"), S("stat", "a/b"),
        S("mkdir", "d"), S("mkdir", "small"), S("symlink", "x", "small"),
        S("open", "d/shared", "WRONLY|CREAT|EXCL"),
        S("write", "m1/m2/m3/new", "new\n"), S("listdir", "m1/m2/m3"),
        S("manifest", "."),
    ]},
    "perms": {"stage": 2, "sets": "rw", "user": 1000, "steps": [
        S("open", "ro644", "WRONLY"), S("open", "user/own", "WRONLY|APPEND"),
        S("append", "user/own", "by 1000\n"), S("read", "user/own"),
        S("write", "user/new", "new by 1000\n"), S("stat", "user/new"),
        S("write", "sticky/mine", "1000 in sticky\n"), S("stat", "sticky/mine"),
        S("unlink", "sticky/root-file"), S("unlink", "sticky/user-file"),
        S("unlink", "small"), S("read", "mode0"), S("chmod", "small", 0o666),
        S("rename", "user/own", "user/own2"), S("mkdir", "user/sub"), S("stat", "user/sub"),
        S("manifest", "user"), S("manifest", "sticky"),
    ]},
    "persist": {"stage": 2, "sets": (), "steps": [S("manifest", ".")]},
    "enospc": {"stage": 2, "sets": ("enospc",), "plain": False, "steps": [
        S("open", "big2m", "WRONLY"), S("read", "big2m"), S("listdir-upper"),
        S("append", "medium", "fits\n"), S("read", "medium"),
        S("manifest", "."),
    ]},
}

FLAGS = {"RDONLY": os.O_RDONLY, "WRONLY": os.O_WRONLY, "RDWR": os.O_RDWR, "CREAT": os.O_CREAT,
         "EXCL": os.O_EXCL, "TRUNC": os.O_TRUNC, "APPEND": os.O_APPEND}


def ename(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def norm_time(t):
    t = int(t)
    return str(t) if t in L.FIXED_TIMES or t < START - 300 else "new"




def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def fields(path, st=None):
    st = st or os.lstat(path)
    t = L_type(st.st_mode)
    return "\t".join((
        t, "%04o" % stat.S_IMODE(st.st_mode), f"{st.st_uid}:{st.st_gid}",
        str(st.st_size) if t != "d" else "-", str(st.st_nlink),
        norm_time(st.st_mtime) if t != "l" else "-",
        sha(path) if t == "f" and st.st_mode & 0o444 else "-",
        "%d,%d" % (os.major(st.st_rdev), os.minor(st.st_rdev)) if t in "cb" else "-",
        os.readlink(path) if t == "l" else "-"))


def L_type(mode):
    for test, t in ((stat.S_ISDIR, "d"), (stat.S_ISREG, "f"), (stat.S_ISLNK, "l"),
                    (stat.S_ISCHR, "c"), (stat.S_ISBLK, "b"), (stat.S_ISFIFO, "p"),
                    (stat.S_ISSOCK, "s")):
        if test(mode):
            return t
    return "?"


def walk(root):
    """Paths under ROOT, sorted, "." first.  os.walk ignores errors, so
    listing failures come back as their own entries."""
    out, errs = ["."], []
    stack = ["."]
    while stack:
        rel = stack.pop()
        full = os.path.join(root, rel)
        try:
            names = sorted(os.listdir(full))
        except OSError as e:
            errs.append(f"{rel}: {ename(e)}")
            continue
        for n in names:
            p = n if rel == "." else rel + "/" + n
            out.append(p)
            try:
                if stat.S_ISDIR(os.lstat(os.path.join(root, p)).st_mode):
                    stack.append(p)
            except OSError as e:
                errs.append(f"{p}: {ename(e)}")
    return sorted(out), errs


# --- readdir through libc, so "." and "..", d_ino and seekdir are seen ---

class _Dirent(ctypes.Structure):
    if sys.platform.startswith("gnu"):
        _fields_ = [("d_ino", ctypes.c_uint64), ("d_reclen", ctypes.c_uint16),
                    ("d_type", ctypes.c_uint8), ("d_namlen", ctypes.c_uint8),
                    ("d_name", ctypes.c_char * 256)]
    else:
        _fields_ = [("d_ino", ctypes.c_uint64), ("d_off", ctypes.c_int64),
                    ("d_reclen", ctypes.c_uint16), ("d_type", ctypes.c_uint8),
                    ("d_name", ctypes.c_char * 256)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.opendir.restype = ctypes.c_void_p
_libc.opendir.argtypes = [ctypes.c_char_p]
_libc.readdir.restype = ctypes.POINTER(_Dirent)
_libc.readdir.argtypes = [ctypes.c_void_p]
_libc.telldir.restype = ctypes.c_long
_libc.telldir.argtypes = [ctypes.c_void_p]
_libc.seekdir.argtypes = [ctypes.c_void_p, ctypes.c_long]
_libc.rewinddir.argtypes = [ctypes.c_void_p]
_libc.closedir.argtypes = [ctypes.c_void_p]


class Dir:
    def __init__(self, path):
        self.d = _libc.opendir(os.fsencode(path))
        if not self.d:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))

    def read(self, n=None):
        out = []
        while n is None or len(out) < n:
            ctypes.set_errno(0)
            ent = _libc.readdir(self.d)
            if not ent:
                e = ctypes.get_errno()
                if e:
                    raise OSError(e, os.strerror(e))
                break
            out.append((os.fsdecode(ent.contents.d_name), ent.contents.d_ino))
        return out

    def tell(self):
        return _libc.telldir(self.d)

    def seek(self, pos):
        _libc.seekdir(self.d, pos)

    def rewind(self):
        _libc.rewinddir(self.d)

    def close(self):
        _libc.closedir(self.d)


def _names_summary(names):
    s = sorted(names)
    if len(s) <= 40:
        return " ".join(s)
    return f"n={len(s)} sha={hashlib.sha256(chr(0).join(s).encode()).hexdigest()[:16]}"


# --- the steps ---

def op_manifest(root, rel):
    paths, errs = walk(os.path.join(root, rel))
    lines = []
    for p in paths:
        full = os.path.join(root, rel, p)
        try:
            lines.append(f"{p}\t{fields(full)}")
        except OSError as e:
            lines.append(f"{p}\t{ename(e)}")
    return lines + [f"!\t{e}" for e in errs]


def op_lookup_all(root):
    """Every path by its full name, and ".." of every directory."""
    paths, errs = walk(root)
    bad = list(errs)
    for p in paths:
        full = os.path.join(root, p)
        try:
            st = os.lstat(full)
        except OSError as e:
            bad.append(f"{p}: {ename(e)}")
            continue
        if stat.S_ISDIR(st.st_mode):
            parent = os.path.dirname(full) if p != "." else os.path.dirname(os.path.abspath(root))
            try:
                a, b = os.stat(os.path.join(full, "..")), os.stat(parent)
                if (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev):
                    bad.append(f"{p}/..: not its parent")
            except OSError as e:
                bad.append(f"{p}/..: {ename(e)}")
    return f"ok paths={len(paths)}" if not bad else "bad " + "; ".join(bad[:10])


def op_readdir(root, rel):
    d = Dir(os.path.join(root, rel))
    try:
        ents = d.read()
    finally:
        d.close()
    names = [n for n, _ in ents]
    dups = len(names) - len(set(names))
    return f"ok dups={dups} {_names_summary(names)}"


def op_readdir_seek(root, rel, k):
    path = os.path.join(root, rel)
    d = Dir(path)
    try:
        full = [n for n, _ in d.read()]
        d.rewind()
        again = [n for n, _ in d.read()]
        d.rewind()
        head = [n for n, _ in d.read(k)]
        pos = d.tell()
        tail = [n for n, _ in d.read()]
        d.seek(pos)
        tail2 = [n for n, _ in d.read()]
    finally:
        d.close()
    d = Dir(path)
    try:
        d.seek(pos)
        reopened = [n for n, _ in d.read()]
    finally:
        d.close()
    bad = []
    if again != full:
        bad.append("rewind gives another list")
    if head + tail != full:
        bad.append("telldir split loses or repeats names")
    if tail2 != tail:
        bad.append("seekdir to a saved offset gives another tail")
    # Linux: an offset reused on a new stream "may point anywhere" (D6),
    # so the reopened stream is recorded, not required.
    reuse = "same" if reopened == tail else "differs"
    return ("ok" if not bad else "bad " + "; ".join(bad)) + f" reopened-offset={reuse}"


def op_getcwd(root, rel):
    here = os.getcwd()
    try:
        os.chdir(os.path.join(root, rel))
        got = os.getcwd()
    finally:
        os.chdir(here)
    want = os.path.realpath(os.path.join(root, rel))
    return "ok" if got == want else f"bad {got}"


def op_read(root, rel):
    path = os.path.join(root, rel)
    with open(path, "rb") as fh:
        data = fh.read()
        past = fh.read(10)
    return f"ok size={len(data)} sha={hashlib.sha256(data).hexdigest()[:16]} past-end={len(past)}"


def op_pread(root, rel, off, n):
    fd = os.open(os.path.join(root, rel), os.O_RDONLY)
    try:
        data = os.pread(fd, n, off)
    finally:
        os.close(fd)
    return f"ok n={len(data)} sha={hashlib.sha256(data).hexdigest()[:16]}"


def op_open(root, rel, flags):
    mask = 0
    for name in flags.split("|"):
        mask |= FLAGS[name]
    os.close(os.open(os.path.join(root, rel), mask, 0o644))
    return "ok"


def op_write(root, rel, text):
    with open(os.path.join(root, rel), "w") as fh:
        fh.write(text)
    return "ok"


def op_append(root, rel, text):
    with open(os.path.join(root, rel), "a") as fh:
        fh.write(text)
    return "ok"


def op_editor_save(root, rel, text):
    path = os.path.join(root, rel)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, path)
    return "ok"


def op_holes(root, rel):
    st = os.stat(os.path.join(root, rel))
    return "ok holes" if st.st_blocks * 512 < st.st_size // 2 else "ok filled"


def op_open_across_copy_up(root, rel):
    """Linux F1 and F3: a read-only descriptor opened before copy-up reads
    the new data afterwards; an mmap made before copy-up keeps the old."""
    path = os.path.join(root, rel)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.read(fd, 3)
        m = mmap.mmap(fd, 0, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)
        old = bytes(m[:])
        with open(path, "r+b") as fh:
            fh.seek(5)
            fh.write(b"NEW")
        rest = os.read(fd, 100)
        now = bytes(m[:])
        m.close()
    finally:
        os.close(fd)
    with open(path, "rb") as fh:
        after = fh.read()
    fd_view = "new" if rest == after[3:] else ("old" if rest == old[3:] else "other")
    map_view = "new" if now == after else ("old" if now == old else "other")
    return f"ok fd={fd_view} mmap={map_view}"


def op_open_fifo(root, rel):
    fd = os.open(os.path.join(root, rel), os.O_RDONLY | os.O_NONBLOCK)
    os.close(fd)
    return "ok"


def op_erofs(root, file, dirname, other_dir):
    j = lambda p: os.path.join(root, p)
    def wmap():
        fd = os.open(j(file), os.O_RDONLY)
        try:
            mmap.mmap(fd, 0, prot=mmap.PROT_READ | mmap.PROT_WRITE, flags=mmap.MAP_SHARED).close()
        finally:
            os.close(fd)
    ops = [
        ("create", lambda: os.close(os.open(j("new-file"), os.O_CREAT | os.O_WRONLY, 0o644))),
        ("open-write", lambda: os.close(os.open(j(file), os.O_WRONLY))),
        ("open-trunc", lambda: os.close(os.open(j(file), os.O_RDONLY | os.O_TRUNC))),
        ("truncate", lambda: os.truncate(j(file), 0)),
        ("unlink", lambda: os.unlink(j(file))),
        ("chmod", lambda: os.chmod(j(file), 0o600)),
        ("chown", lambda: os.chown(j(file), 1, 1)),
        ("utime", lambda: os.utime(j(file), (0, 0))),
        ("rename", lambda: os.rename(j(file), j("renamed"))),
        ("link", lambda: os.link(j(file), j("new-link"))),
        ("symlink", lambda: os.symlink("x", j("new-symlink"))),
        ("mkdir", lambda: os.mkdir(j("new-dir"))),
        ("rmdir", lambda: os.rmdir(j(other_dir))),
        ("mkfifo", lambda: os.mkfifo(j("new-fifo"))),
        ("mknod", lambda: os.mknod(j("new-chr"), stat.S_IFCHR | 0o600, os.makedev(1, 3))),
        ("chmod-dir", lambda: os.chmod(j(dirname), 0o700)),
        ("mmap-write-shared", wmap),
    ]
    out = []
    for name, fn in ops:
        try:
            fn()
            out.append(f"{name}=ok")
        except OSError as e:
            out.append(f"{name}={ename(e)}")
    return "ok " + " ".join(out)


def op_statfs(root, upper):
    v = os.statvfs(root)
    ro = "ro" if v.f_flag & getattr(os, "ST_RDONLY", 1) else "rw"
    rel = "-"
    if upper:
        u = os.statvfs(upper)
        rel = "blocks-as-upper" if (v.f_blocks, v.f_bsize) == (u.f_blocks, u.f_bsize) else "blocks-differ"
    return f"ok namemax={v.f_namemax} {ro} {rel}"


def run_step(root, step, upper):
    op, a = step["op"], step["args"]
    j = lambda p: os.path.join(root, p)
    simple = {
        "stat": lambda p: "ok " + fields(j(p)),
        "follow": lambda p: "ok " + L_type(os.stat(j(p)).st_mode),
        "readlink": lambda p: "ok " + os.readlink(j(p)),
        "unlink": lambda p: os.unlink(j(p)),
        "rmdir": lambda p: os.rmdir(j(p)),
        "mkdir": lambda p: os.mkdir(j(p)),
        "rmtree": lambda p: shutil.rmtree(j(p)),
        "listdir": lambda p: "ok " + _names_summary(os.listdir(j(p))),
        "rename": lambda a_, b: os.rename(j(a_), j(b)),
        "symlink": lambda t, p: os.symlink(t, j(p)),
        "link": lambda a_, b: os.link(j(a_), j(b)),
        "mkfifo": lambda p: os.mkfifo(j(p)),
        "mknod": lambda p, k, ma, mi: os.mknod(
            j(p), (stat.S_IFCHR if k == "c" else stat.S_IFBLK) | 0o644, os.makedev(ma, mi)),
        "truncate": lambda p, n: os.truncate(j(p), n),
        "chmod": lambda p, m: os.chmod(j(p), m),
        "chown": lambda p, u, g: os.chown(j(p), u, g),
        "lchown": lambda p, u, g: os.chown(j(p), u, g, follow_symlinks=False),
        "utime": lambda p, t: os.utime(j(p), (t, t)),
        "listdir-upper": lambda: "ok " + _names_summary(os.listdir(upper)) if upper else "ok -",
    }
    with_root = {
        "lookup-all": op_lookup_all, "readdir": op_readdir, "readdir-seek": op_readdir_seek,
        "getcwd": op_getcwd, "read": op_read, "pread": op_pread, "open": op_open,
        "write": op_write, "append": op_append, "editor-save": op_editor_save,
        "holes": op_holes, "open-across-copy-up": op_open_across_copy_up,
        "open-fifo": op_open_fifo, "erofs": op_erofs,
    }
    if op == "stat-names":
        out = []
        for p in a:
            try:
                os.lstat(j(p))
                out.append(f"{p}=found")
            except OSError as e:
                out.append(f"{p}={ename(e)}")
        return "ok " + " ".join(out)
    if op == "statfs":
        return op_statfs(root, upper)
    if op in with_root:
        r = with_root[op](root, *a)
    else:
        r = simple[op](*a)
    return "ok" if r is None else r


def run(name, root, upper=None):
    sc = SCENARIOS[name]
    if sc.get("user"):
        os.setgroups([])
        os.setgid(sc["user"])
        os.setuid(sc["user"])
    os.umask(0o022)
    for i, step in enumerate(sc["steps"]):
        label = " ".join([step["op"]] + [str(x) for x in step["args"]]).replace("\n", "\\n")
        if step["op"] == "manifest":
            try:
                for line in op_manifest(root, step["args"][0]):
                    print(f"{i:02d}\tM\t{line}")
            except OSError as e:
                print(f"{i:02d}\t{label}\t{ename(e)}")
            continue
        try:
            r = run_step(root, step, upper)
        except OSError as e:
            r = ename(e)
        print(f"{i:02d}\t{label}\t{r}", flush=True)
    return 0


def _parse(path):
    steps = {}
    with open(path, errors="surrogateescape") as fh:
        for line in fh.read().splitlines():
            if not line:
                continue
            k, rest = line.split("\t", 1)
            steps.setdefault(int(k), []).append(rest)
    return steps


GLOBAL_IGNORE = {
    # A merged directory has st_nlink 1 on Linux (I6); a plain one counts
    # its subdirectories.
    "plain": ("d:nlink",),
}


def _blank_manifest_line(line, ignores):
    path, rest = line.split("\t", 1)[1].split("\t", 1)
    cols = rest.split("\t")
    for item in ignores:
        t, _, fld = item.partition(":")
        if cols[0] == t and fld == "nlink":
            cols[4] = "*"
    return "M\t" + path + "\t" + "\t".join(cols)


def _blank_stat(result, ignores):
    if not result.startswith("ok "):
        return result
    return "ok " + _blank_manifest_line("M\t-\t" + result[3:], ignores).split("\t", 2)[2]


def _listed_unstatable(a):
    """Paths the Linux log lists but cannot stat.  Linux reads a lower-only
    directory with one lower layer by passthrough, so it lists a whiteout
    there that lookup hides (D9).  A plain directory has no such name, and
    the translator must hide it in both, so the "strict" tag expects the
    Linux record without these names."""
    hidden = set()
    for lines in a.values():
        for x in lines:
            parts = x.split("\t")
            if parts[0] == "M" and len(parts) == 3 and parts[1] != "!" and parts[2] in errno.errorcode.values():
                hidden.add(parts[1])
    return hidden


def _strict(op, args, want, hidden):
    if op == "readdir":
        drop = {os.path.basename(h) for h in hidden if (os.path.dirname(h) or ".") == str(args[0])}
        head, _, names = want.partition(" dups=0 ")
        if drop and names and not names.startswith("n="):
            return head + " dups=0 " + " ".join(n for n in names.split(" ") if n not in drop)
    if op == "lookup-all" and want.startswith("bad "):
        if all(x.split(":")[0] in hidden for x in want[4:].split("; ")):
            return "ok"
    return want


def compare(name, a_path, b_path, tags):
    """Exit status 0 when the logs agree up to the scenario's listed
    differences for TAGS.  Prints each difference it allowed and each it
    did not."""
    sc = SCENARIOS[name]
    a, b = _parse(a_path), _parse(b_path)
    if not a:
        print(f"compare: {a_path} is empty; refusing to call that a match")
        return 1
    gign = [g for t in tags for g in GLOBAL_IGNORE.get(t, ())]
    hidden = _listed_unstatable(a) if "strict" in tags else set()
    ignored = [p for t in tags for p in sc.get("ignore", {}).get(t, ())]
    bad = 0
    for i, step in enumerate(sc["steps"]):
        for t in tags:
            ignored += step["ignore"].get(t, ())
        la, lb = a.get(i, []), b.get(i, [])
        if step["op"] == "manifest" and la and la[0].startswith("M\t"):
            def keep(line):
                p = line.split("\t", 2)[1]
                if p == "!" and line.split("\t")[2].split(":")[0] in hidden:
                    return False
                return p not in hidden and not any(p == x or p.startswith(x + "/") for x in ignored)
            la = [_blank_manifest_line(x, gign) for x in la if keep(x)]
            lb = [_blank_manifest_line(x, gign) for x in lb if x.startswith("M\t") and keep(x)]
            if la != lb:
                bad += 1
                sa, sb = set(la), set(lb)
                print(f"step {i:02d} manifest differs:")
                for x in sorted(sa - sb)[:30]:
                    print(f"   linux: {x}")
                for x in sorted(sb - sa)[:30]:
                    print(f"   other: {x}")
            continue
        # A record is LABEL<TAB>RESULT and a stat RESULT holds tabs itself.
        label, _, want = la[0].partition("\t") if la else (step["op"], "", "(missing)")
        got = lb[0].partition("\t")[2] if lb else "(missing)"
        if step["op"] == "stat":
            want, got = (_blank_stat(r, gign) for r in (want, got))
        if hidden:
            strict = _strict(step["op"], step["args"], want, hidden)
            if strict != want and (got == strict or (strict == "ok" and got.startswith("ok "))):
                print(f"step {i:02d} {label}: allowed difference (Linux lists a whiteout that "
                      f"lookup hides, D9): linux {want}, other {got}")
                continue
        allowed = [step["diff"][t] for t in tags if t in step["diff"]]
        if got == want:
            continue
        if allowed and (allowed[0] is None or got == allowed[0]):
            print(f"step {i:02d} {label}: allowed difference ({step['why']}): linux {want}, other {got}")
            continue
        bad += 1
        print(f"step {i:02d} {label}: linux {want} | other {got}")
    print(f"compare: {name}: {'agrees' if not bad else f'{bad} steps differ'}")
    return 1 if bad else 0


def upper_state(upper, convention):
    """Meaning, not bytes: a whiteout is a 0:0 char device; a directory is
    opaque by the xattr (linux) or the marker entry (hurd), and the hurd
    marker itself is not an entry."""
    paths, errs = walk(upper)
    out = []
    for p in paths:
        full = os.path.join(upper, p)
        if os.path.basename(p) == L.OPAQUE_MARKER and convention == "hurd":
            continue
        st = os.lstat(full)
        t = L_type(st.st_mode)
        if t == "c" and st.st_rdev == os.makedev(0, 0):
            out.append(f"{p}\twhiteout")
        elif t == "d":
            if convention == "linux":
                try:
                    opq = os.getxattr(full, "trusted.overlay.opaque", follow_symlinks=False) == b"y"
                except OSError:
                    opq = False
            else:
                opq = os.path.lexists(os.path.join(full, L.OPAQUE_MARKER))
            out.append(f"{p}\t{'opaque' if opq else 'd'}")
        else:
            out.append(f"{p}\t{t}")
    return out + [f"!\t{e}" for e in errs]


def ids(root):
    def one():
        seen = {}
        dev = set()
        dmis = 0
        paths, errs = walk(root)
        for p in paths:
            full = os.path.join(root, p)
            st = os.lstat(full)
            seen[p] = st.st_ino
            dev.add(st.st_dev)
            if stat.S_ISDIR(st.st_mode):
                d = Dir(full)
                try:
                    for n, dino in d.read():
                        if n in (".", ".."):
                            continue
                        if dino != os.lstat(os.path.join(full, n)).st_ino:
                            dmis += 1
                finally:
                    d.close()
        return seen, dev, dmis, errs
    s1, dev1, dmis, errs = one()
    s2, _, _, _ = one()
    inos = list(s1.values())
    unique = len(inos) == len(set(inos))
    # Hard links share an inode number; the description has one pair.
    dupl = len(inos) - len(set(inos))
    print(f"entries={len(s1)} distinct-ino={len(set(inos))} shared={dupl} "
          f"stable={'yes' if s1 == s2 else 'no'} devs={len(dev1)} dino-mismatch={dmis} "
          f"errors={len(errs)}")
    return 0 if unique or dupl <= 1 else 1


def _stress(args):
    seed, root, secs, write = args
    rng = random.Random(seed)
    paths, _ = walk(root)
    end = time.time() + secs
    count, unexpected = 0, {}
    expected = {errno.ENOENT, errno.EEXIST, errno.ENOTEMPTY, errno.EXDEV, errno.EISDIR,
                errno.ENOTDIR} if write else {errno.EACCES}
    shared = os.path.join(root, "a")
    while time.time() < end:
        try:
            p = os.path.join(root, rng.choice(paths))
            k = rng.randrange(6 if write else 3)
            if k == 0:
                os.lstat(p)
            elif k == 1:
                if os.path.isfile(p) and not os.path.islink(p):
                    with open(p, "rb") as fh:
                        fh.read(65536)
            elif k == 2:
                if os.path.isdir(p) and not os.path.islink(p):
                    os.listdir(p)
            elif k == 3:
                n = os.path.join(shared, f"s{rng.randrange(20)}")
                with open(n, "w") as fh:
                    fh.write(str(seed))
            elif k == 4:
                os.rename(os.path.join(shared, f"s{rng.randrange(20)}"),
                          os.path.join(shared, f"s{rng.randrange(20)}"))
            else:
                os.unlink(os.path.join(shared, f"s{rng.randrange(20)}"))
        except OSError as e:
            if e.errno not in expected:
                name = ename(e)
                unexpected[name] = unexpected.get(name, 0) + 1
        count += 1
    return count, unexpected


def _copyup_race(args):
    root, names, barrier = args
    out = {}
    for n in names:
        try:
            with open(os.path.join(root, n), "ab") as fh:
                fh.write(b"")
        except OSError as e:
            out[ename(e)] = out.get(ename(e), 0) + 1
    return out


def stress(root, secs, procs, write):
    import multiprocessing
    race = {}
    if write:
        lowers = ["small", "medium", "both", "old", "future", "d/shared", "a/b/c/deep", "big2m"]
        with multiprocessing.Pool(procs) as pool:
            for r in pool.map(_copyup_race, [(root, lowers, None)] * procs):
                for k, v in r.items():
                    race[k] = race.get(k, 0) + v
    with multiprocessing.Pool(procs) as pool:
        results = pool.map(_stress, [(i, root, secs, write) for i in range(procs)])
    unexpected = dict(race)
    for _, e in results:
        for k, v in e.items():
            unexpected[k] = unexpected.get(k, 0) + v
    paths, errs = walk(root)
    lost = []
    for p in paths:
        try:
            os.lstat(os.path.join(root, p))
        except OSError as e:
            lost.append(f"{p}:{ename(e)}")
    print(f"ops={sum(c for c, _ in results)} unexpected={unexpected} walk-errors={errs[:5]} "
          f"listed-but-missing={lost[:5]} dups={len(paths) - len(set(paths))}")
    return 0 if not unexpected and not errs and not lost else 1


MUTATING = {"open", "write", "append", "truncate", "chmod", "chown", "lchown", "utime", "unlink",
            "rmdir", "mkdir", "rmtree", "rename", "symlink", "link", "mkfifo", "mknod",
            "editor-save", "open-across-copy-up"}


def mutating(name):
    for i, step in enumerate(SCENARIOS[name]["steps"]):
        if step["op"] in MUTATING and not (step["op"] == "open" and step["args"][1] == "RDONLY"):
            print(i)
    return 0


def view_class(before, after, observed):
    """For the fault sweep: an operation that failed or was cut short must
    leave the whole view as it was before or as it is after; anything else
    is a mix that lost a name or showed a deleted one."""
    def load(p):
        with open(p) as fh:
            return sorted(l.split("\t", 2)[2] for l in fh.read().splitlines() if "\tM\t" in l)
    b, a, o = load(before), load(after), load(observed)
    if o == b:
        return "before"
    if o == a:
        return "after"
    return "mixed"


def fault_selftest(root):
    """Hand-made states for view_class: the two whole states must be
    accepted and every half-done state of a rename over an existing name
    and of a copy-up must be called mixed."""
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    tree = os.path.join(root, "t")

    def state(fn, out):
        shutil.rmtree(tree, ignore_errors=True)
        os.makedirs(tree)
        for n, data in (("src", "S"), ("dst", "D"), ("keep", "K")):
            with open(os.path.join(tree, n), "w") as fh:
                fh.write(data)
        fn()
        with open(out, "w") as fh:
            for line in op_manifest(tree, "."):
                fh.write(f"00\tM\t{line}\n")
        return out
    p = lambda n: os.path.join(tree, n)
    before = state(lambda: None, os.path.join(root, "before"))
    after = state(lambda: os.rename(p("src"), p("dst")), os.path.join(root, "after"))
    half = [
        ("both names, source copied", lambda: shutil.copy(p("src"), p("dst"))),
        ("neither name", lambda: (os.unlink(p("src")), os.unlink(p("dst")))),
        ("target removed only", lambda: os.unlink(p("dst"))),
        ("a bystander lost", lambda: (os.rename(p("src"), p("dst")), os.unlink(p("keep")))),
        ("torn data", lambda: (os.unlink(p("src")), open(p("dst"), "w").write(""))),
    ]
    bad = []
    for label, want, fn in [("whole before", "before", lambda: None),
                            ("whole after", "after", lambda: os.rename(p("src"), p("dst")))] + \
            [(lab, "mixed", fn) for lab, fn in half]:
        got = view_class(before, after, state(fn, os.path.join(root, "observed")))
        print(f"fault-selftest: {label}: {got}")
        if got != want:
            bad.append(label)
    print("fault-selftest: ok" if not bad else f"fault-selftest: wrong for {bad}")
    return 1 if bad else 0


# name, must, setup, options, during.  must: "refuse" (a bad invocation)
# or "survive" (a hostile layout: refused, or served without a hang or
# crash; the result is recorded beside Linux's).  setup and during are sh
# run in the run's directory; a part "@overlay DIR OPTIONS" of setup (parts
# split on ";;") mounts an overlay there first, and $TMPFS names a tmpfs
# the driver mounted.  during runs after the mount, before the walk.
MOUNT_ARGS = [
    ("missing-lowerdir", "refuse", "mkdir u w", "upperdir=u,workdir=w", ""),
    ("lower-not-a-directory", "refuse", "mkdir l; echo x > f", "lowerdir=f:l", ""),
    ("same-lower-twice", "refuse", "mkdir l", "lowerdir=l:l", ""),
    ("too-many-layers", "refuse", "i=1; while [ $i -le 501 ]; do mkdir $i; i=$((i+1)); done",
     "lowerdir=" + ":".join(str(i) for i in range(1, 502)), ""),
    ("upper-equals-lower", "survive", "mkdir l w; echo x > l/f", "lowerdir=l,upperdir=l,workdir=w", ""),
    ("upper-inside-lower", "survive", "mkdir -p l/u w; echo x > l/f", "lowerdir=l,upperdir=l/u,workdir=w", ""),
    ("upper-inside-mount-point", "survive", "mkdir -p l mnt/u mnt/w; echo x > l/f",
     "lowerdir=l,upperdir=mnt/u,workdir=mnt/w", ""),
    ("work-inside-upper", "survive", "mkdir -p l u/w; echo x > l/f", "lowerdir=l,upperdir=u,workdir=u/w", ""),
    ("work-on-another-filesystem", "survive", "mkdir -p l u $TMPFS/w; echo x > l/f",
     "lowerdir=l,upperdir=u,workdir=$TMPFS/w", ""),
    ("lower-is-an-overlay", "survive", "mkdir -p a1 a2 inner b; echo a > a1/f; echo b > b/g;;"
     "@overlay inner lowerdir=a1:a2", "lowerdir=inner:b", ""),
    ("layer-changed-underneath", "survive", "mkdir l1 l2 u w; echo a > l1/f; echo s > l1/stays",
     "lowerdir=l1:l2,upperdir=u,workdir=w",
     "cat mnt/f >/dev/null; echo new > l1/added; rm l1/f; mkdir l1/f; ls mnt >/dev/null; cat mnt/added"),
    ("symlink-loop-in-a-layer", "survive", "mkdir l1 l2; ln -s loop l1/loop; ln -s b l1/a; ln -s a l1/b",
     "lowerdir=l1:l2", "cat mnt/loop; cat mnt/a; true"),
]


def plan():
    """Every (set, mode, scenario) run, in order.  Mode ro mounts a set
    with no upper (an existing upper becomes the topmost lower); rw
    mounts it with its upper."""
    runs = []
    for set_name, (_, _, kind, stages) in L.SETS.items():
        modes = (["ro"] if 1 in stages else []) + (["rw"] if 2 in stages else [])
        for mode in modes:
            for n, sc in SCENARIOS.items():
                want = sc["sets"]
                if isinstance(want, tuple):
                    ok = set_name in want and mode == "rw"
                elif set_name == "enospc":
                    ok = False
                else:
                    ok = want == "all" or want == mode
                if ok:
                    runs.append((set_name, mode, n))
    return runs


def _opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "scenarios":
        for n, sc in SCENARIOS.items():
            if len(argv) > 2 and str(sc["stage"]) != argv[2]:
                continue
            sets = sc["sets"] if isinstance(sc["sets"], str) else ",".join(sc["sets"])
            print(f"{n}\t{sc['stage']}\t{sets}\t{'plain' if sc.get('plain', True) else '-'}")
        return 0
    if cmd == "mount-args":
        for row in MOUNT_ARGS:
            print("\t".join(row))
        return 0
    if cmd == "plan":
        for r in plan():
            sc = SCENARIOS[r[2]]
            print("\t".join(r) + f"\t{sc['stage']}\t{'plain' if sc.get('plain', True) else '-'}")
        return 0
    if cmd == "run" and len(argv) >= 4:
        return run(argv[2], argv[3], _opt(argv, "--upper"))
    if cmd == "compare" and len(argv) >= 5:
        return compare(argv[2], argv[3], argv[4], (_opt(argv, "--tags") or "").split(","))
    if cmd == "upper-state" and len(argv) >= 3:
        print("\n".join(upper_state(argv[2], _opt(argv, "--convention"))))
        return 0
    if cmd == "ids" and len(argv) == 3:
        return ids(argv[2])
    if cmd in ("stress-ro", "stress-rw") and len(argv) == 5:
        return stress(argv[2], float(argv[3]), int(argv[4]), cmd == "stress-rw")
    if cmd == "mutating" and len(argv) == 3:
        return mutating(argv[2])
    if cmd == "view-class" and len(argv) == 5:
        print(view_class(argv[2], argv[3], argv[4]))
        return 0
    if cmd == "fault-selftest" and len(argv) == 3:
        return fault_selftest(argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
