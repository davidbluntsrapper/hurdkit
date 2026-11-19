#!/bin/bash
# The Linux side of the overlayfs test: every run of overlayfs_ops.py's
# plan against Linux overlayfs on this host (sudo mount -t overlay,
# default options), recorded in OUT/linux/.  overlayfs_test.sh runs it on
# every run, so the oracle is the running kernel's, never a stale copy.
# It also runs alone:
#
#   bash scripts/overlayfs_linux_oracle.sh OUT BINDIR
#
# BINDIR holds the guest binaries that the lowest layer carries in bin/
# (overlayfs_test.sh builds them), so both sides build the same trees.
# For each run OUT/linux/SET.MODE.SCENARIO has:
#   .log           the scenario's records
#   .persist.log   (rw) a manifest after unmount and a fresh mount
#   .upper         (rw) overlayfs_ops.py upper-state of the upper
#   .work          (rw) what the work directory holds after unmount
#   .lowers-ok     present when every lower was unchanged by the run
# plus OUT/linux/mount-args.txt and OUT/linux/hostile.txt.
set -euo pipefail

OUT=$1
BINDIR=$2
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYERS="$SCRIPT_DIR/overlayfs_layers.py"
OPS="$SCRIPT_DIR/overlayfs_ops.py"
STEP_TIMEOUT=${STEP_TIMEOUT:-600}

