# Open5GS_teamproject

Учебный стенд 5G SA: Open5GS + UERANSIM. Проверенный вариант Docker Compose
находится в ветке `feat/docker-compose`.

## Docker Compose: проверенная конфигурация

Проверено 21.10.2026 в Ubuntu 22.04 x86_64 под VirtualBox: 4 ГБ RAM,
2 vCPU, прямой доступ VirtualBox к VT-x.

| Компонент | Версия / образ |
| --- | --- |
| Open5GS | `gradiant/open5gs:2.8.0` |
| WebUI | `gradiant/open5gs-webui:2.8.0` |
| MongoDB | `mongo:6.0` |
| UERANSIM | Локальный образ `open5gs-ueransim:amf-fix` |
| Исходники UERANSIM | Коммит `1ae9bf2062b57595dbcbc4bc1d0a0ccf06815bac` |

Локальный образ собирается из `docker/ueransim/Dockerfile` для gNB и UE.
Баннер UERANSIM показывает `3.3.0`; используемый коммит содержит дополнительные
исправления обработки NAS и выбора AMF.

### 1. Подготовить Linux и репозиторий

Требуются Git, Docker Engine с Compose, доступ к `/dev/net/tun` и поддержка
SCTP в ядре Linux. Сеть Docker `172.18.0.0/24` должна быть доступна для этого
проекта и не пересекаться с другими используемыми сетями.

```bash
sudo systemctl enable --now docker
git clone --branch feat/docker-compose https://github.com/steroxmq/Open5GS_teamproject.git
cd Open5GS_teamproject
cp -n .env.example .env
chmod 600 .env
nano .env
```

В `.env` заполнить `UE_KEY` и `UE_OPC`: каждое значение — 32 шестнадцатеричных
символа. Эти же значения потребуются при создании абонента в WebUI.
`.env` исключён из Git.

Если на этой VM уже установлен нативный Open5GS, перед запуском Compose
остановить его службы и его MongoDB:

```bash
sudo systemctl stop 'open5gs-*.service' mongod.service
```

### 2. Собрать UERANSIM и запустить ядро

Первую сборку выполнять при остановленных контейнерах стенда. Dockerfile
использует сборку UERANSIM с `-j1`.

```bash
sudo docker compose config -q
sudo docker compose --progress=plain build gnb
sudo docker compose up -d mongo webui nrf scp amf smf upf ausf udm udr pcf nssf bsf
sudo docker compose ps
```

Перед следующим этапом MongoDB должен иметь статус `healthy`, функции ядра —
работать без циклических перезапусков. UPF автоматически создаёт и поднимает
`ogstun` с адресом `10.45.0.1/16`, включает IPv4 forwarding и настраивает NAT.

### 3. На новой базе создать абонента

Открыть WebUI **в браузере Ubuntu VM**: <http://localhost:9998>.
На новой базе скрипт `docker/open5gs/config/mongo-init.js` создаёт учётную
запись WebUI `admin` с паролем `1423` для изолированного учебного стенда.
Инициализация выполняется при первом запуске MongoDB с пустым каталогом данных.

Добавить абонента со следующими параметрами:

| Поле | Значение |
| --- | --- |
| IMSI | `999700000000001` |
| K / Subscriber Key | Значение `UE_KEY` из `.env` |
| OP type | `OPc` |
| OPc | Значение `UE_OPC` из `.env` |
| AMF, Authentication Management Field | `8000` |
| Slice SST | `1` |
| Slice SD | Не задан |
| APN / DNN | `internet` |
| Тип PDU-сессии | IPv4 |

Сохранить абонента. Он хранится в томе `mongo-data`; при последующих запусках
повторное создание не требуется. Изменение ключей в `.env` требует также
согласовать ключи этого абонента в WebUI.

### 4. Подключить gNB и UE

```bash
sudo docker compose up -d --no-deps gnb
sudo docker compose logs -f --tail=20 gnb
```

Дождаться `NG Setup procedure is successful`, затем нажать Ctrl+C.
Ctrl+C в команде просмотра логов прекращает просмотр; контейнер продолжает работать.

```bash
sudo docker compose up -d --no-deps ue
sudo docker compose logs -f --tail=40 ue
```

Дождаться успешной регистрации, PDU-сессии и сообщения
`TUN interface[uesimtun0, ...] is up`, затем нажать Ctrl+C.

### 5. Проверить передачу данных

```bash
sudo docker compose exec -T upf ip -br addr show ogstun
sudo docker compose exec -T ue ping -I uesimtun0 -c 5 -W 2 10.45.0.1
sudo docker compose exec -T ue ping -I uesimtun0 -c 5 -W 2 1.1.1.1
```

У `ogstun` должен быть адрес `10.45.0.1/16`; оба ping должны получать ответы.
IP UE назначается динамически — команды используют имя интерфейса.

### Остановка и повторный запуск

`sudo docker compose stop` останавливает контейнеры. Обычный
`sudo docker compose down` удаляет контейнеры и сеть проекта, сохраняя
именованный том `mongo-data` с учётной записью WebUI и абонентом.
Для повторного запуска использовать порядок: ядро → успешный NG Setup gNB → UE.

При работе с существующей базой сохранять том `mongo-data`. Скрипт
`mongo-init.js` подготавливает учётную запись WebUI; создание абонента на новой
базе выполняется на шаге 3.

### Зафиксированные результаты

| Проверка | Ответы | Потери | Средний RTT | Максимальный RTT |
| --- | --- | --- | --- | --- |
| UE → UPF `10.45.0.1` | 5/5 | 0% | 0,799 мс | 1,262 мс |
| UE → Интернет, 100 пакетов за ~100 с | 100/100 | 0% | 3,134 мс | 7,858 мс |
| UE → Интернет после пересоздания контейнеров | 5/5 | 0% | 2,780 мс | 3,752 мс |

После пересоздания контейнеров `ogstun` автоматически восстановился с адресом
`10.45.0.1/16`. Проверка выполнена с сохранённой базой MongoDB.
Чистая установка на второй машине и автоматический запуск после полного
перезапуска VM ещё не проверены. Измерения пропускной способности iperf3
для этого Compose-варианта ещё не выполнялись.

## Native baseline

Проверено в Ubuntu VM 15.10.2026.
UERANSIM 3.3.0; Open5GS 2.8.0.

UE зарегистрировался в сети, получил IPv4 и активную PDU-сессию.
Проверка из UE namespace до 1.1.1.1: 100/100 ответов, 0% потерь,
средняя задержка 6.258 ms.

Скрипт в `scripts/` запускает локальные gNB и UE. Он предполагает,
что Open5GS уже установлен, его службы запущены, а абонент создан.
Это нативный вариант для контрольной VM, не Compose-развёртывание.
