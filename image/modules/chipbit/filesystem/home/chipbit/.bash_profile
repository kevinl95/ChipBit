# ChipBit kiosk user login profile.
# On tty1, set up the Wayland environment and launch cage → chromium in kiosk mode.
# On any other TTY (SSH, serial) this is a plain login shell.

if [ "$(tty)" = "/dev/tty1" ]; then
    # Required for Wayland/DRM access without a display manager.
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export LIBSEAT_BACKEND=logind

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
    # dbus-run-session gives cage the D-Bus session it needs to talk to logind.
    # User data goes to /var/lib/chipbit (persistent, chipbit-owned).
    # Shader cache goes to /tmp so home-dir permissions don't matter.
    export XDG_CACHE_HOME=/tmp/chipbit-xdg-cache
    mkdir -p /tmp/chipbit-xdg-cache
    exec dbus-run-session cage -- chromium-browser \
        --ozone-platform=wayland \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --no-first-run \
        --disable-features=Translate \
        --check-for-update-interval=31536000 \
        --user-data-dir=/var/lib/chipbit/chromium \
        http://127.0.0.1:8080/kiosk
fi
