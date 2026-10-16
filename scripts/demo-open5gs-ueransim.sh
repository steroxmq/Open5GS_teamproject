#!/usr/bin/env bash
set -eo pipefail

ROOT="$HOME/UERANSIM"
if [ -n "$UERANSIM_DIR" ]; then ROOT="$UERANSIM_DIR"; fi

IMSI="imsi-999700000000001"
NETNS="ueransim-999700000000001-internet-psi1"
TUN_IF="uesimtun0"
PING_TARGET="1.1.1.1"
PING_COUNT=100
PING_INTERVAL="0.2"
START_TIMEOUT=120

if [ -n "$DEMO_PING_TARGET" ]; then PING_TARGET="$DEMO_PING_TARGET"; fi
if [ -n "$DEMO_PING_COUNT" ]; then PING_COUNT="$DEMO_PING_COUNT"; fi

if [ "$1" = "--help" ]; then
    cat <<'HELP'
Usage: ./demo-open5gs-ueransim.sh

Starts the local UERANSIM gNB and UE, waits for registration and an active
PDU session, pings 1.1.1.1 from the UE namespace, saves logs under
~/UERANSIM/results/demo-<timestamp>, then stops only the processes it started.
Before the test it enables IPv4 forwarding and adds a scoped UE-subnet NAT rule.
Those host settings are reapplied each time the script runs; they are not saved
across a reboot.

Optional environment variables:
  UERANSIM_DIR       UERANSIM checkout (default: ~/UERANSIM)
  DEMO_PING_TARGET   ping destination (default: 1.1.1.1)
  DEMO_PING_COUNT    number of pings (default: 100)

The subscriber must already exist in Open5GS WebUI with IMSI
999700000000001. This script does not create or modify subscriber data.
HELP
    exit 0
fi

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [ "$(id -u)" -eq 0 ]; then
    die "Run this script as your normal user; it invokes sudo only where needed."
fi

GNB_BIN="$ROOT/build/nr-gnb"
UE_BIN="$ROOT/build/nr-ue"
NRCLI_BIN="$ROOT/build/nr-cli"
GNB_CONFIG="$ROOT/config/open5gs-gnb.yaml"
UE_CONFIG="$ROOT/config/open5gs-ue.yaml"

for path in "$GNB_BIN" "$UE_BIN" "$NRCLI_BIN" "$GNB_CONFIG" "$UE_CONFIG"; do
    [ -e "$path" ] || die "Required file not found: $path"
done

for unit in mongod.service open5gs-amfd.service open5gs-smfd.service open5gs-upfd.service; do
    systemctl is-active --quiet "$unit" || die "$unit is not active. Start the Open5GS services first."
done

if pgrep -x nr-gnb >/dev/null 2>&1 || pgrep -x nr-ue >/dev/null 2>&1; then
    printf 'An nr-gnb or nr-ue process is already running:\n' >&2
    pgrep -a -x nr-gnb >&2 || true
    pgrep -a -x nr-ue >&2 || true
    die "Stop the manually started UERANSIM process in its terminal, then run this script."
fi

sudo -v || die "sudo authorization failed."

WAN_IF="$(ip route get "$PING_TARGET" | awk '{for (i=1; i<=NF; i++) if ($i=="dev") {print $(i+1); exit}}')"
[ -n "$WAN_IF" ] || die "Could not determine the outbound interface for $PING_TARGET."
ip link show ogstun >/dev/null 2>&1 || die "ogstun is missing. Check that open5gs-upfd is running."
sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null || die "Could not enable IPv4 forwarding."
sudo sysctl -w net.ipv4.conf.ogstun.forwarding=1 >/dev/null || die "Could not enable forwarding on ogstun."
sudo sysctl -w "net.ipv4.conf.$WAN_IF.forwarding=1" >/dev/null || die "Could not enable forwarding on $WAN_IF."
sudo iptables -t nat -C POSTROUTING -s 10.45.0.0/16 -o "$WAN_IF" -j MASQUERADE 2>/dev/null ||
    sudo iptables -t nat -A POSTROUTING -s 10.45.0.0/16 -o "$WAN_IF" -j MASQUERADE ||
    die "Could not add the UE subnet NAT rule."
printf 'IPv4 forwarding and UE NAT are ready: ogstun -> %s\n' "$WAN_IF"

if sudo ip netns list | awk '{print $1}' | grep -Fxq "$NETNS"; then
    die "Namespace $NETNS already exists. Inspect it before removing it; the script will not delete an existing namespace."
fi

RESULTS_DIR="$ROOT/results"
RUN_DIR="$RESULTS_DIR/demo-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
umask 077

