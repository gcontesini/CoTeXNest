/**
 * history-panel.js
 *
 * Renders the Version History slide-in panel: a "Save Version" form
 * that commits the project's current working tree, a list of past
 * commits (message, author, relative time), a click-to-view diff for
 * each commit, and a confirm-then-revert action.
 *
 * This module owns only the panel's own DOM subtree (passed in as
 * `container`); it does not know about panel open/close animation,
 * which app.js handles by toggling a CSS class on the panel element.
 */

import {
  listHistory,
  commitChanges,
  getDiff,
  revertTo,
} from "./api.js";

const RELATIVE_TIME_FORMATTER = new Intl.RelativeTimeFormat("en", {
  numeric: "auto",
});

const RELATIVE_TIME_DIVISIONS = [
  { amountInUnit: 60, unit: "second" },
  { amountInUnit: 60, unit: "minute" },
  { amountInUnit: 24, unit: "hour" },
  { amountInUnit: 7, unit: "day" },
  { amountInUnit: 4.34524, unit: "week" },
  { amountInUnit: 12, unit: "month" },
  { amountInUnit: Number.POSITIVE_INFINITY, unit: "year" },
];

/**
 * Format a Unix timestamp (seconds) as a short relative-time string,
 * e.g. "3 hours ago" or "in 2 days", using only Intl -- no date
 * library.
 *
 * @param {number} timestampSeconds - Unix timestamp, in seconds.
 * @returns {string} Human-readable relative time.
 */
export function formatRelativeTime(timestampSeconds) {
  if (typeof timestampSeconds !== "number" || Number.isNaN(timestampSeconds)) {
    return "unknown time";
  }

  let durationInSeconds = (Date.now() - timestampSeconds * 1000) / 1000;

  for (const division of RELATIVE_TIME_DIVISIONS) {
    if (Math.abs(durationInSeconds) < division.amountInUnit) {
      return RELATIVE_TIME_FORMATTER.format(
        -Math.round(durationInSeconds),
        division.unit,
      );
    }

    durationInSeconds /= division.amountInUnit;
  }

  return "a long time ago";
}

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

/**
 * Create and wire up the History panel's contents.
 *
 * @param {object} options
 * @param {Element} options.container - Element the panel body renders into.
 * @param {() => string | null} options.getProjectId - Returns the
 *   currently active project id, or null if none is open.
 * @param {() => string} options.getAuthorName - Returns the display
 *   name to attribute new commits/reverts to.
 * @param {(message: string, kind?: "info"|"success"|"error") => void} options.notify -
 *   Shows a toast/banner message to the user.
 * @param {() => Promise<void> | void} options.onReverted - Called
 *   after a successful revert, so the caller can reload the file
 *   tree/editor/preview.
 * @returns {{ refresh: () => Promise<void> }}
 */
