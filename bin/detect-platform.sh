#!/bin/sh
# Print the service-manager flavor: systemd | freebsd | unsupported
# Exit 0 on known platform, 1 on unsupported.

case "$(uname -s)" in
  Linux)
    if command -v systemctl >/dev/null 2>&1; then
      echo systemd
      exit 0
    fi
    echo unsupported
    exit 1
    ;;
  FreeBSD)
    echo freebsd
    exit 0
    ;;
  *)
    echo unsupported
    exit 1
    ;;
esac
