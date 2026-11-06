#!/usr/bin/env bash
# Проверка всех реализованных пунктов одним запуском:
#   sudo ./scripts/check.sh
# Поднимает сервис в docker, гоняет сценарий через HTTP API и сверяет
# результат с тем, что видно на хосте через virsh. В конце — итог PASS/FAIL.
set -uo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || exec sudo -E bash "$0" "$@"

API=${CM_URL:-http://localhost:8080}
export LIBVIRT_DEFAULT_URI=qemu:///system
ROOT=/var/lib/libvirt/images/cloudmaker
IMAGE_URL=${CM_IMAGE_URL:-https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img}
IMAGE=$ROOT/base/$(basename "$IMAGE_URL")
VM=check-vm
NET=check-net
SUBNET=${CM_CHECK_SUBNET:-10.77.0.0/24}
IMG=check-ubuntu
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

pass=0; fail=0
ok()   { echo -e "  \e[32mPASS\e[0m $*"; pass=$((pass + 1)); }
bad()  { echo -e "  \e[31mFAIL\e[0m $*"; fail=$((fail + 1)); }
step() { echo; echo -e "\e[1m== $*\e[0m"; }
check() { local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }

# api METHOD PATH [JSON]  ->  $CODE, $BODY
api() {
    CODE=$(curl -s -o "$TMP/body" -w '%{http_code}' -X "$1" \
        -H 'Content-Type: application/json' ${3:+--data "$3"} "$API$2")
    BODY=$(cat "$TMP/body")
}
# jf 'python-выражение от d'  — достать поле из $BODY
jf() { python3 -c 'import sys,json; d=json.loads(sys.argv[2]); print(eval(sys.argv[1]))' "$1" "$BODY" 2>/dev/null; }

wait_api() {
    for _ in $(seq 60); do
        curl -sf "$API/api/health" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

# ------------------------------------------------------------------
step "0. Окружение"
check "есть /dev/kvm" test -e /dev/kvm
for bin in virsh docker curl ssh ssh-keygen python3; do
    check "есть $bin" command -v "$bin"
done
if ! command -v virsh >/dev/null || ! command -v docker >/dev/null; then
    echo "Без virsh и docker дальше проверять нечего — см. README, раздел «Подготовка хоста»."
    exit 1
fi

step "0. Базовый образ"
mkdir -p "$ROOT/base"
if [[ ! -f $IMAGE ]]; then
    echo "  качаю $IMAGE_URL"
    curl -fL --progress-bar -o "$IMAGE.part" "$IMAGE_URL" && mv "$IMAGE.part" "$IMAGE"
fi
check "образ скачан: $IMAGE" test -s "$IMAGE"

# ------------------------------------------------------------------
step "п.5  Приложение в контейнере, ВМ на хосте"
$DC up -d --build
if wait_api; then ok "API отвечает на $API"; else bad "API не поднялся"; $DC logs --tail 50; exit 1; fi
check "контейнер cloudmaker запущен" sh -c "docker ps --format '{{.Names}}' | grep -qx cloudmaker"

step "п.10 Веб-интерфейс"
check "по $API/ отдаётся страница" sh -c "curl -sf '$API/' | grep -q 'CloudMaker'"

step "п.7  Регистрация образа"
api POST /api/images "{\"name\":\"$IMG\",\"path\":\"$IMAGE\",\"description\":\"Ubuntu 24.04 minimal (check.sh)\"}"
[[ $CODE == 201 ]] && ok "образ зарегистрирован" || bad "регистрация образа: $CODE $BODY"
api GET /api/images
[[ $(jf "[i['name'] for i in d].count('$IMG')") == 1 ]] && ok "образ есть в списке" || bad "образа нет в списке"
api POST /api/images '{"name":"bad","path":"/nope.img"}'
[[ $CODE == 400 ]] && ok "несуществующий путь отклоняется (400)" || bad "ожидали 400, получили $CODE"

step "п.3  Виртуальная сеть"
api POST /api/networks "{\"name\":\"$NET\",\"mode\":\"nat\",\"subnet\":\"$SUBNET\"}"
[[ $CODE == 201 ]] && ok "сеть создана через API" || bad "создание сети: $CODE $BODY"
check "virsh видит активную сеть cm-$NET" sh -c "virsh net-info cm-$NET | grep -q 'Active:.*yes'"

step "п.9  Квоты"
api GET /api/quota
LIMIT=$(jf "d['vcpus']['limit']")
USED=$(jf "d['vcpus']['used']")
echo "  квота vCPU: занято $USED из $LIMIT"
api POST /api/vms "{\"name\":\"too-big\",\"image\":\"$IMG\",\"vcpus\":$((LIMIT + 1)),\"ram_mb\":512,\"network\":\"$NET\"}"
[[ $CODE == 409 ]] && ok "ВМ сверх квоты не создаётся (409: $(jf "d['error']"))" || bad "ожидали 409, получили $CODE"
check "ВМ too-big не появилась в libvirt" sh -c "! virsh dominfo cm-too-big"

step "п.1 + п.2  Создание ВМ через API с cloud-init"
ssh-keygen -q -t ed25519 -N "" -f "$TMP/key"
BODY_JSON=$(python3 - "$IMG" "$NET" "$TMP/key.pub" <<'EOF'
import json, sys
img, net, pub = sys.argv[1:]
print(json.dumps({
    "name": "check-vm", "image": img, "vcpus": 1, "ram_mb": 1024, "disk_gb": 8,
    "network": net, "hostname": "check-host",
    "ssh_keys": [open(pub).read().strip()],
    "user_data": "#!/bin/bash\necho cloudmaker-user-data-ok > /etc/cm-check\n",
}))
EOF
)
api POST /api/vms "$BODY_JSON"
[[ $CODE == 201 ]] && ok "ВМ создана (201)" || bad "создание ВМ: $CODE $BODY"
check "virsh list видит cm-$VM (ВМ на хосте)" sh -c "virsh list --name | grep -qx cm-$VM"

echo "  жду IP (до 3 минут)…"
IP=""
for _ in $(seq 90); do
    api GET /api/vms/$VM
    IP=$(jf "d['ips'][0] if d['ips'] else ''")
    [[ -n $IP ]] && break
    sleep 2
done
[[ -n $IP ]] && ok "ВМ получила IP $IP" || bad "ВМ не получила IP"

if [[ -n $IP ]]; then
    echo "  жду, пока отработает cloud-init (до 4 минут)…"
    OUT=""
    for _ in $(seq 48); do
        OUT=$(ssh -i "$TMP/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
              -o ConnectTimeout=5 -o BatchMode=yes -o LogLevel=ERROR "ubuntu@$IP" \
              'hostname; cat /etc/cloudmaker /etc/cm-check 2>/dev/null' 2>/dev/null || true)
        grep -q cloudmaker-user-data-ok <<<"$OUT" && break
        sleep 5
    done
    [[ -n $OUT ]] && ok "SSH по ключу из API работает" || bad "не удалось зайти по SSH"
    grep -qx check-host <<<"$OUT" && ok "hostname выставлен cloud-init'ом (check-host)" || bad "hostname не тот: $OUT"
    grep -q cloudmaker-user-data-ok <<<"$OUT" && ok "user_data-скрипт выполнился" || bad "user_data не выполнился"
fi

step "п.1  Остановка и запуск через API"
api POST "/api/vms/$VM/stop?force=1"
[[ $CODE == 200 ]] && ok "stop → 200" || bad "stop: $CODE $BODY"
check "virsh: ВМ выключена" sh -c "virsh domstate cm-$VM | grep -q 'shut off'"
api POST /api/vms/$VM/start
[[ $CODE == 200 ]] && ok "start → 200" || bad "start: $CODE $BODY"
check "virsh: ВМ работает" sh -c "virsh domstate cm-$VM | grep -q running"

step "п.6  Восстановление состояния после перезапуска контейнера"
$DC stop
virsh destroy "cm-$VM" >/dev/null   # «ВМ упала, пока сервис лежал»
check "ВМ сейчас выключена" sh -c "virsh domstate cm-$VM | grep -q 'shut off'"
$DC start
wait_api && ok "сервис снова поднялся" || bad "сервис не поднялся"
sleep 3
check "сервис сам поднял ВМ (желаемое состояние = running)" sh -c "virsh domstate cm-$VM | grep -q running"
api GET /api/vms/$VM
[[ $(jf "d['image']") == "$IMG" ]] && ok "данные о ВМ сохранились (образ $IMG)" || bad "данные о ВМ потерялись: $BODY"

step "п.1  Удаление"
api DELETE /api/vms/$VM
[[ $CODE == 200 ]] && ok "delete → 200" || bad "delete: $CODE $BODY"
check "ВМ пропала из libvirt" sh -c "! virsh dominfo cm-$VM"
check "диск ВМ удалён" test ! -e "$ROOT/vms/$VM"
api DELETE /api/networks/$NET
[[ $CODE == 200 ]] && ok "сеть удалена" || bad "удаление сети: $CODE $BODY"
check "сеть пропала из libvirt" sh -c "! virsh net-info cm-$NET"
api DELETE /api/images/$IMG
[[ $CODE == 200 ]] && ok "образ убран из реестра" || bad "удаление образа: $CODE $BODY"

# ------------------------------------------------------------------
echo
echo -e "\e[1mИтого: PASS $pass, FAIL $fail\e[0m"
echo "Сервис оставлен работать: $API (остановить и подчистить всё — sudo ./scripts/cleanup.sh)"
[[ $fail -eq 0 ]]
