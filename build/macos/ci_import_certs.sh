#!/bin/bash
# CI helper: import the Developer ID certificates into a throwaway keychain and
# export the identities to later steps via $GITHUB_ENV.
#
# Inputs (GitHub secrets, passed as env):
#   MACOS_CERTS_P12_BASE64   base64 of a .p12 holding BOTH "Developer ID
#                            Application" and "Developer ID Installer" + keys
#   MACOS_CERTS_P12_PASSWORD password the .p12 was exported with
#   APPLE_API_KEY_P8         contents of the App Store Connect API key (.p8)
#   APPLE_API_KEY_ID / APPLE_API_ISSUER   for notarytool
#
# If the certs are not configured this exits 0 having exported nothing, and the
# build proceeds unsigned exactly as before.
set -euo pipefail

if [ -z "${MACOS_CERTS_P12_BASE64:-}" ]; then
    echo "::warning::MACOS_CERTS_P12_BASE64 not set -- building UNSIGNED (Gatekeeper will warn)"
    exit 0
fi

KEYCHAIN="${RUNNER_TEMP}/signing.keychain-db"
KEYCHAIN_PW="$(uuidgen)"
echo "${MACOS_CERTS_P12_BASE64}" | base64 --decode > "${RUNNER_TEMP}/certs.p12"

security create-keychain -p "${KEYCHAIN_PW}" "${KEYCHAIN}"
security set-keychain-settings -lut 21600 "${KEYCHAIN}"
security unlock-keychain -p "${KEYCHAIN_PW}" "${KEYCHAIN}"
security import "${RUNNER_TEMP}/certs.p12" -k "${KEYCHAIN}" \
    -P "${MACOS_CERTS_P12_PASSWORD}" -T /usr/bin/codesign -T /usr/bin/productbuild -T /usr/bin/productsign
security set-key-partition-list -S apple-tool:,apple: -s -k "${KEYCHAIN_PW}" "${KEYCHAIN}" >/dev/null
security list-keychains -d user -s "${KEYCHAIN}" $(security list-keychains -d user | tr -d '"')
rm -f "${RUNNER_TEMP}/certs.p12"

APP_ID="$(security find-identity -v -p codesigning "${KEYCHAIN}" | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -1)"
# Installer certs are not "codesigning" identities; list them with the basic policy.
PKG_ID="$(security find-identity -v -p basic "${KEYCHAIN}" | sed -n 's/.*"\(Developer ID Installer:[^"]*\)".*/\1/p' | head -1)"
[ -n "${APP_ID}" ] || { echo "::error::no 'Developer ID Application' identity in the p12"; exit 1; }
echo "APP_SIGN_ID=${APP_ID}"  >> "${GITHUB_ENV}"
echo "CODESIGN_IDENTITY=${APP_ID}" >> "${GITHUB_ENV}"
if [ -n "${PKG_ID}" ]; then
    echo "PKG_SIGN_ID=${PKG_ID}" >> "${GITHUB_ENV}"
else
    echo "::warning::no 'Developer ID Installer' identity in the p12 -- pkg will not be signed/notarized"
fi

if [ -n "${APPLE_API_KEY_P8:-}" ]; then
    echo "${APPLE_API_KEY_P8}" > "${RUNNER_TEMP}/AuthKey.p8"
    echo "APPLE_API_KEY_PATH=${RUNNER_TEMP}/AuthKey.p8" >> "${GITHUB_ENV}"
fi
echo "Imported signing identities: ${APP_ID} / ${PKG_ID:-<none>}"
