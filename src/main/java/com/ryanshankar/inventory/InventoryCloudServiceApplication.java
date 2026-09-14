package com.ryanshankar.inventory;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Profiles;
import com.ryanshankar.inventory.config.DatabaseMigrationConfiguration;

@SpringBootApplication
@Profile("!migrate")
public class InventoryCloudServiceApplication {

	public static void main(String[] args) {
		var context = new SpringApplication(InventoryCloudServiceApplication.class,
				DatabaseMigrationConfiguration.class).run(args);
		if (context.getEnvironment().acceptsProfiles(Profiles.of("migrate"))) {
			context.close();
		}
	}

}
