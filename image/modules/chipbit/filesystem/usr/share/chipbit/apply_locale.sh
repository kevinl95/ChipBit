#!/bin/bash
# Generate a locale so launched titles can actually use it.
#
# Qt titles (GCompris, KStars, Marble) pick their language from
# QLocale::system(), which reads LANG/LC_MESSAGES and ignores LANGUAGE.  On
# Debian, setting LANG=de_DE.UTF-8 silently falls back to C unless locale-gen
# has generated that locale -- which is why "I set LANG and nothing happened"
# is such a common report.  The launcher therefore only sets LANG once the
# locale exists, and this script is what makes it exist.
#
# Invoked as: sudo /usr/share/chipbit/apply_locale.sh de_DE.UTF-8
# Allowed by /etc/sudoers.d/chipbit-locale.
set -euo pipefail

LOCALE="${1:-}"
if [[ -z "${LOCALE}" ]]; then
    echo "usage: $0 <locale>   e.g. de_DE.UTF-8" >&2
    exit 2
fi

# Only ever accept a locale the distribution already knows about.  This runs as
# root from a web form, so the argument is never allowed to reach a shell or to
# name a file: it must match a line in /usr/share/i18n/SUPPORTED verbatim.
CHARSET="${LOCALE##*.}"
if ! grep -qx "${LOCALE} ${CHARSET}" /usr/share/i18n/SUPPORTED; then
    echo "refusing unknown locale: ${LOCALE}" >&2
    exit 1
fi

if locale -a 2>/dev/null | sed 's/utf8/UTF-8/' | grep -qx "${LOCALE}"; then
    echo "locale ${LOCALE} already generated"
    exit 0
fi

echo "generating locale ${LOCALE}"
if ! grep -qx "${LOCALE} ${CHARSET}" /etc/locale.gen 2>/dev/null; then
    echo "${LOCALE} ${CHARSET}" >> /etc/locale.gen
fi
locale-gen "${LOCALE}"
echo "locale ${LOCALE} ready"
