# Open5GS_teamproject

Vyukove prostredie 5G SA: Open5GS + UERANSIM. Overena verzia s Docker Compose
je dostupna vo vetve `main`.

## Docker Compose: overena konfiguracia

Overene 21.10.2026 na Ubuntu 22.04 x86_64 vo VirtualBoxe: 4 GB RAM,
2 vCPU a priamy pristup VirtualBoxu k VT-x.

| Komponent | Verzia / obraz |
| --- | --- |
| Open5GS | `gradiant/open5gs:2.8.0` |
| WebUI | `gradiant/open5gs-webui:2.8.0` |
| MongoDB | `mongo:6.0` |
| UERANSIM | Lokalny obraz `open5gs-ueransim:amf-fix` |
| Zdrojovy kod UERANSIM | Commit `1ae9bf2062b57595dbcbc4bc1d0a0ccf06815bac` |

Lokalny obraz pre gNB a UE sa zostavuje pomocou `docker/ueransim/Dockerfile`.
Pri spusteni UERANSIM vypisuje verziu `3.3.0`; pouzity commit obsahuje dalsie
opravy spracovania NAS a vyberu AMF.

### 1. Priprava Linuxu a repozitara

Potrebne su Git, Docker Engine s Compose, pristup k `/dev/net/tun` a podpora
SCTP v jadre Linuxu. Siet Docker `172.18.0.0/24` musi byt k dispozicii pre tento
projekt a nesmie sa prekryvat s inymi pouzivanymi sietami.

```bash
sudo systemctl enable --now docker
git clone --branch main https://github.com/steroxmq/Open5GS_teamproject.git
cd Open5GS_teamproject
cp -n .env.example .env
chmod 600 .env
nano .env
```

V subore `.env` vyplnte `UE_KEY` a `UE_OPC`: kazda hodnota musi obsahovat
32 hexadecimalnych znakov. Rovnake hodnoty pouzijete pri vytvarani ucastnika
vo WebUI. Subor `.env` je vyluceny zo sledovania v Gite.

Ak je na tejto VM uz nainstalovany nativny Open5GS, pred spustenim Compose
zastavte jeho sluzby a jeho MongoDB:

```bash
sudo systemctl stop 'open5gs-*.service' mongod.service
```

### 2. Zostavenie UERANSIM a spustenie jadra siete

Prve zostavenie vykonajte pri zastavenych kontajneroch tohto prostredia.
Dockerfile zostavuje UERANSIM s parametrom `-j1`.

```bash
sudo docker compose config -q
sudo docker compose --progress=plain build gnb
sudo docker compose up -d mongo webui nrf scp amf smf upf ausf udm udr pcf nssf bsf
sudo docker compose ps
```

Pred dalsim krokom musi mat MongoDB stav `healthy` a funkcie jadra siete musia
bezat bez opakovanych restartov. UPF automaticky vytvori a aktivuje rozhranie
`ogstun` s adresou `10.45.0.1/16`, zapne presmerovanie IPv4 a nastavi NAT.

### 3. Vytvorenie ucastnika v novej databaze

Otvorte WebUI **v prehliadaci Ubuntu VM**: <http://localhost:9998>.
V novej databaze skript `docker/open5gs/config/mongo-init.js` vytvori ucet
WebUI `admin` s heslom `1423` pre izolovane vyukove prostredie.
Inicializacia prebehne pri prvom spusteni MongoDB s prazdnym datovym adresarom.

Pridajte ucastnika s nasledujucimi parametrami:

| Pole | Hodnota |
| --- | --- |
| IMSI | `999700000000001` |
| K / Subscriber Key | Hodnota `UE_KEY` zo suboru `.env` |
| OP type | `OPc` |
| OPc | Hodnota `UE_OPC` zo suboru `.env` |
| AMF, Authentication Management Field | `8000` |
| Slice SST | `1` |
| Slice SD | Nezadane |
| APN / DNN | `internet` |
| Typ relacie PDU | IPv4 |

Ulozte ucastnika. Jeho zaznam sa uchovava vo zvazku `mongo-data`; pri dalsich
spusteniach ho netreba vytvarat znova. Pri zmene klucov v `.env` upravte aj
kluce daneho ucastnika vo WebUI, aby sa hodnoty zhodovali.

### 4. Automaticke spustenie existujuceho labu

Po zostaveni obrazu a vytvoreni ucastnika mozete cely postup spustit prikazom:

```bash
bash scripts/compose-up.sh
```

Skript pouzije sudo pre Docker, spusti jadro siete a pocka na inicializaciu
AMF, SMF a UPF. Overi pritomnost jedneho zaznamu ucastnika v MongoDB, obnovi
gNB, pocka na uspesny NG Setup a potom obnovi UE. Po vytvoreni rozhrania TUN
overi aktualny stav registracie a relacie PDU. Nakoniec posle po tri pakety
ping na UPF a internet. Obnova gNB a UE prerusi ich existujuce spojenie.

