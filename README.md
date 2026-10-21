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

Logiku skriptu overili simulovane scenare Docker CLI; jeho beh so skutocnym
5G labom zatial nebol overeny. Vyssie uvedene merania platia pre povodny
postup spustenia.

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

Po opatovnom vytvoreni kontajnerov sa rozhranie `ogstun` automaticky obnovilo
s adresou `10.45.0.1/16`. Test prebehol so zachovanou databazou MongoDB.
Cista instalacia na druhom pocitaci a automaticke spustenie po uplnom restarte
VM zatial neboli overene. Merania priepustnosti pomocou iperf3 pre tuto verziu
s Compose zatial neboli vykonane.

## Nativne prostredie - vychodiskovy stav

Overene v Ubuntu VM dna 15.10.2026.
UERANSIM 3.3.0; Open5GS 2.8.0.

UE sa zaregistrovalo do siete, ziskalo adresu IPv4 a aktivnu relaciu PDU.
Test z menneho priestoru UE na adresu 1.1.1.1: 100/100 odpovedi,
0% strat a priemerne oneskorenie 6,258 ms.

Skript `scripts/demo-open5gs-ueransim.sh` spusta lokalne gNB a UE. Predpoklada,
ze Open5GS je uz nainstalovany, jeho sluzby su spustene a ucastnik je vytvoreny.
Ide o nativny variant pre kontrolnu VM, ktory nepouziva Docker Compose.
