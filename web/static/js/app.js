"use strict";

const uiMessages = JSON.parse(document.querySelector("#ui-text").dataset.messages);
const t = (key) => uiMessages[key] || key;

// Traduce respuestas conocidas del servidor sin alterar los errores no catalogados.
const serverErrorKeys = {
    "La carpeta seleccionada ya no está disponible.": "connect.folder_unavailable",
    "La carpeta del backup ya no está disponible.": "connect.folder_unavailable",
    "La carpeta del backup no está disponible.": "connect.folder_unavailable",
    "No se pudo abrir el selector de carpetas.": "connect.select_failed",
    "Selecciona una vault de Obsidian.": "export.select_obsidian_destination",
    "Selecciona una carpeta de destino HTML.": "export.select_html_destination",
    "Procesa primero el backup seleccionado.": "process.not_processed",
    "No se pudo completar la limpieza.": "clean.failed",
    "Hay una operación en curso; inténtalo de nuevo cuando termine.": "clean.busy",
    "El nombre del perfil no puede estar vacío.": "profile.name_required",
};
const localizedServerError = (message) => serverErrorKeys[message] ? t(serverErrorKeys[message]) : message;

function tf(key, values = {}) {
    return Object.entries(values).reduce(
        (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
        t(key),
    );
}

// Revela cada mensaje sin cambiar la posición de los caracteres durante la animación.
const typewriterStates = new WeakMap();

function setIndicatorText(element, value, animate = false, once = false) {
    const text = String(value ?? "");
    const previous = typewriterStates.get(element);
    if (previous?.text === text && previous.animate === animate && previous.once === once) return;
    if (previous?.timer) clearTimeout(previous.timer);

    if (!animate || !text || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        element.textContent = text;
        typewriterStates.set(element, {text, animate, once});
        return;
    }

    // Cada letra reserva su espacio desde el primer fotograma.
    const letters = Array.from(text, (character) => {
        const span = document.createElement("span");
        span.textContent = character;
        span.style.visibility = "hidden";
        return span;
    });
    const line = document.createElement("span");
    line.append(...letters);
    element.replaceChildren(line);
    let position = 0;
    const state = {text, animate, once, timer: null};
    function writeNext() {
        if (typewriterStates.get(element) !== state) return;
        if (position === letters.length) {
            if (once) return;
            letters.forEach((letter) => { letter.style.visibility = "hidden"; });
            position = 0;
        } else {
            letters[position++].style.visibility = "visible";
        }
        state.timer = setTimeout(writeNext, position === letters.length ? 1000 : 35);
    }
    typewriterStates.set(element, state);
    state.timer = setTimeout(writeNext, 35);
}

function stopIndicatorText(element) {
    const state = typewriterStates.get(element);
    if (state?.timer) clearTimeout(state.timer);
    typewriterStates.delete(element);
}

const languageMenu = document.querySelector(".language-menu");
const languageMenuToggle = document.querySelector("#language-menu-toggle");
const languageMenuOptions = document.querySelector("#language-menu-options");
languageMenuToggle.addEventListener("click", () => {
    const isExpanded = languageMenuToggle.getAttribute("aria-expanded") === "true";
    languageMenuToggle.setAttribute("aria-expanded", String(!isExpanded));
    languageMenuOptions.hidden = isExpanded;
});
document.addEventListener("click", (event) => {
    if (languageMenu.contains(event.target)) return;
    languageMenuToggle.setAttribute("aria-expanded", "false");
    languageMenuOptions.hidden = true;
});
document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || languageMenuOptions.hidden) return;
    languageMenuToggle.setAttribute("aria-expanded", "false");
    languageMenuOptions.hidden = true;
    languageMenuToggle.focus();
});

const liveHelpToggle = document.querySelector("#live-help-toggle");
const liveHelpBubble = document.createElement("div");
liveHelpBubble.id = "live-help-bubble";
liveHelpBubble.className = "live-help-bubble";
liveHelpBubble.setAttribute("role", "tooltip");
liveHelpBubble.hidden = true;
document.body.append(liveHelpBubble);

let liveHelpEnabled = false;
let liveHelpTarget = null;

function hideLiveHelp(target = liveHelpTarget) {
    if (target) target.removeAttribute("aria-describedby");
    liveHelpTarget = null;
    liveHelpBubble.classList.remove("is-visible");
    liveHelpBubble.hidden = true;
}

function showLiveHelp(target) {
    const message = target?.dataset.help;
    if (!liveHelpEnabled || !message || target === liveHelpTarget) return;

    hideLiveHelp();
    liveHelpTarget = target;
    liveHelpBubble.textContent = message;
    liveHelpBubble.hidden = false;
    target.setAttribute("aria-describedby", liveHelpBubble.id);

    requestAnimationFrame(() => {
        if (liveHelpTarget !== target) return;

        const rect = target.getBoundingClientRect();
        const bubble = liveHelpBubble.getBoundingClientRect();

        let top = rect.top - bubble.height - 10;

        if (top < 8) {
            top = rect.bottom + 10;
        }

        const left = Math.min(
            Math.max(8, rect.left + (rect.width - bubble.width) / 2),
            window.innerWidth - bubble.width - 8,
        );

        liveHelpBubble.style.left = `${left}px`;
        liveHelpBubble.style.top = `${top}px`;
        liveHelpBubble.classList.add("is-visible");
    });
}

function setLiveHelp(enabled) {
    liveHelpEnabled = enabled;
    liveHelpToggle.setAttribute("aria-pressed", String(enabled));
    liveHelpToggle.classList.toggle("is-active", enabled);

    if (!enabled) hideLiveHelp();
}

liveHelpToggle.addEventListener("click", () => {
    setLiveHelp(!liveHelpEnabled);
});

document.addEventListener("pointerover", (event) => {
    showLiveHelp(event.target.closest("[data-help]"));
});

document.addEventListener("pointerout", (event) => {
    const target = event.target.closest("[data-help]");
    if (target && !target.contains(event.relatedTarget)) {
        hideLiveHelp(target);
    }
});

document.addEventListener("focusin", (event) => {
    showLiveHelp(event.target.closest("[data-help]"));
});

document.addEventListener("focusout", (event) => {
    const target = event.target.closest("[data-help]");
    if (target && !target.contains(event.relatedTarget)) {
        hideLiveHelp(target);
    }
});

window.addEventListener("resize", () => hideLiveHelp());

const aboutModal = document.querySelector("#about-modal");
const aboutLogo = document.querySelector("#about-logo");
const aboutClose = document.querySelector("#about-close");
const aboutBackdrop = document.querySelector("[data-about-close]");

function closeAboutModal() {
    aboutModal.hidden = true;
}

aboutLogo.addEventListener("click", () => {
    aboutModal.hidden = false;
    aboutClose.focus();
});

aboutClose.addEventListener("click", closeAboutModal);
aboutBackdrop.addEventListener("click", closeAboutModal);

window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !aboutModal.hidden) {
        closeAboutModal();
    }
});

const connectButton = document.querySelector("#choose-backup");
const connectNavButton = document.querySelector('[data-page-target="conectar"]');
const navButtons = [...document.querySelectorAll("[data-page-target]")];
const pages = [...document.querySelectorAll(".module-panel")];
const result = document.querySelector("#connection-result");
const selectedPath = document.querySelector("#selected-path");
const processPath = document.querySelector("#process-path");
const status = document.querySelector("#status");

function setNavigationState(button, state = null) {
    button.classList.remove("state-locked", "state-running", "state-complete", "state-error");
    if (state) button.classList.add(`state-${state}`);
    button.disabled = state === "locked";
}

for (const button of navButtons) {
    button.addEventListener("click", () => {
        const activePage = button.dataset.pageTarget;
        for (const page of pages) page.hidden = page.id !== activePage;
        for (const item of navButtons) {
            if (item === button) item.setAttribute("aria-current", "page");
            else item.removeAttribute("aria-current");
        }
    });
}

connectButton.addEventListener("click", async () => {
    const backupWasConnected = !result.hidden;
    hideProcessActionResult();
    connectButton.disabled = true;
    setNavigationState(connectNavButton, "running");
    status.textContent = t("connect.selecting");
    try {
        const response = await fetch("/api/connect", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || t("connect.select_failed"));
        if (data.cancelled) {
            status.textContent = t("connect.selection_cancelled");
            status.dataset.state = "pending";
            setNavigationState(connectNavButton, backupWasConnected ? "complete" : null);
            return;
        }
        if (selectedPath.textContent !== data.backup_root) resetProcessProjects();
        selectedPath.textContent = data.backup_root;
        processPath.textContent = data.process_root;
        result.hidden = false;
        status.textContent = t("connect.connected");
        status.dataset.state = "complete";
        setNavigationState(connectNavButton, "complete");
        setNavigationState(processNavButton, null);
        setNavigationState(exportNavButton, "locked");
        processStatus.dataset.state = "pending";
        stopIndicatorText(processStatus);
        processStatus.textContent = t("connect.ready_process");
    } catch (error) {
        status.textContent = localizedServerError(error.message);
        status.dataset.state = "error";
        setNavigationState(connectNavButton, "error");
    } finally {
        connectButton.disabled = false;
    }
});

