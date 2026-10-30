#!/bin/sh
# Fin d'installation : copie l'assistant PeerTube Facile dans le système installé.
# Exécuté par l'installateur Debian (busybox), le système installé est dans /target.
set -e
T=/target
SRC=/peertube-facile/systeme

mkdir -p "$T/opt/peertube-facile" "$T/etc/peertube-facile"
cp -r "$SRC/." "$T/opt/peertube-facile/"
chmod 755 "$T/opt/peertube-facile/"*.sh "$T/opt/peertube-facile/assistant.py"
chmod 700 "$T/etc/peertube-facile"
cp "$T/opt/peertube-facile/services/"*.service "$T/etc/systemd/system/"

# Mot de passe aléatoire pour le compte Linux de secours « admin »
PW="$(tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 16)"
echo "admin:$PW" | chroot "$T" chpasswd
echo "$PW" >"$T/etc/peertube-facile/mot-de-passe-linux"
chmod 600 "$T/etc/peertube-facile/mot-de-passe-linux"

# Nom « peertube.local » sur le réseau, et services de l'assistant
chroot "$T" systemctl enable avahi-daemon.service peertube-facile.service peertube-facile-banner.service
