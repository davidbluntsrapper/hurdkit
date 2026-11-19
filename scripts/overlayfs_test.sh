#!/bin/bash
# overlayfs translator test in the Hurd guest, against Linux overlayfs.
# Run it through make:
#
#   make overlayfs-test OVERLAYFS_DIR=DIR    # translator from a checkout
#   OVERLAYFS_BIN=FILE bash scripts/overlayfs_test.sh
#   OVERLAYFS_ADAPTER=unionfs UNIONFS_BIN=FILE bash scripts/overlayfs_test.sh
#                                            # upstream unionfs, the red run
#   ORACLE_ONLY=1 bash scripts/overlayfs_test.sh
#                                            # record the Linux logs only
#
# The goal is Linux 7.0 overlayfs behaviour in its default configuration
# (writable upper over read-only lowers, copy-up, whiteouts, rename).
# The translator is started as
#   settrans -a MNT $OVERLAYFS_BIN --lowerdir=L1:L2 [--upperdir=U --workdir=W]
# with the leftmost lower topmost, as Linux orders them.
#
# Two oracles.  Linux overlayfs on this host runs every scenario of
# overlayfs_ops.py on the layer sets of overlayfs_layers.py, recorded
# afresh on each run (overlayfs_linux_oracle.sh, sudo).  A plain ext2
# directory in the guest that starts as the merged view of each set runs
# the same scenarios: it shares no code with either overlay, and it must
# agree with Linux except where a step lists a difference.  That plain
# run is the control: the translator is judged only after it passes.
#
# STAGES (default "1 2") picks the brief's stages.  A failed run leaves
# its guest running; REUSE=1 runs the checks again against it.  Every
# guest step is bounded by GUEST_TIMEOUT seconds, so a hung translator
# fails its check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OVERLAYFS_BIN="${OVERLAYFS_BIN:-$HOME/src/overlayfs/overlayfs.static}"
OVERLAYFS_ADAPTER="${OVERLAYFS_ADAPTER:-}"
UNIONFS_BIN="${UNIONFS_BIN:-}"
OVERLAYFS_FAULT_BIN="${OVERLAYFS_FAULT_BIN:-}"
SQUASHFS_BIN="${SQUASHFS_BIN:-$HOME/src/squashfs/squashfs.static}"
CROSS_CC="${CROSS_CC:-$HOME/opt/x86_64-gnu/bin/x86_64-gnu-gcc}"
STAGES="${STAGES:-1 2}"
ORACLE_ONLY="${ORACLE_ONLY:-0}"
STRESS_SECS="${STRESS_SECS:-60}"
FAULT_MAX="${FAULT_MAX:-200}"
# The default sweep takes one step of each kind of change (about 40
# minutes); FAULT_SCENARIOS="copyup remove rename create" FAULT_BUDGET=0
# sweeps every step (about 2 hours 10 minutes).
FAULT_SCENARIOS="${FAULT_SCENARIOS:-copyup:0,2,6,18,27,30 remove:0,4,8,14,22,25 rename:0,3,16,20,28 create:0,3,8,12}"
FAULT_SET="${FAULT_SET:-ext2-2}"
FAULT_BUDGET="${FAULT_BUDGET:-2700}"
# Only the plan's runs whose SET.MODE.SCENARIO matches PLAN (a grep -E
# pattern), for debugging; a filtered run is not an acceptance run.
export PLAN="${PLAN:-.}"
REUSE="${REUSE:-0}"
GUEST_TIMEOUT="${GUEST_TIMEOUT:-600}"
BASE_IMG="${BASE_IMG:-$REPO_ROOT/img/hurd.packages.qcow2}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_KEY="${SSH_KEY_DIR:-$REPO_ROOT/img/keys}/root_ed25519"
TMP_DIR="${TMP_DIR:-$REPO_ROOT/img/tmp}"
WORK="$TMP_DIR/overlayfs-test"
LAYERS="$SCRIPT_DIR/overlayfs_layers.py"
OPS="$SCRIPT_DIR/overlayfs_ops.py"
FT_NAME="overlayfs-test"
mkdir -p "$TMP_DIR"