export function createHistoryPanel({
  container,
  getProjectId,
  getAuthorName,
  notify,
  onReverted,
}) {
  if (!(container instanceof Element)) {
    throw new Error("createHistoryPanel: container must be a DOM Element");
  }

  let activeCommitId = null;

  async function refresh() {
    const projectId = getProjectId();

    container.replaceChildren();

    if (!projectId) {
      container.appendChild(
        createElement("p", "empty-state", "Open a project to see its history."),
      );
      return;
    }

    const commitSection = createElement("div", "panel-section");
    commitSection.appendChild(createElement("h3", null, "Save a new version"));

    const commitForm = createElement("form", "commit-form");
    const messageInput = document.createElement("textarea");
    messageInput.placeholder = "Describe what changed…";
    messageInput.required = true;
    messageInput.maxLength = 500;

    const commitButton = createElement("button", "btn btn-primary", "Save Version");
    commitButton.type = "submit";

    commitForm.appendChild(messageInput);
    commitForm.appendChild(commitButton);
    commitSection.appendChild(commitForm);
    container.appendChild(commitSection);

    commitForm.addEventListener("submit", async (event) => {
      event.preventDefault();

      const message = messageInput.value.trim();

      if (!message) {
        notify("Enter a short message describing this version.", "error");
        return;
      }

      commitButton.disabled = true;

      try {
        await commitChanges(projectId, message, getAuthorName());
        messageInput.value = "";
        notify("Version saved.", "success");
        await refresh();
      } catch (error) {
        notify(`Could not save version: ${error.message}`, "error");
      } finally {
        commitButton.disabled = false;
      }
    });

    const historySection = createElement("div", "panel-section");
    historySection.appendChild(createElement("h3", null, "Past versions"));

    let commits = [];

    try {
      commits = await listHistory(projectId);
    } catch (error) {
      historySection.appendChild(
        createElement(
          "p",
          "empty-state",
          `Could not load history: ${error.message}`,
        ),
      );
      container.appendChild(historySection);
      return;
    }

    if (!Array.isArray(commits) || commits.length === 0) {
      historySection.appendChild(
        createElement("p", "empty-state", "No saved versions yet."),
      );
      container.appendChild(historySection);
      return;
    }

    const commitList = createElement("div", "commit-list");

    for (const commit of commits) {
      commitList.appendChild(renderCommitItem(commit, projectId));
    }

    historySection.appendChild(commitList);
    container.appendChild(historySection);
  }

  function renderCommitItem(commit, projectId) {
    const item = createElement("div", "commit-item");
    item.dataset.commitId = commit.id;

    if (commit.id === activeCommitId) {
      item.classList.add("active");
    }

    item.appendChild(createElement("div", "commit-message", commit.message));

    const meta = createElement(
      "div",
      "commit-meta",
      `${commit.author} · ${formatRelativeTime(commit.timestamp)}`,
    );
    item.appendChild(meta);

    if (Array.isArray(commit.changed_files) && commit.changed_files.length > 0) {
      item.appendChild(
        createElement(
          "div",
          "commit-files",
          commit.changed_files.join(", "),
        ),
      );
    }

    const diffContainer = createElement("div", "commit-diff hidden");
    item.appendChild(diffContainer);

    item.addEventListener("click", async (event) => {
      if (event.target.closest("button")) {
        return;
      }

      const isCurrentlyOpen = activeCommitId === commit.id;
      activeCommitId = isCurrentlyOpen ? null : commit.id;

      container
        .querySelectorAll(".commit-item.active")
        .forEach((element) => element.classList.remove("active"));

      if (isCurrentlyOpen) {
        diffContainer.classList.add("hidden");
        diffContainer.replaceChildren();
        return;
      }

      item.classList.add("active");
      diffContainer.classList.remove("hidden");
      diffContainer.replaceChildren(
        createElement("p", "empty-state", "Loading diff…"),
      );

      try {
        const diffEntries = await getDiff(projectId, commit.id);
        diffContainer.replaceChildren();

        if (!Array.isArray(diffEntries) || diffEntries.length === 0) {
          diffContainer.appendChild(
            createElement("p", "empty-state", "No file changes in this version."),
          );
        } else {
          for (const diffEntry of diffEntries) {
            diffContainer.appendChild(renderDiffEntry(diffEntry));
          }
        }

        const revertButton = createElement(
          "button",
          "btn btn-danger",
          "Revert to this version",
        );
        revertButton.type = "button";

        revertButton.addEventListener("click", async (clickEvent) => {
          clickEvent.stopPropagation();

          const confirmed = window.confirm(
            `Revert the project to "${commit.message}"? ` +
              "This creates a new version on top of the current one -- " +
              "nothing is deleted.",
          );

          if (!confirmed) {
            return;
          }

          revertButton.disabled = true;

          try {
            await revertTo(projectId, commit.id, getAuthorName());
            notify("Reverted. A new version was saved with the restored files.", "success");
            activeCommitId = null;
            await refresh();

            if (onReverted) {
              await onReverted();
            }
          } catch (error) {
            notify(`Could not revert: ${error.message}`, "error");
          } finally {
            revertButton.disabled = false;
          }
        });

        const actionsRow = createElement("div", "commit-actions");
        actionsRow.appendChild(revertButton);
        diffContainer.appendChild(actionsRow);
      } catch (error) {
        diffContainer.replaceChildren(
          createElement(
            "p",
            "empty-state",
            `Could not load diff: ${error.message}`,
          ),
        );
      }
    });

    return item;
  }

  function renderDiffEntry(diffEntry) {
    const entry = createElement("div", "diff-entry");
    entry.dataset.status = diffEntry.status;

    entry.appendChild(
      createElement(
        "div",
        "diff-entry-header",
        `${diffEntry.status.toUpperCase()} · ${diffEntry.file_path}`,
      ),
    );

    const patchBlock = document.createElement("pre");
    patchBlock.textContent = diffEntry.patch || "(no textual diff available)";
    entry.appendChild(patchBlock);

    return entry;
  }

  return { refresh };
}
