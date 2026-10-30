#!/usr/bin/env bash
# PeerTube Facile - fabrique l'image de la clé USB à partir de l'image officielle Debian.
#
#   bash construire/construire-iso.sh [dossier_de_sortie]
#
# Fonctionne sur n'importe quel Linux Debian/Ubuntu (ou dans un conteneur debian),
# sans droits root si xorriso, cpio, curl et gzip sont déjà installés.
set -Eeuo pipefail
trap 'echo "ERREUR à la ligne $LINENO : $BASH_COMMAND" >&2' ERR

PROJET="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SORTIE="${1:-$PROJET/sortie}"
MIROIR="${PTF_MIROIR:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd}"
mkdir -p "$SORTIE/cache"

echo "==> PeerTube Facile : fabrication de l'image de la clé USB"

# 1. Outils nécessaires
manque=()
for outil in xorriso cpio gzip curl sha256sum; do
	command -v "$outil" >/dev/null 2>&1 || manque+=("$outil")
done
if ((${#manque[@]})); then
	if [[ $EUID -eq 0 ]] && command -v apt-get >/dev/null; then
		echo "==> Installation des outils : ${manque[*]}"
		apt-get update -qq
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xorriso cpio gzip curl coreutils ca-certificates >/dev/null
	else
		echo "Outils manquants : ${manque[*]}. Installe-les (sudo apt install xorriso cpio curl) puis relance." >&2
		exit 1
	fi
fi

# 2. Image Debian officielle (netinst, dernière version stable), vérifiée
echo "==> Recherche de la dernière version de Debian"
SUMS="$(curl -fsSL "$MIROIR/SHA256SUMS")"
LIGNE="$(grep -E ' debian-[0-9.]+-amd64-netinst\.iso$' <<<"$SUMS" | head -n1)"
[[ -n "$LIGNE" ]] || { echo "Image netinst introuvable sur $MIROIR" >&2; exit 1; }
SOMME="${LIGNE%% *}"
NOM="${LIGNE##* }"
VERSION="$(sed -E 's/debian-([0-9.]+)-amd64-netinst\.iso/\1/' <<<"$NOM")"
ISO="$SORTIE/cache/$NOM"

if [[ -f "$ISO" ]] && echo "$SOMME  $ISO" | sha256sum -c --quiet - 2>/dev/null; then
	echo "==> $NOM déjà téléchargée"
else
	echo "==> Téléchargement de $NOM"
	curl -fL --progress-bar -o "$ISO.part" "$MIROIR/$NOM"
	mv "$ISO.part" "$ISO"
	echo "$SOMME  $ISO" | sha256sum -c --quiet - || { echo "Somme de contrôle incorrecte, fichier supprimé." >&2; rm -f "$ISO"; exit 1; }
fi

# 3. Ajout du fichier de réponses et de l'assistant dans l'installateur
TRAVAIL="$(mktemp -d)"
trap 'rm -rf "$TRAVAIL"' EXIT
echo "==> Préparation de l'installateur automatique"
xorriso -osirrox on -indev "$ISO" -extract /install.amd/initrd.gz "$TRAVAIL/initrd.gz" >/dev/null 2>&1
chmod u+w "$TRAVAIL/initrd.gz"

AJOUT="$TRAVAIL/ajout"
mkdir -p "$AJOUT/peertube-facile"
cp "$PROJET/cle-usb/preseed.cfg" "$AJOUT/preseed.cfg"
cp "$PROJET/cle-usb/early.sh" "$PROJET/cle-usb/late.sh" "$AJOUT/peertube-facile/"
cp -r "$PROJET/systeme" "$AJOUT/peertube-facile/systeme"
find "$AJOUT" -name '__pycache__' -prune -exec rm -rf {} +
(cd "$AJOUT" && find . | LC_ALL=C sort | cpio -o -H newc -R 0:0 --quiet) | gzip -9 >"$TRAVAIL/ajout.cpio.gz"
# Le noyau Linux accepte plusieurs archives à la suite : on ajoute la nôtre après celle de Debian.
cat "$TRAVAIL/initrd.gz" "$TRAVAIL/ajout.cpio.gz" >"$TRAVAIL/initrd-peertube.gz"

# 4. Nouvelle image, avec les menus de démarrage PeerTube Facile
FINALE="$SORTIE/peertube-facile-debian-$VERSION.iso"
rm -f "$FINALE"
echo "==> Fabrication de $(basename "$FINALE")"
xorriso -indev "$ISO" -outdev "$FINALE" \
	-map "$TRAVAIL/initrd-peertube.gz" /install.amd/initrd.gz \
	-map "$PROJET/cle-usb/isolinux.cfg" /isolinux/isolinux.cfg \
	-map "$PROJET/cle-usb/message.txt" /isolinux/message.txt \
	-map "$PROJET/cle-usb/grub.cfg" /boot/grub/grub.cfg \
	-boot_image any replay >/dev/null 2>&1

sha256sum "$FINALE" | sed "s#  .*/#  #" >"$FINALE.sha256"
echo
echo "Terminé : $FINALE"
echo "Copie-la sur une clé USB avec balenaEtcher (ou Rufus en mode « DD »)."