const startProcessButton = document.querySelector("#start-process");
const processNavButton = document.querySelector('[data-page-target="procesar"]');
const processStatus = document.querySelector("#process-status");
const processSummary = document.querySelector("#process-summary");
const processActionResult = document.querySelector("#process-action-result");
const analyzeBackupButton = document.querySelector("#analyze-backup");
const processProjectSelection = document.querySelector("#process-project-selection");
const processProjectList = document.querySelector("#process-project-list");
const processProjectLoadingStatus = document.querySelector("#process-project-loading-status");
const processProjectSelectAll = document.querySelector("#process-project-select-all");
const processProjectSelectNone = document.querySelector("#process-project-select-none");
const selectedProjectIds = new Set();
const projectCheckboxIds = new Map();
const projectStateLabels = {
    new: t("process.state_new"),
    modified: t("process.state_modified"),
    unchanged: t("process.state_unchanged"),
};
let analyzePollTimer;
let analyzeStatusRequest = null;
let analyzeGeneration = 0;
let analyzeBackupPath = null;
let previousProcessNavState = null;

analyzeBackupButton.after(processProjectLoadingStatus);
processProjectLoadingStatus.hidden = true;

function processNavigationState() {
    for (const state of ["running", "complete", "error", "locked"]) {
        if (processNavButton.classList.contains(`state-${state}`)) return state;
    }
    return null;
}

function restoreProcessNavigation() {
    if (processNavButton.classList.contains("state-running")) {
        setNavigationState(processNavButton, previousProcessNavState);
    }
    previousProcessNavState = null;
}

function resetProcessProjects() {
    analyzeGeneration += 1;
    clearTimeout(analyzePollTimer);
    analyzeStatusRequest = null;
    analyzeBackupPath = null;
    selectedProjectIds.clear();
    projectCheckboxIds.clear();
    processProjectList.replaceChildren();
    processProjectSelection.hidden = true;
    analyzeBackupButton.disabled = false;
    processProjectLoadingStatus.dataset.state = "pending";
    stopIndicatorText(processProjectLoadingStatus);
    processProjectLoadingStatus.textContent = "";
    processProjectLoadingStatus.hidden = true;
    previousProcessNavState = null;
}

function renderProcessProjects(projects, savedProjectIds = null) {
    selectedProjectIds.clear();
    projectCheckboxIds.clear();
    const hasSavedSelection = Array.isArray(savedProjectIds);
    const savedIds = hasSavedSelection ? new Set(savedProjectIds) : null;
    const rows = document.createDocumentFragment();
    const sortedProjects = [...projects].sort((left, right) => {
        if (left.project_id === null) return right.project_id === null ? 0 : 1;
        if (right.project_id === null) return -1;
        return String(left.name ?? "").localeCompare(String(right.name ?? ""), "es", {
            sensitivity: "base",
        });
    });
    for (const project of sortedProjects) {
        const label = document.createElement("label");
        label.className = "export-checkbox";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !hasSavedSelection || savedIds.has(project.project_id);
        const description = document.createElement("span");
        const name = project.project_id === null ? t("process.unassigned") : project.name;
        description.textContent = `${name} (${project.conversation_count}) · `;
        const state = document.createElement("span");
        state.className = "project-state";
        state.dataset.state = project.state;
        state.textContent = projectStateLabels[project.state];
        description.append(state);
        label.append(checkbox, description);
        rows.append(label);
        projectCheckboxIds.set(checkbox, project.project_id);
        if (checkbox.checked) selectedProjectIds.add(project.project_id);
    }
    processProjectList.replaceChildren(rows);
}

function showAnalyzeRunning() {
    if (!analyzeBackupButton.disabled) previousProcessNavState = processNavigationState();
    analyzeBackupButton.disabled = true;
    processProjectSelection.hidden = true;
    processProjectLoadingStatus.hidden = false;
    processProjectLoadingStatus.dataset.state = "working";
    setIndicatorText(processProjectLoadingStatus, t("process.analyzing_backup"), true);
    setNavigationState(processNavButton, "running");
}

function showAnalyzeError(message) {
    clearTimeout(analyzePollTimer);
    analyzeBackupButton.disabled = false;
    processProjectSelection.hidden = true;
    processProjectLoadingStatus.hidden = false;
    processProjectLoadingStatus.dataset.state = "error";
    stopIndicatorText(processProjectLoadingStatus);
    processProjectLoadingStatus.textContent = message;
    restoreProcessNavigation();
}

function renderAnalyzeJob(job) {
    clearTimeout(analyzePollTimer);
    if (job.status === "running") {
        showAnalyzeRunning();
        analyzePollTimer = setTimeout(refreshAnalyzeJob, 1000);
        return;
    }
    if (job.status === "completed") {
        if (!Array.isArray(job.result?.projects)) {
            showAnalyzeError(t("process.analysis_invalid"));
            return;
        }
        renderProcessProjects(job.result.projects, job.result.selected_project_ids);
        processProjectSelection.hidden = false;
        analyzeBackupButton.disabled = false;
        processProjectLoadingStatus.hidden = false;
        processProjectLoadingStatus.dataset.state = "complete";
        stopIndicatorText(processProjectLoadingStatus);
        processProjectLoadingStatus.textContent = t("process.analysis_completed");
        restoreProcessNavigation();
        return;
    }
    if (job.status === "error" || job.status === "partial") {
        showAnalyzeError(localizedServerError(job.error) || t("process.analysis_failed"));
    } else if (job.status === "idle" && analyzeBackupButton.disabled) {
        showAnalyzeError(t("process.analysis_stopped"));
    }
}

function refreshAnalyzeJob() {
    if (result.hidden || analyzeStatusRequest) return analyzeStatusRequest;
    const generation = analyzeGeneration;
    const backupPath = selectedPath.textContent;
    const request = (async () => {
        try {
            const response = await fetch("/api/analyze/status");
            const job = await response.json();
            if (!response.ok) throw new Error(job.detail || t("process.analysis_status_failed"));
            if (generation !== analyzeGeneration || backupPath !== selectedPath.textContent) return;
            renderAnalyzeJob(job);
        } catch (error) {
            if (generation === analyzeGeneration && backupPath === selectedPath.textContent) {
                showAnalyzeError(localizedServerError(error.message));
            }
        }
    })();
    analyzeStatusRequest = request;
    request.finally(() => {
        if (analyzeStatusRequest === request) analyzeStatusRequest = null;
    });
    return request;
}

processProjectList.addEventListener("change", (event) => {
    const checkbox = event.target;
    if (!projectCheckboxIds.has(checkbox)) return;
    const projectId = projectCheckboxIds.get(checkbox);
    if (checkbox.checked) selectedProjectIds.add(projectId);
    else selectedProjectIds.delete(projectId);
});

processProjectSelectAll.addEventListener("click", () => {
    for (const [checkbox, projectId] of projectCheckboxIds) {
        checkbox.checked = true;
        selectedProjectIds.add(projectId);
    }
});

processProjectSelectNone.addEventListener("click", () => {
    for (const checkbox of projectCheckboxIds.keys()) {
        checkbox.checked = false;
    }
    selectedProjectIds.clear();
});

analyzeBackupButton.addEventListener("click", async () => {
    if (analyzeBackupButton.disabled || result.hidden) return;
    analyzeGeneration += 1;
    clearTimeout(analyzePollTimer);
    analyzeStatusRequest = null;
    analyzeBackupPath = selectedPath.textContent;
    showAnalyzeRunning();
    const generation = analyzeGeneration;
    try {
        const response = await fetch("/api/analyze", { method: "POST" });
        const job = await response.json();
        if (!response.ok) throw new Error(job.detail || t("process.analysis_start_failed"));
        if (generation !== analyzeGeneration || analyzeBackupPath !== selectedPath.textContent) return;
        renderAnalyzeJob(job);
    } catch (error) {
        if (generation === analyzeGeneration && analyzeBackupPath === selectedPath.textContent) {
            showAnalyzeError(localizedServerError(error.message));
        }
    }
});

const initialProcessSummary = t("process.summary");
const processSummaryByStage = {
    analysis: t("process.analysis"),
    conversations: t("process.conversations"),
    resources: t("process.resources"),
    indexes: t("process.indexes"),
};
const processPhase = document.querySelector("#process-phase");
const processPercent = document.querySelector("#process-percent");
const processProgressBar = document.querySelector("#process-progress-bar");
const processStageItems = [...document.querySelectorAll("[data-process-stage]")];
const processCurrentLabel = document.querySelector("#process-current-label");
const processCurrentValue = document.querySelector("#process-current-value");
const processCurrentProject = document.querySelector("#process-current-project");
const processProject = document.querySelector("#process-project");
const processConversations = document.querySelector("#process-conversations");
const processImages = document.querySelector("#process-images");
const processAudio = document.querySelector("#process-audio");
const processVideo = document.querySelector("#process-video");
const processPdf = document.querySelector("#process-pdf");
const processFiles = document.querySelector("#process-files");
const processCopiedResources = document.querySelector("#process-copied-resources");
const processMissingResources = document.querySelector("#process-missing-resources");
const processPhaseElapsed = document.querySelector("#process-phase-elapsed");
const processTotalElapsed = document.querySelector("#process-total-elapsed");
const processPhaseTimes = document.querySelector("#process-phase-times");
let processPollTimer;
let processJobRunning = false;
startProcessButton.textContent = t("process.start");
startProcessButton.disabled = false;
processSummary.textContent = initialProcessSummary;

function hideProcessActionResult() {
    processActionResult.hidden = true;
    processActionResult.textContent = "";
}

const processStageOrder = ["analysis", "conversations", "resources", "indexes"];
const processStageLabels = {
    analysis: t("process.analyzing"),
    conversations: t("process.conversation"),
    resources: t("process.file"),
    indexes: t("process.generating"),
};

