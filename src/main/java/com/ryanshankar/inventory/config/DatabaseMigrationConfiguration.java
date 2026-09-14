package com.ryanshankar.inventory.config;

import java.nio.charset.StandardCharsets;
import java.sql.DriverManager;

import org.flywaydb.core.Flyway;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;
import org.springframework.core.io.ClassPathResource;

/** One-shot schema and role provisioning, without web, JPA, or security auto-configuration. */
@Configuration(proxyBeanMethods = false)
@Profile("migrate")
public class DatabaseMigrationConfiguration {
    private static final Logger LOG = LoggerFactory.getLogger(DatabaseMigrationConfiguration.class);

    @Bean
    ApplicationRunner migrateDatabase(Environment environment) {
        return args -> {
            String url = environment.getRequiredProperty("spring.datasource.url");
            String username = environment.getRequiredProperty("spring.datasource.username");
            String password = environment.getRequiredProperty("spring.datasource.password");
            String runtimePassword = environment.getRequiredProperty("DB_APP_PASSWORD");
            if (runtimePassword.length() < 16) {
                throw new IllegalArgumentException("DB_APP_PASSWORD must contain at least 16 characters");
            }
            var result = Flyway.configure().dataSource(url, username, password)
                    .locations("classpath:db/migration").cleanDisabled(true).load().migrate();
            provisionRuntimeRole(url, username, password, runtimePassword);
            LOG.info("Database preparation complete: {} migration(s) applied; runtime permissions configured",
                    result.migrationsExecuted);
        };
    }

    static void provisionRuntimeRole(String url, String username, String password, String runtimePassword)
            throws Exception {
        String sql = new ClassPathResource("db/provision/runtime-role.sql")
                .getContentAsString(StandardCharsets.UTF_8);
        try (var connection = DriverManager.getConnection(url, username, password)) {
            connection.setAutoCommit(false);
            // Bind the secret rather than placing it in SQL text or command-line arguments.
            try (var setting = connection.prepareStatement(
                    "SELECT set_config('inventory.runtime_password', ?, true)")) {
                setting.setString(1, runtimePassword);
                setting.execute();
            }
            try (var statement = connection.createStatement()) {
                statement.execute(sql);
            }
            connection.commit();
        }
    }
}
