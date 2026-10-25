# Ubuntu 24.04 with systemd as PID 1, mirroring the "dev" server layout:
# system unit orca-serve.service running `orca-ide serve` as a normal user with linger.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session libpam-systemd sudo curl ca-certificates python3 iproute2 \
      procps xvfb git libasound2t64 libgbm1 libnss3 libgtk-3-0t64 libxss1 libxtst6 libsecret-1-0 libnotify4 xdg-utils && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash orcauser && echo 'orcauser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/99-test-only
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
