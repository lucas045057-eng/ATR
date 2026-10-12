FROM docker:28-cli AS cli
FROM atr-real-discovery-fixture:20261011
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker-real
RUN ln -sf /usr/local/bin/docker-real /usr/bin/docker \
    && printf 'Defaults:atrreader env_keep += "SSH_ORIGINAL_COMMAND"\natrreader ALL=(root) NOPASSWD: /usr/local/bin/python3 /opt/atr/ssh_gateway.py --policy /etc/atr/policy.json\n' > /etc/sudoers.d/final-read \
    && chmod 440 /etc/sudoers.d/final-read && visudo -cf /etc/sudoers.d/final-read
COPY scripts/ssh_gateway.py /opt/atr/ssh_gateway.py
COPY tests/fixtures/final-e2e/start.py /opt/atr/final-start.py
RUN chmod 444 /opt/atr/ssh_gateway.py /opt/atr/final-start.py
CMD ["python3", "/opt/atr/final-start.py"]
