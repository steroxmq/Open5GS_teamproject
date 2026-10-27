# Pravidla projektu

- Komunikacia s pouzivatelom po rusky; dokumentacia a nove hlasenia po slovensky bez diakritiky.
- Zachovat zmeny pouzivatela, .env a data MongoDB. Nikdy nepouzivat `down -v`.
- Nevypisovat kluce z .env. Neobnovovat ucastnika ani databazu pri merani.
- Pouzivat existujuce obrazy; nevyhnutna kompilacia iba s jednym pracovnikom.
- Start a merania musia zdielat zamok `results/.compose-up.lock`.
- UE IP zistovat dynamicky. Dokaz prenosu vyzaduje skutocnu odpoved cez TUN.
- Pouzivat izolovany iperf3 3.21; TCP ciel 50 Mbit/s nie je maximalna kapacita.
- Surove vysledky uchovavat v novom adresari results; neuspesny beh nesmie mat stav complete.
- Cas celeho HTTP poziadavku nie je ciste sietove RTT. Pilot na spolocnej Docker sieti nie je realne MEC.
- `compose-up.sh` obnovuje gNB a UE. Nevolat ho pocas merania.
- Opravnenia sandboxu a sudo odlisovat od poruchy siete. Historicke vysledky neoznacovat za nove.
- Pracovat v pracovnej vetve, po overeni vytvorit lokalny kontrolny commit; bez push a merge.
