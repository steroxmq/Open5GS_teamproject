# Nazov PR

Pridat HTTP pilot cez UE TUN a meranie odozvy pri TCP zatazi

# Popis PR

Lab doteraz overoval prenos pingom a iperf3. Tato zmena pridava internu
HTTP aplikaciu pre snimac a opakovatelne porovnanie casu celeho HTTP
poziadavku bez riadenej zataze a pri TCP UL/DL s cielom 50 Mbit/s.
POST /readings prijme teplotu 23.5 C a vrati potvrdenie s 296.65 K.

Klient pouziva dynamicku adresu UE, viaze socket na uesimtun0 a overuje
obsah, request_id aj zdroj UPF po NAT. Priama cesta cez Docker eth0
neprejde kontrolou. Meranie kontroluje prekryv celeho HTTP pokusu so
suvislou datovou fazou iperf3 pomocou monotonneho casovania. Zlyhanie
alebo prerusenie zachova ciastocne data a nema stav complete; docasne
trasy a vlastne procesy sa cistia aj po SIGINT/SIGTERM.

Predvolena seria ma tri opakovania kazdej podmienky, 20 poziadaviek na
opakovanie, interval zaciatkov 0.4 s a TCP zataz 16 s. Surove JSONL,
CSV, iperf3 JSON a casovanie, vzorky CPU/RAM, summary.json a report.md
sa ukladaju do noveho results adresara. Start a meranie zdielaju zamok.

## Overene vysledky

Seria z terminalu VM 27.10.2026 o 23:30 po opravach (`41d747a`):
9/9 skupin, 180/180 spravnych HTTP odpovedi; vsetkych 120 pokusov
pri zatazi v potvrdenom datovom okne.
Mediany a p95 boli nezavisle prepocitane zo surovin.

| Podmienka | Median HTTP (ms) | p95 HTTP (ms) | Median TCP prijimaca (Mbit/s) |
| --- | --- | --- | --- |
| Bez riadenej zataze | 3.0490 | 4.674 | - |
| TCP UL | 3.2335 | 6.597 | 49.990 |
| TCP DL | 3.6185 | 7.085 | 50.002 |

p95 pouziva nearest rank ceil(0.95*n), iba uspesne odpovede. Uspesnost
zahrna vsetky pokusy. Novy pilot ma HTTP 201 cez UE 10.45.0.7, spravny
NAT zdroj UPF a obsah odpovede, 3.113 ms a temporary_route_removed=true.
Hash oboch vstupnych HTTP skriptov v suhrnoch sa zhoduje s `41d747a`.
Seria je complete bez cleanup_error. Pri naslednej pasivnej kontrole
chyba docasna /32 trasa UE, listener UPF:5202 aj vsetky procesy iperf3.
Starsia seria z 23:00 a loopback nie su do tychto cisiel zahrnute.

Zdroje: `results/edge-20261027-233033-nb67lqwg/`,
`results/http-20261027-233034-aapeox7c/`; audit a priame doklady ocisty
su v `results/http-audit-20261027-233359-36orwax1/`.
Riadne ukoncenie novej UE serie overuje opravenu verziu pri uspechu;
SIGTERM a chyby ocisty boli overene lokalnymi regresnymi testami.

## Validacia

- 30 testov: HTTP API a cesta, percentily, prekryv, chyby a prerusenie,
  zachovanie dat, obnova tras, ukoncenie procesov vratane potomka.
- Izolovany iperf3 3.21 na loopbacku: oba TCP smery a zrusenie zataze;
  vlastne procesy ukoncene a port uvolneny. Nejde o UE/TUN vysledok.
- Export sledovaneho stromu bez handoff a lokalnych dat: testy, --help
  a docker compose config -q s kopiou .env.example.
- Aktualizovane README podla origin/main, kontrola diff a tajomstiev.

## Obmedzenia

Cas celeho HTTP poziadavku nie je ciste sietove RTT. Pilot na spolocnej
Docker sieti nie je izolovana N6 ani realne MEC; UERANSIM nesimuluje
fyzicku vrstvu NR. Ciel TCP 50 Mbit/s nie je maximalna kapacita.
Skupiny maju pevne poradie a iba tri opakovania, CPU VM kolise; rozdiely
nepreukazuju statisticku vyznamnost ani samostatny vplyv siete.

SIGKILL alebo vypadok VM nemoze garantovat obnovu. Zdrojova IP nie je
autentifikacia. Surove vysledky su lokalne ignorovane artefakty, cerstvy
klon obsahuje postup a skripty na vlastne meranie. Plny start na novej
cistej VM este nebol overeny. Nove UE meranie opravenej verzie spustil
pouzivatel na existujucom labe; agent vykonal audit a pasivne kontroly,
nie nove spustenie labu. Pomocne moduly a server nie su v suhrne
samostatne runtime hashovane. MongoDB, ucastnik a .env sa nemenia.

Podrobnosti: [HTTP navod](edge-service.md),
[meraci zaznam](measurements/http-2026-10-27.md),
[revizia vetvy](reviews/edge-http-2026-10-27.md).