const phaseNames = {
    analysis: t("process.phase_analysis"),
    rebuild: t("process.phase_rebuild"),
    normalize: t("process.phase_normalize"),
    resources: t("process.phase_resources"),
    conversation_files: t("process.phase_conversations"),
    index: t("process.phase_index"),
    finalization: t("process.phase_finalization"),
};

function formatElapsed(seconds) {
    const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
    const remainingSeconds = String(totalSeconds % 60).padStart(2, "0");
    return `${hours}:${minutes}:${remainingSeconds}`;
}

function processValue(value, fallback = 0) {
    return value ?? fallback;
}

function renderProcessStages(job) {
    const activeIndex = processStageOrder.indexOf(job.stage);
    for (const item of processStageItems) {
        const itemIndex = processStageOrder.indexOf(item.dataset.processStage);
        let state = "pending";
        if (job.status === "completed" || (activeIndex >= 0 && itemIndex < activeIndex)) state = "complete";
        else if (job.status === "running" && itemIndex === activeIndex) state = "active";
        item.dataset.state = state;
        item.querySelector(".stage-status").textContent = {
            pending: t("process.pending"),
            active: t("process.in_progress"),
            complete: t("process.completed"),
        }[state];
    }
}

function renderCurrentProcessItem(job) {
    processCurrentLabel.textContent = processStageLabels[job.stage] || t("process.item");
    processCurrentValue.textContent = job.current_item || "—";
    processProject.textContent = job.project || "";
    processCurrentProject.hidden = !job.project;
}

function renderProcessProgress(job = {}) {
    const percent = Math.min(100, Math.max(0, Number(job.percent) || 0));
    const phaseKey = `process.phase_label_${job.phase}`;
    const translatedPhase = job.phase ? t(phaseKey) : null;
    processPhase.textContent = translatedPhase && translatedPhase !== phaseKey
        ? translatedPhase
        : t("process.initial_ready");
    processPercent.textContent = `${Math.round(percent)} %`;
    processProgressBar.value = percent;
    renderProcessStages(job);
    renderCurrentProcessItem(job);
    processConversations.textContent = `${processValue(job.current_conversation)} / ${processValue(job.total_conversations)}`;
    processImages.textContent = processValue(job.images);
    processAudio.textContent = processValue(job.audio);
    processVideo.textContent = processValue(job.video);
    processPdf.textContent = processValue(job.pdf);
    processFiles.textContent = processValue(job.files);
    processCopiedResources.textContent = processValue(job.copied_resources);
    processMissingResources.textContent = processValue(job.missing_resources);
    processPhaseElapsed.textContent = formatElapsed(job.phase_elapsed);
    processTotalElapsed.textContent = formatElapsed(job.total_elapsed);
    const durations = Object.entries(job.phase_durations || {})
        .filter(([, elapsed]) => Math.floor(Number(elapsed) || 0) > 0)
        .map(([phase, elapsed]) => `${phaseNames[phase] || phase} ${formatElapsed(elapsed)}`);
    processPhaseTimes.textContent = durations.join(" · ") || "—";
}

function renderProcessJob(job) {
    renderProcessProgress(job);
    processJobRunning = job.status === "running";
    if (job.status === "running") {
        processSummary.textContent = processSummaryByStage[job.stage] || initialProcessSummary;
        startProcessButton.disabled = true;
        setNavigationState(processNavButton, "running");
        setNavigationState(exportNavButton, "locked");
        processStatus.dataset.state = "working";
        const phaseKey = `process.phase_label_${job.phase}`;
        const translatedPhase = job.phase ? t(phaseKey) : null;
        setIndicatorText(processStatus, translatedPhase && translatedPhase !== phaseKey
            ? translatedPhase
            : t("process.processing"), true);
        processPollTimer = setTimeout(refreshProcessJob, 1000);
        return;
    }

    startProcessButton.disabled = processJobRunning;
    if (job.status === "completed") {
        const data = job.result;
        renderProcessProgress({ ...job, percent: 100 });
        setNavigationState(processNavButton, "complete");
        setNavigationState(exportNavButton, null);
        processStatus.dataset.state = "complete";
        stopIndicatorText(processStatus);
        processStatus.textContent = tf("process.completed_summary", {
            projects: data.project_count,
            conversations: data.conversation_count,
            messages: data.message_count,
            resources: data.resource_count,
        }) + (data.missing_resource_count
            ? ` ${tf("process.missing_resources_summary", { count: data.missing_resource_count })}`
            : "");
        processActionResult.textContent = t("process.processed");
        processActionResult.hidden = false;
    } else if (job.status === "error") {
        hideProcessActionResult();
        processSummary.textContent = initialProcessSummary;
        setNavigationState(processNavButton, "error");
        setNavigationState(exportNavButton, "locked");
        processStatus.dataset.state = "error";
        stopIndicatorText(processStatus);
        processStatus.textContent = job.error?.startsWith("backup_invalido:")
            ? localizedServerError(job.error.slice("backup_invalido: ".length))
            : t("process.failed_complete");
    }
}

async function refreshProcessJob() {
    try {
        const response = await fetch("/api/process/status");
        if (!response.ok) throw new Error(t("process.failed_progress"));
        const job = await response.json();
        renderProcessJob(job);
    } catch (error) {
        hideProcessActionResult();
        startProcessButton.disabled = processJobRunning;
        setNavigationState(processNavButton, "error");
        setNavigationState(exportNavButton, "locked");
        processSummary.textContent = initialProcessSummary;
        processStatus.dataset.state = "error";
        stopIndicatorText(processStatus);
        processStatus.textContent = localizedServerError(error.message);
    }
}

startProcessButton.addEventListener("click", async () => {
    hideProcessActionResult();
    if (selectedProjectIds.size === 0) {
        processStatus.dataset.state = "error";
        stopIndicatorText(processStatus);
        processStatus.textContent = t("process.select_project_required");
        return;
    }

    clearTimeout(processPollTimer);
    startProcessButton.disabled = true;
    setNavigationState(processNavButton, "running");
    setNavigationState(exportNavButton, "locked");
    renderProcessProgress();
    processStatus.dataset.state = "working";
    setIndicatorText(processStatus, t("process.starting"), true);
    try {
        const response = await fetch("/api/process", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_ids: Array.from(selectedProjectIds) }),
        });
        const job = await response.json();
        if (!response.ok) throw new Error(job.detail || t("process.failed_start"));
        renderProcessJob(job);
    } catch (error) {
        hideProcessActionResult();
        startProcessButton.disabled = processJobRunning;
        processSummary.textContent = initialProcessSummary;
        processStatus.dataset.state = "error";
        stopIndicatorText(processStatus);
        processStatus.textContent = localizedServerError(error.message);
        setNavigationState(processNavButton, "error");
        setNavigationState(exportNavButton, "locked");
    }
});

refreshProcessJob();

