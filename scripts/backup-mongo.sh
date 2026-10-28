#!/usr/bin/env bash
# Konzistentny export po zastaveni vsetkych sluzieb projektu okrem MongoDB.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
die() { printf 'CHYBA: %s\n' "$*" >&2; exit 1; }
case "${1:-}" in
  --help|-h)
    cat <<'HELP'
Pouzitie: bash scripts/backup-mongo.sh [rodicovsky-adresar]

Najprv zastavte sluzby labu a spustite iba MongoDB podla docs/server-migration.md.
Skript nevypina sluzby a nemeni databazu. Vyziada sudo pre Docker, ziska
spolocny zamok a odmietne export pri inej beziacej sluzbe tohto projektu.
Ulozi archiv open5gs, pocty zaznamov, verzie a SHA256 do noveho adresara
backups/mongo-*. Zaloha obsahuje tajne kluce a ucty; nepatri do Gitu.
HELP
    exit 0 ;;
esac
(( $# <= 1 )) || die "Pouzite --help."
for tool in docker flock timeout gzip sha256sum git; do
    command -v "$tool" >/dev/null || die "Chyba prikaz: $tool"
done
[[ -f "$ROOT/.env" ]] || die "Chyba existujuci .env; nevytvarajte nove kluce pri prenose."
git -C "$ROOT" diff --quiet HEAD -- || die "Najprv ulozte sledovane zmeny do commitu; zaloha musi mat jednoznacnu verziu kodu."
DOCKER=(docker)
if (( EUID != 0 )); then
    sudo -v || die "Autorizacia sudo zlyhala."
    DOCKER=(sudo -n docker)
fi
dc() {
    timeout --signal=TERM --kill-after=3s 300s "${DOCKER[@]}" compose \
        --project-directory "$ROOT" -f "$ROOT/compose.yaml" -f "$ROOT/compose.edge.yaml" "$@"
}
umask 077
mkdir -p "$ROOT/results"
exec 9>"$ROOT/results/.compose-up.lock"
flock -n 9 || die "Prebieha start, meranie alebo zaloha labu."
running="$(dc ps --status running --services)"
[[ "$running" == mongo ]] || die "Musi bezat iba mongo. Zastavte ostatne sluzby podla navodu; skript ich nezastavi."
id="$(dc ps -q mongo)"
mongo_image="$(timeout 20s "${DOCKER[@]}" inspect -f '{{.Image}}' "$id")"
cached_image="$(timeout 20s "${DOCKER[@]}" image inspect -f '{{.Id}}' mongo:6.0)"
[[ "$mongo_image" == "$cached_image" ]] || die "Tag mongo:6.0 sa lisi od obrazu beziacej databazy. Pred exportom dohodnite presnu verziu obrazov."
parent="${1:-$ROOT/backups}"
mkdir -p -- "$parent"
folder="$(mktemp -d "$parent/mongo-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
finish() {
    code=$?
    trap - EXIT INT TERM
    rm -f -- "$folder/open5gs.archive.gz.partial"
    printf 'Zaloha: %s\n' "$folder"
    if (( code != 0 )); then printf 'CHYBA: Zaloha nie je dokoncena.\n' >&2; fi
    exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
dc exec -T mongo mongosh open5gs --quiet --eval \
    'print(JSON.stringify(db.getCollectionNames().sort().map(n => ({collection:n,count:db.getCollection(n).countDocuments()}))))' \
    >"$folder/collections.json"
git -C "$ROOT" rev-parse HEAD >"$folder/source-commit.txt"
printf '%s\n' "$mongo_image" >"$folder/mongo-image-id.txt"
dc exec -T mongo mongodump --version >"$folder/mongodump-version.txt"
dc exec -T mongo mongodump --db open5gs --archive --gzip \
    >"$folder/open5gs.archive.gz.partial" 2>"$folder/mongodump.stderr"
gzip -t "$folder/open5gs.archive.gz.partial"
mv -- "$folder/open5gs.archive.gz.partial" "$folder/open5gs.archive.gz"
(
    cd -- "$folder"
    sha256sum open5gs.archive.gz collections.json source-commit.txt mongo-image-id.txt mongodump-version.txt > SHA256SUMS
)
printf 'HOTOVO: Archiv MongoDB bol vytvoreny. .env preneste samostatne podla navodu.\n'
