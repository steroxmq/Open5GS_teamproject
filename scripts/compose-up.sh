#!/usr/bin/env bash
# Start the already built Compose lab and save evidence under results/.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMSI="999700000000001"
WAIT_SECONDS="${LAB_WAIT_SECONDS:-120}"
RUN_DIR=""
DOCKER=(docker)
CORE=(mongo webui nrf scp amf smf upf ausf udm udr pcf nssf bsf)

die() { printf 'CHYBA: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  --help|-h)
    cat <<'HELP'
Pouzitie: bash scripts/compose-up.sh

Predpoklady: zostaveny obraz open5gs-ueransim:amf-fix, vyplneny subor
.env a existujuci ucastnik 999700000000001 so zhodnymi klucmi vo WebUI.
Skript spusti jadro siete, obnovi gNB a UE a overi prenos tromi paketmi
ping na UPF a internet. Obnova gNB a UE prerusi ich aktualne spojenie.
Po skonceni ponecha kontajnery spustene. Logy ulozi do results/compose-*.
LAB_WAIT_SECONDS meni limit cakania na jednu fazu (predvolene 120 s).
HELP
    exit 0 ;;
  "") ;;
  *) die "Neznamy argument: $1. Pouzite --help." ;;
esac
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] || die "LAB_WAIT_SECONDS musi byt cele cislo od 1 do 9999."
[[ -f "$ROOT/compose.yaml" && -f "$ROOT/.env" ]] || die "V koreni projektu musi byt compose.yaml a vyplneny subor .env."
for tool in docker timeout flock; do
    command -v "$tool" >/dev/null || die "Chyba prikaz: $tool"
done
if (( EUID != 0 )); then
    sudo -v || die "Autorizacia sudo zlyhala."
    DOCKER=(sudo -n docker)
fi

