/**
 * api.js
 *
 * Thin fetch wrapper around the FastAPI backend. One async function
 * per endpoint. Every function resolves with the parsed JSON body on
 * a 2xx response and throws a descriptive Error (including the
 * response body text, when present) on any other response, or on a
 * network-level failure.
 *
 * All requests use same-origin relative paths -- this module is
 * served by the same FastAPI app that exposes /api/**, so no CORS
 * configuration is needed on either side.
 */

const JSON_HEADERS = Object.freeze({ "Content-Type": "application/json" });

/**
 * Encode a project-relative file path for use inside a URL path,
 * preserving "/" as a path separator while percent-encoding every
 * individual segment (spaces, "#", "?", non-ASCII characters, etc.).
 *
 * @param {string} path - Project-relative file path, e.g. "chapters/intro.tex".
 * @returns {string} The same path with each segment URI-encoded.
 */
function encodeFilePath(path) {
  if (typeof path !== "string" || path.length === 0) {
    throw new Error("encodeFilePath: path must be a non-empty string");
  }

  return path
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

/**
 * Perform a same-origin HTTP request and return its parsed JSON body.
 *
 * @param {string} method - HTTP method, e.g. "GET", "POST".
 * @param {string} url - Same-origin relative URL, e.g. "/api/projects".
 * @param {object} [body] - Optional JSON-serializable request body.
 * @returns {Promise<any>} Parsed JSON response body, or null for an
 *   empty/204 response.
 * @throws {Error} If the request fails at the network level, or the
 *   response status is not in the 200-299 range.
 */
async function request(method, url, body) {
  let response;

  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : JSON_HEADERS,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (networkError) {
    throw new Error(
      `Network error calling ${method} ${url}: ${networkError.message}`,
    );
  }

  if (!response.ok) {
    let detail = "";

    try {
      detail = await response.text();
    } catch {
      detail = "";
    }

    throw new Error(
      `${method} ${url} failed with status ${response.status} ` +
        `${response.statusText}${detail ? `: ${detail}` : ""}`,
    );
  }

  if (response.status === 204) {
    return null;
  }

  const text = await response.text();

  if (text.length === 0) {
    return null;
  }

  return JSON.parse(text);
}

// ─── Projects & files ───

export async function listProjects() {
  return request("GET", "/api/projects");
}

export async function createProject(name, templateId) {
  return request("POST", "/api/projects", {
    name,
    template_id: templateId ?? null,
  });
}

export async function deleteProject(id) {
  return request("DELETE", `/api/projects/${encodeURIComponent(id)}`);
}

export async function getFileTree(projectId) {
  return request(
    "GET",
    `/api/projects/${encodeURIComponent(projectId)}/files`,
  );
}

export async function getFile(projectId, path) {
  return request(
    "GET",
    `/api/projects/${encodeURIComponent(projectId)}/files/${encodeFilePath(path)}`,
  );
}

export async function saveFile(projectId, path, content) {
  return request(
    "PUT",
    `/api/projects/${encodeURIComponent(projectId)}/files/${encodeFilePath(path)}`,
    { content },
  );
}

export async function deleteFile(projectId, path) {
  return request(
    "DELETE",
    `/api/projects/${encodeURIComponent(projectId)}/files/${encodeFilePath(path)}`,
  );
}

// ─── Compilation ───

export async function compile(projectId, mainFile) {
  return request(
    "POST",
    `/api/projects/${encodeURIComponent(projectId)}/compile`,
    { main_file: mainFile ?? null },
  );
}

// ─── History (git-backed) ───

export async function listHistory(projectId) {
  return request(
    "GET",
    `/api/projects/${encodeURIComponent(projectId)}/history`,
  );
}

export async function commitChanges(projectId, message, author) {
  return request(
    "POST",
    `/api/projects/${encodeURIComponent(projectId)}/history/commit`,
    { message, author },
  );
}

export async function getDiff(projectId, commitId) {
  return request(
    "GET",
    `/api/projects/${encodeURIComponent(projectId)}/history/` +
      `${encodeURIComponent(commitId)}/diff`,
  );
}

export async function revertTo(projectId, commitId, author) {
  return request(
    "POST",
    `/api/projects/${encodeURIComponent(projectId)}/history/` +
      `${encodeURIComponent(commitId)}/revert`,
    { author },
  );
}

// ─── LaTeX package manager ───

export async function searchCatalog(query, kind) {
  const params = new URLSearchParams();

  if (query) {
    params.set("query", query);
  }

  if (kind) {
    params.set("kind", kind);
  }

  const queryString = params.toString();

  return request(
    "GET",
    `/api/packages/catalog${queryString ? `?${queryString}` : ""}`,
  );
}

export async function listInstalledPackages() {
  return request("GET", "/api/packages/installed");
}

export async function installPackage(catalogId) {
  return request("POST", "/api/packages/install", { catalog_id: catalogId });
}

export async function addLocalPackage(name, filename, content) {
  return request("POST", "/api/packages/add-local", {
    name,
    filename,
    content,
  });
}

export async function removePackage(packageId) {
  return request(
    "DELETE",
    `/api/packages/${encodeURIComponent(packageId)}`,
  );
}
