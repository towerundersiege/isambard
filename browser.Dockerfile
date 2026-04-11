FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    dbus-x11 \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libu2f-udev \
    libvulkan1 \
    openbox \
    x11vnc \
    xvfb \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash browser

COPY browser_extension/ /opt/yflix-extension/
COPY browser/start-browser.sh /usr/local/bin/start-browser.sh
COPY browser/openbox-rc.xml /etc/xdg/openbox/rc.xml

RUN chmod +x /usr/local/bin/start-browser.sh \
    && mkdir -p /etc/chromium/policies/managed \
    && printf '%s\n' '{' '  "CommandLineFlagSecurityWarningsEnabled": false' '}' > /etc/chromium/policies/managed/managed_policies.json \
    && chown -R browser:browser /opt/yflix-extension /home/browser

EXPOSE 5900

CMD ["/usr/local/bin/start-browser.sh"]
