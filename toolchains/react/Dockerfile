# Operator-owned public dependency acquisition; never a generated Dockerfile.
FROM --platform=linux/amd64 docker.io/library/node@sha256:5cbc7caba8c2c0f0bca675d1b61b9f2857e1cf1853c6164ee9dd409501a936e7
WORKDIR /opt/gca-toolchain-spec
COPY package.json package-lock.json ./
# Package lifecycle scripts are disabled; no generated source or secrets enter here.
RUN npm ci --ignore-scripts --no-audit --no-fund --cache=/opt/npm-cache \
    && rm -rf /opt/gca-toolchain-spec/node_modules \
    && chmod -R a+rX /opt/npm-cache
LABEL org.gca.toolchain.role="react_offline_v1"
LABEL org.gca.toolchain.base="sha256:5cbc7caba8c2c0f0bca675d1b61b9f2857e1cf1853c6164ee9dd409501a936e7"
USER 65532:65532
WORKDIR /workspace
ENV HOME=/tmp CI=true
ENTRYPOINT []
CMD ["node", "--version"]