Logy, stav kontajnerov, adresa UE a vysledky ping sa ulozia do noveho adresara
`results/compose-*`. Pri chybe skript vrati nenulovy kod a vypise cestu k logom.
Kontajnery zostanu spustene. Ak zaznam ucastnika chyba, vytvorte ho vo WebUI
podla kroku 3 a spustite skript znova.

Skript ocakava existujuci lokalny obraz `open5gs-ueransim:amf-fix`. Pre nove
prostredie najprv vykonajte zostavenie z kroku 2. Limit cakania na jednu fazu
je predvolene 120 sekund a mozno ho zmenit premennou `LAB_WAIT_SECONDS`.
Moznosti zobrazi `bash scripts/compose-up.sh --help`.

Automaticky skript bol overeny 21.10.2026 na skutocnom Compose labe
s commitom `cf04c39`: UE ziskal adresu `10.45.0.3`, UPF aj internet odpovedali
na 3/3 pakety bez strat. Priemerny RTT bol 1,057 ms na UPF a 3,772 ms
na internet. Adresa UE sa pri dalsom spusteni moze zmenit.

### Rucne pripojenie gNB a UE

```bash
sudo docker compose up -d --no-deps gnb
sudo docker compose logs -f --tail=20 gnb
```

Pockajte na hlasenie `NG Setup procedure is successful`, potom stlacte Ctrl+C.
Ctrl+C pri sledovani logov ukonci ich vypis; kontajner zostane spusteny.

```bash
sudo docker compose up -d --no-deps ue
sudo docker compose logs -f --tail=40 ue
```

Pockajte na uspesnu registraciu, vytvorenie relacie PDU a hlasenie
`TUN interface[uesimtun0, ...] is up`, potom stlacte Ctrl+C.

### 5. Overenie prenosu dat

```bash
sudo docker compose exec -T upf ip -br addr show ogstun
sudo docker compose exec -T ue ping -I uesimtun0 -c 5 -W 2 10.45.0.1
sudo docker compose exec -T ue ping -I uesimtun0 -c 5 -W 2 1.1.1.1
```

Rozhranie `ogstun` musi mat adresu `10.45.0.1/16`; pri oboch testoch ping musia
prichadzat odpovede. IP adresa UE sa prideluje dynamicky, preto prikazy
pouzivaju nazov rozhrania.

### 6. Meranie TCP a UDP v aktivnom labe

Pred prvym meranim pripravte izolovany iperf3 **3.21**. Instalacny skript
spustite ako bezny pouzivatel:

```bash
sudo apt-get install -y build-essential curl ca-certificates
bash scripts/setup-lab-iperf3.sh
```

Skript overi SHA-256 oficialneho zdrojoveho archivu, skompiluje ho prikazom
`make -j1` a ulozi binarku do
`~/.local/open5gs-lab/iperf3-3.21/bin/iperf3`. Pouziva vlastnu staticku
kniznicu libiperf. Ak instalacia skonci chybou, vypise umiestnenie logov.

Po uspesnom automatickom spusteni labu spustite meranie:

```bash
sudo python3 scripts/measure-compose.py
```

Meraci skript automaticky najde binarku pouzivatela, ktory zavolal `sudo`,
a overi verziu 3.21. Klient aj server pouziju jej absolutnu cestu v sietovych
mennych priestoroch kontajnerov UE a UPF. Systemova verzia z PATH sa automaticky
nepouzije. Na Ubuntu VM musia byt dostupne Python 3 a nastroje `nsenter`, `ip`,
`ss`, `ping` a `timeout`. Verzia, cesta a SHA-256 binarky aj identifikatory
obrazov kontajnerov sa ulozia do metadat merania.

Skript overi trasu z aktualnej adresy UE cez `uesimtun0` na UPF `10.45.0.1`.
Pred zatazou odosle 20 paketov ping na UPF. Nasleduju tri opakovania kazdej
kombinacie TCP/UDP a UL/DL: spolu 12 testov po 5 sekundach. UL znamena
UE -> UPF, DL znamena UPF -> UE. Pred kazdym testom sa spusti server iperf3
viazany na adresu `10.45.0.1`, port 5202. Proces servera ma casovy limit.

Predvoleny ciel TCP je **50 Mbit/s**, ciel UDP **5 Mbit/s** a velkost datagramu
UDP 1200 bajtov. Ide o meranie pri zadanej zatazi, nie o urcenie maximalnej
priepustnosti. Kazdy test pouziva jeden datovy tok. Cely uspesny postup trva
priblizne 1-2 minuty; pri chybe sa zastavi a ulozi dostupne vysledky.

V adresari `results/iperf-*` vzniknu:

