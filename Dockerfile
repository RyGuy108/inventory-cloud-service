# syntax=docker/dockerfile:1
FROM eclipse-temurin:25-jdk-jammy AS build
WORKDIR /workspace
COPY .mvn/ .mvn/
COPY mvnw pom.xml ./
RUN chmod +x mvnw
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -ntp dependency:go-offline
COPY src/ src/
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -ntp -DskipTests package

FROM eclipse-temurin:25-jre-jammy
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
WORKDIR /app
RUN mkdir -p /app/certs \
    && curl --fail --silent --show-error --location --retry 3 \
       https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
       --output /app/certs/global-bundle.pem \
    && chmod 644 /app/certs/global-bundle.pem
COPY --from=build --chown=app:app /workspace/target/inventory-cloud-service-0.0.1-SNAPSHOT.jar app.jar
COPY --chown=app:app release-contract.json /app/release-contract.json
USER app
EXPOSE 8080
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=70.0 -Djava.io.tmpdir=/tmp"
HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent http://localhost:8080/actuator/health/liveness || exit 1
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
