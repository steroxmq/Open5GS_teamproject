# HTTP edge sluzba pre virtualny snimac

Tento krok nadvazuje na overenu seriu 12/12 testov iperf3. Pridava samostatnu
HTTP aplikaciu, ktora prijme teplotu z UE, overi udaj a premeni stupne C na K.
Zakladny `compose.yaml`, ucastnik a existujuce meranie iperf3 zostavaju rovnake.

## Predpoklady

- Funkcny lab podla hlavneho README, Docker Compose a Python 3 v Ubuntu VM.
- Prikazy vykonavajte v koreni svojej kopie projektu ako pri predoslom merani.
- Adresa `172.18.0.30` v sieti `core` musi byt volna pre sluzbu `edge`.

Najprv obnovte overeny lab a overte jeho spojenie:

```bash
bash scripts/compose-up.sh
```

Pokracujte po uspesnom hlaseni `HOTOVO`. Ak start zlyha, najprv vyrieste
jeho logy; pridanie HTTP aplikacie neopravi nefunkcne pripojenie UE.

## Spustenie novej aplikacie

```bash
sudo docker compose -f compose.yaml -f compose.edge.yaml config -q
sudo docker compose -f compose.yaml -f compose.edge.yaml up -d --no-deps edge
sudo docker compose -f compose.yaml -f compose.edge.yaml ps edge
```

Prve spustenie stiahne oficialny obraz `python:3.12-slim-bookworm`. Aplikacia
nevyzaduje kompilaciu ani dalsie Python baliky. Pockajte, kym ma `edge` stav
`healthy`; potom vykonajte:

```bash
sudo python3 scripts/check-edge.py
```

Ocakavany vysledok je HTTP `201`, teplota `23.5` C prevedena na `296.65` K
a hlasenie `HOTOVO`. Pre inu hodnotu pouzite napr.:

```bash
sudo python3 scripts/check-edge.py --temperature 21.25
```

Skript vypise novy adresar `results/edge-*`. Obsahuje `summary.json`,
`report.md`, pouzitu trasu a logy. Uspesny vysledok ma `status: "complete"`.
Poziadavka aj odpoved obsahuju rovnake nahodne `request_id`, aby sa neprevzal
vysledok ineho alebo predchadzajuceho poziadavku.

## Ako overujeme cestu cez UPF

Sluzba ma adresu `172.18.0.30:8080` v existujucej sieti Docker `core`.
Pre tento pilot je siet spolocna s funkciami jadra; nejde o samostatnu
izolovanu siet N6. Test preto overuje trasu aj zdroj paketu explicitne:

1. Zisti aktualnu adresu UE na `uesimtun0` a overi bezne kontajnery labu.
2. Pre ciel `172.18.0.30/32` docasne prida trasu cez `uesimtun0` s adresou UE.
   Iny existujuci zaznam neprepise. Po teste odstrani iba trasu, ktoru sam pridal.
3. HTTP socket sa viaze na `uesimtun0` aj na adresu UE. Data tak prejdu cez
   UERANSIM, gNB a UPF. Pouzije sa uz existujuce pravidlo MASQUERADE na UPF.
4. Aplikacia prijima `POST /readings` iba zo zdrojovej adresy UPF
   `172.18.0.7`. Priamy POST z Docker adresy UE nie je prijaty.
5. Test overi HTTP stav, obsah odpovede, zdroj pozorovany aplikaciou a to,
   ze sa kontajnery pocas testu neobnovili.

Samotne otvorenie aplikacie cez `eth0` nie je tymto overenim. Kontrola
zdrojovej adresy sluzi ako diagnostika cesty v tomto labe, nie ako
autentifikacia ani ochrana proti utocnikovi. API je vnutorna vyukova aplikacia
bez publikovaneho portu hostitela. Spracovany udaj sa vrati v odpovedi;
historia udajov sa v aplikacii neuchovava. Pocitadlo zije iba v pamati
a pri restarte aplikacie sa vynuluje. Vysledok konkretneho testu sa
uchovava v adresari `results/edge-*` na hostitelovi.

## Ukoncenie a navrat k zakladnemu labe

```bash
sudo docker compose -f compose.yaml -f compose.edge.yaml stop edge
```

Tento prikaz zastavi iba novu aplikaciu. Zakladny lab mozete dalej spustat
pomocou `bash scripts/compose-up.sh`. Pri prikazoch pre sluzbu `edge` vzdy
uvedte oba subory `-f`; zakladny Compose tuto dodatocnu sluzbu neobsahuje.