- povodne JSON a chybove vystupy klienta a servera pre kazdy test;
- `runs.csv` s hodnotami jednotlivych behov;
- `summary.json` s metadatami a suhrnnymi hodnotami;
- `report.md` s tabulkou, `ping-upf.txt`, trasa a diagnosticke logy.

Priepustnost sa uvadza na strane prijimaca. Pri UDP sa pocita zo skutocne
prijatych bajtov a casu v intervaloch prijimaca. Celkove percento strat UDP v skupine sa pocita ako
100 * sucet stratenych paketov / sucet paketov. Jitter v suhrne predstavuje
median zaverecnych hodnot prijimaca. ICMP RTT sa uvadza samostatne pred zatazou.

Parametre zobrazi `python3 scripts/measure-compose.py --help`. Pri porovnavani
pouzite zhodne ciele bitrate, trvanie, pocet opakovani a verziu iperf3.
Vypocty metrik a spracovanie chyb boli overene simulovanymi scenarmi.
Prikazy a JSON oboch smerov TCP aj UDP boli overene s iperf3 3.21 na lokalnom
spojeni. V Compose labe bola 21.10.2026 s iperf3 3.21 dokoncena cela seria:
12/12 testov, tri behy TCP UL, TCP DL, UDP UL a UDP DL, kazdy po 5 sekund.
Median prijimaca bol 49,885 Mbit/s pre TCP UL, 49,881 Mbit/s pre TCP DL,
5,000 Mbit/s pre UDP UL a 4,999 Mbit/s pre UDP DL. UDP nezaznamenalo straty;
TCP zaznamenalo spolu 52 opakovanych prenosov v UL a 46 v DL.
Presne hodnoty, parametre, identifikatory a hranice overenia su v
[zazname merania](docs/measurements/compose-2026-10-21.md).

Systemova verzia iperf3 3.9 v kontrolnej VM pocas TCP UL cakala pri ukonceni
na zvysok bloku dat, hoci sprava o ukonceni uz bola vo vstupnej fronte
riadiaceho spojenia. Izolovany iperf3 3.21 tento diagnosticky beh ukoncil
spravne pri zachovanych kontajneroch UE a UPF.

### Zastavenie a opatovne spustenie

Prikaz `sudo docker compose stop` zastavi kontajnery. Prikaz
`sudo docker compose down` bez dalsich parametrov odstrani kontajnery a siet
projektu, pricom zachova pomenovany zvazok `mongo-data` s uctom WebUI
a zaznamom ucastnika. Pri opatovnom spusteni dodrzte poradie:
jadro siete -> gNB s uspesnym NG Setup -> UE.

Pri praci s existujucou databazou zachovajte zvazok `mongo-data`. Skript
`mongo-init.js` pripravuje ucet WebUI; vytvorenie ucastnika v novej databaze
je opisane v kroku 3.

### Zaznamenane vysledky

| Test | Odpovede | Straty | Priemerny RTT | Maximalny RTT |
| --- | --- | --- | --- | --- |
| UE -> UPF `10.45.0.1` | 5/5 | 0% | 0,799 ms | 1,262 ms |
| UE -> internet, 100 paketov za ~100 s | 100/100 | 0% | 3,134 ms | 7,858 ms |
| UE -> internet po opatovnom vytvoreni kontajnerov | 5/5 | 0% | 2,780 ms | 3,752 ms |
| UE -> internet po automatickom spusteni | 3/3 | 0% | 3,772 ms | 4,174 ms |

Po opatovnom vytvoreni kontajnerov sa rozhranie `ogstun` automaticky obnovilo
s adresou `10.45.0.1/16`. Test prebehol so zachovanou databazou MongoDB.
Cista instalacia na druhom pocitaci a automaticke spustenie po uplnom restarte
VM zatial neboli overene. Kompletna seria merani TCP/UDP UL/DL s iperf3 3.21
bola uspesna pri zadanej zatazi 50/5 Mbit/s. Dalsim krokom je overenie postupu
na druhom pocitaci podla tohto README.

## Nativne prostredie - vychodiskovy stav

Overene v Ubuntu VM dna 15.10.2026.
UERANSIM 3.3.0; Open5GS 2.8.0.

UE sa zaregistrovalo do siete, ziskalo adresu IPv4 a aktivnu relaciu PDU.
Test z menneho priestoru UE na adresu 1.1.1.1: 100/100 odpovedi,
0% strat a priemerne oneskorenie 6,258 ms.

Skript `scripts/demo-open5gs-ueransim.sh` spusta lokalne gNB a UE. Predpoklada,
ze Open5GS je uz nainstalovany, jeho sluzby su spustene a ucastnik je vytvoreny.
Ide o nativny variant pre kontrolnu VM, ktory nepouziva Docker Compose.
