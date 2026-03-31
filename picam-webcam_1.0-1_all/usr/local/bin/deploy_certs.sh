#!/bin/bash
# /usr/local/bin/deploy_certs.sh
set -euo pipefail

CONFIG_FILE="/usr/local/etc/deploy_certs.conf"

# Colored logging functions for better visibility
log_info() { echo -e "\e[32m[INFO]\e[0m $1"; }
log_err()  { echo -e "\e[31m[ERROR]\e[0m $1" >&2; }

# 1. Load configuration
if [[ -f "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
else
    log_err "Configuration file not found at $CONFIG_FILE"
    exit 1
fi

# 2. Validate certificates exist locally before starting
if [[ ! -f "${CERT_DIR}/fullchain.cer" ]] || [[ ! -f "${CERT_DIR}/${DOMAIN}.key" ]]; then
    log_err "Certificate files not found in ${CERT_DIR}. Aborting."
    exit 1
fi

log_info "Starting certificate deployment for domain: ${DOMAIN}"

# 3. Iterate through targets and deploy based on type
for target in "${TARGETS[@]}"; do
    # Skip empty lines or commented targets if any slipped into the array
    [[ -z "$target" || "$target" == \#* ]] && continue

    # Parse the pipe-delimited string
    IFS='|' read -r type user host service <<< "$target"
    
    log_info "Deploying to [$host] (Type: $type)"

    case "$type" in
        linux)
            # Copy to /tmp with a safe fixed name, then use sudo to install and restart
            scp "${CERT_DIR}/fullchain.cer" "${user}@${host}:/tmp/fullchain.cer"
            scp "${CERT_DIR}/${DOMAIN}.key" "${user}@${host}:/tmp/domain.key"
            ssh "${user}@${host}" \
                "sudo install -m 644 -o root -g root /tmp/fullchain.cer /etc/ssl/private/fullchain.cer && \
                 sudo install -m 600 -o root -g root /tmp/domain.key /etc/ssl/private/domain.key && \
                 sudo systemctl restart ${service}"
            log_info "Success: Linux deployment on $host completed."
            ;;

        asus)
            # Direct copy to the Asus router's restricted filesystem using ssh pipes and restart httpd
            cat "${CERT_DIR}/fullchain.cer" | ssh "${host}" "cat > /etc/cert.pem"
            log_info "copied cert"
            cat "${CERT_DIR}/${DOMAIN}.key" | ssh "${host}" "cat > /etc/key.pem"
            log_info "copied key"
	    # Tell Asus to commit the files to NVRAM and restart the service
ssh ${host} << 'EOF'
nvram set https_crt_save=0
nvram unset https_crt_file
service restart_httpd
sleep 5
nvram set https_crt_save=1
nvram commit
EOF
            ssh "${host}" "service restart_${service}"
            log_info "service restarted"
            log_info "Success: ASUS deployment on $host completed."
            ;;

        mikrotik)
            # Set required env vars and trigger acme.sh's built-in hook
            export ROUTER_OS_USERNAME="${user}"
            export ROUTER_OS_HOST="${host}"	# NOTE - assumes host and user set in ~/.ssh/config
            # Push to MikroTik manually (bypassing the native hook's naming bugs)

# Upload the certificate and key
	    scp ${CERT_DIR}/fullchain.cer ${ROUTER_OS_HOST}:/fullchain.cer
   	    scp ${CERT_DIR}/${DOMAIN}.key ${ROUTER_OS_HOST}:/privkey.key

# Tell RouterOS to delete old certs, import the new ones, and assign them
            ssh ${ROUTER_OS_HOST} << 'EOF'
/certificate remove [find]
/certificate import file-name=fullchain.cer passphrase=""
/certificate import file-name=privkey.key passphrase=""
/ip service set www-ssl certificate="fullchain.cer_0"
/ip service disable www-ssl
/ip service enable www-ssl
/file remove fullchain.cer
/file remove privkey.key
EOF
		log_info "Success: MikroTik deployment on $host completed."
            ;;

        *)
            log_err "Unknown deployment type '$type' for host $host. Skipping."
            ;;
    esac
done

log_info "All configured deployments completed."

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

