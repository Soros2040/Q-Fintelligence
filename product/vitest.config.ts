// Authorship category: supervisor_infrastructure
// Runtime Harness workspaces are immutable evidence/cache inputs, not test roots.

import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    exclude: [
      ...configDefaults.exclude,
      "**/.local/**",
      "**/99_local_cache/**",
    ],
  },
});
