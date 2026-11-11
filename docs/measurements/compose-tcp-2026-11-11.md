# Meranie TCP bez limitu iperf3 - 11.11.2026

Seria `iperf-20261111-191753-ejaibpd_` vo VirtualBoxe sa skoncila so stavom
`complete`. Dokoncilo sa 12 z 12 planovanych behov. Zaciatok serie bol
`2026-11-11T19:17:53.116750+01:00`.

## Postup

- Trasa: UE -> gNB -> UPF, cez `uesimtun0`. Server iperf3 bol na `10.45.0.1:5202`.
- Nastroj: iperf3 3.21, jeden datovy tok. Tri behy pre kazdy protokol a smer, po 5 sekund.
- TCP: parameter `--tcp-rate 0` vypol obmedzenie rychlosti v iperf3.
- UDP: ciel 5 Mbit/s, datagram 1200 bajtov.
- Pred meranim sa rucne obnovili gNB a UE po tom, ako automaticky start skoncil pri kontrole logu AMF.
  Kontrolny ping z UE na UPF preukazal prenos cez `uesimtun0`.

```bash
sudo python3 scripts/measure-compose.py --tcp-rate 0 --udp-rate 5M --duration 5 --repeats 3
```

## Vysledky

| Protokol | Smer | Behy | Median prijimaca (Mbit/s) | Min | Max | Opakovane prenosy TCP spolu | Straty UDP | Median jitter UDP (ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TCP | UL | 3 | 435,976 | 429,057 | 443,955 | 912 | - | - |
| TCP | DL | 3 | 479,104 | 447,918 | 479,127 | 989 | - | - |
| UDP | UL | 3 | 5,000 | 5,000 | 5,000 | - | 0/7815 (0 %) | 0,0743 |
| UDP | DL | 3 | 5,000 | 4,999 | 5,001 | - | 0/7814 (0 %) | 0,1048 |

UL = UE -> UPF, DL = UPF -> UE. ICMP pred zatazou: priemerny RTT 0,628 ms,
maximum 2,150 ms, straty 0 %.

Priepustnost a straty sa skontrolovali v povodnych JSON suboroch iperf3.
Priepustnost vsetkych behov sa zhoduje s `runs.csv` aj so suhrnom v
`summary.json`. Surove data su v lokalnom archive serie.

Vysledok opisuje dosiahnutu priepustnost jedneho toku pocas 5-sekundovych behov
v tejto VM. Na porovnanie so serverom treba zopakovat rovnake parametre testu.
Seria z [21.10.2026](compose-2026-10-21.md) zachytava prenos pri riadenej zatazi
50 Mbit/s pre TCP a 5 Mbit/s pre UDP.
