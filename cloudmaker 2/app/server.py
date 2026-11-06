#!/usr/bin/env python3
"""CloudMaker — маленькое «облако» поверх libvirt + QEMU/KVM.

Сервис ничего не знает про QEMU напрямую: всё делается через `virsh`
(libvirt), `qemu-img` и генератор ISO для cloud-init. Источник правды
про ВМ — сам libvirt, а в data/*.json лежит только то, чего в libvirt нет
(реестр образов и «желаемое» состояние ВМ для восстановления).

Зависимости: только стандартная библиотека Python 3.
"""

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ.setdefault("LIBVIRT_DEFAULT_URI", "qemu:///system")
HOST = os.environ.get("CM_HOST", "0.0.0.0")
PORT = int(os.environ.get("CM_PORT", "8080"))
DATA_DIR = os.environ.get("CM_DATA_DIR", os.path.join(APP_DIR, "..", "data"))
STORAGE_DIR = os.environ.get("CM_STORAGE_DIR", "/var/lib/libvirt/images/cloudmaker/vms")
QUOTA_VCPUS = int(os.environ.get("CM_QUOTA_VCPUS", "8"))
QUOTA_RAM_MB = int(os.environ.get("CM_QUOTA_RAM_MB", "8192"))
PREFIX = "cm-"  # всё, что создаёт сервис в libvirt, начинается с cm-
STATIC_DIR = os.path.join(APP_DIR, "static")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")
BRIDGE_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,15}$")

LOCK = threading.RLock()
log = logging.getLogger("cloudmaker")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- helpers