const startExportButton = document.querySelector("#start-export");
const exportProgressSection = document.querySelector("#exportar > .process-progress");
const exportNavButton = document.querySelector('[data-page-target="exportar"]');
const backupConnectedAtLoad = !result.hidden;
setNavigationState(connectNavButton, backupConnectedAtLoad ? "complete" : null);
setNavigationState(processNavButton, backupConnectedAtLoad ? null : "locked");
setNavigationState(exportNavButton, "locked");
if (backupConnectedAtLoad) refreshAnalyzeJob();
const exportTabObsidian = document.querySelector("#export-tab-obsidian");
const exportTabHtml = document.querySelector("#export-tab-html");
const exportPanelObsidian = document.querySelector("#export-panel-obsidian");
const exportPanelHtml = document.querySelector("#export-panel-html");
const exportBackupBefore = document.querySelector("#export-backup-before");
const htmlExportBackupBefore = document.querySelector("#html-export-backup-before");
const htmlFolderPath = document.querySelector("#html-folder-path");
const htmlFolderResult = document.querySelector("#html-folder-result");
const htmlFolderDisplay = document.querySelector("#html-folder-display");
const chooseHtmlFolderButton = document.querySelector("#choose-html-folder");
const backupFolderPath = document.querySelector("#backup-folder");
const backupFolderResult = document.querySelector("#backup-folder-result");
const backupFolderDisplay = document.querySelector("#backup-folder-display");
const chooseBackupFolderButton = document.querySelector("#choose-backup-folder");
const htmlBackupFolderPath = document.querySelector("#html-backup-folder");
const htmlBackupFolderResult = document.querySelector("#html-backup-folder-result");
const htmlBackupFolderDisplay = document.querySelector("#html-backup-folder-display");
const htmlChooseBackupFolderButton = document.querySelector("#html-choose-backup-folder");
const obsidianVaultPath = document.querySelector("#obsidian-vault-path");
const vaultResult = document.querySelector("#vault-result");
const obsidianVaultDisplay = document.querySelector("#obsidian-vault-display");
const chooseObsidianVaultButton = document.querySelector("#choose-obsidian-vault");
const vaultWriteAdd = document.querySelector("#vault-write-add");
const vaultWriteOverwrite = document.querySelector("#vault-write-overwrite");
const obsidianAssistedModal = document.querySelector("#obsidian-assisted-modal");
const obsidianAssistedApiKey = document.querySelector("#obsidian-assisted-api-key");
const obsidianAssistedRemember = document.querySelector("#obsidian-assisted-remember");
const obsidianExportNormalButton = document.querySelector("#obsidian-export-normal");
const obsidianAssistedCancelButton = document.querySelector("#obsidian-assisted-cancel");
const obsidianAssistedConfirmButton = document.querySelector("#obsidian-assisted-confirm");
const obsidianAssistedConfirmText = obsidianAssistedConfirmButton.textContent;
const obsidianRememberWarningModal = document.querySelector("#obsidian-remember-warning-modal");
const obsidianRememberAcceptButton = document.querySelector("#obsidian-remember-accept");
const obsidianRememberCancelButton = document.querySelector("#obsidian-remember-cancel");
if (obsidianAssistedModal.open) obsidianAssistedModal.close();
const htmlVaultWriteAdd = document.querySelector("#html-vault-write-add");
const htmlVaultWriteOverwrite = document.querySelector("#html-vault-write-overwrite");
const exportOptimize = document.querySelector("#export-optimize");
const exportPreserveTransparentPng = document.querySelector("#export-preserve-transparent-png");
const exportConvertM4a = document.querySelector("#export-convert-m4a");
const exportIncludeConversationAudio = document.querySelector("#export-include-conversation-audio");
const exportIncludeUserAttachments = document.querySelector("#export-include-user-attachments");
const exportProjectTag = document.querySelector("#export-project-tag");
const exportCustomRoles = document.querySelector("#export-custom-roles");
const exportUserRole = document.querySelector("#export-user-role");
const exportAssistantRole = document.querySelector("#export-assistant-role");
const htmlExportOptimize = document.querySelector("#html-export-optimize");
const htmlExportPreserveTransparentPng = document.querySelector("#html-export-preserve-transparent-png");
const htmlExportConvertM4a = document.querySelector("#html-export-convert-m4a");
const htmlExportIncludeConversationAudio = document.querySelector("#html-export-include-conversation-audio");
const htmlExportIncludeUserAttachments = document.querySelector("#html-export-include-user-attachments");
const htmlExportProjectTag = document.querySelector("#html-export-project-tag");
const htmlExportCustomRoles = document.querySelector("#html-export-custom-roles");
const htmlExportUserRole = document.querySelector("#html-export-user-role");
const htmlExportAssistantRole = document.querySelector("#html-export-assistant-role");
const exportControls = {
    obsidian: {
        writeAdd: vaultWriteAdd,
        writeOverwrite: vaultWriteOverwrite,
        path: obsidianVaultPath,
        backupBefore: exportBackupBefore,
        backupFolder: backupFolderPath,
        optimize: exportOptimize,
        preservePng: exportPreserveTransparentPng,
        convertM4a: exportConvertM4a,
        includeAudio: exportIncludeConversationAudio,
        includeUserAttachments: exportIncludeUserAttachments,
        projectTag: exportProjectTag,
        customRoles: exportCustomRoles,
        userRole: exportUserRole,
        assistantRole: exportAssistantRole,
    },
    html: {
        writeAdd: htmlVaultWriteAdd,
        writeOverwrite: htmlVaultWriteOverwrite,
        path: htmlFolderPath,
        backupBefore: htmlExportBackupBefore,
        backupFolder: htmlBackupFolderPath,
        optimize: htmlExportOptimize,
        preservePng: htmlExportPreserveTransparentPng,
        convertM4a: htmlExportConvertM4a,
        includeAudio: htmlExportIncludeConversationAudio,
        includeUserAttachments: htmlExportIncludeUserAttachments,
        projectTag: htmlExportProjectTag,
        customRoles: htmlExportCustomRoles,
        userRole: htmlExportUserRole,
        assistantRole: htmlExportAssistantRole,
    },
};
const exportStageItems = [...document.querySelectorAll("[data-export-stage]")];
const backupExportStage = document.querySelector('[data-export-stage="backup"]');
const formatExportStage = document.querySelector('[data-export-stage="obsidian"]');
const formatExportStageLabel = formatExportStage.querySelector("span:not(.stage-indicator)");
const obsidianExportStageLabel = formatExportStageLabel.textContent;
const vaultExportStage = document.querySelector('[data-export-stage="vault"]');
const exportCurrentItem = document.querySelector("#export-current-item");
const exportCurrentProject = document.querySelector("#export-current-project");
const exportProject = document.querySelector("#export-project");
const exportResources = document.querySelector("#export-resources");
const exportConversions = document.querySelector("#export-conversions");
const exportMarkdown = document.querySelector("#export-markdown");
const exportFilesLabel = exportMarkdown.previousElementSibling;
const obsidianFilesLabel = exportFilesLabel.textContent;
const exportErrors = document.querySelector("#export-errors");
const exportResult = document.querySelector("#export-result");
const exportOutputPath = document.querySelector("#export-output-path");
const exportResultSummary = document.querySelector("#export-result-summary");
const exportStatus = document.querySelector("#export-status");
const saveExportProfileButton = document.querySelector("#save-export-profile");
const loadExportProfileButton = document.querySelector("#load-export-profile");
const htmlSaveExportProfileButton = document.querySelector("#html-save-export-profile");
const htmlLoadExportProfileButton = document.querySelector("#html-load-export-profile");
const saveExportProfileModal = document.querySelector("#save-export-profile-modal");
const exportProfileName = document.querySelector("#export-profile-name");
const confirmSaveExportProfileButton = document.querySelector("#confirm-save-export-profile");
const cancelSaveExportProfileButton = document.querySelector("#cancel-save-export-profile");
const loadExportProfileModal = document.querySelector("#load-export-profile-modal");
const exportProfileList = document.querySelector("#export-profile-list");
const exportProfileDetails = document.querySelector("#export-profile-details");
const applyExportProfileButton = document.querySelector("#apply-export-profile");
const deleteExportProfileButton = document.querySelector("#delete-export-profile");
const cancelLoadExportProfileButton = document.querySelector("#cancel-load-export-profile");
const vaultLinkUpdateModal = document.querySelector("#vault-link-update-modal");
const vaultLinkUpdateSummary = document.querySelector("#vault-link-update-summary");
const vaultUpdateLinksButton = document.querySelector("#vault-update-links");
const vaultContinueWithoutLinksButton = document.querySelector("#vault-continue-without-links");
const vaultCancelSyncButton = document.querySelector("#vault-cancel-sync");
const exportStageOrder = ["backup", "prepare", "resources", "obsidian", "validate", "vault"];
let exportPollTimer;
let exportControlsDisabled = false;
let pendingVaultExport = null;
let vaultSyncInProgress = false;
let vaultPollTimer;
let obsidianAssistedCheckRequest = null;
let obsidianRememberLoadRequest = null;
let obsidianRememberDeleteRequest = null;
let obsidianRememberWarningAccepted = false;
let obsidianAssistedSavingApiKey = false;
let pendingObsidianExport = null;
let assistedObsidianExport = false;
let assistedObsidianApiKey = null;
let recoveredExportJob = false;
let exportStatusLoaded = false;
let exportStartedThisSession = false;
let selectedExportProfile = null;
let exportProfileTargetFormat = "obsidian";
let includeBackupExportStage = false;
let exportStageFormat = "obsidian";
backupExportStage.hidden = true;

function updatePreserveTransparentPngState(format = null) {
    for (const controls of format ? [exportControls[format]] : Object.values(exportControls)) {
        controls.preservePng.disabled = exportControlsDisabled || !controls.optimize.checked;
        if (!controls.optimize.checked) controls.preservePng.checked = false;
    }
}

exportOptimize.addEventListener("change", () => updatePreserveTransparentPngState("obsidian"));
htmlExportOptimize.addEventListener("change", () => updatePreserveTransparentPngState("html"));
updatePreserveTransparentPngState();

function updateCustomRoleFieldsState(format = null) {
    for (const controls of format ? [exportControls[format]] : Object.values(exportControls)) {
        const disabled = exportControlsDisabled || !controls.customRoles.checked;
        controls.userRole.disabled = disabled;
        controls.assistantRole.disabled = disabled;
    }
}

exportCustomRoles.addEventListener("change", () => updateCustomRoleFieldsState("obsidian"));
htmlExportCustomRoles.addEventListener("change", () => updateCustomRoleFieldsState("html"));
updateCustomRoleFieldsState();

function setExportControlsDisabled(disabled) {
    exportControlsDisabled = disabled;
    startExportButton.disabled = disabled;
    vaultWriteAdd.disabled = disabled;
    vaultWriteOverwrite.disabled = disabled;
    htmlVaultWriteAdd.disabled = disabled;
    htmlVaultWriteOverwrite.disabled = disabled;
    for (const controls of Object.values(exportControls)) {
        controls.backupBefore.disabled = disabled;
        controls.optimize.disabled = disabled;
        controls.convertM4a.disabled = disabled;
        controls.includeAudio.disabled = disabled;
        controls.includeUserAttachments.disabled = disabled;
        controls.projectTag.disabled = disabled;
        controls.customRoles.disabled = disabled;
    }
    saveExportProfileButton.disabled = disabled;
    loadExportProfileButton.disabled = disabled;
    htmlSaveExportProfileButton.disabled = disabled;
    htmlLoadExportProfileButton.disabled = disabled;
    updatePreserveTransparentPngState();
    updateCustomRoleFieldsState();
}