# The oracle's trees are root's, and a mount left by a killed oracle run
# would let rm -rf reach into its layers: unmount first, then delete.
if [[ -d "$WORK" ]]; then
    for m in $(findmnt -rn -o TARGET | grep -F "$WORK/" | sort -r || true); do
        sudo umount "$m" || sudo umount -l "$m"
    done
    sudo rm -rf -- "$WORK"
fi
source "$SCRIPT_DIR/fs_test_lib.sh"
ft_prepare_work

stage() { [[ " $STAGES " == *" $1 "* ]]; }
TIMES="$WORK/times.tsv"
: > "$TIMES"
# skipped WHAT: a step this system does not run, printed now and again
# above the summary.
SKIPS=()
skipped() { SKIPS+=("$1"); echo "$FT_NAME: SKIPPED $1"; }
# check_result NAME STATUS SECONDS [DETAIL]: pass or fail NAME and keep
# how long it took, so a red run shows how fast each check fails.
check_result() {
    local name=$1 rc=$2 secs=$3 detail=${4:-}
    printf '%s\t%s\t%s\n' "$name" "$([[ $rc == 0 ]] && echo pass || echo fail)" "$secs" >> "$TIMES"
    if [[ $rc == 0 ]]; then pass "$name (${secs}s)"; else fail "$name (${secs}s)${detail:+: $detail}"; fi
}

command -v mksquashfs >/dev/null || { echo "$FT_NAME: mksquashfs not found (squashfs-tools)" >&2; exit 1; }
sudo -n true 2>/dev/null || { echo "$FT_NAME: needs sudo for the Linux overlay mounts" >&2; exit 1; }
[[ -f "$SQUASHFS_BIN" ]] || { echo "$FT_NAME: missing $SQUASHFS_BIN (make squashfs)" >&2; exit 1; }

echo "$FT_NAME: building the guest binaries the lowest layer carries"
mkdir -p "$WORK/bin" "$WORK/src"
"$CROSS_CC" -static -O2 -o "$WORK/bin/fs_probe" "$SCRIPT_DIR/fs_probe.c" 2> "$WORK/src/build.log" \
    || { cat "$WORK/src/build.log" >&2; exit 1; }
printf 'const char *ovl_hello (void) { return "overlayfs-dyn-ok"; }\n' > "$WORK/src/lib.c"
printf '#include <stdio.h>\nconst char *ovl_hello (void);\nint main (void) { return puts (ovl_hello ()) < 0; }\n' \
    > "$WORK/src/main.c"
"$CROSS_CC" -shared -fPIC -o "$WORK/bin/libovlhello.so" "$WORK/src/lib.c"
"$CROSS_CC" -o "$WORK/bin/dynhello" "$WORK/src/main.c" -L"$WORK/bin" -lovlhello

echo "$FT_NAME: recording Linux overlayfs on this host ($(uname -r))"
bash "$SCRIPT_DIR/overlayfs_linux_oracle.sh" "$WORK" "$WORK/bin" > "$WORK/oracle.log" 2>&1 \
    || { tail -20 "$WORK/oracle.log" >&2; echo "$FT_NAME: the Linux oracle failed" >&2; exit 1; }
LX="$WORK/linux"
missing=$(python3 "$OPS" plan | while IFS=$'\t' read -r s m c _ _; do
    [[ -f "$LX/$s.$m.$c.lowers-ok" ]] || echo "$s.$m.$c"; done)
check_result "oracle: Linux leaves every lower unchanged" "$([[ -z $missing ]]; echo $?)" 0 "$missing"
if [[ "$ORACLE_ONLY" == 1 ]]; then
    echo "$FT_NAME: Linux logs in $LX"
    finish
