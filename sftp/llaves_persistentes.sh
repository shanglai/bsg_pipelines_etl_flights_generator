#!/bin/bash
# Guarda las llaves de host la primera vez y las reutiliza después (volumen /llaves).
set -e
mkdir -p /llaves
if [ -f /llaves/ssh_host_ed25519_key ]; then
  cp /llaves/ssh_host_* /etc/ssh/
else
  cp /etc/ssh/ssh_host_* /llaves/
fi
chmod 600 /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_rsa_key
