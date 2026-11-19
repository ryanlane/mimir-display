"""Root-side OTA updater files, shipped inside each release.

The systemd unit (mimir-display-updater.service) runs ota_update.sh from the
*active* release's site-packages, so fixes to the updater reach devices with
the next release instead of depending on a checkout under /opt/mimir-display.
"""
