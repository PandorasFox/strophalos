#!/bin/sh
# Set MakeMKV registration key from environment
if [ -n "${MAKEMKV_KEY:-}" ]; then
    mkdir -p /config/.MakeMKV
    # MakeMKV stores its key in settings.conf
    SETTINGS="/config/settings.conf"
    if [ -f "$SETTINGS" ] && grep -q "^app_Key" "$SETTINGS"; then
        sed -i "s|^app_Key.*|app_Key = \"$MAKEMKV_KEY\"|" "$SETTINGS"
    else
        echo "app_Key = \"$MAKEMKV_KEY\"" >> "$SETTINGS"
    fi
fi
