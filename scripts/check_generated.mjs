#!/usr/bin/env node
/**
 * Generated-types drift check (T01).
 *
 * Re-exports the FastAPI OpenAPI document into a temporary directory,
 * regenerates the client types there with openapi-typescript, and fails when
 * the committed generated types differ.  Committed generated files are never
 * hand-edited; this gate is what keeps the React contract in sync with the
 * backend authority.
 */
import { execSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const committedApi = join(root, "frontend/src/generated/api.ts");
const committedSchema = join(root, "frontend/src/generated/openapi.json");

const scratch = mkdtempSync(join(tmpdir(), "xiaopeng-t01-generated-"));
try {
  const schemaPath = join(scratch, "openapi.json");
  const regeneratedPath = join(scratch, "api.ts");
  execSync(
    `${join(root, ".venv/bin/python")} ${join(root, "scripts/export_openapi.py")} ${schemaPath}`,
    { cwd: root, stdio: "pipe" },
  );
  execSync(
    `npx openapi-typescript ${schemaPath} -o ${regeneratedPath}`,
    { cwd: root, stdio: "pipe" },
  );
  const committed = readFileSync(committedApi, "utf8");
  const regenerated = readFileSync(regeneratedPath, "utf8");
  const committedSchemaText = readFileSync(committedSchema, "utf8");
  const regeneratedSchemaText = readFileSync(schemaPath, "utf8");
  const drift = [];
  if (committed !== regenerated) {
    drift.push("frontend/src/generated/api.ts");
  }
  if (committedSchemaText !== regeneratedSchemaText) {
    drift.push("frontend/src/generated/openapi.json");
  }
  if (drift.length > 0) {
    console.error(
      `Generated artifacts drifted from the FastAPI OpenAPI document: ${drift.join(", ")}.\n` +
        "Run `npm run generate:api` and commit the regenerated artifacts.",
    );
    process.exitCode = 1;
  } else {
    console.log(
      "Generated API types and OpenAPI document match the FastAPI authority.",
    );
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
