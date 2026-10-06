# User manager and D-Bus are started by workspace-user-session.service.
if [ "$(id -u)" = 1000 ] && [ -S /run/user/1000/bus ]; then
    export XDG_RUNTIME_DIR=/run/user/1000
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
fi
