# Open5GS_teamproject

## Native baseline

Overene v Ubuntu VM 15.10.2026.
UERANSIM 3.3.0; Open5GS 2.8.0.

UE sa zaregistroval v sieti, ziskal IPv4 a aktivnu PDU sesiu.
Test z UE namespace na 1.1.1.1: 100/100 odpovedi, 0% strata,
priemerna latencia 6.258 ms.

Skript v `scripts/` spusta lokalne gNB a UE. Predpoklada,
ze Open5GS je uz nainstalovany, jeho sluzby bezia a odberatel je vytvoreny.
Toto je nativny variant pre kontrolnu VM, nie Compose nasadenie.
