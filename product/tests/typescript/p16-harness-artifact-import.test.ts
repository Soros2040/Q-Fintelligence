// Authorship category: supervisor_infrastructure
// Seals the OpenHands QF Tool Gateway read boundary for P16 immutable scientific artifacts.

import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { QfToolHost } from "../../apps/control-plane/src/agent/tools.js";
import { storeArtifact } from "../../apps/control-plane/src/artifact-store.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("P16 OpenHands registered artifact read", () => {
  it("reads registered P16 mapping, validation, and hardware-result JSON through the QF tool host", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-p16-openhands-artifact-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("P16 OpenHands artifact read");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "P16 immutable JSON inputs",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const artifactRoot = path.join(directory, "artifacts");
    const manifests = await Promise.all([
      storeArtifact({
        root: artifactRoot,
        data: new TextEncoder().encode(JSON.stringify({ mappings: [{ circuit_index: 0, valid: true }] })),
        mediaType: "application/vnd.qf.p16.tianyan-mcts-mapping+json",
        producer: "test.p16-mapping",
      }),
      storeArtifact({
        root: artifactRoot,
        data: new TextEncoder().encode(JSON.stringify({ validations: [{ circuit_index: 0, valid: true }] })),
        mediaType: "application/vnd.qf.p16.tianyan-validation+json",
        producer: "test.p16-validation",
      }),
      storeArtifact({
        root: artifactRoot,
        data: new TextEncoder().encode(JSON.stringify({ queryIds: ["qid"], results: [{ resultStatus: [0, 1] }] })),
        mediaType: "application/vnd.qf.p16.tianyan-raw-results+json",
        producer: "test.p16-raw-results",
      }),
      storeArtifact({
        root: artifactRoot,
        data: new TextEncoder().encode(JSON.stringify({ queryIds: ["qid"], results: [{ probability: { "00": 1 } }] })),
        mediaType: "application/vnd.qf.p16.tianyan-corrected-results+json",
        producer: "test.p16-corrected-results",
      }),
    ]);
    for (const manifest of manifests) repository.registerArtifact(conversation.conversationId, manifest);

    const host = new QfToolHost(repository, conversation.conversationId, artifactRoot, process.cwd());
    for (const manifest of manifests) {
      expect((await host.execute("read_artifact_excerpt", { sha256: manifest.sha256 })).data.excerpt).toMatch(
        /circuit_index|queryIds/u,
      );
    }
    repository.close();
  }, 30_000);
});