async function synchronizePendingVault(updateMarkdownLinks) {
    if (!pendingVaultExport || vaultSyncInProgress) return;
    if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
    vaultSyncInProgress = true;
    vaultUpdateLinksButton.disabled = true;
    vaultContinueWithoutLinksButton.disabled = true;
    vaultCancelSyncButton.disabled = true;
    setExportControlsDisabled(true);
    exportNavButton.classList.remove("state-complete", "state-error");
    exportNavButton.classList.add("state-running");
    exportStatus.dataset.state = "working";
    setIndicatorText(exportStatus, t("export.syncing"), true);
    try {
        const vaultPayload = {
            vault_path: obsidianVaultPath.value.trim(),
            overwrite_managed: vaultWriteOverwrite.checked,
            update_markdown_links: updateMarkdownLinks,
        };
        if (assistedObsidianExport) {
            if (!assistedObsidianApiKey) throw new Error(t("export.sync_failed"));
            vaultPayload.assisted = true;
            vaultPayload.api_key = assistedObsidianApiKey;
        }
        const request = fetch("/api/export/vault", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(vaultPayload),
        });
        assistedObsidianApiKey = null;
        const response = await request;
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("export.sync_failed"));
        renderExportStages({ ...data, status: "running", stage: data.stage || "vault" });
        clearTimeout(vaultPollTimer);
        void refreshVaultExportJob();
    } catch (error) {
        if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
        assistedObsidianApiKey = null;
        pendingVaultExport = null;
        setExportControlsDisabled(false);
        exportNavButton.classList.remove("state-running", "state-complete");
        exportNavButton.classList.add("state-error");
        renderExportStages({ status: "error", stage: "vault" });
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message) || t("export.sync_failed");
        vaultSyncInProgress = false;
        vaultUpdateLinksButton.disabled = false;
        vaultContinueWithoutLinksButton.disabled = false;
        vaultCancelSyncButton.disabled = false;
    }
}

async function refreshVaultExportJob() {
    try {
        const response = await fetch("/api/export/vault/status");
        if (!response.ok) throw new Error(t("export.sync_progress_failed"));
        const job = await response.json();

        if (job.status === "running") {
            setExportControlsDisabled(true);
            exportNavButton.classList.remove("state-complete", "state-error");
            exportNavButton.classList.add("state-running");
            exportStatus.dataset.state = "working";
            setIndicatorText(exportStatus, t("export.syncing"), true);
            renderExportStages({ ...job, stage: job.stage || "vault" });
            exportCurrentItem.textContent = job.current_item || "—";
            vaultPollTimer = setTimeout(refreshVaultExportJob, 1000);
            return;
        }

        if (job.status === "completed") {
            renderExportStages(job);
            if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
            pendingVaultExport = null;
            setExportControlsDisabled(false);
            exportNavButton.classList.remove("state-running", "state-error");
            exportNavButton.classList.add("state-complete");
            exportStatus.dataset.state = "complete";
            stopIndicatorText(exportStatus);
            exportStatus.textContent = t("export.completed");
            if (job.result?.link_update && Number(job.result.link_update.updated_links) > 0) {
                exportResultSummary.textContent += ` ${tf("export.links_updated", { count: job.result.link_update.updated_links })}`;
            }
            vaultSyncInProgress = false;
            vaultUpdateLinksButton.disabled = false;
            vaultContinueWithoutLinksButton.disabled = false;
            vaultCancelSyncButton.disabled = false;
            return;
        }

        if (job.status === "error") {
            renderExportStages({ ...job, stage: job.stage || "vault" });
            if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
            assistedObsidianApiKey = null;
            pendingVaultExport = null;
            setExportControlsDisabled(false);
            exportNavButton.classList.remove("state-running", "state-complete");
            exportNavButton.classList.add("state-error");
            exportStatus.dataset.state = "error";
            stopIndicatorText(exportStatus);
            exportStatus.textContent = localizedServerError(job.error) || t("export.sync_failed");
            vaultSyncInProgress = false;
            vaultUpdateLinksButton.disabled = false;
            vaultContinueWithoutLinksButton.disabled = false;
            vaultCancelSyncButton.disabled = false;
            return;
        }

        return;
    } catch (error) {
        if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
        assistedObsidianApiKey = null;
        pendingVaultExport = null;
        setExportControlsDisabled(false);
        exportNavButton.classList.remove("state-running", "state-complete");
        exportNavButton.classList.add("state-error");
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message) || t("export.sync_progress_failed");
        renderExportStages({ status: "error", stage: "vault" });
        vaultSyncInProgress = false;
        vaultUpdateLinksButton.disabled = false;
        vaultContinueWithoutLinksButton.disabled = false;
        vaultCancelSyncButton.disabled = false;
    }
}

vaultUpdateLinksButton.addEventListener("click", () => synchronizePendingVault(true));
vaultContinueWithoutLinksButton.addEventListener("click", () => synchronizePendingVault(false));
vaultCancelSyncButton.addEventListener("click", () => {
    if (vaultLinkUpdateModal.open) vaultLinkUpdateModal.close();
    clearTimeout(vaultPollTimer);
    assistedObsidianApiKey = null;
    pendingVaultExport = null;
    setExportControlsDisabled(false);
    exportNavButton.classList.remove("state-running", "state-complete", "state-error");
    renderExportStages({ status: "vault_pending", stage: "vault" });
    exportStatus.dataset.state = "pending";
    stopIndicatorText(exportStatus);
    exportStatus.textContent = t("export.generated_sync_cancelled");
});
vaultLinkUpdateModal.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!vaultSyncInProgress) vaultCancelSyncButton.click();
});

function showObsidianRememberError(error) {
    exportStatus.dataset.state = "error";
    stopIndicatorText(exportStatus);
    exportStatus.textContent = localizedServerError(error.message) || t("export.sync_failed");
}

async function loadRememberedObsidianApiKey() {
    obsidianRememberLoadRequest?.abort();
    const requestController = new AbortController();
    obsidianRememberLoadRequest = requestController;
    obsidianAssistedConfirmButton.disabled = true;
    obsidianAssistedRemember.disabled = true;
    try {
        const response = await fetch("/api/obsidian/api-key", { signal: requestController.signal });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("export.sync_failed"));
        if (obsidianRememberLoadRequest !== requestController) return;
        if (data.remembered === true && typeof data.api_key === "string") {
            obsidianAssistedApiKey.value = data.api_key;
            obsidianAssistedRemember.checked = true;
            obsidianRememberWarningAccepted = true;
        }
    } catch (error) {
        if (error.name === "AbortError") return;
        if (obsidianRememberLoadRequest !== requestController) return;
        obsidianAssistedApiKey.value = "";
        obsidianAssistedRemember.checked = false;
        obsidianRememberWarningAccepted = false;
        showObsidianRememberError(error);
    } finally {
        if (obsidianRememberLoadRequest === requestController) {
            obsidianRememberLoadRequest = null;
            obsidianAssistedConfirmButton.disabled = false;
            obsidianAssistedRemember.disabled = false;
        }
    }
}

async function forgetRememberedObsidianApiKey() {
    if (obsidianRememberDeleteRequest) return;
    const requestController = new AbortController();
    obsidianRememberDeleteRequest = requestController;
    obsidianAssistedRemember.disabled = true;
    obsidianAssistedConfirmButton.disabled = true;
    try {
        const response = await fetch("/api/obsidian/api-key", {
            method: "DELETE",
            signal: requestController.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("export.sync_failed"));
        obsidianRememberWarningAccepted = false;
    } catch (error) {
        if (error.name === "AbortError") return;
        obsidianAssistedRemember.checked = true;
        showObsidianRememberError(error);
    } finally {
        if (obsidianRememberDeleteRequest === requestController) {
            obsidianRememberDeleteRequest = null;
            obsidianAssistedRemember.disabled = false;
            obsidianAssistedConfirmButton.disabled = false;
        }
    }
}

obsidianAssistedRemember.addEventListener("change", () => {
    if (obsidianAssistedRemember.checked) {
        if (!obsidianRememberWarningAccepted && !obsidianRememberWarningModal.open) {
            obsidianRememberWarningModal.showModal();
        }
        return;
    }
    void forgetRememberedObsidianApiKey();
});

obsidianRememberAcceptButton.addEventListener("click", () => {
    obsidianRememberWarningAccepted = true;
    if (obsidianRememberWarningModal.open) obsidianRememberWarningModal.close();
});

function cancelObsidianRememberWarning() {
    obsidianAssistedRemember.checked = false;
    if (obsidianRememberWarningModal.open) obsidianRememberWarningModal.close();
}

obsidianRememberCancelButton.addEventListener("click", cancelObsidianRememberWarning);
obsidianRememberWarningModal.addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelObsidianRememberWarning();
});

function cancelObsidianExportSelection() {
    if (obsidianAssistedSavingApiKey) return;
    obsidianAssistedCheckRequest?.abort();
    obsidianAssistedCheckRequest = null;
    obsidianRememberLoadRequest?.abort();
    obsidianRememberLoadRequest = null;
    obsidianAssistedConfirmButton.disabled = false;
    obsidianAssistedApiKey.value = "";
    obsidianAssistedApiKey.setCustomValidity("");
    obsidianAssistedRemember.checked = false;
    obsidianRememberWarningAccepted = false;
    if (obsidianRememberWarningModal.open) obsidianRememberWarningModal.close();
    assistedObsidianApiKey = null;
    if (obsidianAssistedModal.open) obsidianAssistedModal.close();
    pendingObsidianExport = null;
}

obsidianAssistedCancelButton.addEventListener("click", cancelObsidianExportSelection);
obsidianAssistedModal.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (obsidianAssistedSavingApiKey) return;
    cancelObsidianExportSelection();
});

obsidianExportNormalButton.addEventListener("click", () => {
    const exportRequest = pendingObsidianExport;
    if (!exportRequest || obsidianAssistedSavingApiKey) return;
    obsidianAssistedCheckRequest?.abort();
    obsidianAssistedCheckRequest = null;
    obsidianRememberLoadRequest?.abort();
    obsidianRememberLoadRequest = null;
    assistedObsidianApiKey = null;
    obsidianAssistedApiKey.value = "";
    obsidianAssistedApiKey.setCustomValidity("");
    if (obsidianAssistedModal.open) obsidianAssistedModal.close();
    pendingObsidianExport = null;
    void startPreparedExport({ ...exportRequest, assisted: false });
});

