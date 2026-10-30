#!/usr/bin/env bash
# PeerTube Facile - installation et maintenance de PeerTube (Docker + Caddy).
#
#   install-peertube.sh                  installe (variables PTF_DOMAINE, PTF_EMAIL, PTF_ROOT_PASSWORD)
#   install-peertube.sh --mettre-a-jour  télécharge les nouvelles versions et redémarre
#   install-peertube.sh --redemarrer     redémarre tous les conteneurs
#   install-peertube.sh --certificat     vérifie / attend le certificat HTTPS
#
# Les lignes « ##ETAPE n/total libellé » sont lues par l'assistant pour la barre de progression.
# Codes de sortie : 0 = OK, 3 = PeerTube fonctionne mais le certificat HTTPS n'est pas encore obtenu.

set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
PT_DIR="${PTF_PEERTUBE_DIR:-/srv/peertube}"
TOTAL=7

etape()   { echo "##ETAPE $1/$TOTAL $2"; }
message() { echo "##MESSAGE $*"; }
echec()   { message "$*"; echo "ERREUR : $*"; exit 1; }
trap 'echo "ERREUR à la ligne $LINENO (commande : $BASH_COMMAND)"' ERR

compose() {
	if docker compose version >/dev/null 2>&1; then
		docker compose -f "$PT_DIR/docker-compose.yml" "$@"
	else
		docker-compose -f "$PT_DIR/docker-compose.yml" "$@"
	fi
}

installer_docker() {
	if command -v docker >/dev/null 2>&1 && { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }; then
		echo "Docker est déjà installé."
		return
	fi
	local codename arch
	codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
	arch="$(dpkg --print-architecture)"
	install -m 0755 -d /etc/apt/keyrings
	if curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc; then
		chmod a+r /etc/apt/keyrings/docker.asc
		echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
			>/etc/apt/sources.list.d/docker.list
		if apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin; then
			systemctl enable --now docker
			return
		fi
		echo "Le dépôt Docker officiel n'a pas fonctionné, utilisation des paquets Debian."
		rm -f /etc/apt/sources.list.d/docker.list
		apt-get update
	fi
	apt-get install -y docker.io docker-compose
	systemctl enable --now docker
}

secret_hex() { python3 -c "import secrets; print(secrets.token_hex($1))"; }

ecrire_configuration() {
	: "${PTF_DOMAINE:?domaine manquant}" "${PTF_EMAIL:?e-mail manquant}" "${PTF_ROOT_PASSWORD:?mot de passe manquant}"
	mkdir -p "$PT_DIR"/{data,config,db,redis,caddy/data,caddy/config}
	chmod 700 "$PT_DIR"

	# Les secrets sont générés une seule fois et conservés (réinstallation possible sans tout casser).
	if [[ ! -f "$PT_DIR/.env" ]]; then
		umask 077
		cat >"$PT_DIR/.env" <<-EOF
		PEERTUBE_SECRET=$(secret_hex 32)
		DB_PASSWORD=$(secret_hex 16)
		EOF
		umask 022
	fi
	# shellcheck disable=SC1091
	source "$PT_DIR/.env"

	cat >"$PT_DIR/Caddyfile" <<-EOF
	{
		email $PTF_EMAIL
	}

	$PTF_DOMAINE {
		encode gzip
		reverse_proxy peertube:9000
	}
	EOF

	umask 077
	cat >"$PT_DIR/docker-compose.yml" <<-EOF
	# Généré par PeerTube Facile - ne pas modifier à la main.
	name: peertube
	services:
	  caddy:
	    image: caddy:2
	    container_name: peertube-caddy
	    restart: unless-stopped
	    depends_on:
	      - peertube
	    ports:
	      - "80:80"
	      - "443:443"
	      - "443:443/udp"
	    volumes:
	      - ./Caddyfile:/etc/caddy/Caddyfile:ro
	      - ./caddy/data:/data
	      - ./caddy/config:/config
	    networks: [peertube-net]

	  peertube:
	    image: chocobozzz/peertube:production
	    container_name: peertube
	    restart: unless-stopped
	    depends_on:
	      postgres:
	        condition: service_healthy
	      redis:
	        condition: service_healthy
	    ports:
	      - "9000:9000"
	      - "1935:1935"
	    environment:
	      PEERTUBE_WEBSERVER_HOSTNAME: "$PTF_DOMAINE"
	      PEERTUBE_WEBSERVER_PORT: "443"
	      PEERTUBE_WEBSERVER_HTTPS: "true"
	      PEERTUBE_TRUST_PROXY: '["127.0.0.1", "loopback", "172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/8"]'
	      PEERTUBE_SECRET: "$PEERTUBE_SECRET"
	      PEERTUBE_ADMIN_EMAIL: "$PTF_EMAIL"
	      PT_INITIAL_ROOT_PASSWORD: "$PTF_ROOT_PASSWORD"
	      PEERTUBE_DB_HOSTNAME: postgres
	      PEERTUBE_DB_USERNAME: peertube
	      PEERTUBE_DB_PASSWORD: "$DB_PASSWORD"
	      PEERTUBE_DB_SSL: "false"
	      PEERTUBE_REDIS_HOSTNAME: redis
	    volumes:
	      - ./data:/data
	      - ./config:/config
	    networks: [peertube-net]

	  postgres:
	    image: postgres:17-alpine
	    container_name: peertube-postgres
	    restart: unless-stopped
	    environment:
	      POSTGRES_USER: peertube
	      POSTGRES_PASSWORD: "$DB_PASSWORD"
	      POSTGRES_DB: peertube
	    volumes:
	      - ./db:/var/lib/postgresql/data
	    healthcheck:
	      test: ["CMD-SHELL", "pg_isready -U peertube -d peertube"]
	      interval: 10s
	      timeout: 5s
	      retries: 30
	    networks:
	      peertube-net:
	        aliases: [postgres]

	  redis:
	    image: redis:8-alpine
	    container_name: peertube-redis
	    restart: unless-stopped
	    volumes:
	      - ./redis:/data
	    healthcheck:
	      test: ["CMD", "redis-cli", "ping"]
	      interval: 10s
	      timeout: 5s
	      retries: 30
	    networks:
	      peertube-net:
	        aliases: [redis]

	networks:
	  peertube-net:
	    name: peertube-net
	EOF
	umask 022
}

