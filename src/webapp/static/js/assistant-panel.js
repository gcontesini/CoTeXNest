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
const MODEL_PULL_POLL_INTERVAL_MS = 1500;

// Convenience presets on top of the existing "API style" (anthropic |
// openai-compatible) + base URL fields -- every one of these providers
// speaks an OpenAI-compatible chat-completions API, so no new backend
// integration is needed, just a base URL filled in for you. Base URLs
// confirmed against each provider's own docs; Perplexity's is left
// blank below because three separate checks of their own docs
// returned three different answers -- confirm the current one at
// https://docs.perplexity.ai before filling it in.
const EXTERNAL_PROVIDER_PRESETS = [
  { value: "anthropic", label: "Anthropic (Claude)", kind: "anthropic", baseUrl: null },
  { value: "openai", label: "OpenAI", kind: "openai", baseUrl: null },
  {
    value: "deepseek",
    label: "DeepSeek",
    kind: "openai",
    baseUrl: "https://api.deepseek.com",
  },
  {
    value: "kimi",
    label: "Kimi (Moonshot AI)",
    kind: "openai",
    baseUrl: "https://api.moonshot.ai/v1",
  },
  {
    value: "qwen",
    label: "Qwen (Alibaba DashScope, international)",
    kind: "openai",
    baseUrl: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
  },
  {
    value: "gemini",
    label: "Gemini (Google, OpenAI-compatible)",
    kind: "openai",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
  },
  {
    value: "nvidia",
    label: "NVIDIA NIM",
    kind: "openai",
    baseUrl: "https://integrate.api.nvidia.com/v1",
  },
  {
    value: "perplexity",
    label: "Perplexity (confirm current base URL in their docs)",
    kind: "openai",
    baseUrl: null,
  },
  { value: "custom", label: "Custom OpenAI-compatible endpoint", kind: "openai", baseUrl: null },
];