obsidianAssistedConfirmButton.addEventListener("click", async () => {
    const exportRequest = pendingObsidianExport;
    if (!exportRequest || obsidianAssistedCheckRequest) return;
    const apiKey = obsidianAssistedApiKey.value.trim();
    const requestController = new AbortController();
    obsidianAssistedCheckRequest = requestController;
    obsidianAssistedConfirmButton.disabled = true;
    obsidianAssistedApiKey.setCustomValidity("");
    exportStatus.dataset.state = "working";
    let exportStarted = false;
    try {
        const response = await fetch("/api/obsidian/check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: apiKey }),
            signal: requestController.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.valid !== true) {
            throw new Error(data.detail || t("export.sync_failed"));
        }
        if (obsidianAssistedRemember.checked) {
            obsidianAssistedSavingApiKey = true;
            obsidianAssistedCancelButton.disabled = true;
            obsidianExportNormalButton.disabled = true;
            obsidianAssistedConfirmButton.textContent = t("export.saving");
            try {
                const saveResponse = await fetch("/api/obsidian/api-key", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ api_key: apiKey }),
                    signal: requestController.signal,
                });
                const saveData = await saveResponse.json().catch(() => ({}));
                if (!saveResponse.ok) {
                    throw new Error(saveData.detail || t("export.sync_failed"));
                }
            } finally {
                obsidianAssistedSavingApiKey = false;
                obsidianAssistedCancelButton.disabled = false;
                obsidianExportNormalButton.disabled = false;
                obsidianAssistedConfirmButton.textContent = obsidianAssistedConfirmText;
            }
        }
        assistedObsidianApiKey = apiKey;
        if (obsidianAssistedModal.open) obsidianAssistedModal.close();
        pendingObsidianExport = null;
        exportStarted = true;
        void startPreparedExport({ ...exportRequest, assisted: true });
    } catch (error) {
        if (error.name === "AbortError") return;
        assistedObsidianApiKey = null;
        const message = localizedServerError(error.message) || t("export.sync_failed");
        obsidianAssistedApiKey.setCustomValidity(message);
        obsidianAssistedApiKey.reportValidity();
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = message;
    } finally {
        if (exportStarted) obsidianAssistedApiKey.value = "";
        if (obsidianAssistedCheckRequest === requestController) {
            obsidianAssistedCheckRequest = null;
            obsidianAssistedConfirmButton.disabled = false;
        }
    }
});

function openSaveExportProfile(format) {
    exportProfileTargetFormat = format;
    exportProfileName.value = "";
    saveExportProfileModal.showModal();
    exportProfileName.focus();
}

saveExportProfileButton.addEventListener("click", () => openSaveExportProfile("obsidian"));
htmlSaveExportProfileButton.addEventListener("click", () => openSaveExportProfile("html"));

cancelSaveExportProfileButton.addEventListener("click", () => saveExportProfileModal.close());
saveExportProfileModal.addEventListener("cancel", () => {});

confirmSaveExportProfileButton.addEventListener("click", async () => {
    const name = exportProfileName.value.trim();
    if (!name) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = t("profile.name_required");
        return;
    }

    confirmSaveExportProfileButton.disabled = true;
    const controls = exportControls[exportProfileTargetFormat];
    try {
        const response = await fetch("/api/export/profiles", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name,
                overwrite_managed: controls.writeOverwrite.checked,
                convert_png: controls.optimize.checked,
                convert_wav: controls.optimize.checked,
                preserve_transparent_png: controls.preservePng.checked,
                convert_m4a: controls.convertM4a.checked,
                include_conversation_audio: controls.includeAudio.checked,
                include_user_attachments: controls.includeUserAttachments.checked,
                include_project_tag: controls.projectTag.checked,
                custom_roles: controls.customRoles.checked,
                user_role: controls.userRole.value.trim(),
                assistant_role: controls.assistantRole.value.trim(),
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("profile.save_failed"));
        saveExportProfileModal.close();
        exportStatus.dataset.state = "complete";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = tf("profile.saved", { name });
    } catch (error) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message) || t("profile.save_failed");
    } finally {
        confirmSaveExportProfileButton.disabled = false;
    }
});

function profileBooleanLabel(value) {
    return value === true ? t("profile.yes") : t("profile.no");
}

function showExportProfileDetails(profile) {
    const customRoles = profile.custom_roles === true;
    const lines = [
        `${t("profile.mode")}: ${profile.overwrite_managed === true ? t("export.overwrite") : t("export.add")}`,
        `${t("export.optimize")}: ${profileBooleanLabel(profile.convert_png === true && profile.convert_wav === true)}`,
        `${t("export.preserve_png")}: ${profileBooleanLabel(profile.preserve_transparent_png)}`,
        `${t("export.convert_m4a")}: ${profileBooleanLabel(profile.convert_m4a)}`,
        `${t("export.include_audio")}: ${profileBooleanLabel(profile.include_conversation_audio)}`,
        `${t("export.include_user_attachments")}: ${profileBooleanLabel(profile.include_user_attachments)}`,
        `${t("export.project_tag")}: ${profileBooleanLabel(profile.include_project_tag)}`,
        `${t("export.custom_roles")}: ${profileBooleanLabel(customRoles)}`,
        `${t("export.user_role")}: ${customRoles ? profile.user_role : "—"}`,
        `${t("export.assistant_role")}: ${customRoles ? profile.assistant_role : "—"}`,
    ];
    exportProfileDetails.replaceChildren(...lines.map((line) => {
        const paragraph = document.createElement("p");
        paragraph.textContent = line;
        return paragraph;
    }));
}

async function openLoadExportProfile(format) {
    exportProfileTargetFormat = format;
    selectedExportProfile = null;
    exportProfileList.replaceChildren();
    exportProfileDetails.replaceChildren();
    applyExportProfileButton.disabled = true;
    deleteExportProfileButton.disabled = true;
    try {
        const response = await fetch("/api/export/profiles");
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("profile.load_failed"));
        const profiles = Array.isArray(data.profiles) ? data.profiles : [];
        if (!profiles.length) {
            exportProfileList.textContent = t("profile.none");
        } else {
            for (const profile of profiles) {
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = profile.name;
                button.setAttribute("aria-pressed", "false");
                button.addEventListener("click", () => {
                    selectedExportProfile = profile;
                    applyExportProfileButton.disabled = false;
                    deleteExportProfileButton.disabled = false;
                    for (const profileButton of exportProfileList.querySelectorAll("button")) {
                        profileButton.setAttribute("aria-pressed", String(profileButton === button));
                    }
                    showExportProfileDetails(profile);
                });
                exportProfileList.append(button);
            }
        }
        loadExportProfileModal.showModal();
    } catch (error) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message) || t("profile.load_failed");
    }
}

loadExportProfileButton.addEventListener("click", () => openLoadExportProfile("obsidian"));
htmlLoadExportProfileButton.addEventListener("click", () => openLoadExportProfile("html"));

function cancelLoadingExportProfile() {
    selectedExportProfile = null;
    applyExportProfileButton.disabled = true;
    deleteExportProfileButton.disabled = true;
}

cancelLoadExportProfileButton.addEventListener("click", () => {
    cancelLoadingExportProfile();
    loadExportProfileModal.close();
});
loadExportProfileModal.addEventListener("cancel", cancelLoadingExportProfile);

deleteExportProfileButton.addEventListener("click", async () => {
    const profile = selectedExportProfile;
    if (!profile) return;

    applyExportProfileButton.disabled = true;
    deleteExportProfileButton.disabled = true;
    cancelLoadExportProfileButton.disabled = true;
    try {
        const response = await fetch("/api/export/profiles", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: profile.name }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || t("profile.delete_failed"));

        const profileButton = [...exportProfileList.querySelectorAll("button")]
            .find((button) => button.textContent === profile.name);
        profileButton?.remove();
        selectedExportProfile = null;
        exportProfileDetails.replaceChildren();
        applyExportProfileButton.disabled = true;
        deleteExportProfileButton.disabled = true;
        cancelLoadExportProfileButton.disabled = false;
        if (!exportProfileList.querySelector("button")) {
            exportProfileList.textContent = t("profile.none");
        }
        exportStatus.dataset.state = "complete";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = tf("profile.deleted", { name: profile.name });
    } catch (error) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message) || t("profile.delete_failed");
        applyExportProfileButton.disabled = !selectedExportProfile;
        deleteExportProfileButton.disabled = !selectedExportProfile;
        cancelLoadExportProfileButton.disabled = false;
    }
});

applyExportProfileButton.addEventListener("click", () => {
    const profile = selectedExportProfile;
    if (!profile) return;
    const controls = exportControls[exportProfileTargetFormat];
    controls.writeOverwrite.checked = profile.overwrite_managed === true;
    controls.writeAdd.checked = !controls.writeOverwrite.checked;
    controls.optimize.checked = profile.convert_png === true && profile.convert_wav === true;
    controls.preservePng.checked = profile.preserve_transparent_png === true;
    controls.convertM4a.checked = profile.convert_m4a === true;
    controls.includeAudio.checked = profile.include_conversation_audio === true;
    controls.includeUserAttachments.checked = profile.include_user_attachments === true;
    controls.projectTag.checked = profile.include_project_tag === true;
    controls.customRoles.checked = profile.custom_roles === true;
    controls.userRole.value = profile.user_role;
    controls.assistantRole.value = profile.assistant_role;
    updatePreserveTransparentPngState(exportProfileTargetFormat);
    updateCustomRoleFieldsState(exportProfileTargetFormat);
    loadExportProfileModal.close();
    exportStatus.dataset.state = "complete";
    stopIndicatorText(exportStatus);
    exportStatus.textContent = tf("profile.applied", { name: profile.name });
});