fi

ft_boot

G=/tmp
GH="bash $G/overlayfs_guest.sh"
echo "$FT_NAME: copying the tools, the layers' binaries and the squashfs images"
tar -C "$WORK/bin" -cf "$WORK/bin.tar" .
tar -C "$LX/img" -cf "$WORK/img.tar" .
cp "$SQUASHFS_BIN" "$WORK/squashfs.static"
if ! { put "$LAYERS" "$OPS" "$SCRIPT_DIR/overlayfs_guest.sh" "$SCRIPT_DIR/fs_image_test.py" \
           "$WORK/bin.tar" "$WORK/img.tar" "$WORK/squashfs.static" \
       && g "rm -rf $G/ovl-bin $G/ovl-img && mkdir -p $G/ovl-bin $G/ovl-img /tmp/ovl \
             && tar -C $G/ovl-bin -xf $G/bin.tar && tar -C $G/ovl-img -xf $G/img.tar \
             && python3 -c 'import ctypes'"; }; then
    fail "guest setup: copy the tools and images"
    finish
fi

# tags_for SET: the compare tags a guest run of SET carries besides its
# system's own.
tags_for() {
    local kind
    kind=$(python3 "$LAYERS" sets | awk -F'\t' -v s="$1" '$1 == s { print $3 }')
    [[ $kind == tmpfs* ]] && echo ",tmpfs-upper" || true
}

echo "$FT_NAME: the control: every scenario on a plain directory"
mkdir -p "$WORK/control"
ctl_fail=0
while IFS=$'\t' read -r set mode scen _stage plain; do
    [[ $plain == plain ]] || continue
    key="$set.$mode.$scen"
    t0=$SECONDS
    rc=0
    g "$GH plain $set /tmp/ovl/plain && python3 $G/overlayfs_ops.py run $scen /tmp/ovl/plain" \
        > "$WORK/control/$key.log" || rc=$?
    if [[ $rc == 0 ]]; then
        python3 "$OPS" compare "$scen" "$LX/$key.log" "$WORK/control/$key.log" --tags plain,strict,hurd \
            > "$WORK/control/$key.cmp" || rc=$?
    fi
    check_result "control: $key agrees with Linux, up to the listed differences" "$rc" $((SECONDS - t0)) \
        "$(grep -v 'allowed\|^check' "$WORK/control/$key.cmp" 2>/dev/null | head -8 | tr '\n\t' '  ')"
    [[ $rc == 0 ]] || ctl_fail=1
done < <(python3 "$OPS" plan | awk -F'\t' '($1 "." $2 "." $3) ~ ENVIRON["PLAN"]')
g "rm -rf /tmp/ovl/plain" || true
if [[ $ctl_fail == 1 ]]; then
    fail "translator: not judged, the control failed"
    finish
fi

case "$OVERLAYFS_ADAPTER" in
    "")
        if [[ ! -f "$OVERLAYFS_BIN" ]]; then
            fail "translator: $OVERLAYFS_BIN does not exist (build it: make overlayfs OVERLAYFS_DIR=DIR, or set OVERLAYFS_BIN)"
            finish
        fi
        cp "$OVERLAYFS_BIN" "$WORK/ovl-translator"
        ;;
    unionfs)
        [[ -f "$UNIONFS_BIN" ]] || { fail "translator: UNIONFS_BIN=$UNIONFS_BIN does not exist"; finish; }
        cp "$UNIONFS_BIN" "$WORK/unionfs.static"
        put "$WORK/unionfs.static"
        # unionfs takes the layers as arguments and lets the last one win;
        # Linux's leftmost lower is its topmost.  Nothing is validated
        # here, so unionfs's own refusals are what the test sees.
        cat > "$WORK/ovl-translator" <<'EOF'
