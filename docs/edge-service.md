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

HTTP API a kontrola obsahu odpovedi boli overene lokalnymi automatickymi
testami. Prenos cez skutocny tunel UERANSIM a pravidlo NAT v Ubuntu VM musi
potvrdit spustenie `check-edge.py` v labe. Jeden cas HTTP poziadavku nie je
seriou merani QoS ani dokazom oneskorenia realneho MEC prostredia.

Dalsi krok po uspesnom pilote je seria HTTP poziadaviek bez zataze a pocas
rovnako nastaveneho iperf3 testu, vratane zaznamu CPU a RAM.
