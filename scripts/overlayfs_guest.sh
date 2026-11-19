#!/bin/bash
# Guest side of the overlayfs test (runs in the Hurd guest as root).
# overlayfs_test.sh copies it to /tmp with the layer and scenario tools,
# the binaries, the squashfs images and the translator, then calls:
#
#   overlayfs_guest.sh prepare SET MODE KEY   build the layers of SET under
#                                             /root/ovl/KEY; mount squashfs
#                                             and tmpfs layers; snapshot the
#                                             lowers
#   overlayfs_guest.sh mount KEY [VAR=VALUE...]
#                                             settrans -a KEY/mnt $BIN
#                                             --lowerdir=..., with the
#                                             variables in the
#                                             translator's environment
#   overlayfs_guest.sh unmount KEY            settrans -gR, then check that
#                                             no translator process is left
#   overlayfs_guest.sh lowers-unchanged KEY   snapshot again and compare
#   overlayfs_guest.sh teardown KEY           force every mount off, delete
#   overlayfs_guest.sh plain SET DIR          the plain-directory control's
#                                             tree: the merged view of SET
#   overlayfs_guest.sh arg-case NAME          one row of overlayfs_ops.py
#                                             mount-args: set it up, start
#                                             the translator, print what
#                                             happened, tear it down
#
# The translator is /tmp/ovl-translator (a copy of OVERLAYFS_BIN, or the
# adapter for another invocation).  Every step is idempotent: prepare
# tears down what a failed run left under the same KEY.
set -uo pipefail

# Under /tmp, not /root: the perms scenario runs as uid 1000, which must
# reach the layers through every parent directory.
R=/tmp/ovl
T=/tmp
BIN=$T/ovl-translator
LAYERS="python3 $T/overlayfs_layers.py"

# An active translator inherits settrans's output, so a pipe would never
# close on a successful start; the log file is read afterwards instead.
start() {
    local where=$1 log=$2; shift 2
    settrans -a "$where" "$@" </dev/null >"$log" 2>&1
    local rc=$?
    cat "$log"
    return $rc
}

set_info() { $LAYERS sets | awk -F'\t' -v s="$1" '$1 == s'; }

teardown() {
    local d=$R/$1 m
    [[ -d $d ]] || return 0
    for m in "$d/mnt" "$d/inner" "$d/t" "$d/tmpfs" "$d"/l[0-9]*; do
        [[ -d $m ]] && settrans -fg "$m" 2>/dev/null
    done
    rm -rf "$d"
}

case "${1:-}" in
prepare)
    set=$2 mode=$3 key=$4 d=$R/$4
    teardown "$key"
    mkdir -p "$R"
    IFS=$'\t' read -r _ n kind _ sq _ <<< "$(set_info "$set")"
    $LAYERS build "$set" "$d" --convention hurd --bin $T/ovl-bin || exit 1
    if [[ $sq == sq ]]; then
        cp "$T/ovl-img/$set.l1.img" "$d/l$n.img"
        start "$d/l$n" "$d/l$n.log" $T/squashfs.static --readonly -T file "$d/l$n.img" || exit 1
    fi
    case $kind in
        tmpfs) start "$d/t" "$d/t.log" /hurd/tmpfs 64M || exit 1 ;;
        tmpfs-1m) start "$d/t" "$d/t.log" /hurd/tmpfs 1M || exit 1 ;;
    esac
    upper=$d/upper work=$d/work
    if [[ $kind == tmpfs* ]]; then
        upper=$d/t/upper work=$d/t/work
        $LAYERS build-upper "$set" "$upper" --convention hurd && mkdir "$work" || exit 1
    fi
    ro=""; [[ $mode == ro ]] && ro=--no-upper
    lowers=""
    for l in $($LAYERS lowers "$set" $ro); do lowers+="${lowers:+:}$d/$l"; done
    args="--lowerdir=$lowers"
    [[ $mode == rw ]] && args+=" --upperdir=$upper --workdir=$work"
    echo "$args" > "$d/args"
    echo "$upper" > "$d/upper-path"
    echo "$work" > "$d/work-path"
    mkdir "$d/mnt"
    bash "$0" snapshot "$key" > "$d.before"
    ;;
snapshot)
    d=$R/$2
    for l in "$d"/l[0-9]*; do
        [[ -d $l ]] || continue
        echo "== $(basename "$l")"
        if [[ -f $l.img ]]; then sha256sum < "$l.img"; else $LAYERS snapshot "$l"; fi
    done
    ;;
lowers-unchanged)
    d=$R/$2
    bash "$0" snapshot "$2" > "$d.after"
    cmp -s "$d.before" "$d.after" || { diff "$d.before" "$d.after" | head -20; exit 1; }
    ;;