def run(cmd, check=True, timeout=300):
    log.debug("run: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ApiError(500, f"не найдена программа {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise ApiError(500, f"команда зависла: {' '.join(cmd)}")
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        raise ApiError(500, f"{' '.join(cmd[:3])}: {msg}")
    return res


def virsh(*args, check=True):
    return run(["virsh", *args], check=check)


class JsonStore:
    """Маленький JSON-файл с атомарной записью (tmp + rename)."""

    def __init__(self, filename):
        self.path = os.path.join(DATA_DIR, filename)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


IMAGES = JsonStore("images.json")
VMS = JsonStore("vms.json")


def check_name(name, what="name"):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ApiError(400, f"{what}: только a-z, 0-9 и '-', до 31 символа")
    return name


def int_field(body, key, default, lo, hi):
    val = body.get(key, default)
    try:
        val = int(val)
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} должно быть числом")
    if not lo <= val <= hi:
        raise ApiError(400, f"{key} должно быть от {lo} до {hi}")
    return val


# ---------------------------------------------------------------- libvirt: ВМ

def dom(name):
    return PREFIX + name


def libvirt_domains():
    out = virsh("list", "--all", "--name").stdout
    return [l.strip()[len(PREFIX):] for l in out.splitlines() if l.strip().startswith(PREFIX)]


def dom_state(name):
    res = virsh("domstate", dom(name), check=False)
    return res.stdout.strip() if res.returncode == 0 else None


def dom_resources(name):
    """(vcpu, ram_mb) по данным libvirt."""
    out = virsh("dominfo", dom(name)).stdout
    cpu = ram = 0
    for line in out.splitlines():
        key, _, val = line.partition(":")
        if key.strip() == "CPU(s)":
            cpu = int(val.strip())
        elif key.strip() == "Max memory":
            ram = int(val.split()[0]) // 1024  # KiB -> MiB
    return cpu, ram


def dom_ips(name):
    res = virsh("domifaddr", dom(name), check=False)
    ips = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "ipv4":
            ips.append(parts[3].split("/")[0])
    return ips


def quota_usage():
    cpu = ram = 0
    for name in libvirt_domains():
        c, r = dom_resources(name)
        cpu += c
        ram += r
    return {"vcpus": {"used": cpu, "limit": QUOTA_VCPUS},
            "ram_mb": {"used": ram, "limit": QUOTA_RAM_MB}}


def vm_info(name, rec=None):
    if rec is None:
        rec = VMS.load().get(name, {})
    state = dom_state(name)
    info = {
        "name": name,
        "libvirt_name": dom(name),
        "state": state or "missing",
        "desired_state": rec.get("desired_state"),
        "image": rec.get("image"),
        "network": rec.get("network"),
        "hostname": rec.get("hostname"),
        "disk_gb": rec.get("disk_gb"),
        "created_at": rec.get("created_at"),
        "vcpus": rec.get("vcpus"),
        "ram_mb": rec.get("ram_mb"),
        "ips": [],
    }
    if state:
        info["vcpus"], info["ram_mb"] = dom_resources(name)
        if state == "running":
            info["ips"] = dom_ips(name)
    return info


def list_vms():
    recs = VMS.load()
    names = sorted(set(recs) | set(libvirt_domains()))
    return [vm_info(n, recs.get(n, {})) for n in names]


def resolve_network(net):
    """'default' -> default, 'test' -> cm-test (если это наша сеть)."""
    net = net or "default"
    for cand in (PREFIX + net, net):
        if virsh("net-info", cand, check=False).returncode == 0:
            return cand
    raise ApiError(400, f"сеть {net} не найдена")


def build_user_data(name, hostname, ssh_keys, password, user_data):
    cfg = {
        "hostname": hostname,
        "preserve_hostname": False,
        "ssh_authorized_keys": ssh_keys,
        "write_files": [{"path": "/etc/cloudmaker", "content": f"vm={name}\n"}],
    }
    if password:
        cfg["password"] = password
        cfg["chpasswd"] = {"expire": False}
        cfg["ssh_pwauth"] = True
    # JSON — это валидный YAML, поэтому PyYAML не нужен
    ours = "#cloud-config\n" + json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    if not user_data:
        return ours

    # Пользовательский user_data кладём вторым куском MIME multipart:
    # cloud-init выполнит оба (и cloud-config, и shell-скрипт).
    msg = MIMEMultipart()
    msg.attach(MIMEText(ours, "cloud-config", "utf-8"))
    if user_data.startswith("#!"):
        subtype = "x-shellscript"
    elif user_data.startswith("#cloud-config"):
        subtype = "cloud-config"
    else:
        raise ApiError(400, "user_data должен начинаться с '#cloud-config' или '#!'")
    part = MIMEText(user_data, subtype, "utf-8")
    part.add_header("Merge-Type", "list(append)+dict(recurse_array)+str()")
    msg.attach(part)
    return msg.as_string()


def make_iso(out, files):
    for tool in ("genisoimage", "mkisofs", "xorrisofs"):
        if shutil.which(tool):
            run([tool, "-o", out, "-V", "cidata", "-J", "-r", *files])
            return
    raise ApiError(500, "нужен genisoimage (или mkisofs/xorrisofs) для cloud-init ISO")


def build_seed(vm_dir, name, hostname, ssh_keys, password, user_data):
    cidata = os.path.join(vm_dir, "cidata")
    os.makedirs(cidata, exist_ok=True)
    ud = os.path.join(cidata, "user-data")
    md = os.path.join(cidata, "meta-data")
    with open(ud, "w", encoding="utf-8") as f:
        f.write(build_user_data(name, hostname, ssh_keys, password, user_data))
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"instance-id: {name}-{uuid.uuid4().hex[:8]}\nlocal-hostname: {hostname}\n")
    iso = os.path.join(vm_dir, "seed.iso")
    make_iso(iso, [ud, md])
    return iso


def domain_xml(name, vcpus, ram_mb, disk, seed, network, image):
    e = escape
    return f"""<domain type='kvm'>
  <name>{e(dom(name))}</name>
  <description>CloudMaker VM {e(name)} (image: {e(image)})</description>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{e(disk)}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{e(seed)}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='{e(network)}'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>
"""


