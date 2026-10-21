export type Row = Record<string, any>;

export class ApiError extends Error {
  constructor(message: string, public status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export const api = async (path: string, options: RequestInit = {}) => {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof Blob))
    headers.set("Content-Type", "application/json");
  else headers.set("Content-Type", "application/octet-stream");
  const controller = new AbortController();
  const abort = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) abort();
  else options.signal?.addEventListener("abort", abort, { once: true });
  let timer: ReturnType<typeof setTimeout>;
  try {
    // Cover both response headers and the body; neither may keep the UI locked.
    return await Promise.race([
      (async () => {
        const response = await fetch(`/api/v1${path}`, { ...options, headers, signal: controller.signal });
        const body = await response.json().catch((error) => {
          if (controller.signal.aborted) throw error;
          return {};
        });
        if (!response.ok)
          throw new ApiError(
            response.status === 401 ? '当前请求未被接受，请检查本地服务状态。' :
            typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || `请求失败 (${response.status})`),
            response.status,
          );
        return body;
      })(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          reject(new ApiError('请求超时，运行状态尚未确认；可以重新读取同一调查。'));
          controller.abort();
        }, 15_000);
      }),
    ]);
  } finally {
    clearTimeout(timer!);
    options.signal?.removeEventListener("abort", abort);
  }
};
