export type Row = Record<string, any>;

export const api = async (path: string, options: RequestInit = {}) => {
  const token = sessionStorage.getItem("atr_operator_token") || "";
  const headers = new Headers(options.headers);
  if (!(options.body instanceof Blob))
    headers.set("Content-Type", "application/json");
  else headers.set("Content-Type", "application/octet-stream");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/v1${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail || `请求失败 (${response.status})`),
    );
  return body;
};