def create_vm(body):
    name = check_name(body.get("name"))
    image_name = body.get("image")
    images = IMAGES.load()
    if image_name not in images:
        raise ApiError(400, f"образ {image_name} не зарегистрирован (см. /api/images)")
    image = images[image_name]
    vcpus = int_field(body, "vcpus", 1, 1, 64)
    ram_mb = int_field(body, "ram_mb", 1024, 128, 262144)
    disk_gb = int_field(body, "disk_gb", 10, 1, 2048)
    hostname = body.get("hostname") or name
    if not HOSTNAME_RE.match(hostname):
        raise ApiError(400, "hostname некорректный")
    ssh_keys = body.get("ssh_keys") or []
    if isinstance(ssh_keys, str):
        ssh_keys = [k for k in ssh_keys.splitlines() if k.strip()]
    if not all(isinstance(k, str) for k in ssh_keys):
        raise ApiError(400, "ssh_keys — список строк")
    password = body.get("password") or None
    user_data = body.get("user_data") or None
    start = bool(body.get("start", True))

    with LOCK:
        network = resolve_network(body.get("network"))
        if dom_state(name) is not None:
            raise ApiError(409, f"ВМ {name} уже существует")

        # --- квоты
        usage = quota_usage()
        if usage["vcpus"]["used"] + vcpus > QUOTA_VCPUS:
            raise ApiError(409, f"превышена квота vCPU: занято {usage['vcpus']['used']}, "
                                f"просите {vcpus}, лимит {QUOTA_VCPUS}")
        if usage["ram_mb"]["used"] + ram_mb > QUOTA_RAM_MB:
            raise ApiError(409, f"превышена квота RAM: занято {usage['ram_mb']['used']} MiB, "
                                f"просите {ram_mb}, лимит {QUOTA_RAM_MB}")

        vm_dir = os.path.join(STORAGE_DIR, name)
        try:
            os.makedirs(vm_dir, exist_ok=True)
            # диск ВМ — тонкий overlay поверх базового образа
            disk = os.path.join(vm_dir, "disk.qcow2")
            size = max(disk_gb * 1024 ** 3, int(image.get("virtual_size", 0)))
            run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", image["format"],
                 "-b", image["path"], disk, str(size)])
            seed = build_seed(vm_dir, name, hostname, ssh_keys, password, user_data)
            xml_path = os.path.join(vm_dir, "domain.xml")
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write(domain_xml(name, vcpus, ram_mb, disk, seed, network, image_name))
            virsh("define", xml_path)
            if start:
                virsh("start", dom(name))
        except Exception:
            virsh("destroy", dom(name), check=False)
            virsh("undefine", dom(name), check=False)
            shutil.rmtree(vm_dir, ignore_errors=True)
            raise

        recs = VMS.load()
        recs[name] = {
            "image": image_name, "network": network, "hostname": hostname,
            "vcpus": vcpus, "ram_mb": ram_mb, "disk_gb": disk_gb,
            "desired_state": "running" if start else "stopped",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        VMS.save(recs)
        log.info("создана ВМ %s (%s vCPU, %s MiB, образ %s, сеть %s)",
                 name, vcpus, ram_mb, image_name, network)
        return vm_info(name, recs[name])


def set_desired(name, desired):
    recs = VMS.load()
    if name in recs:
        recs[name]["desired_state"] = desired
        VMS.save(recs)


def vm_action(name, action, force=False):
    check_name(name)
    with LOCK:
        state = dom_state(name)
        if state is None:
            raise ApiError(404, f"ВМ {name} не найдена")
        if action == "start":
            if state != "running":
                virsh("start", dom(name))
            set_desired(name, "running")
        elif action == "stop":
            if state == "running":
                virsh("destroy" if force else "shutdown", dom(name))
            set_desired(name, "stopped")
        elif action == "reboot":
            if state != "running":
                raise ApiError(409, "ВМ не запущена")
            virsh("reboot", dom(name))
        else:
            raise ApiError(404, "неизвестное действие")
        log.info("ВМ %s: %s%s", name, action, " (force)" if force else "")
        return vm_info(name)


def delete_vm(name):
    check_name(name)
    with LOCK:
        recs = VMS.load()
        if dom_state(name) is None and name not in recs:
            raise ApiError(404, f"ВМ {name} не найдена")
        virsh("destroy", dom(name), check=False)
        virsh("undefine", dom(name), check=False)
        shutil.rmtree(os.path.join(STORAGE_DIR, name), ignore_errors=True)
        recs.pop(name, None)
        VMS.save(recs)
        log.info("ВМ %s удалена", name)


# ---------------------------------------------------------------- образы

def register_image(body):
    name = check_name(body.get("name"))
    path = body.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ApiError(400, "path должен быть абсолютным путём к файлу образа")
    if not os.path.isfile(path):
        raise ApiError(400, f"файл {path} не найден (в docker-режиме кладите образы в "
                            f"/var/lib/libvirt/images/cloudmaker/)")
    info = json.loads(run(["qemu-img", "info", "-U", "--output=json", path]).stdout)
    fmt = info.get("format")
    if fmt not in ("qcow2", "raw"):
        raise ApiError(400, f"формат {fmt} не поддерживается (нужен qcow2 или raw)")
    with LOCK:
        images = IMAGES.load()
        if name in images:
            raise ApiError(409, f"образ {name} уже есть")
        images[name] = {
            "path": path,
            "description": str(body.get("description", "")),
            "format": fmt,
            "virtual_size": info.get("virtual-size", 0),
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        IMAGES.save(images)
    log.info("зарегистрирован образ %s -> %s", name, path)
    return {"name": name, **images[name]}


def delete_image(name):
    with LOCK:
        images = IMAGES.load()
        if name not in images:
            raise ApiError(404, f"образ {name} не найден")
        users = [n for n, r in VMS.load().items() if r.get("image") == name]
        if users:
            raise ApiError(409, f"образ используется ВМ: {', '.join(users)}")
        del images[name]
        IMAGES.save(images)


def list_images():
    return [{"name": n, **r} for n, r in sorted(IMAGES.load().items())]


# ---------------------------------------------------------------- сети

def network_info(lv_name):
    xml = virsh("net-dumpxml", lv_name).stdout
    root = ET.fromstring(xml)
    fwd = root.find("forward")
    mode = fwd.get("mode", "nat") if fwd is not None else "isolated"
    br = root.find("bridge")
    ip = root.find("ip")
    subnet = None
    if ip is not None and ip.get("address"):
        prefix = ip.get("prefix") or ip.get("netmask")
        subnet = str(ipaddress.ip_interface(f"{ip.get('address')}/{prefix}").network)
    active = "yes" in next((l for l in virsh("net-info", lv_name).stdout.splitlines()
                            if l.startswith("Active")), "")
    managed = lv_name.startswith(PREFIX)
    return {
        "name": lv_name[len(PREFIX):] if managed else lv_name,
        "libvirt_name": lv_name, "managed": managed, "mode": mode,
        "subnet": subnet, "bridge": br.get("name") if br is not None else None,
        "active": active,
    }


def list_networks():
    out = virsh("net-list", "--all", "--name").stdout
    return [network_info(n.strip()) for n in out.splitlines() if n.strip()]


def create_network(body):
    name = check_name(body.get("name"))
    mode = body.get("mode", "nat")
    lv = PREFIX + name
    if mode in ("nat", "isolated"):
        try:
            net = ipaddress.ip_network(body.get("subnet", ""), strict=True)
        except ValueError:
            raise ApiError(400, "subnet должен быть вида 10.10.0.0/24")
        if net.version != 4 or net.prefixlen > 29:
            raise ApiError(400, "нужна IPv4-подсеть не меньше /29")
        hosts = list(net.hosts())
        gw, first, last = hosts[0], hosts[1], hosts[-1]
        forward = "<forward mode='nat'/>" if mode == "nat" else ""
        xml = f"""<network>
  <name>{lv}</name>
  {forward}
  <ip address='{gw}' netmask='{net.netmask}'>
    <dhcp><range start='{first}' end='{last}'/></dhcp>
  </ip>
</network>
"""
    elif mode == "bridge":
        bridge = body.get("bridge", "")
        if not BRIDGE_RE.match(bridge):
            raise ApiError(400, "bridge — имя хостового бриджа, например br0")
        if not os.path.isdir(f"/sys/class/net/{bridge}/bridge"):
            raise ApiError(400, f"на хосте нет бриджа {bridge} (см. scripts/create_bridge.sh)")
        xml = f"""<network>
  <name>{lv}</name>
  <forward mode='bridge'/>
  <bridge name='{bridge}'/>
</network>
"""
    else:
        raise ApiError(400, "mode: nat | isolated | bridge")

    with LOCK:
        if virsh("net-info", lv, check=False).returncode == 0:
            raise ApiError(409, f"сеть {name} уже существует")
        os.makedirs(DATA_DIR, exist_ok=True)
        xml_path = os.path.join(DATA_DIR, f"net-{name}.xml")
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml)
        virsh("net-define", xml_path)
        try:
            virsh("net-start", lv)
            virsh("net-autostart", lv)
        except ApiError:
            virsh("net-undefine", lv, check=False)
            raise
        finally:
            os.remove(xml_path)
    log.info("создана сеть %s (%s)", lv, mode)
    return network_info(lv)


def delete_network(name):
    check_name(name)
    lv = PREFIX + name
    with LOCK:
        if virsh("net-info", lv, check=False).returncode != 0:
            raise ApiError(404, f"сеть {name} не найдена")
        users = [n for n, r in VMS.load().items() if r.get("network") == lv]
        if users:
            raise ApiError(409, f"сеть используется ВМ: {', '.join(users)}")
        virsh("net-destroy", lv, check=False)
        virsh("net-undefine", lv)


# ---------------------------------------------------------------- восстановление

def reconcile():
    """Приводим реальность к сохранённому желаемому состоянию.

    Вызывается при старте сервиса: если контейнер/сервис перезапустился,
    а ВМ, которая должна работать, лежит — поднимаем её. ВМ с префиксом
    cm-, о которых сервис не знает, «усыновляем».
    """
    with LOCK:
        for lv in virsh("net-list", "--all", "--name").stdout.split():
            if lv.startswith(PREFIX) and not network_info(lv)["active"]:
                log.info("восстановление: запускаю сеть %s", lv)
                virsh("net-start", lv, check=False)

        recs = VMS.load()
        for name in libvirt_domains():
            if name not in recs:
                state = dom_state(name)
                cpu, ram = dom_resources(name)
                recs[name] = {"desired_state": "running" if state == "running" else "stopped",
                              "vcpus": cpu, "ram_mb": ram, "adopted": True}
                log.info("восстановление: найдена ВМ %s, добавляю в состояние", name)
        for name, rec in recs.items():
            state = dom_state(name)
            if state is None:
                log.warning("восстановление: ВМ %s есть в состоянии, но нет в libvirt", name)
            elif rec.get("desired_state") == "running" and state != "running":
                log.info("восстановление: ВМ %s должна работать (%s) — запускаю", name, state)
                res = virsh("start", dom(name), check=False)
                if res.returncode != 0:
                    log.error("не удалось запустить %s: %s", name, res.stderr.strip())
        VMS.save(recs)


# ---------------------------------------------------------------- HTTP

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


@route("GET", "/api/health")
def _health(h):
    virsh("version")
    return 200, {"status": "ok", "libvirt_uri": os.environ["LIBVIRT_DEFAULT_URI"]}


@route("GET", "/api/vms")
def _vms(h):
    return 200, list_vms()


@route("POST", "/api/vms")
def _vm_create(h):
    return 201, create_vm(h.json_body())


@route("GET", "/api/vms/(?P<name>[^/]+)")
def _vm_get(h, name):
    check_name(name)
    if dom_state(name) is None and name not in VMS.load():
        raise ApiError(404, f"ВМ {name} не найдена")
    return 200, vm_info(name)


@route("POST", "/api/vms/(?P<name>[^/]+)/(?P<action>start|stop|reboot)")
def _vm_action(h, name, action):
    return 200, vm_action(name, action, force=h.query_flag("force"))


@route("DELETE", "/api/vms/(?P<name>[^/]+)")
def _vm_delete(h, name):
    delete_vm(name)
    return 200, {"deleted": name}


@route("GET", "/api/images")
def _images(h):
    return 200, list_images()


@route("POST", "/api/images")
def _image_add(h):
    return 201, register_image(h.json_body())


@route("DELETE", "/api/images/(?P<name>[^/]+)")
def _image_delete(h, name):
    delete_image(name)
    return 200, {"deleted": name}


@route("GET", "/api/networks")
def _networks(h):
    return 200, list_networks()


@route("POST", "/api/networks")
def _network_add(h):
    return 201, create_network(h.json_body())


@route("DELETE", "/api/networks/(?P<name>[^/]+)")
def _network_delete(h, name):
    delete_network(name)
    return 200, {"deleted": name}


@route("GET", "/api/quota")
def _quota(h):
    return 200, quota_usage()


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudMaker/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "тело запроса должно быть JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "тело запроса должно быть JSON-объектом")
        return body

    def query_flag(self, key):
        q = urlparse(self.path).query
        return any(p in (key, f"{key}=1", f"{key}=true") for p in q.split("&"))

    def send(self, status, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, method):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if method == "GET" and path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if match and m == method:
                try:
                    status, payload = fn(self, **match.groupdict())
                except ApiError as e:
                    status, payload = e.status, {"error": e.message}
                except Exception as e:  # noqa: BLE001
                    log.exception("ошибка обработки запроса")
                    status, payload = 500, {"error": str(e)}
                return self.send(status, payload)
        self.send(404, {"error": "нет такого пути"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")


def main():
    logging.basicConfig(level=os.environ.get("CM_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    log.info("libvirt: %s, данные: %s, диски ВМ: %s, квоты: %s vCPU / %s MiB",
             os.environ["LIBVIRT_DEFAULT_URI"], DATA_DIR, STORAGE_DIR, QUOTA_VCPUS, QUOTA_RAM_MB)
    try:
        reconcile()
    except ApiError as e:
        log.error("восстановление состояния не удалось: %s", e.message)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("слушаю http://%s:%s", HOST, PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
