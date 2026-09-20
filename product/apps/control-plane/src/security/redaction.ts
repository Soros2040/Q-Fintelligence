const SECRET_ENV_NAMES = [
  "OPENAI_API_KEY",
  "DEEPSEEK_API_KEY",
  "TUSHARE_TOKEN",
  "TIANYAN_CONNECTION_KEY",
] as const;

export function redactSecrets(value: string): string {
  let redacted = value
    .replace(/authorization\s*:\s*bearer\s+\S+/giu, "Authorization: Bearer [REDACTED]")
    .replace(/cookie\s*:\s*[^\r\n]+/giu, "Cookie: [REDACTED]")
    .replace(
      /(["']?(?:api[_ -]?key|access[_ -]?token|connection[_ -]?key)["']?\s*[:=]\s*["']?)[^"'\s,;}]+/giu,
      "$1[REDACTED]",
    );
  for (const name of SECRET_ENV_NAMES) {
    const secret = process.env[name];
    if (secret && secret.length >= 8) redacted = redacted.split(secret).join("[REDACTED]");
  }
  return redacted;
}

export function safeErrorMessage(error: unknown): string {
  if (error instanceof Error) return redactSecrets(error.message).slice(0, 1200);
  // IPC boundaries intentionally reject structured errors. Preserve only their
  // stable diagnostic fields so recovery evidence stays useful without
  // serializing arbitrary payloads (which can contain credentials or data).
  if (error && typeof error === "object" && !Array.isArray(error)) {
    const detail = error as Record<string, unknown>;
    const code = typeof detail.code === "string" ? detail.code : "";
    const message = typeof detail.message === "string" ? detail.message : "";
    if (code || message) return redactSecrets([code, message].filter(Boolean).join(": ")).slice(0, 1200);
  }
  return redactSecrets(String(error)).slice(0, 1200);
}