GNB_LOG="$RUN_DIR/gnb.log"
UE_LOG="$RUN_DIR/ue.log"
STATUS_LOG="$RUN_DIR/ue-status.txt"
PDU_LOG="$RUN_DIR/pdu-session.txt"
PING_LOG="$RUN_DIR/ping.txt"
UE_PID_FILE="$RUN_DIR/ue.pid"
GNB_PID=""
UE_PID=""
UE_SUDO_PID=""

cleanup() {
    exit_code=$?
    trap - EXIT INT TERM
    if [ -n "$UE_PID" ] && sudo kill -0 "$UE_PID" 2>/dev/null; then
        sudo kill -TERM "$UE_PID" 2>/dev/null || true
    fi
    if [ -n "$GNB_PID" ] && kill -0 "$GNB_PID" 2>/dev/null; then
        kill -TERM "$GNB_PID" 2>/dev/null || true
    fi
    if [ -n "$UE_SUDO_PID" ]; then wait "$UE_SUDO_PID" 2>/dev/null || true; fi
    if [ -n "$GNB_PID" ]; then wait "$GNB_PID" 2>/dev/null || true; fi
    printf '\nDemo processes stopped. Logs: %s\n' "$RUN_DIR"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Starting Open5GS/UERANSIM demo.\n'
printf 'IMSI: %s\n' "$IMSI"
printf 'Results: %s\n' "$RUN_DIR"
printf 'Starting gNB; log: %s\n' "$GNB_LOG"
"$GNB_BIN" -c "$GNB_CONFIG" >"$GNB_LOG" 2>&1 &
GNB_PID=$!

sleep 2
kill -0 "$GNB_PID" 2>/dev/null || die "gNB exited during startup. See $GNB_LOG"

printf 'Starting UE; log: %s\n' "$UE_LOG"
sudo bash -c 'printf "%s\n" "$$" > "$1"; exec "$2" -c "$3"' _ "$UE_PID_FILE" "$UE_BIN" "$UE_CONFIG" >>"$UE_LOG" 2>&1 &
UE_SUDO_PID=$!

printf 'Waiting up to %s seconds for registration and an active PDU session...\n' "$START_TIMEOUT"
READY=0
ATTEMPTS=$((START_TIMEOUT / 2))
for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if [ -s "$UE_PID_FILE" ]; then
        UE_PID="$(cat "$UE_PID_FILE")"
        if ! sudo kill -0 "$UE_PID" 2>/dev/null; then
            die "UE exited during startup. See $UE_LOG"
        fi
    fi

    STATUS="$(sudo "$NRCLI_BIN" "$IMSI" --exec status 2>/dev/null || true)"
    PDU="$(sudo "$NRCLI_BIN" "$IMSI" --exec ps-list 2>/dev/null || true)"
    if printf '%s\n' "$STATUS" | grep -Fq 'MM-REGISTERED/NORMAL-SERVICE' &&
       printf '%s\n' "$PDU" | grep -Fq 'state: PS-ACTIVE'; then
        READY=1
        break
    fi
    sleep 2
done

[ "$READY" -eq 1 ] || die "UE did not reach registered state with an active PDU session in time. See $GNB_LOG and $UE_LOG"

printf '%s\n' "$STATUS" | tee "$STATUS_LOG"
printf '\n%s\n' "$PDU" | tee "$PDU_LOG"

UE_IP=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    UE_IP="$(sudo ip netns exec "$NETNS" ip -o -4 addr show dev "$TUN_IF" 2>/dev/null | awk '{split($4, a, "/"); print a[1]}')"
    [ -n "$UE_IP" ] && break
    sleep 1
done
[ -n "$UE_IP" ] || die "Could not find the UE TUN address in namespace $NETNS."

printf '\nPDU tunnel is up: %s on %s\n' "$UE_IP" "$TUN_IF"
printf 'Pinging %s from the UE namespace (%s packets)...\n' "$PING_TARGET" "$PING_COUNT"
if sudo ip netns exec "$NETNS" ping -I "$TUN_IF" -c "$PING_COUNT" -i "$PING_INTERVAL" "$PING_TARGET" >"$PING_LOG" 2>&1; then
    cat "$PING_LOG"
else
    ping_status=$?
    cat "$PING_LOG"
    die "Ping failed with exit status $ping_status. See $PING_LOG, $UE_LOG, and $GNB_LOG"
fi

printf '\nDemo succeeded. The existing Open5GS subscriber was used; no subscriber data was changed.\n'
printf 'Saved evidence:\n  %s\n  %s\n  %s\n  %s\n  %s\n' "$STATUS_LOG" "$PDU_LOG" "$PING_LOG" "$GNB_LOG" "$UE_LOG"
printf 'For the presentation, show that subscriber in Open5GS WebUI and this terminal output/log folder.\n'