dock() { timeout --signal=TERM --kill-after=2s 20s "${DOCKER[@]}" "$@"; }
dc() { dock compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" "$@"; }
dc_up() {
    timeout --signal=TERM --kill-after=3s 240s "${DOCKER[@]}" compose \
        --project-directory "$ROOT" -f "$ROOT/compose.yaml" up "$@"
}

umask 077
mkdir -p "$ROOT/results"
exec 9>"$ROOT/results/.compose-up.lock"
flock -n 9 || die "V tomto projekte uz prebieha spustanie."
RUN_DIR="$(mktemp -d "$ROOT/results/compose-$(date +%Y%m%d-%H%M%S)-XXXXXX")"

finish() {
    local rc=$?
    trap - EXIT INT TERM
    set +e
    dc ps --all >"$RUN_DIR/compose-status.txt" 2>&1
    dc logs --no-color --tail=300 >"$RUN_DIR/containers.log" 2>&1
    printf '%s\n' "$rc" >"$RUN_DIR/exit-code.txt"
    printf '\nLogy: %s\n' "$RUN_DIR"
    if (( rc != 0 )); then printf 'Spustanie sa skoncilo chybou (kod %s).\n' "$rc" >&2; fi
    exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Restrict readiness logs to the container's latest start, including reused core containers.
wait_log() {
    local service=$1 marker=$2 deadline=$((SECONDS + WAIT_SECONDS)) id started running
    printf 'Cakam na %s: %s\n' "$service" "$marker"
    while (( SECONDS < deadline )); do
        id="$(dc ps --all -q "$service" 2>/dev/null)" || id=""
        if [[ -n "$id" ]]; then
            running="$(dock inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" || running="false"
            started="$(dock inspect -f '{{.State.StartedAt}}' "$id" 2>/dev/null)" || started=""
            if [[ "$running" == true && -n "$started" ]] &&
               dc logs --no-color --since "$started" "$service" >"$RUN_DIR/$service-readiness.log" 2>&1 &&
               grep -Fq -- "$marker" "$RUN_DIR/$service-readiness.log"; then
                return 0
            fi
        fi
        sleep 2
    done
    die "$service nedosiahol ocakavany stav. Pozrite $RUN_DIR/$service-readiness.log a containers.log."
}

printf 'Spustanie Compose labu. Logy: %s\n' "$RUN_DIR"
dock info --format '{{.ServerVersion}}' >"$RUN_DIR/docker-version.txt" || die "Docker daemon neodpoveda. Spustite: sudo systemctl start docker"
dc version >"$RUN_DIR/compose-version.txt"
dc config -q || die "Compose konfiguracia nie je platna."
dock image inspect open5gs-ueransim:amf-fix >/dev/null 2>&1 || die "Najprv zostavte obraz: sudo docker compose --progress=plain build gnb"

printf 'Spustam jadro siete...\n'
dc_up -d --no-build "${CORE[@]}" >>"$RUN_DIR/startup.log" 2>&1 || die "Spustenie jadra zlyhalo. Pozrite startup.log."
wait_log amf 'AMF initialize...done'
wait_log smf 'SMF initialize...done'
wait_log upf 'UPF initialize...done'

count="$(dc exec -T --interactive=false mongo mongosh open5gs --quiet --eval \
    'db.subscribers.countDocuments({imsi: "999700000000001"})')" || die "Nepodarilo sa overit zaznam ucastnika v MongoDB."
count="${count//$'\r'/}"
[[ "$count" == 1 ]] || die "Ucastnik $IMSI chyba alebo je duplicitny. Otvorte http://localhost:9998 vo VM, vytvorte jeden zaznam podla README a spustite skript znova."

printf 'Obnovujem spojenie gNB a UE...\n'
dc stop --timeout 8 ue gnb >>"$RUN_DIR/startup.log" 2>&1 || die "Zastavenie gNB a UE zlyhalo. Pozrite startup.log."
dc_up -d --no-deps --no-build --force-recreate gnb >>"$RUN_DIR/startup.log" 2>&1 || die "Spustenie gNB zlyhalo. Pozrite startup.log."
wait_log gnb 'NG Setup procedure is successful'
dc_up -d --no-deps --no-build --force-recreate ue >>"$RUN_DIR/startup.log" 2>&1 || die "Spustenie UE zlyhalo. Pozrite startup.log."
wait_log ue 'TUN interface[uesimtun0,'

dc exec -T --interactive=false ue nr-cli "imsi-$IMSI" --exec status >"$RUN_DIR/ue-status.txt" || die "UE neodpoveda na status."
dc exec -T --interactive=false ue nr-cli "imsi-$IMSI" --exec ps-list >"$RUN_DIR/pdu-session.txt" || die "UE neodpoveda na ps-list."
grep -Fqx 'cm-state: CM-CONNECTED' "$RUN_DIR/ue-status.txt" || die "UE nie je CM-CONNECTED."
grep -Fqx 'mm-state: MM-REGISTERED/NORMAL-SERVICE' "$RUN_DIR/ue-status.txt" || die "UE nie je zaregistrovany v normalnej sluzbe."
grep -Fq 'state: PS-ACTIVE' "$RUN_DIR/pdu-session.txt" || die "UE nema aktivnu PDU sesiu."
dc exec -T --interactive=false ue ip -o -4 addr show dev uesimtun0 >"$RUN_DIR/ue-address.txt"
[[ -s "$RUN_DIR/ue-address.txt" ]] || die "Rozhranie uesimtun0 nema IPv4 adresu."
dc exec -T --interactive=false upf ip -br addr show ogstun >"$RUN_DIR/upf-address.txt"

for target in 10.45.0.1 1.1.1.1; do
    printf 'Overujem UE -> %s...\n' "$target"
    log="$RUN_DIR/ping-$target.txt"
    if dc exec -T --interactive=false ue env LC_ALL=C ping -I uesimtun0 -c 3 -W 2 -w 10 "$target" >"$log" 2>&1; then
        cat "$log"
        grep -Fq ' 0% packet loss' "$log" || die "Ping na $target ma straty. Pozrite $log."
    else
        cat "$log"
        die "Ping na $target zlyhal. Pozrite $log."
    fi
done
printf '\nHOTOVO: UE je pripojeny, UPF aj internet odpovedaju bez strat.\n'