#!/bin/sh
lower= upper=
for a; do
    case $a in
        --lowerdir=*) lower=${a#--lowerdir=} ;;
        --upperdir=*) upper=${a#--upperdir=} ;;
    esac
done
set --
IFS=:
for l in $lower; do set -- "$l" "$@"; done
unset IFS
[ -n "$upper" ] && set -- "$@" -w "$upper"
exec /tmp/unionfs.static "$@"
EOF
        ;;
    *) echo "$FT_NAME: unknown OVERLAYFS_ADAPTER=$OVERLAYFS_ADAPTER" >&2; exit 1 ;;
esac
chmod 755 "$WORK/ovl-translator"
put "$WORK/ovl-translator" || { fail "guest setup: copy the translator"; finish; }

# judge_run SET MODE SCENARIO: prepare, mount, run, compare with Linux;
# then unmount, the upper's meaning, persistence and the lowers.
mkdir -p "$WORK/guest"
judge_run() {
    local set=$1 mode=$2 scen=$3 key="$1.$2.$3" t0 rc out tags log="$WORK/guest/$1.$2.$3"
    local checks
    checks=$(python3 "$OPS" checks "$scen")
    t0=$SECONDS
    if ! out=$(g "$GH prepare $set $mode $key" 2>&1); then
        for c in $checks; do check_result "$c: $key" 1 $((SECONDS - t0)) "layers not prepared: $out"; done
        return
    fi
    if ! out=$(g "$GH mount $key" 2>&1); then
        for c in $checks; do check_result "$c: $key" 1 $((SECONDS - t0)) "mount failed: $(echo "$out" | tr '\n' ' ' | cut -c1-200)"; done
        g "$GH teardown $key" >/dev/null 2>&1 || true
        return
    fi
    rc=0
    tags="overlay,strict,hurd$(tags_for "$set")"
    g "upper=\$(cat /tmp/ovl/$key/upper-path); python3 $G/overlayfs_ops.py run $scen /tmp/ovl/$key/mnt --tags $tags $([[ $mode == rw ]] && echo '--upper $upper')" \
        > "$log.log" 2> "$log.err" || rc=$?
    # A step this system cannot run is never silent.
    while IFS=$'\t' read -r step label res; do
        skipped "$key step $step $label: ${res%%: ok*}"
    done < <(awk -F'\t' '$3 ~ /^skipped \(/' "$log.log")
    python3 "$OPS" compare "$scen" "$LX/$key.log" "$log.log" --tags "$tags" > "$log.cmp" || true
    local secs=$((SECONDS - t0))
    for c in $checks; do
        local v
        v=$(awk -F'\t' -v c="$c" '$1 == "check" && $2 == c { print $3 }' "$log.cmp")
        if [[ $rc != 0 ]]; then
            check_result "$c: $key" 1 "$secs" "the scenario did not finish (rc=$rc$([[ $rc == 124 ]] && echo ', hung'))"
        else
            check_result "$c: $key" "$([[ $v == pass ]]; echo $?)" "$secs" \
                "$(grep -F "[$c] " "$log.cmp" | head -4 | tr '\n\t' '  ' | cut -c1-300)"
        fi
    done
    t0=$SECONDS
    rc=0; out=$(g "$GH unmount $key" 2>&1) || rc=$?
    check_result "unmount: $key" "$rc" $((SECONDS - t0)) "$out"
    if [[ $mode == rw ]]; then
        t0=$SECONDS
        g "python3 $G/overlayfs_ops.py upper-state \$(cat /tmp/ovl/$key/upper-path) --convention hurd" \
            > "$log.upper" 2>&1 || true
        rc=0; diff "$LX/$key.upper" "$log.upper" > "$log.upper.diff" || rc=1
        check_result "upper-state: $key" "$rc" $((SECONDS - t0)) "$(head -6 "$log.upper.diff" | tr '\n\t' '  ')"
        t0=$SECONDS
        rc=0
        left=$(g "find \$(cat /tmp/ovl/$key/work-path) -mindepth 1 ! -type d" 2>&1) || rc=1
        [[ -z $left ]] || rc=1
        if [[ $rc == 0 ]] && g "$GH mount $key" > "$log.remount" 2>&1; then
            g "python3 $G/overlayfs_ops.py run persist /tmp/ovl/$key/mnt" > "$log.persist.log" 2>&1 || rc=1
            g "$GH unmount $key" >/dev/null 2>&1 || rc=1
            python3 "$OPS" compare persist "$LX/$key.persist.log" "$log.persist.log" --tags "$tags" --after "$scen" \
                > "$log.persist.cmp" || rc=1
        else
            rc=1
        fi
        check_result "persist: $key" "$rc" $((SECONDS - t0)) \
            "${left:+work dir holds: $left; }$(grep -v 'allowed\|^check' "$log.persist.cmp" 2>/dev/null | head -4 | tr '\n\t' '  ' | cut -c1-300)"
    fi
    t0=$SECONDS
    rc=0; out=$(g "$GH lowers-unchanged $key" 2>&1) || rc=$?
    check_result "lower-untouched: $key" "$rc" $((SECONDS - t0)) "$(echo "$out" | head -4 | tr '\n' ' ')"
    g "$GH teardown $key" >/dev/null 2>&1 || true
}

