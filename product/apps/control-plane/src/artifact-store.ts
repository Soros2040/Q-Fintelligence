import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import type { ArtifactManifest } from "@q-fintelligence/contracts";
import { CONTRACT_SCHEMA_VERSION } from "@q-fintelligence/contracts";

export interface StoreArtifactOptions {
  root: string;
  data: Uint8Array;
  mediaType: string;
  producer: string;
  parentHashes?: string[];
}

export interface ReadArtifactOptions {
  root: string;
  manifest: ArtifactManifest;
  maxBytes?: number;
  allowedMediaTypes?: readonly string[];
}

const SHA256_PATTERN = /^[a-f0-9]{64}$/u;

function resolveArtifactPath(root: string, manifest: ArtifactManifest): string {
  if (!SHA256_PATTERN.test(manifest.sha256)) throw new Error("invalid artifact hash");
  const expected = path.posix.join("sha256", manifest.sha256.slice(0, 2), manifest.sha256);
  if (manifest.relativePath !== expected) throw new Error("artifact path does not match its registered hash");
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(root, ...manifest.relativePath.split("/"));
  if (!resolved.startsWith(`${resolvedRoot}${path.sep}`)) throw new Error("artifact path traversal rejected");
  return resolved;
}

export async function storeArtifact(options: StoreArtifactOptions): Promise<ArtifactManifest> {
  const sha256 = createHash("sha256").update(options.data).digest("hex");
  const relativePath = path.posix.join("sha256", sha256.slice(0, 2), sha256);
  const destination = path.join(options.root, ...relativePath.split("/"));
  const destinationDir = path.dirname(destination);
  await mkdir(destinationDir, { recursive: true });

  try {
    const current = await stat(destination);
    if (current.size !== options.data.byteLength) {
      throw new Error(`existing artifact size mismatch for ${sha256}`);
    }
    const existing = await readFile(destination);
    const existingHash = createHash("sha256").update(existing).digest("hex");
    if (existingHash !== sha256) throw new Error(`existing artifact hash mismatch for ${sha256}`);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw error;
    }
    const temporary = path.join(destinationDir, `.${sha256}.${randomUUID()}.tmp`);
    try {
      await writeFile(temporary, options.data, { flag: "wx" });
      await rename(temporary, destination);
    } finally {
      await rm(temporary, { force: true });
    }
  }

  return {
    schemaVersion: CONTRACT_SCHEMA_VERSION,
    sha256,
    bytes: options.data.byteLength,
    mediaType: options.mediaType,
    relativePath,
    createdAt: new Date().toISOString(),
    producer: options.producer,
    parentHashes: [...new Set(options.parentHashes ?? [])].sort(),
  };
}

export async function readArtifact(options: ReadArtifactOptions): Promise<Uint8Array> {
  const limit = options.maxBytes ?? 256_000;
  if (options.manifest.bytes > limit) throw new Error(`artifact preview exceeds ${limit} bytes`);
  const allowed = options.allowedMediaTypes ?? ["application/json", "text/plain", "text/markdown"];
  if (!allowed.includes(options.manifest.mediaType)) {
    throw new Error(`artifact media type ${options.manifest.mediaType} is not safe to inline`);
  }
  const artifactPath = resolveArtifactPath(options.root, options.manifest);
  const data = await readFile(artifactPath);
  if (data.byteLength !== options.manifest.bytes) throw new Error("artifact size verification failed");
  const actualHash = createHash("sha256").update(data).digest("hex");
  if (actualHash !== options.manifest.sha256) throw new Error("artifact hash verification failed");
  return data;
}
