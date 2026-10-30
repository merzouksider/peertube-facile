#!/usr/bin/env bash
# Affiche sur l'écran du PC l'adresse de l'assistant et le code d'accès (/etc/issue).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"

# Attendre une adresse IP (au plus 60 s) pour l'afficher en clair.
IP=""
for _ in $(seq 1 30); do
	IP="$(ip -4 route get 9.9.9.9 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
	[[ -n "$IP" ]] && break
	sleep 2
done

CODE="$(python3 "$DIR/assistant.py" --code 2>/dev/null || echo '??????')"
DOMAINE="$(python3 -c 'import json; s=json.load(open("/etc/peertube-facile/etat.json")); print(s.get("domaine","") if s.get("installe") else "")' 2>/dev/null || true)"

{
	echo
	echo "  ================================================================"
	echo "     PeerTube Facile"
	echo "  ================================================================"
	echo
	if [[ -n "$DOMAINE" ]]; then
		echo "     Ton instance PeerTube :  https://$DOMAINE"
		echo
		echo "     Tableau de bord (depuis ton réseau) :"
	else
		echo "     Depuis un autre ordinateur ou un téléphone connecté"
		echo "     à la même box, ouvre cette adresse dans le navigateur :"
	fi
	echo
	echo "        http://${IP:-\\4}:8080"
	echo "        (ou http://peertube.local:8080)"
	echo
	echo "     Code d'accès :  $CODE"
	echo
	echo "  ================================================================"
	echo "  Rien à taper ici : tout se fait depuis le navigateur."
	echo
} >/etc/issue

agetty --reload 2>/dev/null || true
