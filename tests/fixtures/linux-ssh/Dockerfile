FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server procps iproute2 \
    && useradd --uid 10002 --create-home --shell /bin/sh atrreader \
    && usermod -p x atrreader \
    && mkdir -p /run/sshd /etc/atr /srv/atr-fixture /opt/atr \
    && printf 'PORT=8123\nDB_PASSWORD=synthetic-fixture-password\n' > /srv/atr-fixture/app.conf \
    && chmod 555 /srv/atr-fixture && chmod 444 /srv/atr-fixture/app.conf \
    && ln -s /etc/passwd /srv/atr-fixture/link.conf \
    && ln -s /etc /srv/atr-fixture/linked \
    && mkfifo /srv/atr-fixture/fifo.conf \
    && cp /usr/bin/sleep /usr/local/bin/catalog-api
COPY scripts/ssh_gateway.py /opt/atr/ssh_gateway.py
COPY tests/fixtures/linux-ssh/start.py /opt/atr/start.py
COPY tests/fixtures/linux-ssh/transport.py /opt/atr/transport.py
RUN chmod 555 /opt/atr && chmod 444 /opt/atr/*.py
EXPOSE 2222 2223 2224
CMD ["python3", "/opt/atr/start.py"]
