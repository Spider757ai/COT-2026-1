# CloudMaker

Маленькое «облако» на одном Linux-хосте: HTTP API + веб-морда, которые создают,
запускают, останавливают и удаляют виртуалки на QEMU/KVM через libvirt.

Автор: Буянин Артур, БПИ263. Задание «Cloud Maker» (СОТ-2026-1).

## Что сделано

| Пункт | Что | Где смотреть |
|---|---|---|
| **База** | ВМ запускается без GUI: `qemu-img` + cloud-init ISO + `virsh define/start` | `scripts/run_vm_virsh.sh`, раздел «Базовое задание» ниже |
| **1** | HTTP API: создание / запуск / остановка / перезагрузка / удаление ВМ | `app/server.py`, раздел «API» |
| **2** | cloud-init: `user_data`, SSH-ключи, hostname, пароль | `build_seed()` в `app/server.py` |
| **3** | Виртуальные сети (NAT / изолированная) и подключение ВМ к хостовой сети через bridge | `/api/networks`, `scripts/create_bridge.sh` |
| **5** | Приложение в контейнере управляет ВМ на хосте через сокет libvirt, ВМ видны в `virsh list` | `Dockerfile`, `docker-compose.yml` |
| **6** | Восстановление состояния после перезапуска контейнера/сервиса | `reconcile()` в `app/server.py` |
| **7** | Регистрация образов (название, путь, описание) | `/api/images` |
| **9** | Квоты на суммарные vCPU и RAM | `quota_usage()`, переменные `CM_QUOTA_*` |
| **10** | Веб-интерфейс (свой, не swagger) | `app/static/index.html`, открывается на `http://localhost:8080/` |
| **11** | Один скрипт проверяет все пункты выше | `scripts/check.sh` |
| **12** | Скрипт очистки следов на хосте | `scripts/cleanup.sh` |

Пункт 4 не делал - он взаимоисключающий с 5-м, выбрал 5 (ВМ живут на хосте). Пункт 8 (мониторинг) не делал.

## Как проверить (коротко)

Нужен Linux x86_64 с включённой виртуализацией (рассчитано на Ubuntu 22.04/24.04 или Debian 12).

```bash
# 1. поставить libvirt/qemu и docker (если ещё нет)
sudo apt update
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients qemu-utils genisoimage curl docker.io docker-compose-v2
# (на Debian 12 вместо docker-compose-v2 - пакет docker-compose)

# 2. склонировать и запустить проверку
git clone <ссылка на этот репозиторий> cloudmaker
cd cloudmaker
sudo ./scripts/check.sh

# 3. после проверки всё убрать
sudo ./scripts/cleanup.sh          # --all - ещё и скачанный образ удалить
```

`check.sh` сам скачает облачный образ Ubuntu 24.04 minimal (~250 МБ), соберёт и поднимет контейнер,
и пройдёт по всем пунктам. Каждая проверка печатает `PASS`/`FAIL`, в конце итог. Выглядит примерно так:

```
== п.1 + п.2  Создание ВМ через API с cloud-init
  PASS ВМ создана (201)
  PASS virsh list видит cm-check-vm (ВМ на хосте)
  PASS ВМ получила IP 10.77.0.23
  PASS SSH по ключу из API работает
  PASS hostname выставлен cloud-init'ом (check-host)
  PASS user_data-скрипт выполнился
...
Итого: PASS ..., FAIL 0
```

Весь прогон занимает пару минут, большую часть - ожидание загрузки ВМ. Если подсеть `10.77.0.0/24` у вас
уже занята, можно задать другую: `sudo CM_CHECK_SUBNET=10.88.0.0/24 ./scripts/check.sh`.

Что именно делает проверка:

1. Проверяет `/dev/kvm`, `virsh`, `docker`.
2. Поднимает сервис в docker (`docker compose up -d --build`), ждёт `/api/health`.
3. Открывает веб-интерфейс.
4. Регистрирует образ и проверяет, что кривой путь отклоняется.
5. Создаёт NAT-сеть `check-net` и смотрит через `virsh net-info`, что она активна.
6. Пытается создать ВМ больше квоты - должен быть `409`, и ВМ не должна появиться в libvirt.
7. Создаёт ВМ с SSH-ключом, hostname и `user_data`-скриптом, ждёт IP, заходит по SSH и проверяет,
   что hostname правильный и скрипт отработал.