mount)
    key=$2 d=$R/$2; shift 2
    bin=$BIN
    # settrans -a does not hand its environment to the translator
    # (measured: a translator script saw OVERLAYFS_FAULT empty), so the
    # variables reach it through a wrapper that is the translator.
    if [[ $# -gt 0 ]]; then
        bin=$d/env-wrapper
        printf '#!/bin/sh\nexec env %s %s "$@"\n' "$*" "$BIN" > "$bin"
        chmod 755 "$bin"
    fi
    # shellcheck disable=SC2046
    settrans -a "$d/mnt" "$bin" $(cat "$d/args") </dev/null >"$d/mnt.log" 2>&1
    rc=$?
    cat "$d/mnt.log"
    exit $rc
    ;;
unmount)
    d=$R/$2
    # The no-senders notifications for ports a finished client held come
    # in later, so a first EBUSY right after a run can be transient.
    for i in $(seq 25); do
        settrans -gR "$d/mnt" 2>/dev/null && break
        sleep 0.2
    done
    settrans -gR "$d/mnt" 2>/dev/null; rc=$?
    # Only the translator itself: a shell whose command line names the
    # mount point (ssh's bash -c) is not one.
    left=$(ps -eo pid,args | awk -v m="$d/" 'index($0, m) && $2 ~ /ovl-translator|ovl-fault|unionfs|env-wrapper/')
    if [[ -n $left ]]; then echo "translator still running: $left"; exit 1; fi
    exit $rc
    ;;
teardown)
    teardown "$2"
    rm -f "$R/$2.before" "$R/$2.after"
    ;;
plain)
    rm -rf "$3"
    $LAYERS merged "$2" "$3" --bin $T/ovl-bin
    ;;
arg-case)
    name=$2 key=arg-$2 d=$R/arg-$2
    teardown "$key"
    IFS=$'\t' read -r _ must setup opts during < <(python3 $T/overlayfs_ops.py mount-args | awk -F'\t' -v n="$name" '$1 == n')
    mkdir -p "$d/mnt" "$d/tmpfs"
    cd "$d" || exit 1
    start "$d/tmpfs" "$d/tmpfs.log" /hurd/tmpfs 16M >/dev/null || exit 1
    # Paths in the options are relative to the case's directory; the
    # translator gets them absolute.
    absolute() {
        local out="" item key val p v
        IFS=, read -ra items <<< "$1"
        for item in "${items[@]}"; do
            key=${item%%=*} val=${item#*=} v=""
            IFS=: read -ra ps <<< "$val"
            for p in "${ps[@]}"; do
                [[ $p == /* ]] || p=$d/$p
                v+="${v:+:}$p"
            done
            out+=" --$key=$v"
        done
        echo "$out"
    }
    while IFS= read -r part; do
        if [[ $part == @overlay* ]]; then
            read -r _ where o <<< "$part"
            # shellcheck disable=SC2046
            start "$d/$where" "$d/$where.log" $BIN $(absolute "$o") >/dev/null
        elif [[ -n $part ]]; then
            TMPFS=$d/tmpfs sh -c "$part"
        fi
    done < <(echo "$setup" | sed 's/;;/\n/g')
    opts=${opts//\$TMPFS/$d/tmpfs}
    # shellcheck disable=SC2046
    timeout 60 settrans -a "$d/mnt" $BIN $(absolute "$opts") </dev/null >"$d/mnt.log" 2>&1
    rc=$?
    msg=$(tr '\n' ' ' < "$d/mnt.log" | cut -c1-200)
    if [[ $rc == 124 ]]; then
        res="hang"
    elif [[ $rc != 0 ]]; then
        if [[ -z $msg || $msg == *Assertion* || $msg == *signal* ]]; then
            res="crash: rc=$rc $msg"
        else
            res="refused: $msg"
        fi
    else
        res="mounted"
        if [[ -n $during ]]; then
            out=$(timeout 30 sh -c "$during" 2>&1); r=$?
            res+="; during: rc=$r $(echo "$out" | tr '\n' ' ' | cut -c1-120)"
            [[ $r == 124 ]] && res+=" (hang)"
        fi
        walked=$(timeout 60 python3 $T/overlayfs_ops.py ids "$d/mnt" 2>&1 | tail -1); r=$?
        [[ $r == 124 ]] && walked="hang"
        res+="; walk: $walked"
        if timeout 20 fsysopts "$d/mnt" >/dev/null 2>&1; then res+="; alive"; else res+="; dead"; fi
    fi
    cd /
    teardown "$key"
    printf '%s\t%s\t%s\n' "$name" "$must" "$res"
    ;;
*)
    sed -n '2,25p' "$0" >&2
    exit 2
    ;;
esac