# A stage-1 scenario on a writable set belongs to stage 2: the brief runs
# all of stage 1 again once writes exist.
while IFS=$'\t' read -r set mode scen _ _; do
    if [[ $mode == ro ]]; then stage 1 || continue; else stage 2 || continue; fi
    echo "$FT_NAME: $set.$mode.$scen"
    judge_run "$set" "$mode" "$scen"
done < <(python3 "$OPS" plan | awk -F'\t' '($1 "." $2 "." $3) ~ ENVIRON["PLAN"]')

# The checks that read one mounted overlay rather than compare a log.
if stage 1; then
    key=extra-ro
    t0=$SECONDS
    if g "$GH prepare ro ro $key && $GH mount $key" > "$WORK/guest/$key.mount" 2>&1; then
        M=/tmp/ovl/$key/mnt
        t0=$SECONDS
        out=$(g "$M/bin/fs_probe hello; LD_LIBRARY_PATH=$M/bin $M/bin/dynhello; cd $M && python3 $G/fs_image_test.py mmap medium sparse | cut -f1; sha256sum medium sparse | cut -d' ' -f1" 2>&1 || true)
        mm=$(echo "$out" | tail -4)
        rc=1
        [[ "$(echo "$out" | head -2 | tr '\n' ' ')" == "fs-probe-exec-ok overlayfs-dyn-ok " \
           && "$(echo "$mm" | head -2)" == "$(echo "$mm" | tail -2)" ]] && rc=0
        check_result "mmap-exec: a static and a dynamic binary run from the overlay; mmap reads what read reads" \
            "$rc" $((SECONDS - t0)) "$(echo "$out" | tr '\n' ' ' | cut -c1-300)"
        t0=$SECONDS
        out=$(g "python3 $G/overlayfs_ops.py ids $M" 2>&1 || true)
        rc=1; [[ $out == *"shared=1 stable=yes devs=1 dino-mismatch=0 errors=0" ]] && rc=0
        check_result "ids: st_ino unique (one hard-link pair) and stable, one st_dev, d_ino equals st_ino" \
            "$rc" $((SECONDS - t0)) "$out"
        t0=$SECONDS
        rc=0; out=$(GUEST_TIMEOUT=$((STRESS_SECS + 120)) g "python3 $G/overlayfs_ops.py stress-ro $M $STRESS_SECS 8" 2>&1) || rc=$?
        check_result "stress-ro: 8 processes for ${STRESS_SECS}s, no error, no hang" "$rc" $((SECONDS - t0)) "$out"
        t0=$SECONDS
        rc=0; out=$(g "$GH unmount $key" 2>&1) || rc=$?
        check_result "unmount: after the stress run" "$rc" $((SECONDS - t0)) "$out"
    else
        for c in mmap-exec ids stress-ro; do
            check_result "$c: the ro set" 1 $((SECONDS - t0)) "mount failed: $(tr '\n' ' ' < "$WORK/guest/$key.mount" | cut -c1-200)"
        done
    fi
    g "$GH teardown $key" >/dev/null 2>&1 || true

    # The squashfs translator under a lower dies while clients read.
    key=extra-dies
    t0=$SECONDS
    if g "$GH prepare sq-1 rw $key && $GH mount $key" > "$WORK/guest/$key.mount" 2>&1; then
        M=/tmp/ovl/$key/mnt
        rc=0; out=$(g "cat $M/small >/dev/null && pid=\$(ps -eo pid,args | awk -v m=/tmp/ovl/$key/l1.img 'index(\$0, m) && !/awk/ { print \$1 }') && kill -9 \$pid && sleep 2;
                 for p in small medium a/b/c/deep many; do timeout 30 python3 $G/overlayfs_ops.py ids $M/\$p >/dev/null 2>&1; echo \"\$p rc=\$?\"; timeout 30 cat $M/\$p >/dev/null 2>&1; echo \"cat \$p rc=\$?\"; done" 2>&1) || rc=$?
        hung=$(echo "$out" | grep -c 'rc=124' || true)
        rc2=0; out2=$(g "$GH unmount $key" 2>&1) || rc2=$?
        check_result "layer-dies: the squashfs under a lower is killed: no hang, the overlay unmounts" \
            "$([[ $rc == 0 && $hung == 0 && $rc2 == 0 ]]; echo $?)" $((SECONDS - t0)) \
            "$(echo "$out $out2" | tr '\n' ' ' | cut -c1-300)"
    else
        check_result "layer-dies: the squashfs under a lower is killed" 1 $((SECONDS - t0)) \
            "mount failed: $(tr '\n' ' ' < "$WORK/guest/$key.mount" | cut -c1-200)"
    fi
    g "$GH teardown $key" >/dev/null 2>&1 || true