8. Останавливает и запускает ВМ через API, сверяет с `virsh domstate`.
9. Останавливает контейнер, «роняет» ВМ через `virsh destroy`, запускает контейнер обратно -
   сервис должен сам поднять ВМ.
10. Удаляет ВМ, сеть и образ, проверяет, что в libvirt и на диске ничего не осталось.

## Как это устроено

```
браузер / curl
   -> app/server.py в контейнере (порт 8080)
   -> сокет libvirt хоста (/var/run/libvirt, проброшен в контейнер)
   -> libvirtd -> QEMU/KVM на хосте
```

Пара решений, которые я принял:

- Сервис говорит с libvirt только через `virsh`. Можно было взять python-биндинги libvirt, но так
  проще ставить (ничего из pip не нужно) и каждое действие сервиса можно повторить руками в терминале.
  Сам сервис - только стандартная библиотека Python.
- Всё, что создаёт сервис, называется с префиксом `cm-`: ВМ `web` в API - это `cm-web` в `virsh list`.
  Так сервис не трогает чужие ВМ, и скрипт очистки точно знает, что удалять.
- Диск ВМ - тонкий qcow2-оверлей поверх зарегистрированного образа (`qemu-img create -b ...`).
  ВМ создаётся за секунду, базовый образ не портится, и одну базу можно использовать для многих ВМ.
- Пути к дискам одинаковые в контейнере и на хосте (`/var/lib/libvirt/images/cloudmaker` проброшен
  один в один). Это важно: контейнер пишет путь к диску в XML ВМ, а открывает диск уже QEMU на хосте.
  Плюс AppArmor/SELinux на хосте этот каталог уже разрешают для libvirt.
- Источник правды про ВМ - libvirt. В `data/` сервис хранит только то, чего в libvirt нет: реестр
  образов (`images.json`) и параметры/желаемое состояние ВМ (`vms.json`). Файлы пишутся атомарно
  (временный файл + rename), так что при падении посреди записи они не побьются.

### cloud-init (п. 2)

Для каждой ВМ собирается ISO с меткой `cidata` (источник NoCloud) с двумя файлами:

- `meta-data` - `instance-id` и `local-hostname`;
- `user-data` - `#cloud-config`, который сервис генерирует сам: hostname, `ssh_authorized_keys`,
  пароль (если задан) и маркер `/etc/cloudmaker`.

Если в запросе передан свой `user_data` (`#cloud-config ...` или скрипт `#!/bin/bash ...`), он
добавляется вторым куском в MIME multipart. cloud-init выполняет оба, а заголовок `Merge-Type`
говорит ему склеивать списки (например, `runcmd` и `write_files`), а не затирать.
Посмотреть, что получилось для конкретной ВМ: `/var/lib/libvirt/images/cloudmaker/vms/<имя>/cidata/`.

### Сети и бридж (п. 3)

Через API можно создать три вида сетей libvirt:

- `nat` - своя подсеть с DHCP и выходом наружу через NAT хоста;
- `isolated` - то же, но без выхода наружу (ВМ видят только друг друга и хост);
- `bridge` - ВМ подключаются прямо в физическую сеть хоста через существующий бридж и получают
  адрес от того же DHCP, что и сам хост.

Сети создаются с `autostart`, так что переживают перезагрузку хоста.
При создании ВМ параметр `network` - это имя сети из `/api/networks` (`default` - стандартная сеть libvirt).

Бридж в хостовую сеть создаётся один раз руками, потому что это меняет сеть самого хоста:

```bash
ip -br link                                   # смотрим имя проводного интерфейса, например enp3s0
sudo ./scripts/create_bridge.sh enp3s0        # br0 поверх enp3s0 через NetworkManager
curl -X POST localhost:8080/api/networks \
     -d '{"name":"lan","mode":"bridge","bridge":"br0"}'
```

Скрипт делает примерно это:

```bash
nmcli con add type bridge ifname br0 con-name br0 stp no ipv4.method auto
nmcli con add type bridge-slave ifname enp3s0 master br0 con-name br0-port
nmcli con down <старое подключение enp3s0>
nmcli con up br0-port && nmcli con up br0
```

