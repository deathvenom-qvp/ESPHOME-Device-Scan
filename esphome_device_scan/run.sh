#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
#
# Entrypoint. Reads add-on options via bashio and hands them to the Python
# service as environment variables, so app/settings.py has a single, uniform
# source of configuration whether it runs under Supervisor or standalone.
set -euo pipefail

export EDSCAN_ESPHOME_CONFIG_DIR
export EDSCAN_SCAN_INTERVAL_MINUTES
export EDSCAN_AUTO_GENERATE
export EDSCAN_SCAN_ON_STARTUP
export EDSCAN_MAC_POLICY
export EDSCAN_NAME_ADD_MAC_SUFFIX_ACTION
export EDSCAN_DRY_RUN
export EDSCAN_LOG_LEVEL

EDSCAN_ESPHOME_CONFIG_DIR="$(bashio::config 'esphome_config_dir')"
EDSCAN_SCAN_INTERVAL_MINUTES="$(bashio::config 'scan_interval_minutes')"
EDSCAN_AUTO_GENERATE="$(bashio::config 'auto_generate')"
EDSCAN_SCAN_ON_STARTUP="$(bashio::config 'scan_on_startup')"
EDSCAN_MAC_POLICY="$(bashio::config 'mac_policy')"
EDSCAN_NAME_ADD_MAC_SUFFIX_ACTION="$(bashio::config 'name_add_mac_suffix_action')"
EDSCAN_DRY_RUN="$(bashio::config 'dry_run')"
EDSCAN_LOG_LEVEL="$(bashio::config 'log_level')"

bashio::log.info "Starting ESPHome Device Scan..."
bashio::log.info "ESPHome config dir: ${EDSCAN_ESPHOME_CONFIG_DIR}"
bashio::log.info "Parent templates are read from that directory."

if ! bashio::fs.directory_exists "${EDSCAN_ESPHOME_CONFIG_DIR}"; then
    bashio::log.warning \
        "ESPHome config dir '${EDSCAN_ESPHOME_CONFIG_DIR}' does not exist yet." \
        "It will be created on the first generation."
fi

cd /opt/edscan
exec python3 -m app
