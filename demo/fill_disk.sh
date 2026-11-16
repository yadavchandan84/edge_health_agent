#!/usr/bin/env bash
# Scenario 3: fill a small throwaway filesystem (never your real disk).
#
#   sudo bash demo/fill_disk.sh up          # mount a 64 MB tmpfs at /mnt/demo-disk
#   sudo bash demo/fill_disk.sh up-loop     # or: a 64 MB ext4 loopback image instead
#   sudo bash demo/fill_disk.sh fill 95     # write 2 MB files until the mount is 95 % full
#   sudo bash demo/fill_disk.sh clean       # delete the files  -> disk_high closes
#   sudo bash demo/fill_disk.sh down        # unmount and remove
set -euo pipefail

MNT="${MNT:-/mnt/demo-disk}"
SIZE_MB="${SIZE_MB:-64}"
IMG="${IMG:-/tmp/demo-disk.img}"
ts() { date +%H:%M:%S; }
pct() { df --output=pcent "$MNT" | tail -1 | tr -dc '0-9'; }

case "${1:-}" in
  up)
    mkdir -p "$MNT"
    mountpoint -q "$MNT" || mount -t tmpfs -o "size=${SIZE_MB}m" demo-disk "$MNT"
    echo "[$(ts)] tmpfs mounted"; df -h "$MNT" ;;
  up-loop)
    mkdir -p "$MNT"
    [ -f "$IMG" ] || { truncate -s "${SIZE_MB}M" "$IMG"; mkfs.ext4 -q -F "$IMG"; }
    mountpoint -q "$MNT" || mount -o loop "$IMG" "$MNT"
    echo "[$(ts)] loopback ext4 mounted"; df -h "$MNT" ;;
  fill)
    target="${2:-95}"
    mountpoint -q "$MNT" || { echo "run '$0 up' first" >&2; exit 1; }
    i=0
    while [ "$(pct)" -lt "$target" ]; do
      dd if=/dev/zero of="$MNT/fill_$(printf %04d "$i").bin" bs=1M count=2 status=none || break
      i=$((i + 1))
    done
    echo "[$(ts)] wrote $i files, $MNT is at $(pct)%"; df -h "$MNT" ;;
  clean)
    rm -f "$MNT"/fill_*.bin
    echo "[$(ts)] files removed, $MNT is at $(pct)%" ;;
  down)
    umount "$MNT" 2>/dev/null || true
    rmdir "$MNT" 2>/dev/null || true
    rm -f "$IMG"
    echo "[$(ts)] unmounted" ;;
  *)
    sed -n '2,9p' "$0"; exit 1 ;;
esac