То есть физический интерфейс становится «портом» бриджа, а IP хоста переезжает на `br0`.
Если на хосте netplan без NetworkManager (Ubuntu Server), то же самое в `/etc/netplan/01-br0.yaml`:

```yaml
network:
  version: 2
  ethernets:
    enp3s0: { dhcp4: false }
  bridges:
    br0:
      interfaces: [enp3s0]
      dhcp4: true
      parameters: { stp: false }
```

и `sudo netplan apply`. Важно: на время переключения сеть моргнёт, и Wi-Fi-интерфейс в бридж
вставить нельзя - нужен провод. Поэтому `check.sh` проверяет NAT-сеть, а бридж - руками по командам выше.
Убрать бридж: `sudo ./scripts/create_bridge.sh --delete`.

### Восстановление состояния (п. 6)

При каждом старте сервис делает «сверку» (`reconcile()`):

- поднимает свои сети `cm-*`, если они вдруг неактивны;
- для каждой ВМ из `vms.json`, у которой желаемое состояние `running`, а в libvirt она выключена, - запускает её;
- если в libvirt есть ВМ `cm-*`, про которую сервис не знает (например, потеряли `data/`), - добавляет её в состояние, а не теряет;
- если ВМ есть в состоянии, но её удалили из libvirt руками, - пишет предупреждение, а в API она видна со статусом `missing`.

Желаемое состояние меняется только через API: `start` → `running`, `stop` → `stopped`. Поэтому если ВМ
специально выключили через API, после перезапуска сервис её не поднимет, а если она упала сама - поднимет.
Плюс в compose стоит `restart: unless-stopped`, так что контейнер сам поднимется после перезагрузки хоста.

### Квоты (п. 9)

Лимиты задаются переменными окружения (по умолчанию 8 vCPU и 8192 MiB):

```bash
CM_QUOTA_VCPUS=4 CM_QUOTA_RAM_MB=4096 sudo -E docker compose up -d
```

Считается сумма по **всем** ВМ `cm-*`, включая выключенные (выключенная ВМ всё равно в любой момент
может запуститься). Цифры берутся из libvirt (`virsh dominfo`), а не из файла сервиса, поэтому даже ВМ,
созданные мимо API, учитываются. Создание ВМ идёт под блокировкой, так что два параллельных запроса
не проскочат квоту вместе. Текущее использование: `GET /api/quota` и полоски в шапке веб-интерфейса.

## Базовое задание: ВМ руками через virsh

```bash
sudo ./scripts/run_vm_virsh.sh
```

Что происходит по шагам (все команды видны в скрипте):

1. Скачивается облачный образ Ubuntu 24.04 minimal в `/var/lib/libvirt/images/cloudmaker/base/`.
2. `qemu-img create -f qcow2 -F qcow2 -b <образ> disk.qcow2 10G` - диск ВМ поверх образа.
3. Пишутся `user-data` (hostname и пароль) и `meta-data`, из них `genisoimage -V cidata` собирает ISO для cloud-init.
4. Пишется XML ВМ: 1 vCPU, 1 GiB RAM, диск virtio, ISO как CD-ROM, сетевая карта в сеть `default`, последовательная консоль.
5. `virsh define domain.xml` регистрирует ВМ в libvirt, `virsh start cm-basic` её запускает.

Проверить:

```bash
virsh list --all             # cm-basic   running
virsh console cm-basic       # логин ubuntu, пароль cloudmaker (выход - Ctrl+])
virsh domifaddr cm-basic     # IP, через ~30 секунд после старта
sudo ./scripts/run_vm_virsh.sh --delete
```

## Запуск сервиса

### В docker (основной вариант)

```bash
sudo docker compose up -d --build
sudo docker compose logs -f        # логи
```

Открыть `http://localhost:8080/`. В веб-интерфейсе можно добавить образ, создать сеть, создать ВМ
(с ключами и user_data), запускать/выключать/удалять её и видеть IP. Страница обновляется сама раз в 5 секунд.

### Без docker

Нужны `virsh`, `qemu-img`, `genisoimage` на хосте:

```bash
sudo python3 app/server.py
```

