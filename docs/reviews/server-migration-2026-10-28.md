# Priprava prenosu na server - 28.10.2026

Zaklad: `15b606e`, po zlouceni HTTP vetvy do `main`.

## Rozsah

- Navod pre samostatnu Ubuntu Server VM v Proxmox: prostriedky, AVX,
  TUN/SCTP, siete, SSH, prenos kodu a dat, obnova a prijimacie kontroly.
- Preflight uklada iba diagnostiku bez klucov; nemeni siet ani databazu.
- Export MongoDB vyzaduje pozastavenych klientov, cisty sledovany kod,
  spolocny zamok a zhodu tagu MongoDB s obrazom beziaceho kontajnera.
- Export pouziva sukromny adresar, gzip, SHA256 a odstrani nedokonceny archiv.
- Priprava sluzieb, start MongoDB, obnova a start edge v navode pouzivaju
  spolocny zamok. Pri obnove sa pod zamkom znovu kontroluje obraz, stav
  klientov a nova databaza; backup prebehne predtym bez vnoreneho zamku.
- Backup explicitne nacita aj compose.edge.yaml; beziace edge odmietne.
- WebUI sa predvolene publikuje na `127.0.0.1`; vzdialeny pristup je cez SSH.
  Existujuci lokalny pristup na porte 9998 ostava mozny. Na ine rozhranie
  sa da viazat cez `.env`, ale serverovy preflight to odmietne.
- V produkcnych HTTP meracich skriptoch sa nic nemenilo. Dva existujuce
  unit testy uz nevyzaduju skutocny namespace testovacieho procesu ani
  pritomnost Docker CLI pri simulovani zavislosti.

## Vykonane kontroly

- `python3 -m unittest discover -s tests -v`: 48 testov uspesnych.
- Nove testy overuju kolizie vlastnej/cudzej siete, VPN a nativneho
  ogstun, format a prava `.env`, odmietnutie zapisujucich klientov,
  presunuteho tagu MongoDB, necisteho kodu a kolizie zamku. Overena je
  ochrana prav suborov, kontrolny sucet a zlyhany/poskodeny export.
  Dalsie testy spustaju pripravny blok navodu iba s podstavnym Dockerom:
  kolizia zamku nesmie vyvolat stop a chyba stop nesmie prejst na up/export.
- Compose 5.6.0 bez daemona: 16 sluzieb a 16 existujucich bind-mount suborov.
  Pri rovnakej vstupnej kopii .env.example sa model mimo publikovania WebUI
  nezmenil; siete a prava ostali rovnake. Overeny je aj explicitny override
  WEBUI_BIND_IP; serverovy preflight nadalej vyzaduje loopback.
- Python AST, `bash -n`, syntakticka kontrola 13 Bash blokov navodu,
  ASCII novych dokumentov a `git diff --check`: bez chyby.
- --help oboch novych skriptov funguje bez spustenia exportu alebo obnovy.
  Zivy preflight cielovej VM v tejto relacii nebol vykonany.
- Kontrola iba pripravy: pracovny .env, pouzivatelsky handoff a MongoDB
  zostali zachovane; nebol vykonany stop, export ani restore ziveho labu.

Finalne kontroly presli aj z exportu sledovaneho stromu Git bez lokalneho
.env a handoff. Testy a suhrn su v lokalnom ignorovanom adresari
`results/server-migration-validation-20261028-212208-d_hbijpz/`.

## Co este nie je overene

Test exportu pouziva simulovany Docker a testovacie gzip data. Nebol vykonany
skutocny export ani obnova MongoDB, import obrazov alebo spustenie v Proxmox.
Tie vyzaduju zdrojovu a cielovu VM a postup v `docs/server-migration.md`.
Tento commit sam nepotvrdzuje uspesnu migraciu ani novy vysledok cez UE.

Pred skutocnym prenosom musi spravca potvrdit parametre VM, adresny plan,
SSH pristup a ulozisko zaloh. Po prenose treba ulozit nove vysledky merani
a overit spustenie po restarte. Historicke vysledky VirtualBoxu zostavaju
historickymi dokazmi a neoznacuju sa za serverove merania.
