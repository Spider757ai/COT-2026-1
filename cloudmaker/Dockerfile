FROM debian:bookworm-slim

# virsh (клиент libvirt), qemu-img (диски), genisoimage (ISO для cloud-init)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 libvirt-clients qemu-utils genisoimage ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cloudmaker
COPY app/ ./app/

ENV CM_DATA_DIR=/data \
    CM_PORT=8080 \
    LIBVIRT_DEFAULT_URI=qemu:///system \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python3", "app/server.py"]
