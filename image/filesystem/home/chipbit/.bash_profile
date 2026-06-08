# ChipBit kiosk user login profile.
# On tty1, wait for the web service then launch cage → chromium in kiosk mode.
# On any other TTY (SSH, serial) this is a plain login shell.

if [ "$(tty)" = "/dev/tty1" ]; then
    # Wait up to 30 s for the web service to accept connections.
    _waited=0
    until curl -sf http://127.0.0.1:8080/kiosk > /dev/null 2>&1; do
        _waited=$((_waited + 1))
        if [ "${_waited}" -ge 30 ]; then
            break
        fi
        sleep 1
    done
    unset _waited

    # cage creates its own Wayland compositor; chromium runs fullscreen inside it.
    # --kiosk: fullscreen, no address bar, no title bar.
    # No -s flag: disables VT switching (prevents child from escaping to a shell).
    exec cage -- chromium-browser \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --no-first-run \
        --disable-features=Translate \
        --check-for-update-interval=31536000 \
        http://127.0.0.1:8080/kiosk
fi
