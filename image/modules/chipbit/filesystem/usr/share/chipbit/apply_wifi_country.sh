#!/usr/bin/env bash
# Applies WiFi regulatory settings stored in /var/lib/chipbit/wifi_country.
# Run as root at every boot (chipbit-wifi-country.service) and immediately
# after the user picks a country in the setup UI (via sudo from chipbit-web).
set -euo pipefail

COUNTRY_FILE="/var/lib/chipbit/wifi_country"

if [ ! -f "${COUNTRY_FILE}" ]; then
    echo "No country configured — skipping WiFi regulatory setup"
    exit 0
fi

COUNTRY=$(tr -d '[:space:]' < "${COUNTRY_FILE}" | tr '[:lower:]' '[:upper:]')

if ! echo "${COUNTRY}" | grep -qE '^[A-Z]{2}$'; then
    echo "Invalid country code: ${COUNTRY}" >&2
    exit 1
fi

echo "Applying WiFi country: ${COUNTRY}"

# cfg80211 module parameter — takes effect next time cfg80211 loads (i.e. next boot)
echo "options cfg80211 ieee80211_regdom=${COUNTRY}" \
    > /etc/modprobe.d/chipbit-wifi-country.conf

# /etc/default/crda is read by the crda userspace helper on older kernels
echo "REGDOMAIN=${COUNTRY}" > /etc/default/crda

# udev rule: set regulatory domain the moment the ieee80211 PHY appears,
# before NetworkManager or wpa_supplicant start — better timing than a service
cat > /etc/udev/rules.d/70-chipbit-wifi-country.rules <<EOF
ACTION=="add", SUBSYSTEM=="ieee80211", RUN+="/usr/sbin/iw reg set ${COUNTRY}"
EOF

# brcmfmac reads ccode + country_rev as a two-part CLM blob lookup key.
# Both fields must be set; without country_rev some firmware stays in world domain
# and suppresses 2.4 GHz even when ccode is correct.
for _nvram in /lib/firmware/brcm/brcmfmac43455-sdio*.txt \
              /lib/firmware/brcm/brcmfmac43456-sdio*.txt; do
    [ -f "${_nvram}" ] || continue
    if grep -q "^ccode=" "${_nvram}"; then
        sed -i "s/^ccode=.*/ccode=${COUNTRY}/" "${_nvram}"
    else
        echo "ccode=${COUNTRY}" >> "${_nvram}"
    fi
    if grep -q "^country_rev=" "${_nvram}"; then
        sed -i "s/^country_rev=.*/country_rev=0/" "${_nvram}"
    else
        echo "country_rev=0" >> "${_nvram}"
    fi
    echo "Patched: ${_nvram}"
done

# Runtime: unblock radio and set global regulatory domain immediately.
# This won't update the per-device PHY domain (that requires a reboot so
# the driver re-probes NVRAM), but it covers the global domain path.
/usr/sbin/rfkill unblock wifi || true
/usr/sbin/iw reg set "${COUNTRY}" || true

echo "WiFi country applied: ${COUNTRY}"