fi

# Bad invocations (stage 1) and hostile layouts (stage 2), beside Linux.
while IFS=$'\t' read -r name must _; do
    if [[ $must == refuse ]]; then stage 1 || continue; else stage 2 || continue; fi
    t0=$SECONDS
    res=$(g "$GH arg-case $name" 2>&1 | tail -1 | cut -f3) || true
    linux=$(awk -F'\t' -v n="$name" '$1 == n { print $3 }' "$LX/mount-args.txt" | sed "s#$LX/run/arg-$name/##g" | cut -c1-120)
    if [[ $must == refuse ]]; then
        rc=1; [[ $res == refused:* ]] && rc=0
        check_result "mount-args: $name is refused with a message" "$rc" $((SECONDS - t0)) "got: $res; Linux: $linux"
    else
        rc=1; [[ ( $res == refused:* || ( $res == mounted* && $res == *"; alive" && $res != *hang* ) ) ]] && rc=0
        check_result "hostile: $name is refused or served without a hang or crash" "$rc" $((SECONDS - t0)) "got: $res; Linux: $linux"
    fi
    printf '%s\t%s\t%s\n' "$name" "$res" "$linux" >> "$WORK/guest/mount-args.txt"
done < <(python3 "$OPS" mount-args)

if stage 2; then
    key=extra-rw
    t0=$SECONDS
    if g "$GH prepare ext2-2 rw $key && $GH mount $key" > "$WORK/guest/$key.mount" 2>&1; then
        M=/tmp/ovl/$key/mnt
        out=$(g "for p in .wh..wh..opq d/.wh..wh..opq; do
                   python3 -c 'import os,sys
try: os.close(os.open(sys.argv[1], os.O_CREAT | os.O_WRONLY, 0o644)); print(\"created\")
except OSError as e: print(e.strerror)' $M/\$p; done; ls -a $M $M/d | grep -c 'wh\\.\\.wh' || true" 2>&1 || true)
        rc=1; [[ $out != *created* && $(echo "$out" | tail -1) == 0 ]] && rc=0
        check_result "reserved: the opaque marker name cannot be created or listed" "$rc" $((SECONDS - t0)) \
            "$(echo "$out" | tr '\n' ' ')"
        t0=$SECONDS
        rc=0; out=$(GUEST_TIMEOUT=$((STRESS_SECS + 180)) g "python3 $G/overlayfs_ops.py stress-rw $M $STRESS_SECS 8" 2>&1) || rc=$?
        out2=$(g "$GH unmount $key" 2>&1) || rc=1
        check_result "stress-rw: 8 processes for ${STRESS_SECS}s, then a self-consistent tree and an unmount" \
            "$rc" $((SECONDS - t0)) "$out $out2"
    else
        for c in reserved stress-rw; do
            check_result "$c: ext2-2" 1 $((SECONDS - t0)) "mount failed: $(tr '\n' ' ' < "$WORK/guest/$key.mount" | cut -c1-200)"
        done
    fi
    g "$GH teardown $key" >/dev/null 2>&1 || true

    # The fault sweep runs in the guest in one session (overlayfs_guest.sh
    # fault-sweep), so a case costs no ssh; each case's line comes back as
    # it ends.  A case is bad when a lower changed, the next mount
    # failed, the work directory kept a file, or a view is "mixed"
    # (overlayfs_ops.py view-class).  The sweep of one step ends at the
    # first N that does not fire.
    t0=$SECONDS
    if [[ -z "$OVERLAYFS_FAULT_BIN" || ! -f "$OVERLAYFS_FAULT_BIN" ]]; then
        check_result "fault-sweep: fail and exit at every layer mutation of the swept steps" 1 0 \
            "OVERLAYFS_FAULT_BIN='$OVERLAYFS_FAULT_BIN' does not exist"
    else
        mkdir -p "$WORK/fault"
        cp "$OVERLAYFS_FAULT_BIN" "$WORK/ovl-fault"
        chmod 755 "$WORK/ovl-fault"
        put "$WORK/ovl-fault"
        R="$WORK/fault/results.tsv"
        # A case past the budget is not begun, and none takes 15 minutes.
        GUEST_TIMEOUT=$(( FAULT_BUDGET > 0 ? FAULT_BUDGET + 900 : 86400 )) g "FAULT_MAX=$FAULT_MAX $GH fault-sweep $FAULT_SET $FAULT_BUDGET $FAULT_SCENARIOS" \
            > "$R" 2> "$WORK/fault/sweep.err" || true
        summary=$(python3 "$OPS" fault-summary "$R")
        IFS=$'\t' read -r counts nbad nfired done budget detail <<< "$summary"
        [[ $budget == - ]] && budget=
        [[ $detail == - ]] && detail=
        rc=0
        [[ $nbad == 0 && $nfired -gt 0 && $done == 1 && -z $budget ]] || rc=1
        [[ $done == 1 ]] || detail="the sweep did not finish ($(tail -2 "$WORK/fault/sweep.err" | tr '\n' ' '))${detail:+; $detail}"
        [[ -z $budget ]] || detail="FAULT_BUDGET=${FAULT_BUDGET}s ran out; not swept:$budget${detail:+; $detail}"
        [[ $nfired -gt 0 ]] || detail="no case fired: the sweep tested nothing${detail:+; $detail}"
        check_result "fault-sweep: fail and exit at every layer mutation of $FAULT_SCENARIOS on $FAULT_SET ($counts)" \
            "$rc" $((SECONDS - t0)) "$detail"
    fi
fi
for x in "${SKIPS[@]}"; do echo "$FT_NAME: SKIPPED $x"; done
finish
