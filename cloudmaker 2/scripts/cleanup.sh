#!/usr/bin/env bash
# Убирает всё, что CloudMaker и проверки оставили на хосте:
# ВМ cm-*, сети cm-*, диски, состояние сервиса и контейнер.
#   sudo ./scripts/cleanup.sh         # всё, кроме скачанных базовых образов
#   sudo ./scripts/cleanup.sh --all   # плюс базовые образы и docker-образ
set -uo pipefail
cd "$(dirname "$0")/.."
[[ $EUID -eq 0 ]] || exec sudo -E bash "$0" "$@"
export LIBVIRT_DEFAULT_URI=qemu:///system
ROOT=/var/lib/libvirt/images/cloudmaker

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

echo ">> останавливаю сервис"
$DC down 2>/dev/null || true

for d in $(virsh list --all --name | grep '^cm-'); do
    echo ">> удаляю ВМ $d"
    virsh destroy "$d" >/dev/null 2>&1 || true
    virsh undefine "$d" >/dev/null 2>&1 || true
done

for n in $(virsh net-list --all --name | grep '^cm-'); do
    echo ">> удаляю сеть $n"
    virsh net-destroy "$n" >/dev/null 2>&1 || true
    virsh net-undefine "$n" >/dev/null 2>&1 || true
done

echo ">> удаляю диски ВМ и состояние сервиса"
rm -rf "$ROOT/vms" "$ROOT/manual" ./data

if [[ ${1:-} == "--all" ]]; then
    echo ">> удаляю базовые образы и docker-образ"
    rm -rf "$ROOT"
    docker rmi cloudmaker:latest >/dev/null 2>&1 || true
fi

echo "Готово. Осталось в libvirt с префиксом cm-:"
virsh list --all --name | grep '^cm-' || echo "  ВМ: нет"
virsh net-list --all --name | grep '^cm-' || echo "  сетей: нет"
