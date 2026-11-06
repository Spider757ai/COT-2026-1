#!/usr/bin/env bash
# Базовое задание: запуск ВМ руками через qemu-img + cloud-init ISO + virsh,
# без всякого GUI и без нашего сервиса.
#
#   sudo ./scripts/run_vm_virsh.sh            # создать и запустить ВМ cm-basic
#   sudo ./scripts/run_vm_virsh.sh --delete   # удалить её
set -euo pipefail

NAME=cm-basic
ROOT=/var/lib/libvirt/images/cloudmaker
BASE_DIR=$ROOT/base
VM_DIR=$ROOT/manual/$NAME
IMAGE_URL=${CM_IMAGE_URL:-https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img}
IMAGE=$BASE_DIR/$(basename "$IMAGE_URL")
export LIBVIRT_DEFAULT_URI=qemu:///system

if [[ ${1:-} == "--delete" ]]; then
    virsh destroy "$NAME" 2>/dev/null || true
    virsh undefine "$NAME" 2>/dev/null || true
    rm -rf "$VM_DIR"
    echo "ВМ $NAME удалена"
    exit 0
fi

[[ $EUID -eq 0 ]] || { echo "запустите через sudo"; exit 1; }
[[ -e /dev/kvm ]] || { echo "нет /dev/kvm — включите виртуализацию в BIOS"; exit 1; }

# 1. Базовый облачный образ (скачивается один раз)
mkdir -p "$BASE_DIR" "$VM_DIR"
if [[ ! -f $IMAGE ]]; then
    echo ">> качаю образ $IMAGE_URL"
    curl -fL --progress-bar -o "$IMAGE.part" "$IMAGE_URL" && mv "$IMAGE.part" "$IMAGE"
fi

# 2. Диск ВМ: тонкий qcow2-оверлей поверх базового образа (базовый не меняется)
qemu-img create -q -f qcow2 -F qcow2 -b "$IMAGE" "$VM_DIR/disk.qcow2" 10G

# 3. cloud-init NoCloud: два файла на ISO с меткой "cidata"
cat > "$VM_DIR/user-data" <<'EOF'
#cloud-config
hostname: cm-basic
password: cloudmaker
chpasswd: { expire: false }
ssh_pwauth: true
EOF
printf 'instance-id: cm-basic-1\nlocal-hostname: cm-basic\n' > "$VM_DIR/meta-data"
genisoimage -quiet -o "$VM_DIR/seed.iso" -V cidata -J -r "$VM_DIR/user-data" "$VM_DIR/meta-data"

# 4. Описание ВМ для libvirt
cat > "$VM_DIR/domain.xml" <<EOF
<domain type='kvm'>
  <name>$NAME</name>
  <memory unit='MiB'>1024</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$VM_DIR/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$VM_DIR/seed.iso'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'><source network='default'/><model type='virtio'/></interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
EOF

# 5. Сеть default должна быть запущена
virsh net-start default 2>/dev/null || true

# 6. Регистрируем и запускаем
virsh define "$VM_DIR/domain.xml"
virsh start "$NAME"

echo
virsh list --all | grep -E "Name|$NAME"
cat <<EOF

Готово. Дальше:
  virsh console $NAME        # консоль (логин ubuntu / пароль cloudmaker, выход Ctrl+])
  virsh domifaddr $NAME      # IP (появится через ~30 сек после загрузки)
  ssh ubuntu@<IP>            # пароль cloudmaker
  sudo $0 --delete           # удалить ВМ
EOF
