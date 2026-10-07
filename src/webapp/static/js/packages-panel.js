/**
 * packages-panel.js
 *
 * Renders the Packages & Templates slide-in panel: a debounced
 * catalog search (name/description/tags/kind), per-entry Install
 * buttons (disabled for kind="template", since templates are chosen
 * at project-creation time instead), a list of currently installed
 * packages with Remove buttons, and an "add a local .sty/.cls file"
 * form.
 */

import {
  searchCatalog,
  listInstalledPackages,
  installPackage,
  addLocalPackage,
  removePackage,
} from "./api.js";

const SEARCH_DEBOUNCE_MS = 300;

function createElement(tagName, className, textContent) {
  const element = document.createElement(tagName);

  if (className) {
    element.className = className;
  }

  if (textContent !== undefined) {
    element.textContent = textContent;
  }

  return element;
}

function debounce(fn, delayMs) {
  let timerId = null;

  return (...args) => {
    if (timerId !== null) {
      clearTimeout(timerId);
    }

    timerId = setTimeout(() => {
      timerId = null;
      fn(...args);
    }, delayMs);
  };
}

/**
 * Create and wire up the Packages panel's contents.
 *
 * @param {object} options
 * @param {Element} options.container - Element the panel body renders into.
 * @param {(message: string, kind?: "info"|"success"|"error") => void} options.notify -
 *   Shows a toast/banner message to the user.
 * @returns {{ refresh: () => Promise<void> }}
 */
