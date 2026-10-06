# Public fixed dependency seeding only; generated source never enters this build.
FROM --platform=linux/amd64 docker.io/library/maven@sha256:c97d0fa42bed8c381518d76d7ca541ff5c6427b21fa93ac6fc4bd68ffb9a258d
WORKDIR /opt/gca-toolchain-spec
COPY pom.xml ./
COPY seed/ ./src/
RUN mvn -B -q -Dmaven.repo.local=/opt/maven-repo \
      org.apache.maven.plugins:maven-dependency-plugin:3.11.0:go-offline \
    && mvn -B -q -Dmaven.repo.local=/opt/maven-repo -DfailIfNoTests=true package \
    && rm -rf src target \
    && chmod -R a+rX /opt/maven-repo
LABEL org.gca.toolchain.role="spring_offline_v1"
LABEL org.gca.toolchain.base="sha256:c97d0fa42bed8c381518d76d7ca541ff5c6427b21fa93ac6fc4bd68ffb9a258d"
USER 65532:65532
WORKDIR /workspace
ENV HOME=/tmp CI=true MAVEN_OPTS="-Xmx512m -XX:ActiveProcessorCount=1"
ENTRYPOINT []
CMD ["java", "-version"]
