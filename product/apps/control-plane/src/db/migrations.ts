import { mkdirSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export interface MigrationResult {
  database: DatabaseSync;
  applied: string[];
}

export function openDatabase(databasePath: string, migrationsDirectory: string): MigrationResult {
  mkdirSync(path.dirname(databasePath), { recursive: true });
  const database = new DatabaseSync(databasePath);
  database.exec("PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;");

  const applied: string[] = [];
  const files = readdirSync(migrationsDirectory)
    .filter((name) => /^\d{4}_[a-z0-9_]+[.]sql$/u.test(name))
    .sort();

  for (const file of files) {
    const version = file.replace(/[.]sql$/u, "");
    const hasMigrationTable = database
      .prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'")
      .get();
    if (hasMigrationTable) {
      const found = database.prepare("SELECT 1 FROM schema_migrations WHERE version = ?").get(version);
      if (found) continue;
    }

    const sql = readFileSync(path.join(migrationsDirectory, file), "utf8");
    if (file.startsWith("0001_")) {
      database.exec(sql);
    } else if (/^-- qf:migration-foreign-keys-off$/mu.test(sql)) {
      database.exec("PRAGMA foreign_keys = OFF; BEGIN IMMEDIATE;");
      try {
        database.exec(sql);
        const violations = database.prepare("PRAGMA foreign_key_check").all();
        if (violations.length !== 0) {
          throw new Error(`migration ${version} produced ${violations.length} foreign-key violations`);
        }
        database.exec("COMMIT;");
      } catch (error) {
        try { database.exec("ROLLBACK;"); } catch { /* transaction already rolled back */ }
        throw error;
      } finally {
        database.exec("PRAGMA foreign_keys = ON;");
      }
    } else {
      database.exec(`BEGIN IMMEDIATE;\n${sql}\nCOMMIT;`);
    }
    applied.push(version);
  }

  database.exec("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;");
  return { database, applied };
}
