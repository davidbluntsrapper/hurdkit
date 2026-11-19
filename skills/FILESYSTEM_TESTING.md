# Skill: Test a Hurd filesystem translator against Linux

## Trigger

Use this skill when you change a disk filesystem translator (`fatfs` in a
Hurd tree, or `exfatfs` or `squashfs` in its own repo) and need to prove
that it reads and writes images the way Linux does. Also use it when you
add a filesystem to the test. `make exfatfs-test` covers fatfs and exfatfs.
`make squashfs-test` covers squashfs and is described in
[The squashfs test](#the-squashfs-test). `make overlayfs-test` covers the
overlayfs union translator and is described in
[The overlayfs test](#the-overlayfs-test).

## Run the test

`make exfatfs-test` builds the translators, boots a throwaway overlay of
the packages image, and runs `scripts/exfatfs_test.sh`:

```sh
make exfatfs-test EXFATFS_DIR=~/src/exfatfs        # fatfs and exfatfs
make exfatfs-test FS=fat                           # the fatfs control only
make exfatfs-test FS=exfat                         # exfatfs only
make exfatfs-test EXFAT_WRITE=0                    # exFAT read checks only
make exfatfs-test REUSE=1                          # rerun against a failed run's guest
```

fatfs is built from `HURD_TREE`, by default the pinned `3p/hurd`.
exfatfs is built from its own repo,
[exfatfs](https://github.com/hurdkit/exfatfs), checked out at
`EXFATFS_DIR`.

`FS=fat` builds and tests fatfs alone. With the pinned tree it builds,
but `fat: mount` fails until the fatfs fix lands (see Gotchas).

The run ends with `exfatfs-test: N passed, M failed` and exits non-zero on
any failure. Each `PASS` and `FAIL` line names one check.

A check can also end `KNOWN`: it fails because of a gap in an interface
outside the translator, such as glibc's, which the translator cannot
close. `known NAME REASON STATUS` in `scripts/fs_test_lib.sh` records it.
A known check still runs its comparison, and STATUS is the comparison's
exit status. While the comparison fails, the check prints `KNOWN` with
the reason, the summary counts it apart (`N passed, 0 failed, 2 known
gaps`), and the run does not fail. When the comparison passes, the gap
has closed, and the check fails with `known gap closed; promote this
check` until it is made an ordinary `pass`/`fail` check. Use `known` only
for a gap the translator cannot fix, and give the reason with the file
or interface that causes it.

The work files (images, manifests, the written image) stay in
`img/tmp/exfatfs-test/` for inspection, and the QEMU log is
`img/tmp/exfatfs-test-qemu.log`.

The target takes these variables:

| Variable | Default | Meaning |
|---|---|---|
| `HURD_TREE` | `3p/hurd` | Hurd source tree fatfs is built from |
| `EXFATFS_DIR` | `~/src/exfatfs` | Checkout of the exfatfs repo |
| `TEST_IMG` | `img/hurd.packages.qcow2` | Image the guest overlay is based on |
| `SSH_KEY_DIR` | `img/keys` | Directory that holds `root_ed25519` |
| `SSH_PORT` | `2222` | Host port forwarded to the guest's sshd |
| `BUILD` | `~/work/x86_64-gnu-build` | Cross build root; fatfs goes to `$(BUILD)/hurd-translators` |
| `FS` | `fat exfat` | Filesystems to build and test |
| `EXFAT_WRITE` | `1` | `0` skips the exFAT write checks |
| `REUSE` | `0` | `1` runs the checks against the guest a failed run left running |
| `GUEST_TIMEOUT` | `300` | Seconds each guest step may take before its check fails |

If another guest already uses port 2222, pass a free `SSH_PORT`. Two runs
from the same checkout share `img/tmp/exfatfs-test-qemu.pid` and cannot
run at the same time. Give each concurrent run its own checkout and its
own `BUILD`. `make exfatfs` builds in `EXFATFS_DIR` itself, so
concurrent runs also need their own exfatfs checkout.

### After a failed run

A failed run leaves its guest running, so you can inspect the mounts with
the `ssh` command it prints. The next run refuses to start while that
guest is up. Do one of these:

- Run again with `REUSE=1`. The checks run against the same guest. The
  run first clears every mount point the test uses, then copies the
  translators and images again. A successful `REUSE=1` run also leaves
  the guest running.
- Stop the guest with `/bin/kill $(cat img/tmp/exfatfs-test-qemu.pid)`,
  then run without `REUSE`. Do this when a translator in the guest is
  stopped or hung, because clearing its mount point would hang too.

A guest step that fails (an `ssh` or `scp` error, a crashed translator, or
a step that takes longer than `GUEST_TIMEOUT`) fails the check it belongs
to. The run still prints the summary and keeps the guest.

The host loop mounts all use `img/tmp/exfatfs-test/mnt`. The script
unmounts it on exit and before it deletes the work directory, so an
aborted run does not leave a loop mount behind.

## How the translators are built

`make -C toolchain fs-translators` copies `HURD_TREE` to
`$(BUILD)/hurd-translators-src`, configures the copy out of tree for
`x86_64-gnu`, builds the libraries a libdiskfs translator links, and
builds `fatfs/fatfs.static`. The binaries are static, so they run in the
guest whatever Hurd libraries the Debian image ships. The `storeio` target
uses the same `hurd-configure` recipe.

`make -C toolchain exfatfs` runs `make` in `EXFATFS_DIR` with
`HURD_PREFIX` set to the cross prefix, the way the `9pfs` target builds
`3p/9pfs`. The exfatfs repo links libdiskfs, libstore, and libhurdbugaddr
from the prefix. The `hurd` stage does not install them, so the
`libdiskfs` target installs them from `3p/hurd` the first time. The
static binary links the store types that the prefix's libstore has:
`device` and `remap`, plus `gunzip` if zlib was in the prefix when
libstore was built.

`make -C toolchain squashfs` builds `SQUASHFS_DIR` the same way. It runs
`compress-libs` first (see
[How the compression libraries are built](#how-the-compression-libraries-are-built)),
so a first `libdiskfs` build finds zlib and libstore gets `gunzip`. It
asks the squashfs repo for all five compressors, so a missing library
fails the build instead of leaving its compressor out.

The top-level target builds only the translators that `FS` names
(`<fs>fs` for each `<fs>`). Before any build step it checks that
`HURD_TREE` has each in-tree one, and stops if not.

The copy and the build directory are recreated on every run, so a run
never depends on an earlier build. A full build takes about 15 seconds of
wall time on a 12-thread host. The copy is taken when the build starts,
so edits to `HURD_TREE` after that point are not in the binaries.

The copy is `cp -a`, so it includes any build products in `HURD_TREE`. A
tree that was configured and built in place (it has a `config.status`)
still works: a test with such a copy of `3p/hurd` built fatfs, because
the out-of-tree build compiles every object again in its own directory.

## What the test checks

The fatfs run is the control. It proves the build, the copy into the
guest, and the file-store mount before exfatfs is judged.

| Check | What must hold |
|---|---|
| `fat: mount` | fatfs mounts a FAT16 image read-only on a file store |
| `fat: manifest` | Every file's type, size, and sha256 match the host's copy (by content, since fatfs serves only 8.3 names) |
| `exfat: mount` | exfatfs mounts the exFAT image read-only |
| `exfat: manifest` | Every path's type, size, mtime, and sha256 match the Linux kernel driver's view |
| `exfat: case-insensitive lookup` | `MixedCase.TXT` and `mixedcase.txt` open the same file |
| `exfat: a name past NAME_MAX is hidden, its sibling still listed` | A 304-byte UTF-8 name is left out of the listing and its directory still lists |
| `exfat: writes refused with EROFS` | `touch` on the read-only mount fails with `Read-only file system` |
| `exfat: refuses bad-name.img` | A wrong file system name gives the translator's own error and a non-zero exit, not a crash |
| `exfat: refuses bad-checksum.img` | Same, for a wrong boot checksum |
| `exfat: refuses truncated.img` | Same, for a volume longer than its file |
| `exfat: original mount still serves after the corrupt-image attempts` | The first mount still answers |
| `exfat-rw: read-write mount` | exfatfs mounts a fresh copy read-write |
| `exfat-rw: operations` | `fs_image_test.py mutate` runs without error in the guest |
| `exfat-rw: guest view matches expected` | The guest's manifest equals the same operations applied to a host directory |
| `exfat-rw: clean unmount` | `settrans -g` succeeds |
| `exfat-rw: guest view after remount` | The manifest is unchanged after a remount |
| `exfat-rw: fsck.exfat clean, VolumeDirty cleared` | `fsck.exfat -n` accepts the written image, and VolumeDirty is clear after the clean unmount |
| `exfat-rw: Linux reads back what Hurd wrote` | The Linux driver's manifest of the written image equals the expected one |
| `exfat-rw: a dirty volume is written with a warning and stays dirty` | A volume with VolumeDirty set mounts writable and says it was not unmounted cleanly and to run `fsck.exfat`, as the Linux driver does; after a write and `settrans -g`, VolumeDirty is still set and `fsck.exfat -n` accepts the image |
| `exfat-rw: Linux reads back what Hurd wrote to the dirty volume` | The Linux driver reads the file written there |
| `exfat-rw: reading a read-write mount leaves the image unchanged` | Reads on a read-write mount write no byte to the image |

`scripts/fs_image_test.py` runs on both the host and the guest, so the
manifest format is the same on both sides. A manifest line is
`type size mtime sha256 path`, tab separated and sorted by path.
`fs_image_test.py compare` refuses an empty manifest as a match.

The test tree (`fs_image_test.py tree`) has one entry per reader path:
an empty file, files of one cluster and one cluster plus one byte, a 5 MiB
file, a 204-character name, BMP and surrogate-pair names, a name of
exactly 255 UTF-8 bytes, 300 entries in one directory, and a file whose
clusters are not contiguous.

## The oracles

The test trusts three programs that do not share code with the Hurd:

- The Linux kernel exfat driver, through `sudo mount -t exfat -o loop`.
  It fills the source image and reads back the image that Hurd wrote.
  The mounts use `time_offset=0`, so mtimes compare as UTC.
- `fsck.exfat -n` from exfatprogs. It checks the source image before the
  guest sees it and the written image after.
- `filefrag`, through sudo. It proves that `frag.bin` has more than one
  extent. If it does not, the run stops before the guest boots.

The expected state after the write operations is the same operations
applied by Python to a plain host copy of the image's contents.

## Host prerequisites

- The cross toolchain in `~/opt/x86_64-gnu` (`make toolchain`) and
  autoconf and automake for the out-of-tree configure.
- The packages image and its keys (`make packages`).
- Passwordless sudo for `mount`, `umount`, and `filefrag`, and the Linux
  `exfat` kernel module.
- `exfatprogs` (`mkfs.exfat`, `fsck.exfat`), `dosfstools` (`mkfs.vfat`),
  `mtools` (`mcopy`), and `e2fsprogs` (`filefrag`).
- Python 3.9 or later on the host. The guest gets `python3` from the
  packages stage.

## Add a filesystem

1. Make sure the translator is the directory `<fs>fs` in the Hurd tree
   and that the directory builds `<fs>fs.static`. The build and the test
   both derive the name from the entry `<fs>` in `FS`. A translator in
   its own repo, like exfatfs or squashfs, needs its own toolchain target and its own
   branch in the `bins` loop of the script instead.
2. Add a `case` branch to the image loop in `scripts/exfatfs_test.sh`. It
   must leave `$WORK/<fs>.img` and the host's manifest in
   `$WORK/host-<fs>.txt`. Build the image with a tool that does not share
   code with the translator, and take the manifest from an independent
   reader of the image.
3. Add `<fs>` to the default `FS` in the top-level `Makefile` and in the
   script, and `<fs>fs` to the default `FS_TRANSLATORS` in
   `toolchain/Makefile`.

The mount and manifest checks then run for the new filesystem. Add checks
for its own behavior after the exFAT blocks, with a `pass` or `fail`
line per check.

## The squashfs test

`make squashfs-test` checks the read-only `squashfs` translator against
the Linux kernel squashfs driver. The goal is parity with Linux: the
guest's view of every image must equal the Linux view, field by field.

### Run it

```sh
make squashfs-test SQUASHFS_DIR=~/src/squashfs    # build, boot, run every check
make squashfs-test REUSE=1                         # rerun against a failed run's guest
make squashfs-test FUZZ_SEED=7 FUZZ_COUNT=500
```

The translator comes from its own repo,
[squashfs](https://github.com/hurdkit/squashfs), checked out at
`SQUASHFS_DIR`. The target builds it with `make squashfs` (see
[How the translators are built](#how-the-translators-are-built)) and
takes `squashfs.static` from that checkout. `make squashfs` builds in
`SQUASHFS_DIR` itself, so concurrent runs need their own squashfs
checkout. The other variables are the ones in the table above
(`TEST_IMG`, `SSH_KEY_DIR`, `SSH_PORT`, `REUSE`, `GUEST_TIMEOUT`), plus:

| Variable | Default | Meaning |
|---|---|---|
| `SQUASHFS_DIR` | `~/src/squashfs` | Checkout of the squashfs repo |
| `FUZZ_SEED` | `1` | Seed of the fuzz images; copy k of seed s is the same on every run |
| `FUZZ_COUNT` | `200` | Number of fuzz images |
| `SQUASHFS_BIN` | `~/src/squashfs/squashfs.static` | Script only: the binary used as the translator; the target sets it to `SQUASHFS_DIR/squashfs.static` |
| `LINK_SECS` | `60` | Script only: how long the hard-link check runs its 16 processes of lookups |
| `CROSS_CC` | `~/opt/x86_64-gnu/bin/x86_64-gnu-gcc` | Script only: compiler for `scripts/fs_probe.c` |

To check the harness itself, or to run it without a build, call the
script with any binary as the translator:

```sh
SQUASHFS_BIN=/path/to/stub SSH_PORT=2241 SSH_KEY_DIR=... BASE_IMG=... \
    bash scripts/squashfs_test.sh
```

The work files stay in `img/tmp/squashfs-test/`: the source trees
(`src-*`), the images (`img/`, `corrupt/`, `fuzz/`), the expected,
Linux and guest manifests (`expect-*.txt`, `linux-*.txt`,
`guest-*.txt`), each comparison's diff (`compare-*.txt`) and the fuzz
results (`fuzz-results.txt`). The QEMU log is
`img/tmp/squashfs-test-qemu.log` and the pidfile
`img/tmp/squashfs-test-qemu.pid`.

### The images

`scripts/squashfs_images.py` writes four source trees and the pseudo
definitions for each. mksquashfs builds the images without root: owners,
device nodes, modes that the user cannot read and xattrs come from
pseudo definitions (`-pf`). One list in the script (`pseudo_defs`)
produces both the definitions and the changes to the expected manifest,
so the two cannot disagree.

| Tree | What it holds | Images |
|---|---|---|
| `main` | Empty and 1-byte files; files one byte under, at and over 4 KiB, 128 KiB and 1 MiB; a 5 MiB random file; an 8 MiB compressible file; two identical files; sparse files with holes inside and at the end; 3500 entries in one directory; 40 nested directories; an empty directory; 255-byte ASCII and UTF-8 names; other UTF-8 names; short, 1500-byte, dangling and directory symlinks; three hard links to one file; a fifo; a socket; char and block devices, some with large major and minor numbers; 300 files with distinct uids and gids; setuid, setgid and sticky bits, mode 0000; mtimes 0, 4000000000 and 4294967295; a static Hurd binary and a shell script | One per entry of `VARIANTS`: gzip, lzo, xz, lz4 and zstd, each with default and non-default options (`-Xcompression-level`, `-Xwindow-size`, `-Xstrategy`, `-Xalgorithm`, `-Xbcj x86,arm`, `-Xdict-size`, `-Xhc`); uncompressed (`-noI -noD -noF -noX`); `-b 4K`; `-b 1M`; `-no-fragments`; `-always-use-fragments -tailends`; `-no-duplicates`; `-no-exports -no-sparse` |
| `wide` | 72173 inodes in 71 directories, 4201 distinct ids (the id table spans three metadata blocks), and a 5 GiB sparse file with data across the 4 GiB boundary | `wide.img` (gzip) |
| `xattr` | `user.*`, `trusted.*` and `security.*` xattrs on files and a directory, a 6148-byte value, 120 xattrs on one file, two files with the same set, an empty value | `xattr.img` (xz) |
| `small` | A few files, a compressible multi-block file, one fragment, three ids | `small.img` (gzip), the source of the corrupt and fuzz images; `lzma.img` (compressor id 2, which Linux does not support); `compressor-99.img` |
| `links` | One file with 2000 names in one directory | `links.img` (gzip) |

`squashfs_images.py craft KIND IMG` writes three images by hand, field by
field, that no tool writes: `dirloop.img`, where directory `a` holds `b`
and `b` holds `a` again; `unsorted.img`, whose directory `d` lists
`a1 b2 b10 b1 c3 b4` in that order, which is not strcmp order; and
`huge.img`, whose file claims 2^62 bytes and lists one block.

### What the squashfs test checks

Every check name starts with `squash:`.

| Check | What must hold |
|---|---|
| `oracle: Linux reads NAME.img as the source tree` | Host only. The Linux manifest equals the source tree's with the pseudo definitions applied (inode numbers, st_blocks, and directory sizes and link counts left out, because the source tree cannot know them) |
| `oracle: Linux reads the xattrs of the pseudo definitions` | Host only. Every xattr, read as root, has the defined length and sha256 |
| `oracle: unsquashfs extracts gzip.img as the source tree` | Host only. `sudo unsquashfs` gives the same manifest |
| `oracle: Linux refuses lzma.img`, `... compressor-99.img` | Host only. Linux does not mount them |
| `NAME: mount` | The translator mounts the image read-only on a file store |
| `Hurd interface: every device number fits the Hurd's dev_t` | Host only, and `KNOWN` while it fails. glibc on the Hurd keeps an 8-bit major number and a minor number without its bits 8 to 15 (`mmmmMMmm`), so a device number outside that cannot reach any Hurd program. This check lists the image's numbers that do not fit, which the test tree has (`dev/big-numbers`, `dev/disk-high`) |
| `NAME: manifest matches Linux` | The guest's full manifest equals the Linux one in every field: type, size, mtime, sha256, mode with setuid, setgid and sticky bits, uid, gid, nlink (directories too), device numbers as the Hurd's `dev_t` holds Linux's (`fs_image_test.py hurd-dev`), symlink target, inode number and st_blocks |
| `lookups: a missing name gives ENOENT, .. at the root leaves the mount` | `lstat` of missing names gives ENOENT; `ROOT/..` is the mount point's parent; `DIR/..` is the root |
| `readdir (readdir1) of X lists each entry once` | `fs_probe readdir1` lists the root and the 3500-entry directory with one `dir_readdir` RPC per entry; the names equal Linux's, `.` and `..` included, each once |
| `readdir (seekdir 7) of X lists each entry once` | Same, reading 7 entries, then `telldir`, `closedir`, `opendir` and `seekdir` back |
| `exec a static binary from the image`, `exec a script from the image` | Both run from the mount and print their marker |
| `mmap reads the same bytes as Linux` | The sha256 of five files read through `mmap` equals Linux's |
| `writes refused with EROFS` | Create, open for write, truncate, unlink, chmod, chown, utime, rename, link, symlink, mkdir, rmdir and mkfifo all fail with EROFS |
| `8 parallel readers get the single reader's manifest` | Eight full manifests taken at once all equal the one taken alone, so a concurrency bug shows apart from a reader bug |
| `statvfs gives the flags Linux gives` | `os.statvfs` of the gzip mount sets the same `ST_*` flags, by name, as on Linux (`ST_RDONLY ST_RELATIME`; Linux and the Hurd number them differently) |
| `xattr: user, trusted and security xattrs match Linux` | `KNOWN` while it fails: glibc's `hurd/xattr.c` passes only `gnu.*` names. `fs_probe xattrs` (glibc `llistxattr` and `lgetxattr`, as any Hurd program sees them) must list the same names, lengths and sha256 of values as Linux, for every entry of the image |
| `hard links: 16 processes of lookups for Ns, and the mount still answers` | 16 processes `lstat` random names of `links.img` for `LINK_SECS` seconds without an error, under a time bound, and a `ls` of the mount then answers. A translator that locks the child and then the directory again, against the order of libdiskfs's name cache, wedges within seconds |
| `a directory loop gives EIO, and 16 processes walking it do not hang the mount` | `a/b/a` of `dirloop.img` gives EIO (Linux gives ELOOP), 16 processes `lstat` paths through the loop and their `..` for 30 s, and the mount then answers; refusing the image passes too |
| `a directory not in strcmp order: the names Linux finds are found` | For each name of `unsorted.img`'s `d`, a lookup finds it or gives the same errno as on Linux, whose lookup stops only at an entry whose first byte sorts after the name's |
| `a file claiming 2^62 bytes gives EIO` | `lstat` of `huge.img`'s file gives EIO within a time bound, and the mount then answers; refusing the image passes too. Linux is not asked: reading that file hangs Linux 7.0's reader |
| `refuses lzma.img, naming the compressor`, `refuses compressor-99.img, ...` | The mount fails with the translator's own message, which names `lzma` or `99` |
| `corrupt NAME.img: at least as strict as Linux` | See below |
| `the gzip mount still serves after the corrupt images` | The first mount still answers |
| `fuzz: N corrupted images, no crash or hang` | See below |
| `clean unmount (settrans -gR) of every mounted image` | Every `settrans -gR` succeeds. `-R` because a lookup of a fifo starts `/hurd/fifo` on it below the mount, and plain `-g` then fails with EBUSY; tmpfs does the same |

The corrupt images are copies of `small.img`: truncated at half of
`bytes_used`, a bad magic, version 3.1, an id table offset past the end,
and one bit flipped in the middle of the first metadata block of the
inode, directory, fragment and id tables and in the first data block.
Before the guest boots, the run records what Linux does with each:
`refused` (no mount), `errors` (mounts, some access fails) or `served`
(every access succeeds). Where Linux refuses or errors, the translator
must refuse the mount with its own message or fail an access. Where
Linux serves, the translator may serve. It must never crash or hang:
after a walk the translator must still answer `fsysopts`. Linux serves
some of these images (it lists an empty root for the directory-table
flip, and the id table of `small.img` is stored uncompressed, so a flip
changes a uid). The recorded levels are printed as `Linux on corrupt
NAME.img: LEVEL`.

The fuzz images are `FUZZ_COUNT` copies of `small.img` with 1 to 4
random bytes replaced. `fs_image_test.py mount-walk` runs in the guest,
mounts each on its own mount point (so a hung translator does not block
the next), walks it, and checks that the translator is still alive. Each
image ends `refused`, `served`, `crash` or `hang`. Any `crash` or `hang`
fails the check, and the message names the seed. The results are in
`fuzz-results.txt`, one line per image.

### Proving the harness

Every guest check must fail, not error out, when the translator is
missing or wrong. To see the red run, use a stub as `SQUASHFS_BIN`, for
example a script that serves an empty directory:

```sh
printf '#!/bin/sh\nexec /hurd/tmpfs 64M\n' > /tmp/stub && chmod 755 /tmp/stub
SQUASHFS_BIN=/tmp/stub ... bash scripts/squashfs_test.sh
```

The `oracle:` checks still pass with a stub. They prove the oracles, not
the translator.

### How the compression libraries are built

The translator links zlib, liblzma, liblzo2, liblz4 and libzstd
statically. `make compress-libs` (`make -C toolchain compress-libs`, stages
`zlib`, `xz`, `zstd`, `lz4`, `lzo`) downloads each pinned release tarball
into `~/work/x86_64-gnu-src`, checks its sha256 against the value in
`toolchain/Makefile`, builds it in a fresh `$(BUILD)/<lib>` with the cross
compiler and installs the headers, the `.a` and the `.pc` file into
`~/opt/x86_64-gnu`. No shared objects are installed. liblzma is built
without the xz tools, and libzstd without threads. A stage skips when its
`.a` is installed, and its gate links a static GNU/Hurd program that
calls the library. To move to a new release, change the `*_VER` and
`*_SHA256` lines together.

### squashfs host prerequisites

- squashfs-tools 4.6 or later (`mksquashfs`, `unsquashfs`).
- A Linux kernel with squashfs and all five decompressors
  (`CONFIG_SQUASHFS_{ZLIB,LZO,XZ,LZ4,ZSTD}`) and `CONFIG_SQUASHFS_XATTR`.
- Passwordless sudo for `mount`, `umount`, `unsquashfs`, `rm` (of the
  extraction) and `python3` (to read files the user cannot read, and
  `trusted.*` xattrs).
- About 1 GB free under `img/tmp`, and a host filesystem with sparse
  files and `user.*` xattrs for the source trees.
- The cross compiler, for `scripts/fs_probe.c`.

### squashfs gotchas

- mksquashfs 4.6.1 corrupts its heap when it decodes a long hex (`0x`)
  xattr value in a pseudo definition, and it rejects an empty value. The
  builder encodes values in base64 (`0s`), and the empty value comes from
  a `user.*` xattr on the source file.
- A pseudo modify (`M`) of a directory conflicts with pseudo definitions
  of entries inside it, and mksquashfs then ignores it.
- mksquashfs cannot read a file the user cannot read. Give the source
  file a readable mode and set the real mode with a pseudo `M`.
- Do not loop-mount `huge.img` on the host and read its file: Linux 7.0
  serves the 2^62-byte size, and a `dd` of its first MiB stays in D state
  (the reader waits on a page that never completes).
- mksquashfs pads the image to 4 KiB, so a truncation must cut
  `bytes_used`, not the file length, or it removes only padding.
- The guest's Python (3.14) reads at most 1024 bytes in `os.readlink`
  (its `MAXPATHLEN`), so the 1539-byte symlink came back truncated.
  `fs_image_test.py` falls back to `readlink(1)` when the target is
  shorter than `st_size`.
- The guest's Python has no `os.listxattr`, so the guest side of the
  xattr check uses `fs_probe xattrs` (glibc's `llistxattr` and
  `lgetxattr`).

## The overlayfs test

`make overlayfs-test` checks the overlayfs translator, a libnetfs union
of one writable upper directory over read-only lower directories, against
Linux overlayfs in its default configuration (the host kernel, 7.0 when
the test was written). It was written before the translator, as its
acceptance contract.

### Run it

```sh
make overlayfs-test OVERLAYFS_DIR=~/src/overlayfs SQUASHFS_DIR=~/src/squashfs
make overlayfs-test STAGES=1                   # the read-only stage only
make overlayfs-test REUSE=1                    # rerun against a failed run's guest
make overlayfs-oracle                          # record the Linux logs only
```

The test starts the translator as

```sh
settrans -a MNT $OVERLAYFS_BIN --lowerdir=L1:L2 [--upperdir=U --workdir=W]
```

with the leftmost lower topmost, as Linux orders them. No `--upperdir`
means a read-only overlay. `make overlayfs-test` builds the translator
only when `OVERLAYFS_DIR` holds a checkout. Without a binary the test
runs the oracle and the control, then fails with
`translator: ... does not exist`.

| Variable | Default | Meaning |
|---|---|---|
| `OVERLAYFS_DIR` | `~/src/overlayfs` | Checkout of the overlayfs repo |
| `OVERLAYFS_BIN` | `~/src/overlayfs/overlayfs.static` | Script only: the binary used as the translator |
| `OVERLAYFS_FAULT_BIN` | `OVERLAYFS_DIR/overlayfs-fault.static` | The fault build for the fault sweep |
| `OVERLAYFS_ADAPTER` | empty | Script only: `unionfs` runs upstream unionfs (`UNIONFS_BIN`) through an adapter that turns the options into unionfs's arguments. This is the stage-1 red run |
| `SQUASHFS_BIN` | `~/src/squashfs/squashfs.static` | The squashfs translator that serves the squashfs lowers |
| `STAGES` | `1 2` | `1`: the read-only sets with no upper. `2`: the writable sets, which run all of stage 1 again |
| `STRESS_SECS` | `60` | Length of `stress-ro` and `stress-rw` |
| `FAULT_SCENARIOS` | see [The fault sweep](#the-fault-sweep) | Steps the fault sweep covers: `SCENARIO` (every mutating step) or `SCENARIO:K,K` (those steps), space separated |
| `FAULT_SET`, `FAULT_MAX` | `ext2-2`, 200 | The set the sweep runs on; the largest N per step and mode |
| `FAULT_BUDGET` | `2700` | Seconds after which the sweep begins no further step; a step it leaves out fails the check. `0`: no limit |
| `ORACLE_ONLY` | `0` | `1`: build the binaries, record the Linux logs, stop |
| `PLAN` | `.` | Script only: run the control and the translator only on plan rows whose `SET.MODE.SCENARIO` matches this `grep -E` pattern. For debugging; a filtered run is not an acceptance run |

The work files stay in `img/tmp/overlayfs-test/`: `linux/` (the Linux
logs, one per run, with `.persist.log`, `.upper`, `.work` and
`.lowers-ok` beside them, the squashfs images in `img/`, and
`mount-args.txt`), `control/` (the plain-directory logs and their
comparisons), `guest/` (the translator's logs, comparisons and upper
states), `fault/` and `times.tsv` (every check with its result and
seconds).

### The oracles and the control

`scripts/overlayfs_layers.py` describes the layer sets once and builds
them on both sides. `scripts/overlayfs_ops.py` holds the scenarios as
data, runs one against a directory, and prints one record per step.

| Set | Lowers | Upper | Purpose |
|---|---|---|---|
| `ext2-1` | one ext2 directory | ext2 | base case |
| `ext2-2` | two | ext2 | file over file, dir over dir, file over dir, dir over file |
| `sq-1` | a squashfs image | ext2 | the main use |
| `sq-tmp` | a squashfs image | tmpfs | live-system shape |
| `ro` | two | none | read-only overlay |
| `pre` | one | an upper with whiteouts, an opaque directory and copied-up files, in each system's convention | reading existing state |
| `wh-lower` | three: whiteouts and an opaque directory in the middle, whiteouts in the lowest | ext2 | Linux's rules for lower whiteouts |
| `enospc` | one | a 1 MiB tmpfs | a copy-up that does not fit |

The lowest layer holds every object kind the brief lists: empty, small,
300 KiB, 2 MiB and sparse 8 MiB files, a static and a dynamic Hurd binary
with its library, nested directories, four kinds of symlink, a hard-link
pair, a fifo, a socket, char and block devices, setuid, setgid, sticky
and 0000 modes, owners 0 and 1000, old and 2038 mtimes, a 255-byte name
and a 5,000-entry directory.

1. **Linux.** `scripts/overlayfs_linux_oracle.sh` runs every row of
   `overlayfs_ops.py plan` against `sudo mount -t overlay` on the host,
   on every run, so the logs always come from the running kernel.
   Opaque directories carry `trusted.overlay.opaque=y` there.
2. **A plain directory.** The guest builds the merged view of each set
   from the description (`overlayfs_layers.py merged`), runs the same
   scenario on it, and compares with Linux. It shares no code with
   either overlay. A step may list the result a tag gives instead of
   Linux's, with the reason, and `compare` prints every difference it
   allowed. This run is the **control**: the translator is judged only
   after every control run passes.

| Difference | Tag | Why |
|---|---|---|
| A copied-up hard link breaks; `st_nlink` of the other name stays the lower's | plain | No index in the default configuration (Linux H2, H4) |
| A directory that exists in a lower is renamed | plain | Linux gives `EXDEV` (redirect_dir off); the next step renames it back |
| `mknod c 0 0` | plain | Linux refuses the whiteout's device number with `EPERM` |
| An `mmap` made before copy-up sees a later write | plain | Linux keeps it on the lower file (F3) |
| `st_nlink` of directories | plain | Linux gives 1 for a merged directory (I6) |
| `statfs` relation to the upper | plain | No upper |
| A whiteout Linux lists in a lower-only directory but cannot stat | strict (plain and translator) | Linux reads such a directory by passthrough (D9); the translator must hide it |
| Unlink in a sticky directory gives `EACCES` | hurd | `libfshelp/perms-checkdirmod.c` |
| A sparse copy-up on a tmpfs upper is filled | hurd+tmpfs-upper | The Hurd's tmpfs stores no holes |
| A directory's mtime is not compared | hurd+tmpfs-upper | The Hurd's tmpfs never updates a directory's mtime when an entry is added or removed (measured on a bare tmpfs, no overlay) |
| `truncate both 3` of `copyup` is not run; `open both WRONLY` runs instead, and `both` is no longer compared | hurd+tmpfs-upper | A shrinking truncate to a non-zero size on the Hurd's tmpfs panics gnumach (`vm_map_copyin_page_list: Assertion 'm->wire_count == 1'`), with no overlay involved. The run prints `SKIPPED` for it, during the run and again above the summary |

A field a tag never compares (the `st_nlink` and mtime rows) is printed
as an allowed difference for each manifest line where it hides one. A
step a tag does not run carries an `instead` entry in
`overlayfs_ops.py` and is recorded as `skipped (WHY); ran STEP: RESULT`;
`compare` accepts that record only for the tags the entry names, and
`compare persist --after SCENARIO` keeps ignoring the paths the scenario
stopped comparing.

Records normalise what the two systems cannot share: an mtime that is
not one of the description's fixed times prints `new`, and symlink
permission bits are not compared (the Hurd applies the umask to them).
Inode numbers and `st_dev` are never compared with Linux. The upper is
compared by meaning (`overlayfs_ops.py upper-state`): whiteouts as char
0:0 at the same paths, and a directory opaque in one exactly when it is
in the other, by xattr on Linux and by a `.wh..wh..opq` entry on the Hurd.

### What the overlayfs test checks

Each scenario check is reported per run, as `CHECK: SET.MODE.SCENARIO`.

| Check | Stage | What must hold |
|---|---|---|
| `oracle: Linux leaves every lower unchanged` | host | Every Linux run leaves each lower identical (content, mode, owner, mtime, ctime) |
| `control: ...` | both | The plain-directory run agrees with Linux, up to the listed differences |
| `manifest`, `lookup`, `readdir`, `getcwd`, `read`, `symlink`, `special`, `statfs` | 1 | The steps of scenario `read` agree with Linux: the merged manifest; every path by name and `..` of every directory; whiteout and opaque names `ENOENT`; readdir with `.` and `..`, no duplicates, rewind, `telldir`/`seekdir`, the 5,000 entries; `getcwd` at depth 3 in lower-only, merged and upper-only directories; checksums, `pread`, reads past the end; `readlink`, following, `lstat`; fifo, socket and device nodes; `f_namemax`, `ST_RDONLY` and whether the blocks are the upper's |
| `erofs` | 1 | Every mutating call, and a writable shared `mmap`, gives what Linux gives with no upper |
| `unmount`, `lower-untouched` | both | `settrans -gR` works and leaves no translator process; each lower is unchanged (snapshot for ext2, image checksum for squashfs) |
| `mmap-exec`, `ids`, `stress-ro` | 1 | On set `ro`: a static and a dynamic binary run from the overlay and `mmap` reads what `read` reads; `st_ino` unique (one hard-link pair) and stable over two walks, one `st_dev`, `d_ino` equal to `st_ino`; 8 processes for `STRESS_SECS` with no error |
| `layer-dies` | 1 | The squashfs translator under `sq-1`'s lower is killed: reads give errors within 30 s and the overlay unmounts |
| `mount-args` | 1 | Missing `--lowerdir`, a lower that is not a directory, one lower twice and 501 lowers are refused with a message |
| `ops-copyup`, `ops-remove`, `ops-rename`, `ops-create`, `copy-up-attrs`, `open-across-copy-up`, `perms`, `enospc` | 2 | The write scenarios agree with Linux |
| `upper-state`, `persist` | 2 | The upper means what Linux's means; after a remount the manifest is Linux's and the work directory holds no files |
| `reserved`, `stress-rw` | 2 | `.wh..wh..opq` cannot be created or listed; 8 writers for `STRESS_SECS`, then every listed name stats and the overlay unmounts |
| `raw-names` | 2 | Directory RPCs that no libc call sends, with a name that is not one path component (`a/b`, `../x`, `/x`, empty, `.`, `..`) in `dir_mkdir`, `dir_rmdir`, `dir_unlink`, `dir_rename`, `dir_link` and a symlink's `dir_link`, sent by uid 1000 with `scripts/overlayfs_raw.c`, are refused with `EINVAL`. The overlay keeps taking writes, the directory and a closed sibling are unchanged, and no stray name appears in upper or work. The upper is a scratch ext2 image with its own ext2fs, so that a failure stays inside the test; do not point `overlayfs_raw` at an overlay whose upper is on the guest's root filesystem |
| `hostile` | 2 | Each layout of `overlayfs_ops.py mount-args` marked `survive` is refused or served without a hang or crash, printed beside Linux's result |
| `fault-sweep` | 2 | See below |

### The fault sweep

The fault build is a second binary from the translator's sources with
one hook in the module that changes layers. `OVERLAYFS_FAULT=fail:N` in
its environment fails the Nth layer mutation, and `exit:N` exits there.
It prints a line containing `OVERLAYFS_FAULT fired` on stderr when it
injects. `settrans -a` does not pass its environment to the translator
(measured: a translator script started with `env OVERLAYFS_FAULT=fail:7
settrans -a ...` saw the variable empty), so `overlayfs_guest.sh mount
KEY --bin FILE VAR=VALUE` starts a wrapper script that sets the
variables and execs the binary.

The sweep runs inside the guest in one ssh session
(`overlayfs_guest.sh fault-sweep`), and each case's result line comes
back as it ends, into `fault/results.tsv`. `FAULT_SET` is prepared once.
The translator only reads the lowers, and every case checks that they
are unchanged. For each step K that `overlayfs_ops.py mutating` lists:

1. The views before and after K come from the real translator.
2. For each mode, `fail` and then `exit`, and N = 1, 2, ... until a case
   in which the fault does not fire (at most `FAULT_MAX`): the upper and
   work directories are emptied again (`overlayfs_guest.sh reset`),
   steps 0 to K-1 run on the real translator, and step K runs on the
   fault build. In `fail` mode the view is taken there. In `exit` mode
   it is not, because the translator has exited and the mount point
   shows the bare directory underneath.
3. The real translator mounts again and the view is taken.

A case is bad when the next mount fails, a lower changed, the work
directory still holds a file, or a view is `mixed`. `overlayfs_ops.py
view-class` names the view. How it judges a step is data, the table
`FAULT_JUDGE` in `overlayfs_ops.py`, by op:

| Judge | Ops | The view must be |
|---|---|---|
| atomic | every op that makes one visible change, maybe after an invisible copy-up (`open`, `append`, `truncate`, `chmod`, `chown`, `utime`, `unlink`, `rmdir`, `mkdir`, `rename`, `symlink`, `link`, `mkfifo`, `mknod`, `open-across-copy-up`) | `before` or `after`, exactly or up to copy-up traces |
| compound | `write` (create, then fill), `editor-save` (write a temporary, then rename), `rmtree` (one unlink per entry) | as for atomic, or `between`: every name that both the before and the after view have is still there, each such entry that is the same in both is unchanged (up to copy-up traces), and no name of a lower that both views hide is visible |

A failed or interrupted operation may leave its parent chain or its
source copied up. Linux does not undo a copy-up either. Such a view is
`before+copyup` or `after+copyup`, and the class names each path and
field it tolerated. Only these differences are tolerated:

- `st_nlink` of a directory or regular file becomes 1. A lower-only
  directory that is copied up becomes a merged directory, which reports
  1 (I6). A copied-up name of a lower hard link reports 1 while its other
  name keeps the lower count (H4).
- A directory's mtime reads `new`, and only when the fault hit a
  `file_utimens`. The translator sets a copied-up directory's own times
  after the rename that puts it in place, and restores its parent's
  times after that. Both calls are best effort, as Linux's restore of
  the parent's times is (`txn_exec.c` `ST_SET_TIMES` and
  `ST_RESTORE_TIMES`), so a fault there leaves the time that the rename
  gave.

Names, types, data, mode, owner and file mtimes never differ in a
copy-up trace. `overlayfs_ops.py fault-selftest DIR` proves `view-class`
on states made by hand: the whole states and both kinds of trace are
accepted, and a lost name, a shown lower name, changed data, a changed
mode, a changed file mtime, a directory mtime without a `file_utimens`
fault, a link count other than 1, and every half-done state of a rename
over an existing name are `mixed`.

The check line counts the cases, the cases that fired, the bad ones and
each class of view after the next mount, so the copy-up traces stay
visible. The check fails when a case is bad, when no case fired (the
sweep tested nothing), when the guest session did not end, or when
`FAULT_BUDGET` ran out before the last step. Its message lists each step
where N=1 did not fire, which is expected for a step that fails before
it changes a layer (`EEXIST`, say).

A case costs about 4 seconds: two views of the 5,059-path tree at about
1.6 s each, the lowers' snapshot at 0.7 s, and the mounts, replays and
unmounts. Measured phase by phase, the old sweep, which ran each guest
command over ssh and built the layers again for each case, cost about
14 s a case, and rebuilding the layers (3.5 to 4.6 s) was its largest
part.

The whole sweep, every mutating step of `copyup`, `remove`, `rename` and
`create`, is 1,926 cases and took 2 hours 10 minutes, about 30 minutes of
it in `rmtree many` (5,000 unlinks, cut at `FAULT_MAX`). Run it with:

```sh
make overlayfs-test FAULT_SCENARIOS="copyup remove rename create" FAULT_BUDGET=0
```

The default sweeps 21 steps, one for each kind of change the whole sweep
showed, so the sweep of a default run takes about 40 minutes:

| Scenario | Steps | What they change |
|---|---|---|
| `copyup` | 0, 2, 6, 18, 27, 30 | open of a lower file for writing (copy-up with data), a new file (`write`, compound), open with `O_TRUNC` (an empty copy), append under a chain of lower directories, `utime` of the sparse 8 MiB file, `lchown` of a symlink |
| `remove` | 0, 4, 8, 14, 22, 25 | unlink of a lower file (whiteout), of an upper file, of a file in a directory both lowers have, rmdir of a merged directory, mkdir where a whiteout is (opaque), `rmtree` of a merged tree (compound) |
| `rename` | 0, 3, 16, 20, 28 | a lower file, a lower file into a lower-only directory, a lower file over a lower file under a chain, an upper directory, `editor-save` (compound) |
| `create` | 0, 3, 8, 12 | symlink, hard link of a lower file, fifo, `mknod c 0 0` (refused) |


To drive the sweep's loop without a fault build, point
`OVERLAYFS_FAULT_BIN` at a script that prints the marker for some N and
execs `/tmp/ovl-translator` (the real translator), and narrow it with
`FAULT_SCENARIOS=create:0,3`.

### Proving the harness

Upstream unionfs (built static from `hurd/unionfs`) through
`OVERLAYFS_ADAPTER=unionfs` with `STAGES=1` is the stage-1 red run. It
must fail `readdir`, `symlink` and `manifest`.

### overlayfs gotchas

- `cp -a` on the Hurd cannot copy a socket (`EINVAL`), so the guest
  rebuilds the plain directory from the description for each scenario
  instead of copying it.
- The guest's `/root` is not reachable by uid 1000, so the guest works
  under `/tmp/ovl`; the `perms` scenario runs as uid 1000.
- `stat` of a fifo starts `/hurd/fifo` on it, so unmounts use
  `settrans -gR` with retries.
- `overlayfs_guest.sh unmount` looks for a translator left running by
  program name. Matching the key's path alone also matched the caller's
  own `bash -c` line whenever that line named the mount point, and the
  unmount then failed.
- On the guest's `/hurd/tmpfs`, a shrinking truncate to a non-zero size
  panics gnumach and the guest reboots: write 35 bytes, `ftruncate(fd,
  3)`. Truncating to 0 and growing work. Try anything like it on a
  throwaway guest only.
- The guest's `/hurd/tmpfs` never updates a directory's mtime when an
  entry is added or removed; ext2fs does.
- `PLAN` reaches awk through `ENVIRON`, because `awk -v` treats
  backslashes as escapes and would turn `\.` into `.`.

## Gotchas

- `ssh guest "settrans -a ..."` never returns when the mount succeeds. The
  active translator inherits settrans's stdout and stderr, and ssh waits
  for every holder of the channel. `mount_fs` in the script sends the
  output to a log file in the guest and prints it afterwards. Do the same
  in any new guest command that starts a translator.
- Do not `pkill -f` a script name from an agent's shell. The shell has
  the same string on its command line and kills itself. Stop QEMU with the
  pid in `img/tmp/exfatfs-test-qemu.pid`.
- Upstream fatfs reads the 32-bit fields `total_sectors_32` and
  `sectors_per_fat_32` with `read_word`, which reads 16 bits, so a FAT32
  volume fails or gets a truncated size. hurdkit/hurd's exfatfs change
  fixes this too. `total_sectors_32` is also used by FAT16 volumes of more
  than 65535 sectors, and the 64 MiB control image is one, so with the
  pinned `3p/hurd` (no fix) `fat: mount` fails with `Number of total
  sectors is zero`. Run the control with a `HURD_TREE` that has the fix.
- exfat-fuse always asks for `allow_other`, so it cannot mount without
  root unless `user_allow_other` is set. The test uses the kernel driver.
- The kernel exfat driver does not fill freed gaps first. To get a
  fragmented file, `fs_image_test.py fragment` fills the volume, frees
  every other file, then writes.
- Linux cannot create a name longer than 255 UTF-8 bytes. The test creates
  a 104-character placeholder and `fs_image_test.py patch-long-name`
  rewrites it on the image to 100 CJK characters (304 bytes), fixing the
  name hash and the set checksum. `fsck.exfat` then checks the result.