function presetForKindAndBaseUrl(kind, baseUrl) {
  return (
    EXTERNAL_PROVIDER_PRESETS.find(
      (preset) => preset.kind === kind && preset.baseUrl && preset.baseUrl === baseUrl,
    ) ?? EXTERNAL_PROVIDER_PRESETS.find((preset) => preset.kind === kind) // generic "openai"/"anthropic"
  );
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) {
    return null;
  }

  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return null;
  }

  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return minutes > 0 ? `${minutes}m ${secs}s` : `${secs}s`;
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
 * @param {(message: string) => void} [options.onError] - Called for
 *   chat, compile-fix, and auto-scan failures, in addition to the
 *   user-facing toast -- lets the caller keep a record (e.g. the Log
 *   Files panel) without this module knowing that panel exists.
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
  onError,
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

  /**
   * Show a "Thinking…" placeholder while a request is in flight, so a
   * slow reply (a cold-start model pull can take minutes) reads as
   * "working" rather than "did nothing happen?".
   *
   * @returns {Element} The placeholder bubble -- call `.remove()` on
   *   it once the real reply (or an error) has been handled.
   */
  function appendPendingIndicator() {
    chatLogContainer.querySelector(".empty-state")?.remove();

    const bubble = createElement(
      "div",
      "chat-message chat-message-assistant chat-message-pending",
      "Thinking…",
    );

    chatLogContainer.appendChild(bubble);
    scrollChatToBottom();

    return bubble;
  }

  chatInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) {
      return;
    }

    event.preventDefault();

    if (!chatForm.querySelector('button[type="submit"]').disabled) {
      chatForm.requestSubmit();
    }
  });

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
    const pendingBubble = appendPendingIndicator();

    try {
      const result = await api.assistantChat(
        projectId,
        conversation,
        getCurrentFilePath(),
      );
      conversation.push({ role: "assistant", content: result.reply });
      pendingBubble.remove();
      appendMessage({
        role: "assistant",
        content: result.reply,
        proposals: result.proposals,
      });
      if (result.provider_notice) {
        notify(result.provider_notice, "info");
      }
    } catch (error) {
      pendingBubble.remove();
      notify(`Assistant request failed: ${error.message}`, "error");
      onError?.(error.message);
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

    const pendingBubble = appendPendingIndicator();

    try {
      const result = await api.assistantFixCompileError(projectId, logText, mainFile);
      pendingBubble.remove();
      appendMessage({
        role: "assistant",
        content: result.suggestion,
        proposals: result.proposal ? [result.proposal] : [],
      });
      if (result.provider_notice) {
        notify(result.provider_notice, "info");
      }
    } catch (error) {
      pendingBubble.remove();
      notify(`Assistant request failed: ${error.message}`, "error");
      onError?.(error.message);
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
    const localFields = createElement("div", "panel-section");
    localFields.classList.toggle("hidden", cfg.provider !== "local");

    const localModelRow = createElement("div", "form-row");
    localModelRow.appendChild(
      createElement(
        "label",
        null,
        "Model -- paste any tag exactly as shown on https://ollama.com/library " +
          "(e.g. qwen2.5-coder:3b), or pick one already pulled",
      ),
    );
    const localModelInput = document.createElement("input");
    localModelInput.type = "text";
    localModelInput.value = cfg.local_model ?? "";
    localModelInput.placeholder = "e.g. qwen2.5-coder:3b";
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

    localModelRow.append(localModelInput, modelDatalist);
    localFields.appendChild(localModelRow);

    if (!cfg.local_model) {
      localFields.appendChild(
        createElement(
          "p",
          "empty-state",
          "No model configured yet -- the assistant won't do anything " +
            "until you pull one here or add an external API key below.",
        ),
      );
    }

    // Pull progress (item: download progress bar + ETA + ready pop-up)
    const pullRow = createElement("div", "model-pull-row");
    const pullButton = createElement("button", "btn btn-icon", "Pull model");
    pullButton.type = "button";
    const pullProgressBar = document.createElement("progress");
    pullProgressBar.max = 100;
    pullProgressBar.value = 0;
    pullProgressBar.classList.add("hidden");
    const pullStatusLabel = createElement("span", "model-pull-status", "");
    pullRow.append(pullButton, pullProgressBar, pullStatusLabel);
    localFields.appendChild(pullRow);

    let pullPollTimer = null;

    pullButton.addEventListener("click", async () => {
      const modelName = localModelInput.value.trim();

      if (!modelName) {
        notify(
          "Type a model name first (paste one from https://ollama.com/library).",
          "error",
        );
        return;
      }

      pullButton.disabled = true;
      pullProgressBar.classList.remove("hidden");
      pullProgressBar.removeAttribute("value"); // indeterminate until we see byte counts
      pullStatusLabel.textContent = "starting…";

      try {
        await api.pullAssistantModel(modelName);
      } catch (error) {
        notify(`Could not start pulling the model: ${error.message}`, "error");
        pullButton.disabled = false;
        pullProgressBar.classList.add("hidden");
        return;
      }

      let lastSample = null;

      pullPollTimer = setInterval(async () => {
        let status;

        try {
          status = await api.getAssistantModelPullStatus(modelName);
        } catch {
          return; // transient poll failure -- try again next tick
        }

        if (status.total_bytes && status.completed_bytes !== null) {
          pullProgressBar.value = Math.min(
            100,
            (status.completed_bytes / status.total_bytes) * 100,
          );

          let etaText = "";
          const now = Date.now();

          if (lastSample) {
            const deltaBytes = status.completed_bytes - lastSample.bytes;
            const deltaMs = now - lastSample.time;

            if (deltaBytes > 0 && deltaMs > 0) {
              const remainingBytes = status.total_bytes - status.completed_bytes;
              const etaSeconds = (remainingBytes / deltaBytes) * (deltaMs / 1000);
              const formatted = formatEta(etaSeconds);
              if (formatted) {
                etaText = ` -- ETA ${formatted}`;
              }
            }
          }

          lastSample = { bytes: status.completed_bytes, time: now };
          pullStatusLabel.textContent =
            `${status.status} (${formatBytes(status.completed_bytes)} / ` +
            `${formatBytes(status.total_bytes)})${etaText}`;
        } else {
          pullStatusLabel.textContent = status.status || "working…";
        }

        if (status.done) {
          clearInterval(pullPollTimer);
          pullPollTimer = null;
          pullButton.disabled = false;
          pullProgressBar.classList.add("hidden");

          if (status.error) {
            notify(`Model pull failed: ${status.error}`, "error");
          } else {
            notify(`Model "${modelName}" is ready.`, "success");
            await refreshSettings();
          }
        }
      }, MODEL_PULL_POLL_INTERVAL_MS);
    });

    providerSection.appendChild(localFields);

    // External provider fields
    const externalFields = createElement("div", "panel-section");
    externalFields.classList.toggle("hidden", cfg.provider !== "external");

    const presetRow = createElement("div", "form-row");
    presetRow.appendChild(createElement("label", null, "Provider"));
    const presetSelect = document.createElement("select");
    for (const preset of EXTERNAL_PROVIDER_PRESETS) {
      const option = document.createElement("option");
      option.value = preset.value;
      option.textContent = preset.label;
      presetSelect.appendChild(option);
    }
    presetRow.appendChild(presetSelect);

    const kindRow = createElement("div", "form-row hidden");
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

    const currentPreset = presetForKindAndBaseUrl(
      cfg.external_provider_kind,
      cfg.external_base_url,
    );
    if (currentPreset) {
      presetSelect.value = currentPreset.value;
    }

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

    presetSelect.addEventListener("change", () => {
      const preset = EXTERNAL_PROVIDER_PRESETS.find((p) => p.value === presetSelect.value);
      if (!preset) {
        return;
      }
      kindSelect.value = preset.kind;
      baseUrlInput.value = preset.baseUrl ?? "";
      // A model id from one provider is essentially never a model id
      // another provider recognizes -- switching the preset without
      // clearing this would silently let a stale, mismatched model
      // get saved alongside the new base URL (observed live: Gemini's
      // base URL saved together with NVIDIA's leftover model id,
      // which fails every request with an opaque "400 Bad Request"
      // until someone notices the mismatch).
      externalModelInput.value = "";
    });

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

    externalFields.append(presetRow, kindRow, baseUrlRow, modelRow, apiKeyRow);
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

    // Fallback providers -- tried in order, automatically, whenever
    // the active external provider comes back rate-limited/out of
    // quota (see generate_completion's failover in assistant.py).
    const fallbackSection = createElement("div", "assistant-settings-section");
    fallbackSection.appendChild(createElement("h3", null, "Fallback providers"));
    fallbackSection.appendChild(
      createElement(
        "p",
        null,
        "Tried in order, automatically, whenever the active external " +
          "provider is rate-limited or out of quota -- the one that " +
          "works becomes the new active provider.",
      ),
    );

    if (cfg.fallback_profiles.length === 0) {
      fallbackSection.appendChild(
        createElement("p", "empty-state", "No fallback providers configured."),
      );
    } else {
      const list = createElement("div", "fallback-profile-list");
      for (const profile of cfg.fallback_profiles) {
        const row = createElement("div", "fallback-profile-row");
        row.appendChild(
          createElement(
            "span",
            null,
            `${profile.label} (${profile.provider_kind}, ${profile.model})`,
          ),
        );
        const removeButton = createElement("button", "btn btn-icon", "Remove");
        removeButton.type = "button";
        removeButton.addEventListener("click", async () => {
          try {
            await api.removeAssistantFallbackProvider(profile.id);
            notify("Fallback provider removed.", "success");
            await refreshSettings();
          } catch (error) {
            notify(`Could not remove fallback provider: ${error.message}`, "error");
          }
        });
        row.appendChild(removeButton);
        list.appendChild(row);
      }
      fallbackSection.appendChild(list);
    }

    const fallbackPresetRow = createElement("div", "form-row");
    fallbackPresetRow.appendChild(createElement("label", null, "Add fallback -- provider"));
    const fallbackPresetSelect = document.createElement("select");
    for (const preset of EXTERNAL_PROVIDER_PRESETS) {
      const option = document.createElement("option");
      option.value = preset.value;
      option.textContent = preset.label;
      fallbackPresetSelect.appendChild(option);
    }
    fallbackPresetRow.appendChild(fallbackPresetSelect);

    const fallbackLabelRow = createElement("div", "form-row");
    fallbackLabelRow.appendChild(createElement("label", null, "Label"));
    const fallbackLabelInput = document.createElement("input");
    fallbackLabelInput.type = "text";
    fallbackLabelInput.placeholder = "e.g. DeepSeek backup";
    fallbackLabelRow.appendChild(fallbackLabelInput);

    const fallbackBaseUrlRow = createElement("div", "form-row");
    fallbackBaseUrlRow.appendChild(
      createElement("label", null, "Base URL (leave blank for the provider's default)"),
    );
    const fallbackBaseUrlInput = document.createElement("input");
    fallbackBaseUrlInput.type = "text";
    fallbackBaseUrlRow.appendChild(fallbackBaseUrlInput);

    fallbackPresetSelect.addEventListener("change", () => {
      const preset = EXTERNAL_PROVIDER_PRESETS.find(
        (p) => p.value === fallbackPresetSelect.value,
      );
      fallbackBaseUrlInput.value = preset?.baseUrl ?? "";
    });

    const fallbackModelRow = createElement("div", "form-row");
    fallbackModelRow.appendChild(createElement("label", null, "Model"));
    const fallbackModelInput = document.createElement("input");
    fallbackModelInput.type = "text";
    fallbackModelRow.appendChild(fallbackModelInput);

    const fallbackApiKeyRow = createElement("div", "form-row");
    fallbackApiKeyRow.appendChild(createElement("label", null, "API key"));
    const fallbackApiKeyInput = document.createElement("input");
    fallbackApiKeyInput.type = "password";
    fallbackApiKeyInput.autocomplete = "off";
    fallbackApiKeyRow.appendChild(fallbackApiKeyInput);

    const addFallbackButton = createElement("button", "btn btn-icon", "Add fallback provider");
    addFallbackButton.type = "button";
    addFallbackButton.addEventListener("click", async () => {
      const preset = EXTERNAL_PROVIDER_PRESETS.find(
        (p) => p.value === fallbackPresetSelect.value,
      );
      const label = fallbackLabelInput.value.trim() || preset?.label || "fallback";
      const model = fallbackModelInput.value.trim();
      const apiKey = fallbackApiKeyInput.value.trim();

      if (!model || !apiKey) {
        notify("A fallback provider needs at least a model and an API key.", "error");
        return;
      }

      addFallbackButton.disabled = true;

      try {
        await api.addAssistantFallbackProvider({
          label,
          provider_kind: preset?.kind ?? "openai",
          base_url: fallbackBaseUrlInput.value.trim() || null,
          model,
          api_key: apiKey,
        });
        notify("Fallback provider added.", "success");
        await refreshSettings();
      } catch (error) {
        notify(`Could not add fallback provider: ${error.message}`, "error");
        addFallbackButton.disabled = false;
      }
    });

    fallbackSection.append(
      fallbackPresetRow,
      fallbackLabelRow,
      fallbackBaseUrlRow,
      fallbackModelRow,
      fallbackApiKeyRow,
      addFallbackButton,
    );
    wrapper.appendChild(fallbackSection);

    // Writing style
    const styleSection = createElement("div", "assistant-settings-section");
    styleSection.appendChild(createElement("h3", null, "Writing style"));

    if (cfg.writing_style_profile) {
      styleSection.appendChild(
        createElement("p", null, `Current profile: ${cfg.writing_style_profile}`),
      );

      const deleteStyleButton = createElement("button", "btn btn-icon", "Delete writing style");
      deleteStyleButton.type = "button";
      deleteStyleButton.addEventListener("click", async () => {
        deleteStyleButton.disabled = true;

        try {
          await api.deleteWritingStyle();
          notify("Writing style deleted.", "success");
          await refreshSettings();
        } catch (error) {
          notify(`Could not delete writing style: ${error.message}`, "error");
          deleteStyleButton.disabled = false;
        }
      });
      styleSection.appendChild(deleteStyleButton);
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

  // A provider rate-limit/quota error is the one auto-scan failure
  // worth backing off from rather than just trying again next tick:
  // observed live, a free-tier quota as low as 20 requests/day is
  // real and gets exhausted almost immediately by a 15-second poll
  // loop, after which every retry is guaranteed to fail the same way
  // until the quota resets -- hammering it every 15s the whole time
  // accomplishes nothing and is exactly the kind of avoidable
  // unreliability this was asked to be fixed.
  const AUTO_SCAN_RATE_LIMIT_COOLDOWN_MS = 10 * 60 * 1000;
  const RATE_LIMIT_MESSAGE_PATTERN = /\b429\b|rate.?limit|quota|resource_exhausted/i;

  let lastScannedContent = null;
  let autoScanBusy = false;
  let autoScanCooldownUntil = 0;

  async function runAutoScanTick() {
    if (autoScanBusy || Date.now() < autoScanCooldownUntil) {
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
      onError?.(`Auto-scan: ${error.message}`);

      if (RATE_LIMIT_MESSAGE_PATTERN.test(error.message)) {
        autoScanCooldownUntil = Date.now() + AUTO_SCAN_RATE_LIMIT_COOLDOWN_MS;
        console.warn(
          `assistant auto-scan: backing off for ${AUTO_SCAN_RATE_LIMIT_COOLDOWN_MS / 60000}m after a rate-limit/quota error`,
        );
      }
    } finally {
      autoScanBusy = false;
    }
  }

  setInterval(runAutoScanTick, AUTO_SCAN_INTERVAL_MS);

  return { refreshSettings, askAboutCompileError };
}