## Rozsah overenia

Historicky pilot z 27.10.2026 je ulozeny v
`results/edge-20261027-215037-x7doy1sq/summary.json`: stav complete,
HTTP 201, zdroj UE 10.45.0.5, NAT zdroj UPF 172.18.0.7 a prevod
23.5 C na 296.65 K. Ide o jeden poziadavok, nie seriu QoS.

## Opakovatelna seria HTTP

```bash
sudo python3 scripts/check-edge.py
sudo python3 scripts/measure-http.py
```

Predvolene: tri opakovania kazdej skupiny (bez zataze, TCP UL, TCP DL),
20 poziadaviek v kazdom opakovani, planovany interval zaciatkov 0.4 s,
TCP trva 16 s s cielom 50 Mbit/s. Poziadavky su sekvencne, bez keep-alive;
pri pomalej odpovedi sa planovany interval nemusi dodrzat. Skutocne casy
su v CSV. HTTP zacina 3 s po spusteni ulohy iperf3; samotne cakanie nie
je dokaz prekryvu.

Po dokonceni sa overi vystup klienta aj servera iperf3, suvislost intervalov
a nenulovy prenos v kazdom intervale klienta. Skript uklada monotonne
casy tesne pred spustenim klienta P0 a po jeho ukonceni P1. Ak suvisla
datova faza trvala D sekund vnutri tohto intervalu, jej zaciatok je najneskor
P1-D a jej koniec najskor P0+D. Cely HTTP pokus musi lezat v garantovanom
okne [P1-D+0.1, P0+D-0.1]. Prazdne okno sa odmietne. Timestamp startu
iperf3 pred riadiacou vymenou sa na dokaz prekryvu nepouziva. Neskory start,
timeout alebo nepreukazany prekryv sposobi stav failed. Ide o dokaz prekryvu
s intervalmi prenosu, nie dokaz obsadenia linky v kazdej mikrosekunde.
Kazda odpoved sa overuje rovnako ako v pilote vratane request_id a zdroja.

Novy adresar `results/http-*` obsahuje surove HTTP zaznamy JSONL,
JSON/stderr a prikazy iperf3, CSV, summary.json a report.md.
Median a p95 sa pocitaju iba z uspesnych odpovedi; p95 je nearest rank
na indexe ceil(0.95*n). Uspesnost zahrna vsetky pokusy. Trvanie HTTP
pochadza z merania v klientovi od vytvorenia socketu po precitanie tela,
nezahrna spustenie procesu nsenter. Nie je to ciste sietove RTT.
Ponuknuta zataz a dosiahnuta priepustnost prijimaca su oddelene.

Subory resources obsahuju vzorky suroveho CPU /proc/stat, RAM /proc/meminfo
a load average hostitela pred seriou a po poziadavkach. Suhrn kazdeho
opakovania obsahuje CPU busy z rozdielu pocitadiel (bez idle a iowait,
vratane steal, bez dvojiteho zapocitania guest), minimum MemAvailable
a maximum pouziteho swapu zo vzoriek. Nejde o metriku samostatneho
kontajnera ani o nepretrzite meranie pamate. Verzie Pythonu, jadra,
Docker/Compose, nsenter, ip, iperf3, hash binarky a identity kontajnerov
su v suhrne. `by_condition` agreguje HTTP podla idle/UL/DL; `groups`
uchovava aj jednotlive opakovania a ich stav. Pri chybe zataze sa zachovaju
uz ziskane HTTP udaje. SIGINT a SIGTERM nastavia failed a vyvolaju obnovu
docasnej trasy; beziaca uloha iperf3 moze pred ukoncenim dobehnut do limitu.
SIGKILL a vypadok VM neumoznuju garantovat obnovu trasy ani zapis suhrnu.

V relacii implementacie bol pristup k Docker socketu zamietnuty aj mimo
sandboxu a `sudo -n` hlasilo `sudo: a password is required`. Nova seria
preto zatial nebola overena na aktivnom Compose labe. Historicky uspech
pilota tento limit nenahradza. Existujuce data MongoDB sa nemenili.

Aktualnu kontrolu pristupu a lokalneho overenia opisuje
[zaznam z 27.10.2026](measurements/http-2026-10-27.md).
