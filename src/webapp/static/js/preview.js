/**
 * preview.js
 *
 * Renders a base64-encoded compiled PDF into the preview pane using
 * pdfjs-dist, or shows a readable compile-error log instead. See the
 * import map in index.html for the exact CDN build "pdfjs-dist"
 * resolves to.
 */

import * as pdfjsLib from "pdfjs-dist";

// Must point at the ESM worker build published inside the same
// pdfjs-dist version/range resolved by the "pdfjs-dist" entry in the
// import map (index.html). pdf.js requires a dedicated worker script;
// without this, getDocument() throws at runtime. This filename
// ("build/pdf.worker.min.mjs") matches the pdfjs-dist v4 package
// layout at authoring time -- if a future pdfjs-dist major version
// renames or relocates it, update this constant and the import map
// together.
const PDF_WORKER_URL = "https://esm.sh/pdfjs-dist@4/build/pdf.worker.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = PDF_WORKER_URL;

const RENDER_SCALE = 1.5;

/**
 * Decode a base64 string into a Uint8Array of raw bytes.
 *
 * @param {string} base64 - Base64-encoded data.
 * @returns {Uint8Array} Decoded bytes.
 */
function base64ToUint8Array(base64) {
  const binaryString = atob(base64);
  const length = binaryString.length;
  const bytes = new Uint8Array(length);

  for (let i = 0; i < length; i += 1) {
    bytes[i] = binaryString.charCodeAt(i);
  }

  return bytes;
}

function assertContainer(container, fnName) {
  if (!(container instanceof Element)) {
    throw new Error(`${fnName}: container must be a DOM Element`);
  }
}

/**
 * Remove all current preview content (PDF pages, error log, or
 * placeholder text) from the container.
 *
 * @param {Element} container - Preview pane element to clear.
 */
export function clearPreview(container) {
  assertContainer(container, "clearPreview");
  container.replaceChildren();
}

/**
 * Render a readable, styled error state from a LaTeX compile log.
 *
 * @param {Element} container - Preview pane element to render into.
 * @param {string} logText - Raw compiler log/output text.
 */
export function showCompileError(container, logText) {
  assertContainer(container, "showCompileError");

  clearPreview(container);

  const errorBlock = document.createElement("div");
  errorBlock.className = "preview-error";

  const heading = document.createElement("p");
  heading.className = "preview-error-heading";
  heading.textContent = "Compile failed — see log below";

  const logBlock = document.createElement("pre");
  logBlock.className = "preview-error-log";
  logBlock.textContent =
    logText && logText.length > 0 ? logText : "(no log output)";

  errorBlock.appendChild(heading);
  errorBlock.appendChild(logBlock);
  container.appendChild(errorBlock);
}

/**
 * Render every page of a base64-encoded PDF into the container,
 * stacked vertically, each page on its own <canvas>.
 *
 * @param {Element} container - Preview pane element to render into.
 * @param {string} pdfBase64 - Base64-encoded PDF bytes, as returned
 *   by the compile endpoint's `pdf_base64` field.
 * @returns {Promise<void>} Resolves once every page has rendered, or
 *   once a readable error state has been shown in its place.
 */
export async function renderPdf(container, pdfBase64) {
  assertContainer(container, "renderPdf");

  if (typeof pdfBase64 !== "string" || pdfBase64.length === 0) {
    showCompileError(container, "No PDF data was returned by the compiler.");
    return;
  }

  clearPreview(container);

  const statusMessage = document.createElement("p");
  statusMessage.className = "preview-status";
  statusMessage.textContent = "Rendering PDF…";
  container.appendChild(statusMessage);

  let pdfDocument;

  try {
    const pdfBytes = base64ToUint8Array(pdfBase64);
    const loadingTask = pdfjsLib.getDocument({ data: pdfBytes });
    pdfDocument = await loadingTask.promise;
  } catch (error) {
    showCompileError(
      container,
      `Could not render the compiled PDF: ${error.message}`,
    );
    return;
  }

  clearPreview(container);

  const pageList = document.createElement("div");
  pageList.className = "pdf-pages";
  container.appendChild(pageList);

  for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
    let page;

    try {
      page = await pdfDocument.getPage(pageNumber);
    } catch (error) {
      console.warn(`preview.js: failed to load page ${pageNumber}`, error);
      continue;
    }

    const viewport = page.getViewport({ scale: RENDER_SCALE });

    const canvas = document.createElement("canvas");
    canvas.className = "pdf-page";
    canvas.width = viewport.width;
    canvas.height = viewport.height;

    const context = canvas.getContext("2d");

    pageList.appendChild(canvas);

    try {
      await page.render({ canvasContext: context, viewport }).promise;
    } catch (error) {
      console.warn(`preview.js: failed to render page ${pageNumber}`, error);
    }
  }
}
