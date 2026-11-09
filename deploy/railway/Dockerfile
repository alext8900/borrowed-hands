# Borrowed Hands on Hermes. Build context is the repo root at the source commit Railway builds.
# Pinned Hermes Agent v0.22.7 (multi-arch index digest, pushed 2026-11-03). aiohttp and cryptography, which the
# plugin needs, are already in this image's venv.
FROM nousresearch/hermes-agent:v0.22.7@sha256:e6d81ae52ebf6a43efa529701405509f20054ba818f179ddd748bed4f8af584c
USER root
COPY --chmod=0755 borrowed-hands /opt/bh/plugins/borrowed-hands
COPY --chmod=0644 deploy/railway/config.yaml /opt/bh/config.default.yaml
COPY --chmod=0755 deploy/railway/bh-start.sh /opt/bh/bh-start.sh
# BH_LISTEN=1 only switches the listener on. The plugin still requires the process to be `hermes gateway run`,
# so `hermes plugins validate`, doctor and CLI processes never open the port.
# Only for Railway edge ingress. Generic plugin installs leave trusted-proxy handling off.
ENV BH_PORT=8790 BH_LISTEN=1 BH_TRUSTED_PROXY=railway
EXPOSE 8790
ENTRYPOINT ["/opt/bh/bh-start.sh"]
CMD ["gateway", "run"]