export function createPackagesPanel({ container, notify }) {
  if (!(container instanceof Element)) {
    throw new Error("createPackagesPanel: container must be a DOM Element");
  }

  let installedPackageIds = new Set();

  async function refresh() {
    container.replaceChildren();

    const searchSection = createElement("div", "panel-section");
    searchSection.appendChild(createElement("h3", null, "Search catalog"));

    const searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.placeholder = "Search packages and templates…";
    searchSection.appendChild(searchInput);

    const resultsList = createElement("div", "package-list");
    searchSection.appendChild(resultsList);
    container.appendChild(searchSection);

    const installedSection = createElement("div", "panel-section");
    installedSection.appendChild(createElement("h3", null, "Installed packages"));
    const installedList = createElement("div", "package-list");
    installedSection.appendChild(installedList);
    container.appendChild(installedSection);

    const addLocalSection = createElement("div", "panel-section");
    addLocalSection.appendChild(createElement("h3", null, "Add a local file"));
    addLocalSection.appendChild(buildAddLocalForm());
    container.appendChild(addLocalSection);

    async function refreshInstalled() {
      installedList.replaceChildren(
        createElement("p", "empty-state", "Loading installed packages…"),
      );

      try {
        const installed = await listInstalledPackages();
        installedPackageIds = new Set(
          Array.isArray(installed) ? installed.map((pkg) => pkg.id) : [],
        );
        installedList.replaceChildren();

        if (!Array.isArray(installed) || installed.length === 0) {
          installedList.appendChild(
            createElement("p", "empty-state", "No packages installed yet."),
          );
          return;
        }

        for (const pkg of installed) {
          installedList.appendChild(renderInstalledItem(pkg));
        }
      } catch (error) {
        installedList.replaceChildren(
          createElement(
            "p",
            "empty-state",
            `Could not load installed packages: ${error.message}`,
          ),
        );
      }
    }

    function renderInstalledItem(pkg) {
      const item = createElement("div", "package-item");

      const top = createElement("div", "package-item-top");
      top.appendChild(createElement("span", "package-name", pkg.name));
      top.appendChild(createElement("span", "package-kind-badge", pkg.kind));
      item.appendChild(top);

      if (Array.isArray(pkg.files) && pkg.files.length > 0) {
        item.appendChild(
          createElement("div", "package-description", pkg.files.join(", ")),
        );
      }

      const removeButton = createElement("button", "btn btn-danger", "Remove");
      removeButton.type = "button";
      removeButton.addEventListener("click", async () => {
        const confirmed = window.confirm(`Remove "${pkg.name}"?`);

        if (!confirmed) {
          return;
        }

        removeButton.disabled = true;

        try {
          await removePackage(pkg.id);
          notify(`Removed "${pkg.name}".`, "success");
          await refreshInstalled();
          await runSearch(searchInput.value.trim());
        } catch (error) {
          notify(`Could not remove package: ${error.message}`, "error");
          removeButton.disabled = false;
        }
      });

      item.appendChild(removeButton);
      return item;
    }

    function renderSearchResultItem(entry) {
      const item = createElement("div", "package-item");

      const top = createElement("div", "package-item-top");
      top.appendChild(createElement("span", "package-name", entry.name));
      top.appendChild(createElement("span", "package-kind-badge", entry.kind));
      item.appendChild(top);

      if (entry.description) {
        item.appendChild(
          createElement("div", "package-description", entry.description),
        );
      }

      if (Array.isArray(entry.tags) && entry.tags.length > 0) {
        const tagsRow = createElement("div", "package-tags");

        for (const tag of entry.tags) {
          tagsRow.appendChild(createElement("span", "package-tag", tag));
        }

        item.appendChild(tagsRow);
      }

      if (entry.kind === "template") {
        item.appendChild(
          createElement(
            "p",
            "package-note",
            "Templates are chosen when creating a new project.",
          ),
        );
      } else {
        const alreadyInstalled = installedPackageIds.has(entry.id);

        const installButton = createElement(
          "button",
          "btn btn-primary",
          alreadyInstalled ? "Installed" : "Install",
        );
        installButton.type = "button";
        installButton.disabled = alreadyInstalled;

        installButton.addEventListener("click", async () => {
          installButton.disabled = true;
          installButton.textContent = "Installing…";

          try {
            await installPackage(entry.id);
            notify(`Installed "${entry.name}".`, "success");
            await refreshInstalled();
          } catch (error) {
            notify(`Could not install package: ${error.message}`, "error");
            installButton.disabled = false;
            installButton.textContent = "Install";
          }
        });

        item.appendChild(installButton);
      }

      return item;
    }

    async function runSearch(query) {
      resultsList.replaceChildren(
        createElement("p", "empty-state", "Searching…"),
      );

      try {
        const entries = await searchCatalog(query || null, null);
        resultsList.replaceChildren();

        if (!Array.isArray(entries) || entries.length === 0) {
          resultsList.appendChild(
            createElement("p", "empty-state", "No matching packages or templates."),
          );
          return;
        }

        for (const entry of entries) {
          resultsList.appendChild(renderSearchResultItem(entry));
        }
      } catch (error) {
        resultsList.replaceChildren(
          createElement("p", "empty-state", `Search failed: ${error.message}`),
        );
      }
    }

    const debouncedSearch = debounce(
      () => runSearch(searchInput.value.trim()),
      SEARCH_DEBOUNCE_MS,
    );

    searchInput.addEventListener("input", debouncedSearch);

    await refreshInstalled();
    await runSearch("");
  }

  function buildAddLocalForm() {
    const form = createElement("form", "commit-form");

    const nameRow = createElement("div", "form-row");
    nameRow.appendChild(createElement("label", null, "Display name"));
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.required = true;
    nameInput.placeholder = "My Custom Class";
    nameRow.appendChild(nameInput);

    const fileRow = createElement("div", "form-row");
    fileRow.appendChild(createElement("label", null, "File (.sty / .cls / .bib)"));
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.required = true;
    fileRow.appendChild(fileInput);

    const submitButton = createElement("button", "btn btn-primary", "Add local file");
    submitButton.type = "submit";

    form.appendChild(nameRow);
    form.appendChild(fileRow);
    form.appendChild(submitButton);

    form.addEventListener("submit", (event) => {
      event.preventDefault();

      const file = fileInput.files[0];
      const displayName = nameInput.value.trim();

      if (!file || !displayName) {
        notify("Choose a file and enter a display name.", "error");
        return;
      }

      submitButton.disabled = true;

      const reader = new FileReader();

      reader.onerror = () => {
        notify("Could not read the selected file.", "error");
        submitButton.disabled = false;
      };

      reader.onload = async () => {
        try {
          await addLocalPackage(displayName, file.name, String(reader.result));
          notify(`Added "${displayName}".`, "success");
          form.reset();
          await refresh();
        } catch (error) {
          notify(`Could not add local package: ${error.message}`, "error");
        } finally {
          submitButton.disabled = false;
        }
      };

      reader.readAsText(file);
    });

    return form;
  }

  return { refresh };
}
