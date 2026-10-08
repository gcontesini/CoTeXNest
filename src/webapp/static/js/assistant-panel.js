/**
 * assistant-panel.js
 *
 * Contents of the bottom "AI assistant" dock: a Chat tab (conversation
 * log + input, where every assistant reply that proposes a file
 * change renders as a card the user must explicitly Accept or Reject)
 * and a Settings tab (provider/model/API key, writing-style samples).
 *
 * This module owns only the dock's two panel bodies (passed in as
 * `chatLogContainer`/`chatForm`/`chatInput`/`settingsContainer`); it
 * does not know about the dock's own open/close/tab-switching chrome,
 * which app.js handles -- the same split used by history-panel.js and
 * packages-panel.js for the slide-in panels.
 *
 * Also runs the 15-second auto-scan: while a project/file is open and
 * its content has changed since the last scan, it asks the assistant
 * "anything worth improving?" and, if it proposes something, appends
 * it to the chat log exactly like a manual turn would. A scan that
 * fails (provider not configured/reachable) fails silently -- it must
 * never surface a toast every 15 seconds just because the user hasn't
 * set up a provider yet.
 */

import * as api from "./api.js";

const AUTO_SCAN_INTERVAL_MS = 15000;

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

function describeProposal(proposal) {
  switch (proposal.action) {
    case "write_file":
      return `Update ${proposal.path}`;
    case "create_entry":
      return `Create ${proposal.entry_type === "dir" ? "folder" : "file"} ${proposal.path}`;
    case "delete_entry":
      return `Delete ${proposal.path}`;
    case "rename_entry":
      return `Move "${proposal.path}" to "${proposal.new_path}"`;
    default:
      return proposal.action;
  }
}

/**
 * Create and wire up the assistant dock's chat log/input and settings
 * form, and start the 15-second auto-scan loop.
 *
 * @param {object} options
 * @param {Element} options.chatLogContainer
 * @param {HTMLFormElement} options.chatForm
 * @param {HTMLTextAreaElement} options.chatInput
 * @param {Element} options.settingsContainer
 * @param {(message: string, kind?: "info"|"success"|"error") => void} options.notify
 * @param {() => string | null} options.getProjectId
 * @param {() => string | null} options.getCurrentFilePath
 * @param {() => string | null} options.getCurrentEditorContent
 * @param {(proposal: object) => void} [options.onProposalApplied] -
 *   Called after a proposal is successfully applied, so the caller
 *   can refresh the file tree/editor if the change touched the
 *   currently-open file.
 * @returns {{ refreshSettings: () => Promise<void>, askAboutCompileError: (logText: string) => Promise<void> }}
 */
