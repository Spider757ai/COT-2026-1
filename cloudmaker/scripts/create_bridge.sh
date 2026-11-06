#!/usr/bin/env bash
# Создаёт на хосте бридж br0 и вставляет в него физический интерфейс,
# чтобы ВМ попадали прямо в хостовую (физическую) сеть и брали IP от её DHCP.
#
#   sudo ./scripts/create_bridge.sh enp3s0        # br0 поверх enp3s0
#   sudo ./scripts/create_bridge.sh enp3s0 br1    # своё имя бриджа
#   sudo ./scripts/create_bridge.sh --delete [br0]
#
# ВНИМАНИЕ: на время переключения сеть хоста моргнёт. Если вы сидите на машине
# по SSH через этот же интерфейс — можно потерять доступ. Wi-Fi в бридж
# вставить нельзя (ограничение 802.11), нужен проводной интерфейс.
# Используется NetworkManager (nmcli) — он есть в Ubuntu Desktop, Fedora и т.д.
set -euo pipefail

if [[ ${1:-} == "--delete" ]]; then
    BR=${2:-br0}
    nmcli con delete "$BR-port" 2>/dev/null || true
    nmcli con delete "$BR" 2>/dev/null || true
    echo "бридж $BR удалён"
    exit 0
fi

IFACE=${1:?укажите физический интерфейс, например enp3s0 (см. ip link)}
BR=${2:-br0}

ip link show "$IFACE" >/dev/null
nmcli con add type bridge ifname "$BR" con-name "$BR" stp no ipv4.method auto ipv6.method auto
nmcli con add type bridge-slave ifname "$IFACE" master "$BR" con-name "$BR-port"
# старое подключение интерфейса гасим, чтобы адрес получил уже бридж
OLD=$(nmcli -g GENERAL.CONNECTION device show "$IFACE" || true)
if [[ -n $OLD && $OLD != "$BR-port" ]]; then
    nmcli con down "$OLD" || true
fi
nmcli con up "$BR-port"
nmcli con up "$BR"

sleep 3
ip -br addr show "$BR"
echo
echo "Бридж $BR готов. Теперь можно создать сеть в CloudMaker:"
echo "  curl -X POST localhost:8080/api/networks -d '{\"name\":\"lan\",\"mode\":\"bridge\",\"bridge\":\"$BR\"}'"
