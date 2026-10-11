FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server procps iproute2 sudo \
    && useradd --uid 10002 --create-home --shell /bin/sh atrreader \
    && usermod -p x atrreader \
    && useradd --uid 10003 --create-home --shell /bin/sh atrpassword \
    && mkdir -p /run/sshd /etc/atr /srv/atr-fixture /opt/atr \
    && printf 'PORT=8123\nDB_PASSWORD=synthetic-fixture-password\n' > /srv/atr-fixture/app.conf \
    && chmod 555 /srv/atr-fixture && chmod 444 /srv/atr-fixture/app.conf \
    && ln -s /etc/passwd /srv/atr-fixture/link.conf \
    && ln -s /etc /srv/atr-fixture/linked \
    && mkfifo /srv/atr-fixture/fifo.conf \
    && cp /usr/bin/sleep /usr/local/bin/catalog-api
RUN useradd --uid 10004 --create-home --shell /bin/sh atrstandard \
    && groupadd docker && useradd --uid 10005 --create-home --shell /bin/sh --groups docker atrrisk
COPY scripts/ssh_gateway.py /opt/atr/ssh_gateway.py
COPY tests/fixtures/linux-ssh/start.py /opt/atr/start.py
COPY tests/fixtures/linux-ssh/transport.py /opt/atr/transport.py
COPY tests/fixtures/linux-ssh/readonly_cli.py /opt/atr/readonly_cli.py
COPY tests/fixtures/linux-ssh/docker_read.py /usr/local/sbin/atr-docker-read
RUN chmod 555 /usr/local/sbin/atr-docker-read \
    && printf 'atrstandard ALL=(root) NOPASSWD: /usr/local/sbin/atr-docker-read list\n' > /etc/sudoers.d/atr-docker-read \
    && chmod 440 /etc/sudoers.d/atr-docker-read && visudo -cf /etc/sudoers.d/atr-docker-read
RUN chmod 555 /opt/atr/readonly_cli.py \
    && ln -sf /opt/atr/readonly_cli.py /usr/bin/systemctl \
    && ln -sf /opt/atr/readonly_cli.py /usr/bin/journalctl \
    && ln -sf /opt/atr/readonly_cli.py /usr/bin/docker
RUN chmod 555 /opt/atr && chmod 444 /opt/atr/*.py && chmod 555 /opt/atr/readonly_cli.py \
    && ln -s /usr/local/bin/python3 /usr/bin/python3
EXPOSE 2222 2223 2224 2225 2226 2227 2228
CMD ["python3", "/opt/atr/start.py"]
