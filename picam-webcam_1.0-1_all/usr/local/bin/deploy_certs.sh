#!/bin/bash
# /usr/local/bin/deploy_certs.sh

PICAM_USERNAME="jfk"
PICAM_HOSTNAME="picam1.thirteenb.mywire.org"
ECCDOMAIN="*.thirteenb.mywire.org_ecc"
DOMAIN="*.thirteenb.mywire.org"
CERT_DIR="/home/jfk/.acme.sh/${ECCDOMAIN}"
#CERT_DIR="/root/.acme.sh/${DOMAIN}"
#CERT_DIR="/etc/letsencrypt/live/${DOMAIN}_ecc" # _ecc is appended if using default ECDSA keys

# 1. Push to Ubuntu/webcam (PiCam1)
scp ${CERT_DIR}/fullchain.cer ${CERT_DIR}/${DOMAIN}.key ${PICAM_USER}@${PICAM_HOSTNAME}:/etc/ssl/private/
ssh ${PICAM_USERNAME}@${PICAM_HOSTNAME} "sudo systemctl restart webcam"

# 2. Push to ASUS ZenWiFi AP
#scp ${CERT_DIR}/fullchain.cer admin@192.168.0.2:/etc/cert.pem
#scp ${CERT_DIR}/${DOMAIN}.key admin@192.168.0.2:/etc/key.pem
#ssh admin@192.168.0.2 "service restart_httpd"

# 3. Push to MikroTik using acme.sh's native RouterOS hook
#export ROUTER_OS_USERNAME="admin"
#export ROUTER_OS_HOST="192.168.0.1"
#/root/.acme.sh/acme.sh --deploy -d "$DOMAIN" --deploy-hook routeros