export function createAssistantPanel({
  chatLogContainer,
  chatForm,
  chatInput,
  settingsContainer,
  notify,
  getProjectId,
  getCurrentFilePath,
  getCurrentEditorContent,
  onProposalApplied,
}) {
  const conversation = [];

  function scrollChatToBottom() {
    chatLogContainer.scrollTop = chatLogContainer.scrollHeight;
  }

  function renderProposalCard(proposal) {
    const card = createElement("div", "proposal-card");
    card.appendChild(createElement("div", "proposal-card-title", describeProposal(proposal)));

    if (proposal.description) {
      card.appendChild(createElement("div", "proposal-card-path", proposal.description));
    }

    if (proposal.content && proposal.action !== "delete_entry") {
      const pre = document.createElement("pre");
      pre.textContent = proposal.content;
      card.appendChild(pre);
    }

    const actions = createElement("div", "proposal-card-actions");
    const acceptButton = createElement("button", "btn btn-primary", "Accept");
    acceptButton.type = "button";
    const rejectButton = createElement("button", "btn", "Reject");
    rejectButton.type = "button";

    acceptButton.addEventListener("click", async () => {
      const projectId = getProjectId();

      if (!projectId) {
        notify("Open a project before applying a change.", "error");
        return;
      }

      acceptButton.disabled = true;
      rejectButton.disabled = true;

      try {
        const result = await api.assistantApply(projectId, proposal);
        actions.remove();
        card.appendChild(
          createElement(
            "div",
            "proposal-card-status",
            `Applied (commit ${result.commit_id.slice(0, 7)}).`,
          ),
        );
        notify("Applied the assistant's change.", "success");
        onProposalApplied?.(proposal);
      } catch (error) {
        notify(`Could not apply change: ${error.message}`, "error");
        acceptButton.disabled = false;
        rejectButton.disabled = false;
      }
    });

    rejectButton.addEventListener("click", () => {
      actions.remove();
      card.appendChild(createElement("div", "proposal-card-status", "Dismissed."));
    });

    actions.append(acceptButton, rejectButton);
    card.appendChild(actions);

    return card;
  }

  function appendMessage({ role, content, proposals = [], auto = false }) {
    chatLogContainer.querySelector(".empty-state")?.remove();

    const bubble = createElement(
      "div",
      `chat-message chat-message-${role}${auto ? " chat-message-auto" : ""}`,
    );

    if (auto) {
      bubble.appendChild(createElement("span", "chat-message-label", "Auto-suggested"));
    }

    bubble.appendChild(document.createTextNode(content));

    for (const proposal of proposals) {
      bubble.appendChild(renderProposalCard(proposal));
    }

    chatLogContainer.appendChild(bubble);
    scrollChatToBottom();
  }

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const text = chatInput.value.trim();
    if (!text) {
      return;
    }

    const projectId = getProjectId();
    if (!projectId) {
      notify("Open a project before asking the assistant.", "error");
      return;
    }

    appendMessage({ role: "user", content: text });
    conversation.push({ role: "user", content: text });
    chatInput.value = "";

    const submitButton = chatForm.querySelector('button[type="submit"]');
    submitButton.disabled = true;

    try {
      const result = await api.assistantChat(
        projectId,
        conversation,
        getCurrentFilePath(),
      );
      conversation.push({ role: "assistant", content: result.reply });
      appendMessage({
        role: "assistant",
        content: result.reply,
        proposals: result.proposals,
      });
    } catch (error) {
      notify(`Assistant request failed: ${error.message}`, "error");
    } finally {
      submitButton.disabled = false;
    }
  });

  async function askAboutCompileError(logText) {
    const projectId = getProjectId();
    const mainFile = getCurrentFilePath();

    if (!projectId || !mainFile) {
      notify("Open a project and file before asking the assistant.", "error");
      return;
    }

    appendMessage({
      role: "user",
      content: "The project failed to compile. Can you help fix it?",
    });

    try {
      const result = await api.assistantFixCompileError(projectId, logText, mainFile);
      appendMessage({
        role: "assistant",
        content: result.suggestion,
        proposals: result.proposal ? [result.proposal] : [],
      });
    } catch (error) {
      notify(`Assistant request failed: ${error.message}`, "error");
    }
  }

  // ─── Settings ───

  async function refreshSettings() {
    settingsContainer.replaceChildren();

    let cfg;

    try {
      cfg = await api.getAssistantConfig();
    } catch (error) {
      settingsContainer.appendChild(
        createElement("p", "empty-state", `Could not load settings: ${error.message}`),
      );
      return;
    }

    const wrapper = createElement("div", "assistant-settings");

    // Provider choice
    const providerSection = createElement("div", "assistant-settings-section");
    providerSection.appendChild(createElement("h3", null, "Provider"));

    const radioRow = createElement("div", "radio-row");
    const localLabel = document.createElement("label");
    const localRadio = document.createElement("input");
    localRadio.type = "radio";
    localRadio.name = "assistant-provider";
    localRadio.value = "local";
    localRadio.checked = cfg.provider === "local";
    localLabel.append(localRadio, " Local (Ollama)");

    const externalLabel = document.createElement("label");
    const externalRadio = document.createElement("input");
    externalRadio.type = "radio";
    externalRadio.name = "assistant-provider";
    externalRadio.value = "external";
    externalRadio.checked = cfg.provider === "external";
    externalLabel.append(externalRadio, " External API");

    radioRow.append(localLabel, externalLabel);
    providerSection.appendChild(radioRow);

    // Local provider fields
    const localFields = createElement("div", "form-row");
    localFields.classList.toggle("hidden", cfg.provider !== "local");
    localFields.appendChild(
      createElement(
        "label",
        null,
        "Model (pick one, or type a new Ollama model tag to pull it on first use)",
      ),
    );
    const localModelInput = document.createElement("input");
    localModelInput.type = "text";
    localModelInput.value = cfg.local_model;
    localModelInput.setAttribute("list", "assistant-local-model-options");
    const modelDatalist = document.createElement("datalist");
    modelDatalist.id = "assistant-local-model-options";

    try {
      const models = await api.listAssistantModels();
      for (const model of models) {
        const option = document.createElement("option");
        option.value = model.id;
        modelDatalist.appendChild(option);
      }
    } catch {
      // Best-effort: an empty datalist just means free-typing only.
    }

    localFields.append(localModelInput, modelDatalist);
    providerSection.appendChild(localFields);

    // External provider fields
    const externalFields = createElement("div", "panel-section");
    externalFields.classList.toggle("hidden", cfg.provider !== "external");

    const kindRow = createElement("div", "form-row");
    kindRow.appendChild(createElement("label", null, "API style"));
    const kindSelect = document.createElement("select");
    for (const [value, label] of [
      ["anthropic", "Anthropic"],
      ["openai", "OpenAI-compatible"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = cfg.external_provider_kind === value;
      kindSelect.appendChild(option);
    }
    kindRow.appendChild(kindSelect);

    const baseUrlRow = createElement("div", "form-row");
    baseUrlRow.appendChild(
      createElement("label", null, "Base URL (leave blank for the provider's default)"),
    );
    const baseUrlInput = document.createElement("input");
    baseUrlInput.type = "text";
    baseUrlInput.value = cfg.external_base_url ?? "";
    baseUrlRow.appendChild(baseUrlInput);

    const modelRow = createElement("div", "form-row");
    modelRow.appendChild(createElement("label", null, "Model"));
    const externalModelInput = document.createElement("input");
    externalModelInput.type = "text";
    externalModelInput.value = cfg.external_model ?? "";
    modelRow.appendChild(externalModelInput);

    const apiKeyRow = createElement("div", "form-row");
    apiKeyRow.appendChild(
      createElement(
        "label",
        null,
        cfg.has_external_api_key ? "API key (configured — leave blank to keep it)" : "API key",
      ),
    );
    const apiKeyInput = document.createElement("input");
    apiKeyInput.type = "password";
    apiKeyInput.autocomplete = "off";
    apiKeyRow.appendChild(apiKeyInput);

    if (cfg.has_external_api_key) {
      const clearKeyButton = createElement("button", "btn btn-icon", "Clear key");
      clearKeyButton.type = "button";
      clearKeyButton.addEventListener("click", async () => {
        try {
          await api.updateAssistantConfig({ clear_external_api_key: true });
          notify("API key cleared.", "success");
          await refreshSettings();
        } catch (error) {
          notify(`Could not clear key: ${error.message}`, "error");
        }
      });
      apiKeyRow.appendChild(clearKeyButton);
    }

    externalFields.append(kindRow, baseUrlRow, modelRow, apiKeyRow);
    providerSection.appendChild(externalFields);

    localRadio.addEventListener("change", () => {
      localFields.classList.remove("hidden");
      externalFields.classList.add("hidden");
    });
    externalRadio.addEventListener("change", () => {
      localFields.classList.add("hidden");
      externalFields.classList.remove("hidden");
    });

    const saveButton = createElement("button", "btn btn-primary", "Save provider settings");
    saveButton.type = "button";
    saveButton.addEventListener("click", async () => {
      const update = {
        provider: localRadio.checked ? "local" : "external",
      };

      if (update.provider === "local") {
        update.local_model = localModelInput.value.trim();
      } else {
        update.external_provider_kind = kindSelect.value;
        update.external_base_url = baseUrlInput.value.trim() || null;
        update.external_model = externalModelInput.value.trim();
        if (apiKeyInput.value.trim()) {
          update.external_api_key = apiKeyInput.value.trim();
        }
      }

      saveButton.disabled = true;

      try {
        await api.updateAssistantConfig(update);
        notify("Assistant settings saved.", "success");
        await refreshSettings();
      } catch (error) {
        notify(`Could not save settings: ${error.message}`, "error");
        saveButton.disabled = false;
      }
    });

    providerSection.appendChild(saveButton);
    wrapper.appendChild(providerSection);

    // Writing style
    const styleSection = createElement("div", "assistant-settings-section");
    styleSection.appendChild(createElement("h3", null, "Writing style"));

    if (cfg.writing_style_profile) {
      styleSection.appendChild(
        createElement("p", null, `Current profile: ${cfg.writing_style_profile}`),
      );
    } else {
      styleSection.appendChild(
        createElement("p", "empty-state", "No writing style learned yet."),
      );
    }

    const samplesContainer = createElement("div", "form-row");
    const sampleTextareas = [];

    function addSampleField() {
      const textarea = document.createElement("textarea");
      textarea.className = "writing-style-sample";
      textarea.placeholder = "Paste a writing sample…";
      sampleTextareas.push(textarea);
      samplesContainer.appendChild(textarea);
    }

    addSampleField();

    const addSampleButton = createElement("button", "btn btn-icon", "+ Add another sample");
    addSampleButton.type = "button";
    addSampleButton.addEventListener("click", () => addSampleField());

    const learnButton = createElement("button", "btn btn-primary", "Learn My Writing Style");
    learnButton.type = "button";
    learnButton.addEventListener("click", async () => {
      const samples = sampleTextareas.map((textarea) => textarea.value.trim()).filter(Boolean);

      if (samples.length === 0) {
        notify("Paste at least one writing sample first.", "error");
        return;
      }

      learnButton.disabled = true;

      try {
        await api.learnWritingStyle(samples);
        notify("Learned your writing style.", "success");
        await refreshSettings();
      } catch (error) {
        notify(`Could not learn writing style: ${error.message}`, "error");
      } finally {
        learnButton.disabled = false;
      }
    });

    styleSection.append(samplesContainer, addSampleButton, learnButton);
    wrapper.appendChild(styleSection);

    settingsContainer.appendChild(wrapper);
  }

  // ─── Auto-scan (every 15s, only when content actually changed) ───

  let lastScannedContent = null;
  let autoScanBusy = false;

  async function runAutoScanTick() {
    if (autoScanBusy) {
      return;
    }

    const projectId = getProjectId();
    const filePath = getCurrentFilePath();
    const content = getCurrentEditorContent();

    if (!projectId || !filePath || content === null || content === undefined) {
      return;
    }

    if (content === lastScannedContent) {
      return;
    }

    lastScannedContent = content;
    autoScanBusy = true;

    try {
      const result = await api.assistantSuggest(projectId, filePath);

      if (result.proposals.length > 0) {
        appendMessage({
          role: "assistant",
          content: result.reply,
          proposals: result.proposals,
          auto: true,
        });
      }
    } catch (error) {
      console.warn("assistant auto-scan failed:", error.message);
    } finally {
      autoScanBusy = false;
    }
  }

  setInterval(runAutoScanTick, AUTO_SCAN_INTERVAL_MS);

  return { refreshSettings, askAboutCompileError };
}