attendre_peertube() {
	local i
	for i in $(seq 1 120); do
		if curl -fsS -o /dev/null http://127.0.0.1:9000/api/v1/config; then
			echo "PeerTube répond."
			return 0
		fi
		[[ $((i % 6)) -eq 0 ]] && echo "PeerTube démarre encore… ($((i * 5)) s)"
		sleep 5
	done
	compose logs --tail 40 peertube || true
	return 1
}

certificat_present() {
	compgen -G "$PT_DIR/caddy/data/caddy/certificates/*/$PTF_DOMAINE/$PTF_DOMAINE.crt" >/dev/null
}

attendre_certificat() {
	local i
	for i in $(seq 1 36); do
		if certificat_present; then
			echo "Certificat HTTPS obtenu pour $PTF_DOMAINE."
			return 0
		fi
		sleep 5
	done
	echo "Le certificat n'est pas encore obtenu. Derniers messages du serveur web :"
	compose logs --tail 15 caddy 2>&1 | grep -iE "error|challenge|obtain|acme" | tail -n 8 || true
	return 1
}

case "${1:-}" in
--mettre-a-jour)
	TOTAL=3
	etape 1 "Mise à jour du système Debian"
	apt-get update && apt-get -y upgrade
	etape 2 "Téléchargement des nouvelles versions de PeerTube"
	compose pull
	etape 3 "Redémarrage avec les nouvelles versions"
	compose up -d --remove-orphans
	docker image prune -f >/dev/null || true
	attendre_peertube || echec "PeerTube ne répond pas après la mise à jour. Consulte le journal."
	message "Mise à jour terminée."
	exit 0
	;;
--redemarrer)
	TOTAL=2
	etape 1 "Arrêt des conteneurs"
	compose down
	etape 2 "Redémarrage"
	compose up -d
	attendre_peertube || echec "PeerTube ne répond pas après le redémarrage."
	message "PeerTube a redémarré."
	exit 0
	;;
--certificat)
	TOTAL=1
	etape 1 "Vérification du certificat HTTPS"
	if attendre_certificat; then message "Le certificat HTTPS est en place."; exit 0; fi
	message "Toujours pas de certificat : vérifie les ports 80/443 et le DNS."
	exit 3
	;;
"") ;;
*) echec "Option inconnue : $1" ;;
esac

etape 1 "Préparation du système"
apt-get update
apt-get install -y ca-certificates curl gnupg

etape 2 "Installation de Docker"
installer_docker

etape 3 "Préparation de la configuration"
ecrire_configuration

etape 4 "Téléchargement de PeerTube (quelques minutes, environ 1 Go)"
compose pull

etape 5 "Démarrage de PeerTube"
compose up -d

etape 6 "Premier démarrage de PeerTube (création de la base)"
attendre_peertube || echec "PeerTube ne démarre pas. Le journal ci-dessous indique pourquoi."

etape 7 "Obtention du certificat HTTPS (Let's Encrypt)"
if attendre_certificat; then
	message "PeerTube est en ligne sur https://$PTF_DOMAINE"
	exit 0
fi
message "PeerTube fonctionne, mais le certificat HTTPS n'est pas encore obtenu (ports ou DNS)."
exit 3