function selectExportTab(format) {
    const isObsidian = format === "obsidian";
    exportTabObsidian.setAttribute("aria-selected", String(isObsidian));
    exportTabHtml.setAttribute("aria-selected", String(!isObsidian));
    exportPanelObsidian.hidden = !isObsidian;
    exportPanelHtml.hidden = isObsidian;
    if (!exportControlsDisabled) setExportStageFormat(format);
    startExportButton.disabled = exportControlsDisabled;
}

function setExportStageFormat(format) {
    exportStageFormat = format;
    exportStageOrder[3] = format;
    formatExportStage.dataset.exportStage = format;
    formatExportStageLabel.textContent = format === "html"
        ? obsidianExportStageLabel.replace("Obsidian", "HTML")
        : obsidianExportStageLabel;
    exportFilesLabel.textContent = format === "html" ? "HTML" : obsidianFilesLabel;
    vaultExportStage.hidden = format === "html";
}

exportTabObsidian.addEventListener("click", () => selectExportTab("obsidian"));
exportTabHtml.addEventListener("click", () => selectExportTab("html"));
selectExportTab("obsidian");

function renderExportStages(job = {}) {
    if (job.stage === "html" || job.result?.format === "html") setExportStageFormat("html");
    if (job.stage === "obsidian" || job.result?.format === "obsidian") setExportStageFormat("obsidian");
    const activeIndex = exportStageOrder.indexOf(job.stage);
    const vaultIndex = exportStageOrder.indexOf("vault");
    const vaultPending = (job.status === "completed" && job.result?.vault_pending === true)
        || job.status === "vault_pending";
    for (const item of exportStageItems) {
        if (item === backupExportStage) item.hidden = !includeBackupExportStage;
        const itemIndex = exportStageOrder.indexOf(item.dataset.exportStage);
        let state = "pending";
        if ((job.status === "completed" && !vaultPending)
            || (vaultPending && itemIndex < vaultIndex)
            || (activeIndex >= 0 && itemIndex < activeIndex)) {
            state = "complete";
        } else if (job.status === "running" && itemIndex === activeIndex) {
            state = "active";
        }
        item.dataset.state = state;
        const stageStatus = item.querySelector(".stage-status");
        if (state === "complete") {
            stageStatus.textContent = t("export.finalized");
        } else if (state === "active") {
            const total = Number(job.total);
            if (total > 0) {
                const current = Number(job.current) || 0;
                const percent = Math.round(Math.min(100, Math.max(0, (current / total) * 100)));
                stageStatus.textContent = `${t("process.in_progress")} · ${percent} %`;
            } else {
                stageStatus.textContent = t("process.in_progress");
            }
        } else {
            stageStatus.textContent = t("process.pending");
        }
    }
}

function translatedExportPhase(stage) {
    if (stage === "html") return t("export.stage_obsidian").replace("Obsidian", "HTML").replace(/^\d+\.\s*/, "");
    const key = `export.stage_${stage}`;
    const translation = t(key);
    return translation !== key ? translation.replace(/^\d+\.\s*/, "") : t("export.exporting");
}

function renderExportJob(job = {}, { recovered = false } = {}) {
    renderExportStages(job);
    exportCurrentItem.textContent = job.current_item || "—";
    exportProject.textContent = job.project || "";
    exportCurrentProject.hidden = !job.project;
    exportResources.textContent = `${processValue(job.processed_resources)} / ${processValue(job.total_resources)}`;
    exportConversions.textContent = processValue(job.converted_resources);
    exportMarkdown.textContent = processValue(exportStageFormat === "html" ? job.html_files : job.markdown_files);
    exportErrors.textContent = processValue(job.errors);

    if (job.status === "running") {
        setExportControlsDisabled(true);
        exportNavButton.classList.remove("state-complete", "state-error");
        exportNavButton.classList.add("state-running");
        exportStatus.dataset.state = "working";
        const phaseLabel = translatedExportPhase(job.stage || job.phase);
        setIndicatorText(exportStatus, job.last_error
            ? `${phaseLabel}. ${t("common.error")}: ${localizedServerError(job.last_error)}`
            : phaseLabel, true);
        exportPollTimer = setTimeout(refreshExportJob, 1000);
        return;
    }

    setExportControlsDisabled(false);
    exportNavButton.classList.remove("state-running");
    if (job.status === "completed") {
        const data = job.result;
        exportOutputPath.textContent = data.output_root;
        exportResultSummary.textContent = (data.format === "html"
            ? `${data.html_files} HTML, ${t("export.counter_resources")}: ${data.resource_count}, ${t("export.counter_conversions")}: ${data.converted_resource_count}.`
            : tf("export.result_summary", { markdown: data.markdown_count, resources: data.resource_count, conversions: data.converted_resource_count }))
            + (data.missing_resources ? ` ${tf("export.missing_resources_summary", { count: data.missing_resources })}` : "");
        exportResult.hidden = false;
        if (data.format === "obsidian" && data.vault_pending === true) {
            pendingVaultExport = data;
            if (recovered) {
                renderExportStages({ status: "vault_pending", stage: "vault" });
                exportNavButton.classList.remove("state-running", "state-error");
                exportNavButton.classList.add("state-complete");
                exportStatus.dataset.state = "complete";
                stopIndicatorText(exportStatus);
                exportStatus.textContent = t("export.completed");
                return;
            }
            setExportControlsDisabled(true);
            exportNavButton.classList.remove("state-complete", "state-error");
            exportNavButton.classList.add("state-running");
            exportStatus.dataset.state = "working";
            const linkCount = Number(data.vault_analysis?.markdown_link_update_count) || 0;
            if (linkCount > 0) {
                vaultLinkUpdateSummary.textContent = tf("vault_changes.detected_links", { count: linkCount });
                if (!vaultLinkUpdateModal.open) vaultLinkUpdateModal.showModal();
            } else {
                void synchronizePendingVault(false);
            }
            return;
        }
        const completedWithWarnings = data.status === "success_with_warnings";
        exportNavButton.classList.add("state-complete");
        exportStatus.dataset.state = "complete";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = completedWithWarnings
            ? t("export.completed_warnings")
            : data.format === "html" ? t("export.completed").replace("Obsidian", "HTML") : t("export.completed");
    } else if (job.status === "error") {
        assistedObsidianApiKey = null;
        exportNavButton.classList.add("state-error");
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(job.error) || t("export.failed_complete");
    }
}

async function refreshExportJob() {
    try {
        const response = await fetch("/api/export/status");
        if (!response.ok) throw new Error(t("export.progress_failed"));
        const job = await response.json();
        if (!exportStatusLoaded && !exportStartedThisSession && job.status !== "idle") {
            recoveredExportJob = true;
        }
        exportStatusLoaded = true;
        renderExportJob(job, { recovered: recoveredExportJob });
    } catch (error) {
        assistedObsidianApiKey = null;
        setExportControlsDisabled(false);
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message);
    }
}

chooseObsidianVaultButton.addEventListener("click", async () => {
    chooseObsidianVaultButton.disabled = true;
    try {
        const response = await fetch("/api/select-folder", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || t("export.select_vault_failed"));
        if (data.cancelled === true) return;
        if (data.path) {
            obsidianVaultPath.value = data.path;
            obsidianVaultDisplay.textContent = data.path;
            vaultResult.hidden = false;
        }
    } catch (error) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message);
    } finally {
        chooseObsidianVaultButton.disabled = false;
    }
});

async function selectFolderInto(button, pathInput, pathDisplay, resultBlock, purpose = null) {
    button.disabled = true;
    try {
        const response = await fetch("/api/select-folder", {
            method: "POST",
            ...(purpose === "html" ? {
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ purpose: "html" }),
            } : {}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || t("export.select_vault_failed"));
        if (data.cancelled === true) return;
        if (data.path) {
            pathInput.value = data.path;
            pathDisplay.textContent = data.path;
            resultBlock.hidden = false;
        }
    } catch (error) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message);
    } finally {
        button.disabled = false;
    }
}

chooseHtmlFolderButton.addEventListener("click", () => {
    selectFolderInto(chooseHtmlFolderButton, htmlFolderPath, htmlFolderDisplay, htmlFolderResult, "html");
});

chooseBackupFolderButton.addEventListener("click", () => {
    selectFolderInto(chooseBackupFolderButton, backupFolderPath, backupFolderDisplay, backupFolderResult);
});

htmlChooseBackupFolderButton.addEventListener("click", () => {
    selectFolderInto(htmlChooseBackupFolderButton, htmlBackupFolderPath, htmlBackupFolderDisplay, htmlBackupFolderResult);
});

