#!/bin/bash
set -e

APP_DIR='/opt/${sanitizedProductName}'
EXECUTABLE='${executable}'
SANDBOX="$APP_DIR/chrome-sandbox"

if type update-alternatives >/dev/null 2>&1; then
  update-alternatives --install "/usr/bin/$EXECUTABLE" "$EXECUTABLE" \
    "$APP_DIR/$EXECUTABLE" 100 || ln -sf "$APP_DIR/$EXECUTABLE" "/usr/bin/$EXECUTABLE"
else
  ln -sf "$APP_DIR/$EXECUTABLE" "/usr/bin/$EXECUTABLE"
fi

# The packaged executable must retain one Chromium sandbox. Ubuntu may report
# that unshare works for root even when AppArmor blocks it for the operator, so
# keep the SUID sandbox installed with root ownership and the required mode.
chown root:root "$SANDBOX"
chmod 4755 "$SANDBOX"

if hash update-mime-database 2>/dev/null; then
  update-mime-database /usr/share/mime || true
fi
if hash update-desktop-database 2>/dev/null; then
  update-desktop-database /usr/share/applications || true
fi
