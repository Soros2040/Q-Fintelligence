import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { readArtifact, storeArtifact } from "../../apps/control-plane/src/artifact-store.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("SHA-256 artifact store", () => {
  it("writes content to a deterministic content-addressed path", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "qf-artifact-test-"));
    temporaryRoots.push(root);
    const data = new TextEncoder().encode("q-fintelligence");
    const manifest = await storeArtifact({
      root,
      data,
      mediaType: "text/plain",
      producer: "artifact-store.test",
    });

    expect(manifest.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(manifest.relativePath).toBe(`sha256/${manifest.sha256.slice(0, 2)}/${manifest.sha256}`);
    expect(new Uint8Array(await readFile(path.join(root, ...manifest.relativePath.split("/"))))).toEqual(data);
  });

  it("rejects path traversal and oversized previews before reading", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "qf-artifact-test-"));
    temporaryRoots.push(root);
    const data = new TextEncoder().encode("safe");
    const manifest = await storeArtifact({ root, data, mediaType: "text/plain", producer: "test" });
    await expect(readArtifact({ root, manifest: { ...manifest, relativePath: "../secret" } })).rejects.toThrow(/path/);
    await expect(readArtifact({ root, manifest, maxBytes: 1 })).rejects.toThrow(/exceeds/);
  });

  it.runIf(process.platform === "linux")("fails promptly when the artifact root is beneath a non-directory device", async () => {
    await expect(storeArtifact({
      root: "/dev/null/qf-p07-artifact-failure",
      data: new TextEncoder().encode("fault"),
      mediaType: "text/plain",
      producer: "artifact-store.failure-injection",
    })).rejects.toMatchObject({ code: "ENOTDIR" });
  });
});