function prepareExportRequest() {
    const isHtmlExport = exportTabHtml.getAttribute("aria-selected") === "true";
    const format = isHtmlExport ? "html" : "obsidian";
    const controls = exportControls[format];
    const destinationPath = controls.path.value.trim();
    if (!destinationPath) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = isHtmlExport
            ? t("export.select_html_destination")
            : t("export.select_obsidian_destination");
        return null;
    }
    const backupBefore = controls.backupBefore.checked;
    const backupFolder = controls.backupFolder.value.trim();
    if (backupBefore && !backupFolder) {
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = t("export.select_backup_destination");
        return null;
    }
    return {
        format,
        backupBefore,
        payload: {
        format,
        backup_before: backupBefore,
        backup_folder: backupFolder,
        convert_png: controls.optimize.checked,
        convert_wav: controls.optimize.checked,
        preserve_transparent_png: controls.preservePng.checked,
        convert_m4a: controls.convertM4a.checked,
        include_conversation_audio: controls.includeAudio.checked,
        include_user_attachments: controls.includeUserAttachments.checked,
        include_project_tag: controls.projectTag.checked,
        custom_roles: controls.customRoles.checked,
        user_role: controls.userRole.value.trim(),
        assistant_role: controls.assistantRole.value.trim(),
        ...(isHtmlExport
            ? { html_path: destinationPath }
            : { vault_path: destinationPath, overwrite_managed: vaultWriteOverwrite.checked }),
        },
    };
}

async function startPreparedExport(exportRequest) {
    const { backupBefore, payload, assisted } = exportRequest;
    assistedObsidianExport = assisted;
    recoveredExportJob = false;
    exportStartedThisSession = true;
    includeBackupExportStage = backupBefore;
    exportProgressSection.hidden = false;
    clearTimeout(exportPollTimer);
    renderExportJob({});
    setExportControlsDisabled(true);
    exportResult.hidden = true;
    exportStatus.dataset.state = "working";
    setIndicatorText(exportStatus, t("export.starting"), true);
    try {
        const response = await fetch("/api/export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const job = await response.json();
        if (!response.ok) throw new Error(job.detail || t("export.start_failed"));
        renderExportJob(job);
    } catch (error) {
        assistedObsidianApiKey = null;
        setExportControlsDisabled(false);
        exportNavButton.classList.remove("state-running");
        exportNavButton.classList.add("state-error");
        exportStatus.dataset.state = "error";
        stopIndicatorText(exportStatus);
        exportStatus.textContent = localizedServerError(error.message);
    }
}

startExportButton.addEventListener("click", () => {
    const exportRequest = prepareExportRequest();
    if (!exportRequest) return;
    if (exportRequest.format === "obsidian") {
        pendingObsidianExport = exportRequest;
        obsidianAssistedApiKey.value = "";
        obsidianAssistedApiKey.setCustomValidity("");
        obsidianAssistedRemember.checked = false;
        obsidianRememberWarningAccepted = false;
        obsidianAssistedConfirmButton.disabled = false;
        if (!obsidianAssistedModal.open) obsidianAssistedModal.showModal();
        obsidianAssistedApiKey.focus();
        void loadRememberedObsidianApiKey();
        return;
    }
    void startPreparedExport({ ...exportRequest, assisted: false });
});

refreshExportJob();

const cleanDeleteExports = document.querySelector("#clean-delete-exports");
const startCleanButton = document.querySelector("#start-clean");
const cleanModal = document.querySelector("#clean-modal");
const cleanConfirmProgress = document.querySelector("#clean-confirm-progress");
const cleanConfirmStatus = document.querySelector("#clean-confirm-status");
const cancelCleanButton = document.querySelector("#cancel-clean");
const cleanModuleSummary = document.querySelector("#limpiar .panel-summary");
const initialCleanSummary = t("clean.summary");
const cleanHoldDuration = 4000;
const cleanRequiredKeys = new Set(["a", "l"]);
const cleanKeysDown = new Set();
let cleanHoldStartedAt = null;
let cleanHoldTimer = null;
let cleanRequestInProgress = false;

function cleanConfirmationPrompt() {
    return t(cleanDeleteExports.checked ? "clean.prompt_delete_exports" : "clean.prompt_keep_exports");
}

function resetCleanConfirmation() {
    if (cleanHoldTimer !== null) {
        clearInterval(cleanHoldTimer);
        cleanHoldTimer = null;
    }
    cleanHoldStartedAt = null;
    cleanKeysDown.clear();
    cleanConfirmProgress.value = 0;
    cleanConfirmStatus.textContent = cleanConfirmationPrompt();
    startCleanButton.disabled = cleanRequestInProgress;
    cancelCleanButton.disabled = cleanRequestInProgress;
}

function cancelCleanHold() {
    if (cleanRequestInProgress) return;
    resetCleanConfirmation();
}

function resetProcessAfterClean() {
    clearTimeout(processPollTimer);
    processJobRunning = false;
    startProcessButton.textContent = t("process.start");
    startProcessButton.disabled = false;
    processSummary.textContent = initialProcessSummary;
    renderProcessProgress();
    hideProcessActionResult();
    setNavigationState(connectNavButton, "complete");
    setNavigationState(processNavButton, null);
    setNavigationState(exportNavButton, "locked");
    processStatus.dataset.state = "pending";
    stopIndicatorText(processStatus);
    processStatus.textContent = t("clean.process_reset");
}

function resetExportAfterClean() {
    clearTimeout(exportPollTimer);
    clearTimeout(vaultPollTimer);
    renderExportJob({});
    exportOptimize.checked = false;
    exportPreserveTransparentPng.checked = false;
    exportConvertM4a.checked = false;
    exportIncludeConversationAudio.checked = false;
    exportIncludeUserAttachments.checked = false;
    htmlExportIncludeUserAttachments.checked = false;
    exportProjectTag.checked = false;
    exportCustomRoles.checked = false;
    exportUserRole.value = t("export.user_role");
    exportAssistantRole.value = t("export.assistant_role");
    vaultWriteAdd.checked = true;
    vaultWriteOverwrite.checked = false;
    setExportControlsDisabled(false);
    setNavigationState(exportNavButton, "locked");
    exportResult.hidden = true;
    exportOutputPath.textContent = "";
    exportResultSummary.textContent = "";
    exportStatus.dataset.state = "pending";
    stopIndicatorText(exportStatus);
    exportStatus.textContent = t("export.ready");
}

function finishCleanWithError(message) {
    cleanRequestInProgress = false;
    cleanKeysDown.clear();
    cleanHoldStartedAt = null;
    cleanConfirmProgress.value = 0;
    cleanConfirmStatus.textContent = message;
    startCleanButton.disabled = false;
    cancelCleanButton.disabled = false;
}

async function executeClean() {
    try {
        const response = await fetch("/api/clean", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ delete_exports: cleanDeleteExports.checked }),
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 409) {
            finishCleanWithError(localizedServerError(data.detail) || t("clean.busy"));
            return;
        }
        if (!response.ok) {
            finishCleanWithError(localizedServerError(data.detail) || t("clean.failed"));
            return;
        }

        cleanRequestInProgress = false;
        resetProcessAfterClean();
        resetExportAfterClean();
        cleanModuleSummary.textContent = t("clean.completed");
        if (cleanModal.open) cleanModal.close();
    } catch (error) {
        finishCleanWithError(localizedServerError(error.message) || t("clean.failed"));
    }
}

function beginCleanHold() {
    if (cleanHoldTimer !== null || cleanRequestInProgress) return;
    cleanHoldStartedAt = performance.now();
    cleanConfirmProgress.value = 0;
    cleanHoldTimer = setInterval(() => {
        const elapsed = performance.now() - cleanHoldStartedAt;
        const progress = Math.min(elapsed / cleanHoldDuration, 1);
        cleanConfirmProgress.value = progress * 4;
        cleanConfirmStatus.textContent = tf(
            cleanDeleteExports.checked ? "clean.confirm_delete_exports" : "clean.confirm_keep_exports",
            { seconds: (progress * 4).toFixed(1) },
        );
        if (progress < 1) return;

        clearInterval(cleanHoldTimer);
        cleanHoldTimer = null;
        cleanKeysDown.clear();
        cleanHoldStartedAt = null;
        cleanRequestInProgress = true;
        cleanConfirmProgress.value = 4;
        cleanConfirmStatus.textContent = t("clean.in_progress");
        startCleanButton.disabled = true;
        cancelCleanButton.disabled = true;
        executeClean();
    }, 50);
}

startCleanButton.addEventListener("click", () => {
    if (cleanRequestInProgress || cleanModal.open) return;
    resetCleanConfirmation();
    cleanModuleSummary.textContent = initialCleanSummary;
    cleanModal.showModal();
});

cleanDeleteExports.addEventListener("change", () => {
    if (cleanModal.open && cleanHoldTimer === null && !cleanRequestInProgress) {
        cleanConfirmStatus.textContent = cleanConfirmationPrompt();
    }
});

window.addEventListener("keydown", (event) => {
    const key = event.key.toLowerCase();
    if (!cleanModal.open || cleanRequestInProgress || !cleanRequiredKeys.has(key) || event.repeat) return;
    event.preventDefault();
    cleanKeysDown.add(key);
    if ([...cleanRequiredKeys].every((requiredKey) => cleanKeysDown.has(requiredKey))) beginCleanHold();
});

window.addEventListener("keyup", (event) => {
    const key = event.key.toLowerCase();
    if (!cleanRequiredKeys.has(key)) return;
    cleanKeysDown.delete(key);
    if (!cleanRequestInProgress && cleanHoldTimer !== null
        && ![...cleanRequiredKeys].every((requiredKey) => cleanKeysDown.has(requiredKey))) {
        cancelCleanHold();
    }
});

window.addEventListener("blur", cancelCleanHold);

cancelCleanButton.addEventListener("click", () => {
    cancelCleanHold();
    if (cleanModal.open) cleanModal.close();
});

cleanModal.addEventListener("close", resetCleanConfirmation);
