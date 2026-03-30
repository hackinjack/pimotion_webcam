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
            # Direct copy to the Asus router's restricted filesystem and restart httpd
            scp "${CERT_DIR}/fullchain.cer" "${user}@${host}:/etc/cert.pem"
            scp "${CERT_DIR}/${DOMAIN}.key" "${user}@${host}:/etc/key.pem"
            ssh "${user}@${host}" "service ${service}"
            log_info "Success: ASUS deployment on $host completed."
            ;;

        mikrotik)
            # Set required env vars and trigger acme.sh's built-in hook
            export ROUTER_OS_USERNAME="${user}"
            export ROUTER_OS_HOST="${host}"
            
            if [[ -x "$ACME_SH_BIN" ]]; then
                "$ACME_SH_BIN" --deploy -d "${DOMAIN}" --deploy-hook "$service"
                log_info "Success: MikroTik deployment on $host completed."
            else
                log_err "acme.sh binary not found at $ACME_SH_BIN or not executable."
            fi
            ;;

        *)
            log_err "Unknown deployment type '$type' for host $host. Skipping."
            ;;
    esac
done

log_info "All configured deployments completed."
