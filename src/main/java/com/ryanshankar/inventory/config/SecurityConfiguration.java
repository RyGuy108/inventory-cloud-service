package com.ryanshankar.inventory.config;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.MDC;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;
import org.springframework.core.env.Profiles;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.authentication.JwtGrantedAuthoritiesConverter;
import org.springframework.security.provisioning.InMemoryUserDetailsManager;
import org.springframework.security.web.SecurityFilterChain;
import tools.jackson.databind.json.JsonMapper;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Map;

@Configuration(proxyBeanMethods = false)
public class SecurityConfiguration {
    private static final JsonMapper JSON = JsonMapper.builder().build();

    @Bean
    @Profile({"local", "test"})
    InMemoryUserDetailsManager localUsers(Environment env) {
        var encoder = new BCryptPasswordEncoder();
        var users = new ArrayList<UserDetails>();
        users.add(User.withUsername("admin").password("{bcrypt}" + encoder.encode(password(env, "admin"))).roles("ADMIN").build());
        users.add(User.withUsername("customer").password("{bcrypt}" + encoder.encode(password(env, "customer"))).roles("CUSTOMER").build());
        if (env.acceptsProfiles(Profiles.of("test"))) {
            users.add(User.withUsername("other").password("{bcrypt}" + encoder.encode(password(env, "other"))).roles("CUSTOMER").build());
        }
        return new InMemoryUserDetailsManager(users);
    }

    private String password(Environment env, String account) {
        String value = env.getRequiredProperty("app.auth." + account + "-password");
        if (value.isBlank() || value.length() < 12 || value.getBytes(java.nio.charset.StandardCharsets.UTF_8).length > 72) {
            throw new IllegalArgumentException("Local " + account + " password must be at least 12 characters and at most 72 UTF-8 bytes");
        }
        return value;
    }

    @Bean
    @Profile({"local", "test"})
    SecurityFilterChain localSecurity(HttpSecurity http) throws Exception {
        common(http, "Basic realm=\"inventory-local\"");
        http.httpBasic(basic -> basic.authenticationEntryPoint((request, response, ex) -> {
            response.setHeader("WWW-Authenticate", "Basic realm=\"inventory-local\"");
            error(request, response, 401, "Unauthorized", "Valid credentials are required");
        }));
        return http.build();
    }

    @Bean
    @Profile("!local & !test")
    SecurityFilterChain productionSecurity(HttpSecurity http) throws Exception {
        common(http, "Bearer");
        var roles = new JwtGrantedAuthoritiesConverter();
        roles.setAuthoritiesClaimName("roles");
        roles.setAuthorityPrefix("ROLE_");
        var converter = new JwtAuthenticationConverter();
        converter.setJwtGrantedAuthoritiesConverter(roles);
        http.oauth2ResourceServer(oauth -> oauth.jwt(jwt -> jwt.jwtAuthenticationConverter(converter))
                .authenticationEntryPoint((request, response, ex) -> {
                    response.setHeader("WWW-Authenticate", "Bearer");
                    error(request, response, 401, "Unauthorized", "A valid access token is required");
                }));
        return http.build();
    }

    private void common(HttpSecurity http, String challenge) throws Exception {
        http.sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS));
        // This API accepts JSON and Authorization headers only. It has no cookie/form authentication.
        http.csrf(csrf -> csrf.disable());
        http.formLogin(form -> form.disable());
        http.logout(logout -> logout.disable());
        http.requestCache(cache -> cache.disable());
        http.authorizeHttpRequests(auth -> auth
                .requestMatchers("/", "/index.html", "/openapi.yaml", "/error", "/actuator/health", "/actuator/health/**").permitAll()
                .requestMatchers(HttpMethod.POST, "/api/v1/products", "/api/v1/products/**").hasRole("ADMIN")
                .requestMatchers("/actuator/**").hasRole("ADMIN")
                .requestMatchers("/api/v1/**").authenticated()
                .anyRequest().denyAll());
        http.exceptionHandling(errors -> errors
                .authenticationEntryPoint((request, response, ex) -> {
                    response.setHeader("WWW-Authenticate", challenge);
                    error(request, response, 401, "Unauthorized", "Authentication is required");
                })
                .accessDeniedHandler((request, response, ex) -> error(request, response, 403, "Forbidden", "You do not have permission for this operation")));
        http.headers(headers -> headers.contentSecurityPolicy(csp -> csp.policyDirectives(
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")));
    }

    private static void error(HttpServletRequest request, HttpServletResponse response, int status, String title, String detail)
            throws IOException {
        response.setStatus(status);
        response.setContentType("application/problem+json");
        JSON.writeValue(response.getOutputStream(), Map.of("type", "about:blank", "title", title,
                "status", status, "detail", detail, "instance", request.getRequestURI(),
                "requestId", String.valueOf(MDC.get("requestId"))));
    }
}
