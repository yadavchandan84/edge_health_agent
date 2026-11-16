#!/usr/bin/env bash
# Scenario 5: lose the uplink for a while, then get it back.
#
#   sudo bash demo/net_toggle.sh 75            # outage of 75 s, then restore automatically
#   sudo MODE=link IFACE=eth0 bash demo/net_toggle.sh 75
#
# MODE=firewall (default when iptables exists): drops all outbound traffic except
#   loopback and the agent's own dashboard port, so the browser keeps working while
#   gateway, internet, DNS, TCP and HTTP probes all fail. Safe over SSH.
# MODE=link: takes the interface down. Most realistic, but do NOT use it over SSH.
#
# The network is restored on exit, including Ctrl-C.
# 75 s = 5 probe rounds: network_lost opens on the 3rd failed round (30-45 s in,
# depending on where the outage falls in the 15 s probe cycle).
set -euo pipefail

SECS="${1:-75}"
PORT="${PORT:-8765}"
CHAIN=ZF_DEMO
ts() { date +%H:%M:%S; }

if [ -z "${MODE:-}" ]; then
  if command -v iptables >/dev/null; then MODE=firewall; else MODE=link; fi
fi
IFACE="${IFACE:-$(ip route show default | awk '{print $5; exit}')}"

restore() {
  if [ "$MODE" = firewall ]; then
    iptables -D OUTPUT -j "$CHAIN" 2>/dev/null || true
    iptables -F "$CHAIN" 2>/dev/null || true
    iptables -X "$CHAIN" 2>/dev/null || true
  else
    ip link set dev "$IFACE" up
    # Some setups (WSL2, plain VMs without NetworkManager) drop the default route
    # with the link; put it back if it is missing.
    if [ -n "${GW:-}" ] && ! ip route show default | grep -q .; then
      ip route add default via "$GW" dev "$IFACE" || true
    fi
  fi
  echo "[$(ts)] network restored ($MODE)"
}

GW="$(ip route show default | awk '{print $3; exit}')"
trap restore EXIT

if [ "$MODE" = firewall ]; then
  iptables -N "$CHAIN"
  iptables -A "$CHAIN" -o lo -j ACCEPT
  iptables -A "$CHAIN" -p tcp --sport "$PORT" -j ACCEPT
  iptables -A "$CHAIN" -j DROP
  iptables -I OUTPUT 1 -j "$CHAIN"
else
  ip link set dev "$IFACE" down
fi
echo "[$(ts)] network DOWN via $MODE${IFACE:+ ($IFACE)} for ${SECS}s (network_lost expected 30-45 s from now, on the 3rd failed 15 s round)"
sleep "$SECS"