Настройки (переменные окружения): `CM_PORT` (8080), `CM_DATA_DIR` (`./data`),
`CM_STORAGE_DIR` (`/var/lib/libvirt/images/cloudmaker/vms`), `CM_QUOTA_VCPUS` (8), `CM_QUOTA_RAM_MB` (8192),
`LIBVIRT_DEFAULT_URI` (`qemu:///system`).

## API

Всё в JSON. Ошибки - `{"error": "текст"}` с кодом 400 (кривой запрос), 404 (нет такого), 409 (конфликт/квота), 500.

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/api/health` | жив ли сервис и libvirt |
| GET | `/api/vms` | список ВМ |
| POST | `/api/vms` | создать ВМ |
| GET | `/api/vms/{name}` | одна ВМ (состояние, IP, ресурсы) |
| POST | `/api/vms/{name}/start` | запустить |
| POST | `/api/vms/{name}/stop` | выключить через ACPI; `?force=1` - выдернуть питание |
| POST | `/api/vms/{name}/reboot` | перезагрузить |
| DELETE | `/api/vms/{name}` | удалить ВМ вместе с диском |
| GET / POST | `/api/images` | список / регистрация образа |
| DELETE | `/api/images/{name}` | убрать образ из реестра (файл не удаляется; нельзя, если образ используется) |
| GET / POST | `/api/networks` | список / создание сети |
| DELETE | `/api/networks/{name}` | удалить сеть (нельзя, если в ней есть ВМ) |
| GET | `/api/quota` | использование квот |

Пример полного цикла через curl:

```bash
# образ (файл должен лежать в /var/lib/libvirt/images/cloudmaker/, чтобы его видел и контейнер, и хост)
curl -X POST localhost:8080/api/images -d '{
  "name": "ubuntu-24",
  "path": "/var/lib/libvirt/images/cloudmaker/base/ubuntu-24.04-minimal-cloudimg-amd64.img",
  "description": "Ubuntu 24.04 minimal"}'

# сеть
curl -X POST localhost:8080/api/networks -d '{"name":"dev","mode":"nat","subnet":"10.50.0.0/24"}'

# ВМ
curl -X POST localhost:8080/api/vms -d '{
  "name": "web-1",
  "image": "ubuntu-24",
  "vcpus": 2, "ram_mb": 2048, "disk_gb": 15,
  "network": "dev",
  "hostname": "web-1",
  "ssh_keys": ["'"$(cat ~/.ssh/id_ed25519.pub)"'"],
  "user_data": "#cloud-config\npackages: [nginx]\n"}'

curl localhost:8080/api/vms/web-1          # смотрим ips
ssh ubuntu@<ip>

curl -X POST localhost:8080/api/vms/web-1/stop
curl -X POST localhost:8080/api/vms/web-1/start
curl -X DELETE localhost:8080/api/vms/web-1
```

Поля при создании ВМ: `name` (обязательно, `a-z0-9-`), `image` (обязательно), `vcpus` (1), `ram_mb` (1024),
`disk_gb` (10, но не меньше размера образа), `network` (`default`), `hostname` (= name), `ssh_keys` (список),
`password`, `user_data`, `start` (true).

## Структура

```
app/server.py           сервис: API, работа с libvirt, cloud-init, квоты, восстановление
app/static/index.html   веб-интерфейс (чистый HTML + JS)
Dockerfile, docker-compose.yml
scripts/run_vm_virsh.sh базовое задание - ВМ руками
scripts/create_bridge.sh бридж в хостовую сеть
scripts/check.sh        проверка всего
scripts/cleanup.sh      уборка
```

## Если что-то не работает

- `FAIL есть /dev/kvm` - не включена виртуализация в BIOS (VT-x / AMD-V) или это ВМ без вложенной виртуализации.
- `Network is already in use by interface ...` при создании сети - подсеть пересекается с уже существующей, возьмите другую.
- `permission denied` на сокет libvirt - запускать нужно через `sudo` (контейнер ходит в сокет от root).
- ВМ не получает IP в сети `default` - `sudo virsh net-start default`.
- Логи сервиса: `sudo docker compose logs -f`, консоль ВМ: `sudo virsh console cm-<имя>`.
