/** Thin fetch wrapper. Every call goes through the /api prefix, proxied to the server. */

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// A turn can wait for the session lock and then run to its deadline, so this outlasts both.
const DEFAULT_TIMEOUT_MS = 300_000;

function messageForStatus(status: number, body: unknown): string {
  if (body && typeof body === "object" && "message" in body) {
    return String((body as { message: unknown }).message);
  }
  if (status === 422) return "The server rejected that request. Check the message and try again.";
  if (status === 404) return "That item was not found.";
  return `The server returned an error (${status}). Please try again.`;
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init.headers },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check that the stack is running.");
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, messageForStatus(response.status, body));
  }
  return body as T;
}