MOUNTS=()
# Unmount everything this script mounted, last first, on any exit.  A
# mount left behind would make the next rm -rf reach into the layers.
unmount_all() {
    local i
    for ((i = ${#MOUNTS[@]} - 1; i >= 0; i--)); do
        if mountpoint -q "${MOUNTS[$i]}" 2>/dev/null; then
            sudo umount "${MOUNTS[$i]}" 2>/dev/null || sudo umount -l "${MOUNTS[$i]}"
        fi
    done
    MOUNTS=()
}
trap unmount_all EXIT
mnt() { sudo mount "$@"; MOUNTS+=("${*: -1}"); }

L="$OUT/linux"
sudo rm -rf "$L"
mkdir -p "$L/img" "$L/run" "$L/snap"

# The squashfs image of each set's tree, made once: the guest gets the
# same image file.
image_for() {
    local set=$1 img="$L/img/$1.l1.img"
    if [[ ! -f $img ]]; then
        sudo python3 -I "$LAYERS" build "$set" "$L/img/$set.src" --convention linux \
            --bin "$BINDIR" --sq-source
        local tree
        tree=$(ls -d "$L/img/$set.src"/l*.tree | head -1)
        sudo mksquashfs "$tree" "$img" -noappend -quiet -comp gzip >/dev/null
        sudo rm -rf "$L/img/$set.src"
    fi
    echo "$img"
}

# setup SET MODE DIR: build and mount the layers of SET under DIR, and
# set OPTS to the overlay's mount options.  Not run in a subshell, so
# MOUNTS keeps what it mounts.
setup() {
    local set=$1 mode=$2 dir=$3 kind pre sq lowers=() n i l
    read -r _ n kind pre sq _ < <(python3 "$LAYERS" sets | grep "^$set	")
    sudo python3 -I "$LAYERS" build "$set" "$dir" --convention linux --bin "$BINDIR"
    for ((i = 1; i <= n; i++)); do
        if [[ $sq == sq && $i == "$n" ]]; then
            mnt -o loop,ro -t squashfs "$(image_for "$set")" "$dir/l$i"
        fi
    done
    case $kind in
        tmpfs) mnt -t tmpfs -o size=64m tmpfs "$dir/t" ;;
        tmpfs-1m) mnt -t tmpfs -o size=1m tmpfs "$dir/t" ;;
    esac
    if [[ $kind == tmpfs* ]]; then
        sudo python3 -I "$LAYERS" build-upper "$set" "$dir/t/upper" --convention linux
        sudo mkdir "$dir/t/work"
    fi
    for l in $(python3 "$LAYERS" lowers "$set" $([[ $mode == ro ]] && echo --no-upper)); do
        lowers+=("$dir/$l")
    done
    OPTS="lowerdir=$(IFS=:; echo "${lowers[*]}")"
    if [[ $mode == rw ]]; then
        OPTS+=",upperdir=$(upper_of "$dir" "$kind"),workdir=$(work_of "$dir" "$kind")"
    fi
    sudo mkdir -p "$dir/mnt"
}
upper_of() { [[ $2 == tmpfs* ]] && echo "$1/t/upper" || echo "$1/upper"; }
work_of() { [[ $2 == tmpfs* ]] && echo "$1/t/work" || echo "$1/work"; }

# Lowers are checked by content and both times: copy-up must not touch them.
lower_snapshot() {
    local dir=$1 out=$2 l
    : > "$out"
    for l in "$dir"/l[0-9]*; do
        [[ -d $l ]] || continue
        echo "== $(basename "$l")" >> "$out"
        sudo timeout "$STEP_TIMEOUT" python3 -I "$LAYERS" snapshot "$l" >> "$out"
    done
}

while IFS=$'\t' read -r set mode scen _stage _plain; do
    key="$set.$mode.$scen"
    dir="$L/run/$key"
    echo "overlayfs-oracle: $key"
    setup "$set" "$mode" "$dir"
    read -r _ n kind _ < <(python3 "$LAYERS" sets | grep "^$set	")
    lower_snapshot "$dir" "$L/snap/$key.before"
    mnt -t overlay overlay -o "$OPTS" "$dir/mnt"
    upper=""
    [[ $mode == rw ]] && upper=$(upper_of "$dir" "$kind")
    sudo timeout "$STEP_TIMEOUT" python3 -I "$OPS" run "$scen" "$dir/mnt" ${upper:+--upper "$upper"} \
        > "$L/$key.log"
    sudo umount "$dir/mnt"
    if [[ $mode == rw ]]; then
        mnt -t overlay overlay -o "$OPTS" "$dir/mnt"
        sudo timeout "$STEP_TIMEOUT" python3 -I "$OPS" run persist "$dir/mnt" > "$L/$key.persist.log"
        sudo umount "$dir/mnt"
        sudo python3 -I "$OPS" upper-state "$upper" --convention linux > "$L/$key.upper"
        (cd "$(work_of "$dir" "$kind")" && sudo find . -mindepth 1 | LC_ALL=C sort) > "$L/$key.work"
    fi
    lower_snapshot "$dir" "$L/snap/$key.after"
    if cmp -s "$L/snap/$key.before" "$L/snap/$key.after"; then touch "$L/$key.lowers-ok"; fi
    unmount_all
    sudo rm -rf "$dir"
done < <(python3 "$OPS" plan)

# Refused invocations and hostile layouts, as Linux handles them.
echo "overlayfs-oracle: mount-args and hostile layouts"
TMPFS="$L/tmpfs"
mkdir -p "$TMPFS"
while IFS=$'\t' read -r name must setup_cmd opts during; do
    dir="$L/run/arg-$name"
    sudo mkdir -p "$dir/mnt"
    mnt -t tmpfs -o size=16m tmpfs "$TMPFS"
    while IFS= read -r part; do
        if [[ $part == @overlay* ]]; then
            read -r _ where o <<< "$part"
            (cd "$dir" && sudo mount -t overlay overlay -o "$o" "$where") && MOUNTS+=("$dir/$where")
        elif [[ -n $part ]]; then
            (cd "$dir" && sudo env TMPFS="$TMPFS" sh -c "$part")
        fi
    done < <(echo "$setup_cmd" | sed 's/;;/\n/g')
    opts=${opts//\$TMPFS/$TMPFS}
    rc=0
    out=$(cd "$dir" && sudo timeout 60 mount -t overlay overlay -o "$opts" mnt 2>&1) || rc=$?
    if [[ $rc == 0 ]]; then
        MOUNTS+=("$dir/mnt")
        res="mounted"
        if [[ -n $during ]]; then
            d_out=$(cd "$dir" && sudo timeout 30 sh -c "$during" 2>&1) || d_out+=" (rc=$?)"
            res+="; during: $(echo "$d_out" | tr '\n' ' ' | cut -c1-200)"
        fi
        walked=$(sudo timeout 60 python3 -I "$OPS" ids "$dir/mnt" 2>&1 | tail -1) || walked="walk failed or hung"
        res+="; walk: $walked"
    elif [[ $rc == 124 ]]; then
        res="hang"
    else
        res="refused: $(echo "$out" | tr '\n' ' ')"
    fi
    printf '%s\t%s\t%s\n' "$name" "$must" "$res" >> "$L/mount-args.txt"
    unmount_all
    sudo rm -rf "$dir"
done < <(python3 "$OPS" mount-args)
echo "overlayfs-oracle: done, $(ls "$L"/*.log | wc -l) logs in $L"
