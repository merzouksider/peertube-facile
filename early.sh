#!/bin/sh
# Choisit le disque où installer Debian : le premier disque qui n'est PAS
# la clé USB d'installation. Exécuté par l'installateur Debian (busybox).

INSTALL_MEDIA="$(mount | grep ' /cdrom ' | cut -d' ' -f1 | head -n1)"
[ -z "$INSTALL_MEDIA" ] && INSTALL_MEDIA="$(mount | grep ' /hd-media ' | cut -d' ' -f1 | head -n1)"

TARGET=""
for d in $(list-devices disk); do
	case "$INSTALL_MEDIA" in
		"$d"*) continue ;;
	esac
	TARGET="$d"
	break
done

if [ -n "$TARGET" ]; then
	logger -t peertube-facile "Disque cible : $TARGET (clé : $INSTALL_MEDIA)"
	debconf-set partman-auto/disk "$TARGET"
	debconf-set grub-installer/bootdev "$TARGET"
else
	logger -t peertube-facile "Aucun disque interne trouvé"
fi
