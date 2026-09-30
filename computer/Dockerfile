FROM ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    xfce4-session xfwm4 xfce4-panel xfdesktop4 thunar xfce4-terminal \
    xvfb x11vnc novnc dbus-x11 xdotool x11-utils procps \
    python3 python3-tk libreoffice-writer libreoffice-calc evince poppler-utils \
    git ca-certificates curl xz-utils fonts-dejavu-core \
    libnss3 libatk-bridge2.0-0 libdrm2 libgbm1 libasound2t64 libxkbcommon0 libgtk-3-0t64 \
    libxss1 libxtst6 libsecret-1-0 && rm -rf /var/lib/apt/lists/*
COPY codium.deb /tmp/codium.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/codium.deb && rm -rf /var/lib/apt/lists/* /tmp/codium.deb
COPY node.tar.xz /tmp/node.tar.xz
RUN tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && rm /tmp/node.tar.xz && npm install --global pnpm@11.26.0 && npm cache clean --force
COPY chromium/ /opt/chromium/
COPY entrypoint.py tools.py bridge.py note.py synthetic.txt /opt/aos/
COPY chromium.desktop codium.desktop /usr/local/share/applications/
RUN usermod --login agent --home /home/agent --move-home ubuntu && groupmod --new-name agent ubuntu && mkdir /workspace && chown 1000:1000 /workspace && \
    printf '#!/bin/sh\nexec /opt/chromium/chrome --no-sandbox --disable-dev-shm-usage --user-data-dir=/home/agent/chromium "$@"\n' > /usr/local/bin/chromium && chmod 755 /usr/local/bin/chromium && \
    printf '#!/bin/sh\nexec /usr/bin/codium --no-sandbox --disable-dev-shm-usage "$@"\n' > /usr/local/bin/codium && chmod 755 /usr/local/bin/codium && \
    dpkg-query -W > /opt/aos/packages.txt
ENV HOME=/home/agent DISPLAY=:99 LANG=C.UTF-8 XDG_RUNTIME_DIR=/run/user/1000 BROWSER=/usr/local/bin/chromium
USER 1000:1000
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/python3", "/opt/aos/entrypoint.py"]
ARG SOURCE_SHA256
LABEL com.aos.component="desktop" com.aos.source-sha256="${SOURCE_SHA256}"
